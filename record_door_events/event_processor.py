#!/usr/bin/env python3

import json
import time
import uuid
import shutil
import subprocess

from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import paho.mqtt.client as mqtt

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

TARGET_DURATION = (
    PRE_EVENT +
    POST_EVENT
)

MAX_WORKERS = 10

MQTT_TOPIC = "record_door_events/video_ready"

mqtt_client = None

# ==================================================
# Ready retention
# ==================================================

READY_RETENTION_DAYS = int(
    OPTIONS.get(
        "ready_retention_days",
        3
    )
)

READY_RETENTION_SECONDS = (
    READY_RETENTION_DAYS *
    24 *
    60 *
    60
)

# ==================================================
# MQTT options
# ==================================================

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

# ==================================================
# MQTT
# ==================================================

def mqtt_connect():
    global mqtt_client

    try:
        mqtt_client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="record-door-events"
        )

        if MQTT_USERNAME:
            mqtt_client.username_pw_set(
                MQTT_USERNAME,
                MQTT_PASSWORD
            )

        mqtt_client.connect(
            MQTT_HOST,
            MQTT_PORT,
            60
        )

        mqtt_client.loop_start()

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

# ==================================================
# Cleanup ready directory
# ==================================================

def cleanup_ready_directory():
    try:
        if not READY_DIR.exists():
            return

        now = time.time()

        deleted = 0

        for video_file in READY_DIR.iterdir():
            if not video_file.is_file():
                continue

            try:
                age = (
                    now -
                    video_file.stat().st_mtime
                )

            except FileNotFoundError:
                continue

            if age > READY_RETENTION_SECONDS:
                try:
                    video_file.unlink()

                    deleted += 1

                    log(
                        f"Deleted old ready video: "
                        f"{video_file.name}"
                    )

                except FileNotFoundError:
                    pass

                except Exception as e:
                    log(
                        f"Unable to delete old "
                        f"ready video "
                        f"{video_file.name}: {e}"
                    )

        if deleted:
            log(
                f"Ready cleanup: "
                f"deleted {deleted} file(s)"
            )

    except Exception as e:
        log(
            f"Ready cleanup error: {e}"
        )

# ==================================================
# Buffer
# ==================================================

def get_segments():
    return sorted(
        BUFFER_DIR.glob("segment_*.ts"),
        key=lambda p: p.name
    )


def get_segment_timestamp(segment):
    try:
        timestamp_string = (
            segment.stem
            .replace(
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

# ==================================================
# Event marker
# ==================================================

def delete_event_marker(event_file):
    try:
        event_file.unlink()

        log(
            f"Event marker removed: "
            f"{event_file.name}"
        )

    except FileNotFoundError:
        pass

    except Exception as e:
        log(
            f"Unable to remove event marker "
            f"{event_file.name}: {e}"
        )

# ==================================================
# Wait for segment to become stable
# ==================================================

def wait_for_segment(
    segment,
    timeout=5.0
):
    deadline = (
        time.time() +
        timeout
    )

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
# Probe video duration
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

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        return None

    try:
        return float(
            result.stdout.strip()
        )

    except Exception:
        return None

# ==================================================
# Validate video
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
            f"Video validation failed: "
            f"file too small ({size} bytes)"
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

    if duration < 5.0:
        log(
            f"Video validation failed: "
            f"duration only {duration:.2f}s"
        )

        return False

    return True

# ==================================================
# Create concat list
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
            f.write(
                f"file '{segment}'\n"
            )

# ==================================================
# Process event
# ==================================================

def process_event(event_file):

    temp_event_dir = None

    try:
        # --------------------------------------------------
        # Event timestamp
        # --------------------------------------------------

        try:
            event_timestamp = float(
                event_file.stem.replace(
                    "event_",
                    ""
                )
            )

        except Exception as e:
            log(
                f"Invalid event file "
                f"{event_file}: {e}"
            )

            return

        log(
            f"EVENT START: "
            f"{datetime.fromtimestamp(event_timestamp)}"
        )

        # --------------------------------------------------
        # Wait until +POST_EVENT
        # --------------------------------------------------

        target_time = (
            event_timestamp +
            POST_EVENT
        )

        wait_time = (
            target_time -
            time.time()
        )

        if wait_time > 0:
            log(
                f"Event {event_file.name}: "
                f"waiting {wait_time:.1f}s"
            )

            time.sleep(
                wait_time
            )


        # Give segmenter time to finish
        # the current TS file.

        time.sleep(
            2.0
        )

        # --------------------------------------------------
        # Requested interval
        # --------------------------------------------------

        start_time = (
            event_timestamp -
            PRE_EVENT
        )

        end_time = (
            event_timestamp +
            POST_EVENT
        )

        log(
            f"Event {event_file.name}: "
            f"interval "
            f"{datetime.fromtimestamp(start_time)}"
            f" -> "
            f"{datetime.fromtimestamp(end_time)}"
        )

        # --------------------------------------------------
        # Select overlapping segments
        # --------------------------------------------------

        all_segments = get_segments()

        selected = []

        previous_segment = None

        for segment in all_segments:
            segment_time = (
                get_segment_timestamp(
                    segment
                )
            )

            if segment_time is None:
                continue

            if segment_time <= start_time:
                previous_segment = segment

            if (
                segment_time >= start_time
                and
                segment_time <= end_time
            ):
                selected.append(
                    segment
                )

        if previous_segment is not None:
            if previous_segment not in selected:
                selected.insert(
                    0,
                    previous_segment
                )

        selected.sort(
            key=lambda p: (
                get_segment_timestamp(p)
                if get_segment_timestamp(p) is not None
                else 0
            )
        )

        if not selected:
            log(
                f"Event {event_file.name}: "
                f"ERROR: required segments "
                f"not found"
            )

            return

        selected = list(
            dict.fromkeys(
                selected
            )
        )

        log(
            f"Event {event_file.name}: "
            f"selected {len(selected)} segments"
        )

        # --------------------------------------------------
        # Wait for all selected files
        # --------------------------------------------------

        stable_segments = []

        for segment in selected:
            if wait_for_segment(
                segment,
                timeout=5.0
            ):
                stable_segments.append(
                    segment
                )

            else:
                log(
                    f"Event {event_file.name}: "
                    f"WARNING: segment not ready: "
                    f"{segment.name}"
                )

        if not stable_segments:
            log(
                f"Event {event_file.name}: "
                f"ERROR: no stable segments"
            )

            return

        # --------------------------------------------------
        # Temporary directory
        # --------------------------------------------------

        TEMP_DIR.mkdir(
            parents=True,
            exist_ok=True
        )

        temp_event_dir = (
            TEMP_DIR /
            f"event_{uuid.uuid4().hex}"
        )

        temp_event_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        # --------------------------------------------------
        # Copy segments
        # --------------------------------------------------

        local_segments = []

        for segment in stable_segments:
            if not segment.exists():
                log(
                    f"Event {event_file.name}: "
                    f"segment disappeared: "
                    f"{segment.name}"
                )

                continue

            local_segment = (
                temp_event_dir /
                segment.name
            )

            try:
                shutil.copyfile(
                    segment,
                    local_segment
                )

            except Exception as e:
                log(
                    f"Event {event_file.name}: "
                    f"ERROR copying "
                    f"{segment.name}: {e}"
                )

                continue

            try:
                size = (
                    local_segment.stat().st_size
                )

            except Exception:
                size = 0

            if size <= 0:
                log(
                    f"Event {event_file.name}: "
                    f"ERROR: empty segment: "
                    f"{segment.name}"
                )

                continue

            local_segments.append(
                local_segment
            )

        log(
            f"Event {event_file.name}: "
            f"copied {len(local_segments)} "
            f"segments to temporary storage"
        )

        if not local_segments:
            log(
                f"Event {event_file.name}: "
                f"ERROR: no usable segments"
            )

            return

        for segment in local_segments:
            log(
                f"Using segment: "
                f"{segment.name}"
            )

        # --------------------------------------------------
        # Concat
        # --------------------------------------------------

        concat_file = (
            temp_event_dir /
            "concat.txt"
        )

        create_concat_file(
            concat_file,
            local_segments
        )

        # --------------------------------------------------
        # Intermediate video
        # --------------------------------------------------

        intermediate_file = (
            temp_event_dir /
            "combined.ts"
        )

        concat_command = [

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

            "-an",

            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-crf",
            "22",

            "-pix_fmt",
            "yuv420p",

            "-y",

            str(intermediate_file)
        ]

        log(
            f"Event {event_file.name}: "
            f"combining segments"
        )

        result = subprocess.run(
            concat_command
        )

        if result.returncode != 0:
            log(
                f"Event {event_file.name}: "
                f"FFmpeg combine ERROR: "
                f"exit code {result.returncode}"
            )

            return

        if not intermediate_file.exists():
            log(
                f"Event {event_file.name}: "
                f"ERROR: intermediate file "
                f"was not created"
            )

            return

        # --------------------------------------------------
        # Determine intermediate duration
        # --------------------------------------------------

        intermediate_duration = (
            get_video_duration(
                intermediate_file
            )
        )

        if intermediate_duration is None:
            log(
                f"Event {event_file.name}: "
                f"ERROR: unable to determine "
                f"intermediate duration"
            )

            return

        log(
            f"Intermediate duration: "
            f"{intermediate_duration:.2f}s"
        )

        # --------------------------------------------------
        # Final output
        # --------------------------------------------------

        timestamp = datetime.fromtimestamp(
            event_timestamp
        )

        output_file = (
            RECORD_DIR /
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

        # --------------------------------------------------
        # Final trim
        # --------------------------------------------------

        first_segment_timestamp = (
            get_segment_timestamp(
                stable_segments[0]
            )
        )

        if first_segment_timestamp is None:
            log(
                f"Event {event_file.name}: "
                f"ERROR: invalid first segment timestamp"
            )

            return

        trim_offset = max(
            0.0,
            start_time -
            first_segment_timestamp
        )

		available_after_offset = (
			intermediate_duration -
			trim_offset
		)

		if available_after_offset <= 0:
			log(
				f"Event {event_file.name}: "
				f"ERROR: no video available after "
				f"start offset "
				f"(intermediate={intermediate_duration:.2f}s, "
				f"offset={trim_offset:.3f}s)"
			)

			return

		if available_after_offset < TARGET_DURATION:
			log(
				f"Event {event_file.name}: "
				f"WARNING: only "
				f"{available_after_offset:.2f}s "
				f"available after start offset"
			)

		final_duration = min(
			TARGET_DURATION,
			available_after_offset
		)

        # --------------------------------------------------
        # Final FFmpeg
        # --------------------------------------------------

        final_command = [

            "ffmpeg",

            "-hide_banner",

            "-loglevel",
            "error",

            "-xerror",

            "-i",
            str(intermediate_file),

            "-ss",
            f"{trim_offset:.3f}",

            "-t",
            f"{final_duration:.3f}",

            "-an",

            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-crf",
            "22",

            "-pix_fmt",
            "yuv420p",

            "-avoid_negative_ts",
            "make_zero",

            "-movflags",
            "+faststart",

            "-y",

            str(output_file)
        ]

        log(
            f"Event {event_file.name}: "
            f"final trim "
            f"offset={trim_offset:.3f}s "
            f"duration={final_duration:.3f}s"
        )

        result = subprocess.run(
            final_command
        )

        if result.returncode != 0:
            log(
                f"Event {event_file.name}: "
                f"FFmpeg final trim ERROR: "
                f"exit code {result.returncode}"
            )

            return

        # --------------------------------------------------
        # Validate
        # --------------------------------------------------

        if not validate_video(
            output_file
        ):
            log(
                f"Event {event_file.name}: "
                f"ERROR: generated video "
                f"failed validation"
            )

            try:
                output_file.unlink()

            except FileNotFoundError:

                pass

            return

        # --------------------------------------------------
        # Move to ready
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
            output_file.rename(
                ready_file
            )

        except Exception as e:
            log(
                f"Event {event_file.name}: "
                f"ERROR moving video to ready: "
                f"{e}"
            )

            return

        log(
            f"Event {event_file.name}: "
            f"RECORDING READY: "
            f"{ready_file.name}"
        )

        # --------------------------------------------------
        # MQTT
        # --------------------------------------------------

        if mqtt_client is None:
            log(
                "MQTT unavailable; "
                "video remains in ready/"
            )

            return

        try:
            result = mqtt_client.publish(
                MQTT_TOPIC,
                str(ready_file),
                qos=1,
                retain=False
            )

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                log(
                    f"MQTT published: "
                    f"{ready_file}"
                )

            else:
                log(
                    f"MQTT publish failed: "
                    f"rc={result.rc}"
                )

        except Exception as e:
            log(
                f"MQTT publish exception: "
                f"{e}"
            )

    finally:
        if temp_event_dir is not None:
            shutil.rmtree(
                temp_event_dir,
                ignore_errors=True
            )

        delete_event_marker(
            event_file
        )

# ==================================================
# Main
# ==================================================

def main():
    log(
        "Event processor started."
    )

    log(
        f"Ready retention: "
        f"{READY_RETENTION_DAYS} day(s)"
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

    # Initial cleanup
    cleanup_ready_directory()

    mqtt_connect()

    executor = ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    )

    submitted = set()

    last_cleanup = 0

    while True:
        # --------------------------------------------------
        # Periodic cleanup
        # --------------------------------------------------

        current_time = time.time()

        if (
            current_time -
            last_cleanup
            >= 3600
        ):

            cleanup_ready_directory()

            last_cleanup = current_time

        # --------------------------------------------------
        # Process events
        # --------------------------------------------------

        events = sorted(
            EVENT_DIR.glob(
                "event_*.evt"
            )
        )

        for event_file in events:
            if event_file in submitted:
                continue

            submitted.add(
                event_file
            )

            log(
                f"Submitting event: "
                f"{event_file.name}"
            )

            executor.submit(
                process_event,
                event_file
            )

        existing_events = set(
            EVENT_DIR.glob(
                "event_*.evt"
            )
        )

        submitted.intersection_update(
            existing_events
        )

        time.sleep(
            0.1
        )

# ==================================================
# Start
# ==================================================

if __name__ == "__main__":
    main()
