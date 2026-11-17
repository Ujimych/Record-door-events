import json
import logging
import math
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BUFFER_DIR = Path("/media/record-door-events/buffer")
EVENT_DIR = Path("/media/record-door-events/events")
READY_DIR = Path("/media/record-door-events/ready")
TEMP_DIR = Path("/media/record-door-events/tmp")
OPTIONS_FILE = Path("/data/options.json")

MQTT_TOPIC = "record_door_events/video_ready"

STALE_BUFFER_SECONDS = 120
RETRY_DELAY = 5
MAX_WORKERS = 10

# Keep the encoded clip close to the source quality. A fixed low bitrate
# destroyed fine details on high-resolution cameras, especially at night.
VIDEO_CRF = "18"
VIDEO_MAXRATE = "12M"
VIDEO_BUFSIZE = "24M"

OUTPUT_MIN_SIZE = 10 * 1024
OUTPUT_DURATION_RATIO = 0.95

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("event_processor")


def load_options():
    try:
        with OPTIONS_FILE.open() as f:
            return json.load(f)
    except Exception as e:
        log.error("Cannot read options: %s", e)
        return {}


OPTIONS = load_options()
SEGMENT_SECONDS = float(OPTIONS.get("segment_seconds", 1))
BUFFER_SECONDS = float(OPTIONS.get("buffer_seconds", 60))
PRE_EVENT = float(OPTIONS.get("pre_event_seconds", 5))
POST_EVENT = float(OPTIONS.get("post_event_seconds", 10))
TARGET_DURATION = PRE_EVENT + POST_EVENT
DEBUG_MODE = bool(OPTIONS.get("debug_mode", False))
EVENT_DATA_RETENTION_DAYS = max(1, int(OPTIONS.get("event_data_retention_days", 3)))
READY_RETENTION_DAYS = max(1, int(OPTIONS.get("ready_retention_days", 3)))
EVENT_DATA_RETENTION_SECONDS = EVENT_DATA_RETENTION_DAYS * 24 * 60 * 60
READY_RETENTION_SECONDS = READY_RETENTION_DAYS * 24 * 60 * 60

TEMP_DIR.mkdir(parents=True, exist_ok=True)
READY_DIR.mkdir(parents=True, exist_ok=True)


def remove_path(path):
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except Exception as e:
        log.warning("Cannot remove %s: %s", path, e)


def event_dir_timestamp(path):
    parts = path.name.split("_")
    if len(parts) >= 4 and parts[0] == "event":
        try:
            start = datetime.strptime(
                f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M%S"
            ).timestamp()
            return start + int(parts[3]) / 1000.0
        except (ValueError, IndexError):
            pass

    try:
        return path.stat().st_mtime
    except OSError:
        return None


def cleanup_temp_dir():
    # Remove leftovers from interrupted builds. In debug mode recent event
    # folders are kept so that the copied segments can be inspected.
    try:
        entries = list(TEMP_DIR.iterdir())
    except OSError:
        return

    now = time.time()

    for path in entries:
        if DEBUG_MODE and path.is_dir() and path.name.startswith("event_"):
            timestamp = event_dir_timestamp(path)
            if timestamp is not None and now - timestamp <= EVENT_DATA_RETENTION_SECONDS:
                continue

        remove_path(path)


cleanup_temp_dir()


def cleanup_event_data(keep_path=None):
    if not DEBUG_MODE:
        if keep_path is not None:
            remove_path(keep_path)
        return

    now = time.time()

    for path in TEMP_DIR.glob("event_*"):
        if keep_path is not None and path == keep_path:
            continue

        if not path.is_dir():
            continue

        timestamp = event_dir_timestamp(path)
        if timestamp is None or now - timestamp > EVENT_DATA_RETENTION_SECONDS:
            log.info("Removing old event data: %s", path.name)
            remove_path(path)


def prune_ready_dir(keep_path=None):
    now = time.time()

    for path in READY_DIR.glob("*.mp4"):
        if keep_path is not None and path == keep_path:
            continue

        try:
            if now - path.stat().st_mtime <= READY_RETENTION_SECONDS:
                continue
            path.unlink()
            log.info("Removed old ready video: %s", path.name)
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("Cannot remove old ready video %s: %s", path, e)


# Segment names contain the wall-clock start of the segment. The segment
# before the requested window is used only when it is close enough to belong
# to the same continuous recording; otherwise it is a leftover from an older
# recorder run.
SEGMENT_GAP_LIMIT = max(5.0, SEGMENT_SECONDS * 4.0)
SEGMENT_CLOSE_TIMEOUT = max(5.0, SEGMENT_SECONDS * 3.0 + 2.0)
MIN_OUTPUT_SECONDS = min(TARGET_DURATION, 5.0)
MAX_EVENT_AGE = max(0.0, BUFFER_SECONDS - PRE_EVENT + SEGMENT_SECONDS * 5.0)

MQTT_HOST = OPTIONS.get("mqtt_host", "core-mosquitto")
MQTT_PORT = int(OPTIONS.get("mqtt_port", 1883))
MQTT_USER = OPTIONS.get("mqtt_username", "")
MQTT_PASSWORD = OPTIONS.get("mqtt_password", "")


def parse_event_time(path):
    try:
        value = float(path.stem.replace("event_", ""))
        if not math.isfinite(value) or value <= 0 or value > 4102444800:
            return None
        return value
    except Exception:
        return None


def segment_time(path):
    try:
        return datetime.strptime(path.stem.replace("segment_", ""), "%Y%m%d_%H%M%S").timestamp()
    except Exception:
        return None


def get_segments():
    result = []
    for path in BUFFER_DIR.glob("segment_*.ts"):
        ts = segment_time(path)
        if ts is None:
            continue
        try:
            if path.stat().st_size > 0:
                result.append((ts, path))
        except OSError:
            pass
    return sorted(result)


def get_newest_segment():
    newest = None
    for path in BUFFER_DIR.glob("segment_*.ts"):
        if newest is None or path.name > newest.name:
            newest = path
    return newest


def delete_event_marker(event_file):
    try:
        event_file.unlink()
        log.info("Event marker removed: %s", event_file.name)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error("Cannot remove event marker %s: %s", event_file, e)


def run_ffmpeg(command, timeout=300):
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        stderr = result.stderr.strip()

        if result.returncode != 0:
            log.error("FFmpeg failed: %s", stderr[-2000:])
            return False

        if stderr:
            # A successful encode can still contain decoder errors from a
            # damaged source segment. Keep them in the application log so the
            # copied debug segments can be checked later.
            log.warning("FFmpeg diagnostics: %s", stderr[-2000:])

        return True
    except subprocess.TimeoutExpired:
        log.error("FFmpeg timed out")
        return False
    except Exception as e:
        log.error("FFmpeg exception: %s", e)
        return False


def wait_for_segments(end_time):
    wait_until = end_time + max(3.0, SEGMENT_SECONDS * 2)

    while time.time() < wait_until:
        segments = get_segments()
        if segments and segments[-1][0] >= end_time:
            return segments
        time.sleep(1)

    return get_segments()


def wait_for_segment_closed(path, timeout):
    deadline = time.time() + timeout

    while time.time() < deadline:
        if not path.exists():
            return False

        newest = get_newest_segment()
        if newest is not None and newest.name > path.name:
            return True

        time.sleep(0.25)

    log.warning("Segment is still being written: %s", path.name)

    try:
        return path.exists() and path.stat().st_size > 0
    except FileNotFoundError:
        return False


def copy_segment(source, destination):
    if not wait_for_segment_closed(source, SEGMENT_CLOSE_TIMEOUT):
        log.warning("Segment is not available: %s", source.name)
        return False

    try:
        shutil.copy2(source, destination)
    except FileNotFoundError:
        log.warning("Segment disappeared while copying: %s", source.name)
        return False
    except Exception as e:
        log.error("Cannot copy segment %s: %s", source.name, e)
        return False

    try:
        if destination.stat().st_size <= 0:
            destination.unlink(missing_ok=True)
            return False
    except FileNotFoundError:
        return False

    return True


def publish_mqtt(video_file):
    try:
        import paho.mqtt.publish as publish

        kwargs = {
            "hostname": MQTT_HOST,
            "port": MQTT_PORT,
            "topic": MQTT_TOPIC,
            "payload": str(video_file)
        }

        if MQTT_USER:
            kwargs["auth"] = {"username": MQTT_USER, "password": MQTT_PASSWORD}

        publish.single(**kwargs)
        log.info("MQTT published: %s", video_file)
        return True
    except Exception as e:
        log.error("MQTT publish failed: %s", e)
        return False


def get_video_duration(path):
    command = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return None
        duration = float(result.stdout.strip())
        if not math.isfinite(duration) or duration <= 0:
            return None
        return duration
    except Exception:
        return None


def get_video_fps(path):
    command = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate",
        "-of", "default=noprint_wrappers=1",
        str(path)
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return None
    except Exception:
        return None

    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()

    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = values.get(key)
        if not raw or raw in ("0/0", "N/A"):
            continue
        try:
            if "/" in raw:
                numerator, denominator = raw.split("/", 1)
                fps = float(numerator) / float(denominator)
            else:
                fps = float(raw)
        except (ValueError, ZeroDivisionError):
            continue
        if 1.0 < fps <= 60.0:
            return fps

    return None


def validate_video(path, expected_duration):
    try:
        if not path.exists():
            return False
        size = path.stat().st_size
    except FileNotFoundError:
        return False

    if size < OUTPUT_MIN_SIZE:
        log.error("Validation failed: file is too small (%d bytes)", size)
        return False

    duration = get_video_duration(path)
    if duration is None:
        log.error("Validation failed: cannot determine duration")
        return False

    minimum = MIN_OUTPUT_SECONDS
    if expected_duration is not None:
        minimum = max(minimum, expected_duration * OUTPUT_DURATION_RATIO)

    if duration < minimum:
        log.error("Validation failed: %.2fs, expected at least %.2fs", duration, minimum)
        return False

    fps = get_video_fps(path)
    log.info("Validated: %.2fs, %.1f MB, %.1f fps", duration, size / 1024 / 1024, fps or 0.0)
    return True


def get_segment_gap_limit(segments):
    # The camera decides the real segment duration: with stream copy FFmpeg
    # can only split on keyframes, so a configured one-second segment may
    # actually be four or more seconds long. Use the observed interval to
    # avoid dropping a segment that already contains the start of the window.
    intervals = sorted(
        next_ts - ts
        for (ts, _), (next_ts, _) in zip(segments, segments[1:])
        if next_ts > ts
    )

    if not intervals:
        return SEGMENT_GAP_LIMIT

    typical = intervals[len(intervals) // 2]
    return max(SEGMENT_GAP_LIMIT, min(30.0, typical * 2.5))


def select_segments(start_time, end_time, segments):
    selected = []
    previous = None

    for ts, path in segments:
        if ts <= start_time:
            previous = (ts, path)
        # A segment starting exactly at end_time belongs entirely to the
        # period after the requested window and is not needed.
        if start_time <= ts < end_time:
            selected.append((ts, path))

    # The previous segment is needed only when the requested window starts
    # inside it. If a segment starts exactly at start_time it already covers
    # the beginning of the window.
    if previous is not None and (not selected or selected[0][0] > start_time):
        if start_time - previous[0] <= get_segment_gap_limit(segments):
            selected.insert(0, previous)

    return selected


def find_event_segment(segments, event_time):
    candidate = None

    for ts, path in segments:
        if ts <= event_time:
            candidate = (ts, path)
        else:
            break

    if candidate is None and segments:
        candidate = min(segments, key=lambda item: abs(item[0] - event_time))

    return candidate


def create_event_work_dir(base):
    work_dir = TEMP_DIR / base

    # A retry for the same event replaces the data from the failed attempt.
    if work_dir.exists():
        remove_path(work_dir)

    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def write_event_log(log_file, oldest_name, copied_segments, event_name):
    with log_file.open("w", encoding="utf-8") as f:
        f.write(f"{oldest_name}    <- old\n\n")

        for _, path in copied_segments:
            marker = "    <- event" if path.name == event_name else ""
            f.write(f"{path.name}{marker}\n")


def create_concat_file(concat_file, segments):
    # The concat demuxer advances its virtual timeline by the duration it
    # reads for each file. MPEG-TS has no reliable duration in its header and
    # the newest segment may still be open, so FFmpeg can estimate a segment
    # as only one frame long. When that happens the following files are placed
    # almost at the same timestamp, all frames overlap, and the encoded clip
    # becomes a one second timelapse. Segment names contain the wall-clock
    # start of the segment, so the difference between two names is a reliable
    # duration for the concatenated timeline.
    with concat_file.open("w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")

        for index, (ts, path) in enumerate(segments):
            duration = None

            if index + 1 < len(segments):
                next_ts = segments[index + 1][0]
                if ts is not None and next_ts > ts:
                    duration = next_ts - ts

            if duration is None:
                duration = get_video_duration(path)

            if duration is None or not math.isfinite(duration) or duration <= 0:
                duration = SEGMENT_SECONDS

            f.write(f"file '{path}'\n")
            f.write(f"duration {duration:.6f}\n")


def calculate_expected_duration(local_segments, start_time):
    if not local_segments:
        return 0.0

    last_ts, last_path = local_segments[-1]
    last_duration = get_video_duration(last_path)

    if last_duration is None or not math.isfinite(last_duration) or last_duration <= 0:
        last_duration = SEGMENT_SECONDS

    effective_start = max(start_time, local_segments[0][0])
    available = last_ts + last_duration - effective_start

    return min(TARGET_DURATION, max(0.0, available))


def build_video(work_dir, concat_file, trim_offset, source_fps, normalize=False):
    output_file = work_dir / "event.mp4"

    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
    ]

    if not normalize:
        command += ["-ss", f"{trim_offset:.3f}"]

    command += [
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
    ]

    if normalize:
        # Rebuild the timeline from the number of decoded frames. This is a
        # fallback for segments whose internal timestamps are compressed or
        # non-monotonic; trim is applied after the timeline has been rebuilt.
        command += [
            "-vf",
            f"setpts=N/({source_fps:.6f}*TB),"
            f"trim=start={trim_offset:.3f}:duration={TARGET_DURATION:.3f},"
            f"setpts=PTS-STARTPTS",
            "-t", f"{TARGET_DURATION:.3f}",
        ]
    else:
        command += [
            "-vf", f"setpts=PTS-STARTPTS,fps={source_fps:.6f}",
            "-t", f"{TARGET_DURATION:.3f}",
        ]

    command += [
        "-an",
        "-r", f"{source_fps:.6f}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", VIDEO_CRF,
        "-maxrate", VIDEO_MAXRATE,
        "-bufsize", VIDEO_BUFSIZE,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",
        "-y", str(output_file)
    ]

    log.info(
        "Building video: offset=%.3fs, duration=%.3fs, fps=%.3f, normalize=%s",
        trim_offset, TARGET_DURATION, source_fps, normalize
    )

    if not run_ffmpeg(command):
        return None

    return output_file


def process_event(event_file):
    event_time = parse_event_time(event_file)

    if event_time is None:
        log.error("Invalid event filename: %s", event_file.name)
        delete_event_marker(event_file)
        return True

    now = time.time()

    if now - event_time > MAX_EVENT_AGE:
        log.error(
            "Event is too old for the current buffer: %s (age %.1fs, limit %.1fs)",
            event_file.name, now - event_time, MAX_EVENT_AGE
        )
        delete_event_marker(event_file)
        return True

    stamp = datetime.fromtimestamp(event_time).strftime("%Y%m%d_%H%M%S")
    base = f"event_{stamp}_{int(event_time * 1000) % 1000:03d}"

    # Create the event data directory immediately. The debug data must remain
    # available even if the event cannot be processed or the app is restarted.
    work_dir = create_event_work_dir(base)
    log.info("Event data directory: %s", work_dir)

    try:
        start_time = event_time - PRE_EVENT
        end_time = event_time + POST_EVENT
        wait_until = end_time + SEGMENT_SECONDS * 2.0
        now = time.time()

        if now < wait_until:
            delay = wait_until - now
            log.info(
                "Event %s: waiting %.1fs before copying segments "
                "(post %.1fs + 2 x segment %.1fs)",
                event_file.name, delay, POST_EVENT, SEGMENT_SECONDS
            )
            time.sleep(delay)

        segments = wait_for_segments(end_time)

        if not segments:
            log.warning("No buffer segments available")
            write_event_log(work_dir / "log.txt", "none", [], None)
            return False

        oldest_name = segments[0][1].name
        newest_time = segments[-1][0]
        age = time.time() - newest_time

        if age > STALE_BUFFER_SECONDS:
            log.warning("Buffer is stale; newest segment is %.1fs old: %s", age, datetime.fromtimestamp(newest_time))
            write_event_log(work_dir / "log.txt", oldest_name, [], None)
            return False

        selected = select_segments(start_time, end_time, segments)

        if not selected:
            log.warning("No suitable segments for event %s", event_file.name)
            write_event_log(work_dir / "log.txt", oldest_name, [], None)
            return False

        event_segment = find_event_segment(segments, event_time)
        event_name = event_segment[1].name if event_segment is not None else None

        log.info(
            "Event %s: oldest=%s, event segment=%s, selected=%d",
            event_file.name, oldest_name, event_name or "none", len(selected)
        )

        # Work on a private copy of the segments. The recorder may still be
        # writing the newest segment and the ring buffer cleanup may remove
        # old files while the video is being built.
        local_segments = []

        for _, source in selected:
            destination = work_dir / source.name
            if copy_segment(source, destination):
                copied_ts = segment_time(destination)
                if copied_ts is not None:
                    local_segments.append((copied_ts, destination))

        local_segments.sort(key=lambda item: item[0])

        if not local_segments:
            log.warning("No usable segments copied for event %s", event_file.name)
            write_event_log(work_dir / "log.txt", oldest_name, [], event_name)
            return False

        copied_names = {path.name for _, path in local_segments}
        if event_name not in copied_names:
            replacement = find_event_segment(local_segments, event_time)
            replacement_name = replacement[1].name if replacement is not None else None
            log.warning(
                "Event segment %s was not copied; marking %s instead",
                event_name or "none", replacement_name or "none"
            )
            event_name = replacement_name

        write_event_log(
            work_dir / "log.txt", oldest_name, local_segments, event_name
        )

        log.info(
            "Event %s: copied %d segment(s), event data=%s",
            event_file.name, len(local_segments), work_dir
        )

        concat_file = work_dir / "concat.txt"
        create_concat_file(concat_file, local_segments)

        trim_offset = max(0.0, start_time - local_segments[0][0])
        expected_duration = calculate_expected_duration(local_segments, start_time)

        log.info(
            "Event %s: trim=%.3fs, expected=%.3fs",
            event_file.name, trim_offset, expected_duration
        )

        if expected_duration < MIN_OUTPUT_SECONDS:
            log.error(
                "Not enough footage for event %s: only %.2fs available",
                event_file.name, expected_duration
            )
            return False

        fps_source = local_segments[len(local_segments) // 2][1]
        source_fps = get_video_fps(fps_source)

        if source_fps is None:
            source_fps = 25.0
            log.warning("Cannot detect source frame rate; using 25 fps")

        output_file = build_video(work_dir, concat_file, trim_offset, source_fps)
        valid = output_file is not None and validate_video(output_file, expected_duration)

        if not valid:
            if output_file is not None:
                output_file.unlink(missing_ok=True)

            log.warning("Retrying event %s with normalized timestamps", event_file.name)
            output_file = build_video(
                work_dir, concat_file, trim_offset, source_fps, normalize=True
            )
            valid = output_file is not None and validate_video(output_file, expected_duration)

        if not valid:
            if output_file is not None:
                output_file.unlink(missing_ok=True)
            log.error("Cannot create a valid video for event %s", event_file.name)
            return False

        ready_file = READY_DIR / f"{base}.mp4"
        partial_ready = READY_DIR / f".{base}.mp4.part"

        try:
            # Keep the event copy for diagnostics; only the ready copy is
            # published. Copy through a temporary name so Home Assistant never
            # sees a partially written MP4.
            shutil.copyfile(output_file, partial_ready)
            partial_ready.replace(ready_file)
        except Exception as e:
            try:
                partial_ready.unlink(missing_ok=True)
            except OSError:
                pass
            log.error("Cannot copy video to ready: %s", e)
            return False

        prune_ready_dir(keep_path=ready_file)

        if not publish_mqtt(ready_file):
            return False

        log.info("RECORDING READY: %s", ready_file)
        delete_event_marker(event_file)
        return True

    except Exception as e:
        log.exception("Event processing failed: %s", e)
        return False

    finally:
        cleanup_event_data(keep_path=work_dir)

        if DEBUG_MODE:
            log.info("Event data kept for debugging: %s", work_dir)
        else:
            log.info("Event data removed: %s", work_dir)


def main():
    log.info("Event processor started")
    log.info("Buffer: %ss, segment: %ss", BUFFER_SECONDS, SEGMENT_SECONDS)
    log.info("Pre-event: %ss, post-event: %ss, target: %ss", PRE_EVENT, POST_EVENT, TARGET_DURATION)
    log.info(
        "Video quality: CRF %s, maxrate %s, bufsize %s",
        VIDEO_CRF, VIDEO_MAXRATE, VIDEO_BUFSIZE
    )
    log.info(
        "Debug mode: %s, event data retention: %s day(s)",
        "on" if DEBUG_MODE else "off", EVENT_DATA_RETENTION_DAYS
    )
    log.info("Ready retention: %s day(s)", READY_RETENTION_DAYS)

    if PRE_EVENT + POST_EVENT > BUFFER_SECONDS:
        log.warning(
            "pre_event_seconds + post_event_seconds (%ss) exceed buffer_seconds (%ss); "
            "a complete event video cannot be built",
            TARGET_DURATION, BUFFER_SECONDS
        )

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = {}
    retry_at = {}

    while True:
        try:
            for event_file, future in list(futures.items()):
                if future.done():
                    try:
                        success = future.result()
                    except Exception as e:
                        log.error("Worker exception for %s: %s", event_file.name, e)
                        success = False

                    del futures[event_file]

                    if not success and event_file.exists():
                        retry_at[event_file] = time.time() + RETRY_DELAY
                        log.warning("Event will be retried: %s", event_file.name)

            for event_file in sorted(EVENT_DIR.glob("event_*.evt")):
                if event_file in futures:
                    continue

                if time.time() < retry_at.get(event_file, 0):
                    continue

                futures[event_file] = executor.submit(process_event, event_file)

            time.sleep(1)

        except Exception as e:
            log.exception("Main loop error: %s", e)
            time.sleep(2)


if __name__ == "__main__":
    main()
