import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BUFFER_DIR = Path("/media/record-door-events/buffer")
EVENT_DIR = Path("/media/record-door-events/events")
RECORD_DIR = Path("/media/record-door-events/recordings")
READY_DIR = Path("/media/record-door-events/ready")
TEMP_DIR = Path("/media/record-door-events/tmp")
OPTIONS_FILE = Path("/data/options.json")

MQTT_TOPIC = "record_door_events/video_ready"

PRE_EVENT = 5.0
POST_EVENT = 10.0
TARGET_DURATION = 15.0
STALE_BUFFER_SECONDS = 120
RETRY_DELAY = 5
MAX_WORKERS = 10

VIDEO_BITRATE = "2M"
VIDEO_MAXRATE = "2M"
VIDEO_BUFSIZE = "4M"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("event_processor")

TEMP_DIR.mkdir(parents=True, exist_ok=True)
RECORD_DIR.mkdir(parents=True, exist_ok=True)
READY_DIR.mkdir(parents=True, exist_ok=True)


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
MQTT_HOST = OPTIONS.get("mqtt_host", "core-mosquitto")
MQTT_PORT = int(OPTIONS.get("mqtt_port", 1883))
MQTT_USER = OPTIONS.get("mqtt_username", "")
MQTT_PASSWORD = OPTIONS.get("mqtt_password", "")


def parse_event_time(path):
    try:
        return float(path.stem.replace("event_", ""))
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
        if ts is not None and path.exists() and path.stat().st_size > 0:
            result.append((ts, path))
    return sorted(result)


def delete_event_marker(event_file):
    try:
        event_file.unlink()
        log.info("Event marker removed: %s", event_file.name)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error("Cannot remove event marker %s: %s", event_file, e)


def run_ffmpeg(command):
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            log.error("FFmpeg failed: %s", result.stderr.strip())
            return False
        return True
    except Exception as e:
        log.error("FFmpeg exception: %s", e)
        return False


def wait_for_segments(end_time):
    while time.time() < end_time:
        segments = get_segments()
        if segments:
            newest = segments[-1][0]
            if newest >= end_time - SEGMENT_SECONDS:
                return segments
        time.sleep(1)
    return get_segments()


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


def validate_video(path):
    if not path.exists() or path.stat().st_size == 0:
        return False

    command = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("Validation failed: %s", result.stderr.strip())
            return False

        duration = float(result.stdout.strip())
        log.info("Validated: %.2fs, %.1f MB", duration, path.stat().st_size / 1024 / 1024)
        return duration > 0
    except Exception as e:
        log.error("Validation exception: %s", e)
        return False


def process_event(event_file):
    event_time = parse_event_time(event_file)
    if event_time is None:
        log.error("Invalid event filename: %s", event_file.name)
        delete_event_marker(event_file)
        return True

    now = time.time()

    if now - event_time > BUFFER_SECONDS + POST_EVENT + 30:
        log.error("Event is too old for current buffer: %s", event_file.name)
        delete_event_marker(event_file)
        return True

    start_time = event_time - PRE_EVENT
    end_time = event_time + POST_EVENT

    if now < end_time:
        time.sleep(end_time - now + 1)

    segments = get_segments()

    if not segments:
        log.warning("No buffer segments available")
        return False

    newest_time = segments[-1][0]
    age = time.time() - newest_time

    if age > STALE_BUFFER_SECONDS:
        log.warning("Buffer is stale; newest segment is %.1fs old: %s", age, datetime.fromtimestamp(newest_time))
        return False

    selected = [(ts, path) for ts, path in segments if ts <= end_time and ts + SEGMENT_SECONDS >= start_time]

    if not selected:
        log.warning("No suitable segments for event %s", event_file.name)
        return False

    selected = sorted(selected)

    log.info("Event %s: selected %d segment(s)", event_file.name, len(selected))
    for _, path in selected:
        log.info("  %s", path.name)

    stamp = datetime.fromtimestamp(event_time).strftime("%Y%m%d_%H%M%S")
    base = f"event_{stamp}_{int(event_time * 1000) % 1000:03d}"

    work_dir = TEMP_DIR / base
    work_dir.mkdir(parents=True, exist_ok=True)

    combined = work_dir / "combined.mp4"
    normalized = work_dir / "normalized.mp4"
    output_file = READY_DIR / f"{base}.mp4"

    try:
        copied = []

        for index, (_, source) in enumerate(selected):
            destination = work_dir / f"segment_{index:04d}.ts"

            try:
                with source.open("rb") as src, destination.open("wb") as dst:
                    while True:
                        data = src.read(1024 * 1024)
                        if not data:
                            break
                        dst.write(data)
                copied.append(destination)
            except Exception as e:
                log.error("Cannot copy %s: %s", source, e)
                return False

        concat_file = work_dir / "concat.txt"
        with concat_file.open("w") as f:
            for path in copied:
                f.write(f"file '{path.name}'\n")

        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c", "copy", "-movflags", "+faststart", "-y", str(combined)
        ]

        if not run_ffmpeg(command):
            return False

        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror",
            "-i", str(combined),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-b:v", VIDEO_BITRATE,
            "-maxrate", VIDEO_MAXRATE,
            "-bufsize", VIDEO_BUFSIZE,
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "+faststart",
            "-y", str(normalized)
        ]

        if not run_ffmpeg(command):
            return False

        first_segment_time = selected[0][0]
        trim_offset = max(0.0, start_time - first_segment_time)

        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror",
            "-i", str(normalized),
            "-ss", f"{trim_offset:.3f}",
            "-t", f"{TARGET_DURATION:.3f}",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-b:v", VIDEO_BITRATE,
            "-maxrate", VIDEO_MAXRATE,
            "-bufsize", VIDEO_BUFSIZE,
            "-pix_fmt", "yuv420p",
            "-an",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            "-y", str(output_file)
        ]

        if not run_ffmpeg(command):
            return False

        if not validate_video(output_file):
            try:
                output_file.unlink()
            except FileNotFoundError:
                pass
            return False

        if not publish_mqtt(output_file):
            return False

        log.info("RECORDING READY: %s", output_file)
        delete_event_marker(event_file)
        return True

    except Exception as e:
        log.exception("Event processing failed: %s", e)
        return False

    finally:
        try:
            for path in work_dir.glob("*"):
                path.unlink(missing_ok=True)
            work_dir.rmdir()
        except Exception:
            pass


def main():
    log.info("Event processor started")
    log.info("Buffer: %ss, segment: %ss", BUFFER_SECONDS, SEGMENT_SECONDS)
    log.info("Video bitrate: %s", VIDEO_BITRATE)

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
