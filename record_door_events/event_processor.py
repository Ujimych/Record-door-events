#!/usr/bin/env python3

import json
import time
import uuid
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

import paho.mqtt.client as mqtt

import os


APP_VERSION = os.environ.get(
    "APP_VERSION",
    "unknown"
)

# ==================================================
# Paths
# ==================================================

BUFFER_DIR = Path("/media/record-door-events/buffer")
EVENT_DIR = Path("/media/record-door-events/events")
RECORD_DIR = Path("/media/record-door-events/recordings")
READY_DIR = Path("/media/record-door-events/ready")
TEMP_DIR = Path("/media/record-door-events/tmp")

OPTIONS_FILE = Path("/data/options.json")


# ==================================================
# Logging
# ==================================================

def log(message):

    print(
        f"[event-processor] {message}",
        flush=True
    )


# ==================================================
# Options
# ==================================================

try:

    with OPTIONS_FILE.open(
        "r",
        encoding="utf-8"
    ) as f:

        OPTIONS = json.load(f)

except Exception as e:

    log(
        f"ERROR: unable to read "
        f"{OPTIONS_FILE}: {e}"
    )

    OPTIONS = {}


# ==================================================
# Recording parameters
# ==================================================

PRE_EVENT = float(
    OPTIONS.get(
        "pre_event_seconds",
        5
    )
)

POST_EVENT = float(
    OPTIONS.get(
        "post_event_seconds",
        10
    )
)

EVENT_MERGE = float(
    OPTIONS.get(
        "event_merge_seconds",
        3
    )
)

TARGET_DURATION = (
    PRE_EVENT +
    POST_EVENT
)


# ==================================================
# MQTT
# ==================================================

MQTT_TOPIC = "record_door_events/video_ready"

MQTT_HOST = OPTIONS.get(
    "mqtt_host",
    "core-mosquitto"
)

MQTT_PORT = int(
    OPTIONS.get(
        "mqtt_port",
        1883
    )
)

MQTT_USERNAME = OPTIONS.get(
    "mqtt_username",
    ""
)

MQTT_PASSWORD = OPTIONS.get(
    "mqtt_password",
    ""
)

mqtt_client = None


# ==================================================
# MQTT connection
# ==================================================

def mqtt_disconnect():

    global mqtt_client

    if mqtt_client is None:
        return

    try:
        mqtt_client.loop_stop()

    except Exception:
        pass

    try:
        mqtt_client.disconnect()

    except Exception:
        pass

    mqtt_client = None


def mqtt_connect():

    global mqtt_client

    mqtt_disconnect()

    try:

        client = mqtt.Client(
            callback_api_version=
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="record-door-events"
        )

        if MQTT_USERNAME:

            client.username_pw_set(
                MQTT_USERNAME,
                MQTT_PASSWORD
            )

        client.connect(
            MQTT_HOST,
            MQTT_PORT,
            60
        )

        client.loop_start()

        mqtt_client = client

        log(
            f"MQTT connected: "
            f"{MQTT_HOST}:{MQTT_PORT}"
        )

        return True

    except Exception as e:

        log(
            f"MQTT connection failed: {e}"
        )

        mqtt_client = None

        return False


def mqtt_publish(video_file):

    global mqtt_client

    for attempt in range(1, 4):

        if mqtt_client is None:

            if not mqtt_connect():

                log(
                    f"MQTT unavailable, "
                    f"attempt {attempt}/3"
                )

                time.sleep(2)

                continue

        try:

            result = mqtt_client.publish(
                MQTT_TOPIC,
                str(video_file),
                qos=1,
                retain=False
            )

            if result.rc != mqtt.MQTT_ERR_SUCCESS:

                log(
                    f"MQTT publish failed: "
                    f"rc={result.rc}"
                )

                mqtt_disconnect()

                time.sleep(2)

                continue

            try:

                result.wait_for_publish(
                    timeout=10
                )

            except Exception:

                pass

            if result.is_published():

                log(
                    f"MQTT published: "
                    f"{video_file}"
                )

                return True

            log(
                "MQTT publish timeout"
            )

            mqtt_disconnect()

        except Exception as e:

            log(
                f"MQTT publish exception: {e}"
            )

            mqtt_disconnect()

        time.sleep(2)

    return False


# ==================================================
# Segment helpers
# ==================================================

def get_segments():

    return sorted(
        BUFFER_DIR.glob("segment_*.ts"),
        key=lambda p: p.name
    )


def get_segment_timestamp(segment):

    try:

        timestamp_string = (
            segment.stem.replace(
                "segment_",
                ""
            )
        )

        return datetime.strptime(
            timestamp_string,
            "%Y%m%d_%H%M%S"
        ).timestamp()

    except Exception:

        return None


def wait_for_segment(
    segment,
    timeout=5.0
):

    deadline = time.time() + timeout

    last_size = -1
    stable_count = 0

    while time.time() < deadline:

        if not segment.exists():

            time.sleep(0.2)
            continue

        try:

            size = segment.stat().st_size

        except FileNotFoundError:

            time.sleep(0.2)
            continue

        if size > 0 and size == last_size:

            stable_count += 1

        else:

            stable_count = 0

        last_size = size

        if stable_count >= 2:

            return True

        time.sleep(0.2)

    try:

        return (
            segment.exists()
            and
            segment.stat().st_size > 0
        )

    except FileNotFoundError:

        return False


# ==================================================
# Event helpers
# ==================================================

def get_event_timestamp(event_file):

    try:

        return float(
            event_file.stem.replace(
                "event_",
                ""
            )
        )

    except Exception:

        return None


def get_event_files():

    result = []

    for path in EVENT_DIR.glob("event_*.evt"):

        timestamp = get_event_timestamp(path)

        if timestamp is None:
            continue

        result.append(
            (timestamp, path)
        )

    result.sort(
        key=lambda item: item[0]
    )

    return result


def delete_event_marker(event_file):

    try:

        event_file.unlink()

        log(
            f"Event marker removed: "
            f"{event_file.name}"
        )

        return True

    except FileNotFoundError:

        return True

    except Exception as e:

        log(
            f"Unable to remove event marker "
            f"{event_file.name}: {e}"
        )

        return False


# ==================================================
# Copy segment
# ==================================================

def copy_segment(
    segment,
    temp_event_dir
):

    if not segment.exists():

        return None

    if not wait_for_segment(
        segment,
        timeout=5.0
    ):

        log(
            f"Segment not ready: "
            f"{segment.name}"
        )

        return None

    destination = (
        temp_event_dir /
        segment.name
    )

    try:

        shutil.copyfile(
            segment,
            destination
        )

    except FileNotFoundError:

        return None

    except Exception as e:

        log(
            f"Unable to copy "
            f"{segment.name}: {e}"
        )

        return None

    try:

        if destination.stat().st_size <= 0:

            destination.unlink(
                missing_ok=True
            )

            return None

    except Exception:

        return None

    return destination


# ==================================================
# Copy pre-event segments
# ==================================================

def copy_pre_event_segments(
    event_timestamp,
    temp_event_dir
):

    start_time = (
        event_timestamp -
        PRE_EVENT
    )

    segments = get_segments()

    selected = []

    previous = None

    for segment in segments:

        timestamp = get_segment_timestamp(
            segment
        )

        if timestamp is None:
            continue

        if timestamp <= start_time:

            previous = segment

        if (
            timestamp >= start_time
            and
            timestamp <= event_timestamp
        ):

            selected.append(segment)

    if previous is not None:

        if previous not in selected:

            selected.insert(
                0,
                previous
            )

    selected.sort(
        key=lambda p:
        get_segment_timestamp(p)
        or 0
    )

    copied = []

    for segment in selected:

        local = copy_segment(
            segment,
            temp_event_dir
        )

        if local is not None:

            copied.append(local)

    return copied


# ==================================================
# Copy final segments
# ==================================================

def copy_all_required_segments(
    start_time,
    end_time,
    temp_event_dir
):

    segments = get_segments()

    selected = []

    previous = None

    for segment in segments:

        timestamp = get_segment_timestamp(
            segment
        )

        if timestamp is None:
            continue

        if timestamp <= start_time:

            previous = segment

        if (
            timestamp >= start_time
            and
            timestamp <= end_time
        ):

            selected.append(segment)

    if previous is not None:

        if previous not in selected:

            selected.insert(
                0,
                previous
            )

    selected.sort(
        key=lambda p:
        get_segment_timestamp(p)
        or 0
    )

    copied = []

    for segment in selected:

        local = copy_segment(
            segment,
            temp_event_dir
        )

        if local is not None:

            copied.append(local)

    return copied


# ==================================================
# Concat
# ==================================================

def create_concat_file(
    concat_file,
    segments
):

    with concat_file.open(
        "w",
        encoding="utf-8"
    ) as f:

        f.write(
            "ffconcat version 1.0\n"
        )

        for segment in segments:

            path = str(
                segment
            ).replace(
                "'",
                "'\\''"
            )

            f.write(
                f"file '{path}'\n"
            )


# ==================================================
# Video duration
# ==================================================

def get_video_duration(video_file):

    command = [

        "ffprobe",

        "-v",
        "error",

        "-show_entries",
        "format=duration",

        "-of",
        "default=noprint_wrappers=1:nokey=1",

        str(video_file)
    ]

    try:

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15
        )

    except Exception as e:

        log(
            f"ffprobe exception: {e}"
        )

        return None

    if result.returncode != 0:

        return None

    try:

        return float(
            result.stdout.strip()
        )

    except Exception:

        return None


# ==================================================
# Video validation
# ==================================================

def validate_video(video_file):

    if not video_file.exists():

        return False

    try:

        size = video_file.stat().st_size

    except Exception:

        return False

    if size < 10000:

        log(
            f"Video too small: "
            f"{size} bytes"
        )

        return False

    duration = get_video_duration(
        video_file
    )

    if duration is None:

        log(
            "Video validation failed: "
            "duration unavailable"
        )

        return False

    log(
        f"Video validated: "
        f"{video_file.name}, "
        f"duration={duration:.2f}s"
    )

    if duration < max(
        1.0,
        min(
            5.0,
            TARGET_DURATION
        )
    ):

        log(
            f"Video validation failed: "
            f"duration only {duration:.2f}s"
        )

        return False

    return True


# ==================================================
# Build video
# ==================================================

def build_video(
    event_timestamp,
    temp_event_dir,
    local_segments
):

    if not local_segments:

        log(
            "No local segments available"
        )

        return None

    local_segments.sort(
        key=lambda p:
        get_segment_timestamp(p)
        or 0
    )

    first_timestamp = get_segment_timestamp(
        local_segments[0]
    )

    if first_timestamp is None:

        log(
            "Unable to determine "
            "first segment timestamp"
        )

        return None

    concat_file = (
        temp_event_dir /
        "concat.ffconcat"
    )

    create_concat_file(
        concat_file,
        local_segments
    )

    combined_file = (
        temp_event_dir /
        "combined.ts"
    )

    # One and only one video encoding pass.
    #
    # We first create a continuous elementary
    # transport stream and then perform the final
    # trim/MP4 creation in the same FFmpeg process.
    #
    # This replaces the previous:
    #
    # segments -> combined.ts
    # combined.ts -> MP4
    #
    # two-encode pipeline.

    timestamp = datetime.fromtimestamp(
        event_timestamp
    )

    output_file = (
        temp_event_dir /
        (
            "event_"
            +
            timestamp.strftime(
                "%Y%m%d_%H%M%S"
            )
            +
            "_"
            +
            uuid.uuid4().hex[:6]
            +
            ".mp4"
        )
    )

    trim_offset = max(
        0.0,
        (
            event_timestamp -
            PRE_EVENT -
            first_timestamp
        )
    )

    command = [

        "ffmpeg",

        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",

        "-f",
        "concat",

        "-safe",
        "0",

        "-i",
        str(concat_file),

        "-ss",
        f"{trim_offset:.3f}",

        "-t",
        f"{TARGET_DURATION:.3f}",

        "-an",

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "22",

        "-pix_fmt",
        "yuv420p",

        "-movflags",
        "+faststart",

        "-avoid_negative_ts",
        "make_zero",

        "-y",

        str(output_file)
    ]

    log(
        f"Creating video: "
        f"offset={trim_offset:.3f}s "
        f"duration={TARGET_DURATION:.3f}s"
    )

    try:

        result = subprocess.run(
            command,
            timeout=300
        )

    except subprocess.TimeoutExpired:

        log(
            "FFmpeg timeout while creating video"
        )

        return None

    except Exception as e:

        log(
            f"FFmpeg exception: {e}"
        )

        return None

    if result.returncode != 0:

        log(
            f"FFmpeg failed: "
            f"exit code {result.returncode}"
        )

        return None

    if not validate_video(
        output_file
    ):

        try:

            output_file.unlink()

        except FileNotFoundError:

            pass

        return None

    return output_file


# ==================================================
# Wait and collect merged events
# ==================================================

def collect_event_group(
    primary_timestamp
):

    event_files = get_event_files()

    group = []

    for timestamp, path in event_files:

        if timestamp == primary_timestamp:

            group.append(
                path
            )

            break

    if not group:

        return [], primary_timestamp

    latest_timestamp = primary_timestamp

    deadline = (
        latest_timestamp +
        POST_EVENT
    )

    log(
        f"Waiting for event group: "
        f"post-event deadline="
        f"{datetime.fromtimestamp(deadline)}"
    )

    while True:

        now = time.time()

        if now < deadline:

            sleep_time = min(
                0.5,
                deadline - now
            )

            time.sleep(
                max(
                    0.05,
                    sleep_time
                )
            )

        found_new = False

        for timestamp, path in get_event_files():

            if path in group:

                continue

            if timestamp < latest_timestamp:

                continue

            if (
                timestamp -
                latest_timestamp
            ) <= EVENT_MERGE:

                group.append(path)

                latest_timestamp = timestamp

                deadline = (
                    latest_timestamp +
                    POST_EVENT
                )

                found_new = True

                log(
                    f"Merging event: "
                    f"{path.name}; "
                    f"new deadline="
                    f"{datetime.fromtimestamp(deadline)}"
                )

        if now >= deadline and not found_new:

            break

    group.sort(
        key=lambda p:
        get_event_timestamp(p)
        or 0
    )

    return group, latest_timestamp


# ==================================================
# Process event group
# ==================================================

def process_event_group(
    primary_file
):

    primary_timestamp = get_event_timestamp(
        primary_file
    )

    if primary_timestamp is None:

        log(
            f"Invalid event file: "
            f"{primary_file.name}"
        )

        return False

    log(
        f"EVENT START: "
        f"{datetime.fromtimestamp(primary_timestamp)}"
    )

    group, latest_event = collect_event_group(
        primary_timestamp
    )

    if not group:

        return False

    start_time = (
        primary_timestamp -
        PRE_EVENT
    )

    end_time = (
        latest_event +
        POST_EVENT
    )

    log(
        f"Event interval: "
        f"{datetime.fromtimestamp(start_time)}"
        f" -> "
        f"{datetime.fromtimestamp(end_time)}"
    )

    temp_event_dir = (
        TEMP_DIR /
        f"event_{uuid.uuid4().hex}"
    )

    temp_event_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    try:

        # --------------------------------------------------
        # Snapshot the pre-event part immediately.
        # This prevents the oldest required segments
        # from disappearing from the ring buffer while
        # we wait for the post-event period.
        # --------------------------------------------------

        copied_pre = copy_pre_event_segments(
            primary_timestamp,
            temp_event_dir
        )

        log(
            f"Pre-event snapshot: "
            f"{len(copied_pre)} segments"
        )

        # --------------------------------------------------
        # At this point the event group has finished.
        # Copy everything required for the final video.
        # --------------------------------------------------

        copied_all = copy_all_required_segments(
            start_time,
            end_time,
            temp_event_dir
        )

        log(
            f"Final segment snapshot: "
            f"{len(copied_all)} segments"
        )

        if not copied_all:

            log(
                "ERROR: no usable segments"
            )

            return False

        # Remove duplicates.
        unique_segments = []

        seen_names = set()

        for segment in (
            copied_pre +
            copied_all
        ):

            if segment.name in seen_names:

                continue

            seen_names.add(
                segment.name
            )

            unique_segments.append(
                segment
            )

        unique_segments.sort(
            key=lambda p:
            get_segment_timestamp(p)
            or 0
        )

        log(
            f"Using {len(unique_segments)} "
            f"unique segments"
        )

        output_file = build_video(
            primary_timestamp,
            temp_event_dir,
            unique_segments
        )

        if output_file is None:

            log(
                "ERROR: video creation failed"
            )

            return False

        # --------------------------------------------------
        # Move atomically into ready/
        # --------------------------------------------------

        READY_DIR.mkdir(
            parents=True,
            exist_ok=True
        )

        ready_file = (
            READY_DIR /
            output_file.name
        )

        try:

            output_file.replace(
                ready_file
            )

        except Exception as e:

            log(
                f"ERROR moving video to ready: {e}"
            )

            return False

        log(
            f"RECORDING READY: "
            f"{ready_file.name}"
        )

        # --------------------------------------------------
        # MQTT
        # --------------------------------------------------

        if not mqtt_publish(
            ready_file
        ):

            log(
                "MQTT delivery failed. "
                "Event markers will be kept "
                "for retry."
            )

            return False

        # --------------------------------------------------
        # Only now can event markers be deleted.
        # --------------------------------------------------

        for event_file in group:

            delete_event_marker(
                event_file
            )

        return True

    finally:

        shutil.rmtree(
            temp_event_dir,
            ignore_errors=True
        )


# ==================================================
# Main
# ==================================================

def main():

    log(
        f"Event processor {APP_VERSION} started."
    )

    EVENT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    RECORD_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    READY_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    TEMP_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    mqtt_connect()

    last_retry_time = 0

    while True:

        events = get_event_files()

        if not events:

            time.sleep(0.2)
            continue

        primary_timestamp, primary_file = (
            events[0]
        )

        log(
            f"Processing event: "
            f"{primary_file.name}"
        )

        success = process_event_group(
            primary_file
        )

        if success:

            last_retry_time = 0

            continue

        # --------------------------------------------------
        # Do not delete failed events.
        # Leave them in events/ and retry later.
        # --------------------------------------------------

        now = time.time()

        if (
            now -
            last_retry_time
            >= 5
        ):

            log(
                f"Event processing failed: "
                f"{primary_file.name}. "
                f"Retrying in 5 seconds."
            )

            last_retry_time = now

        time.sleep(5)


# ==================================================
# Start
# ==================================================

if __name__ == "__main__":

    main()
