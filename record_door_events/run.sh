#!/bin/bash

set -e

echo "======================================"
echo " Record Door Events"
echo "======================================"

#
# ==================================================
# Read options
# ==================================================
#

RTSP_URL="$(python3 -c "
import json
with open('/data/options.json') as f:
    print(json.load(f)['rtsp_url'])
")"

BUFFER_SECONDS="$(python3 -c "
import json
with open('/data/options.json') as f:
    print(json.load(f)['buffer_seconds'])
")"

SEGMENT_SECONDS="$(python3 -c "
import json
with open('/data/options.json') as f:
    print(json.load(f)['segment_seconds'])
")"

#
# ==================================================
# Read application version from config.yaml
# ==================================================
#

APP_VERSION="$(python3 -c "
import re

with open('/app/config.yaml', encoding='utf-8') as f:
    text = f.read()

match = re.search(
    r'^version:\s*[\"'\"']?([^\"'\"'\s]+)',
    text,
    re.MULTILINE
)

print(match.group(1) if match else 'unknown')
")"

export APP_VERSION

echo "Record Door Events version: ${APP_VERSION}"

#
# ==================================================
# Validate RTSP
# ==================================================
#

if [ -z "$RTSP_URL" ]; then
    echo "ERROR: rtsp_url is empty!"
    exit 1
fi

#
# ==================================================
# Directories
# ==================================================
#

BUFFER_DIR="/media/record-door-events/buffer"
EVENT_DIR="/media/record-door-events/events"
RECORD_DIR="/media/record-door-events/recordings"
READY_DIR="/media/record-door-events/ready"
TEMP_DIR="/media/record-door-events/tmp"

mkdir -p "$BUFFER_DIR"
mkdir -p "$EVENT_DIR"
mkdir -p "$RECORD_DIR"
mkdir -p "$READY_DIR"
mkdir -p "$TEMP_DIR"

#
# ==================================================
# Maximum buffer segments
# ==================================================
#

MAX_SEGMENTS=$((BUFFER_SECONDS / SEGMENT_SECONDS + 10))

echo "RTSP: configured"
echo "Buffer: ${BUFFER_SECONDS}s"
echo "Segment: ${SEGMENT_SECONDS}s"
echo "Maximum segments: ${MAX_SEGMENTS}"

echo "Buffer directory: $BUFFER_DIR"
echo "Event directory: $EVENT_DIR"
echo "Recording directory: $RECORD_DIR"
echo "Ready directory: $READY_DIR"
echo "Temporary directory: $TEMP_DIR"

#
# ==================================================
# Ring buffer cleanup
# ==================================================
#

cleanup_buffer() {

    echo "Buffer cleanup process started."

    while true; do

        python3 - "$BUFFER_DIR" "$MAX_SEGMENTS" <<'PY'

import sys
from pathlib import Path

buffer_dir = Path(sys.argv[1])
max_segments = int(sys.argv[2])

files = sorted(
    buffer_dir.glob("segment_*.ts"),
    key=lambda p: p.name
)

count = len(files)

if count > max_segments:

    delete_count = count - max_segments

    for path in files[:delete_count]:

        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            print(
                f"Unable to delete buffer segment "
                f"{path}: {e}",
                flush=True
            )

PY

        sleep 2

    done
}

cleanup_buffer &

#
# ==================================================
# Event processor
# ==================================================
#

python3 /event_processor.py &

#
# ==================================================
# FFmpeg
# ==================================================
#

echo "Starting FFmpeg..."

exec ffmpeg \
    -hide_banner \
    -loglevel error \
    -rtsp_transport tcp \
    -i "$RTSP_URL" \
    -an \
    -c:v copy \
    -f segment \
    -segment_time "$SEGMENT_SECONDS" \
    -reset_timestamps 1 \
    -strftime 1 \
    -segment_format mpegts \
    "$BUFFER_DIR/segment_%Y%m%d_%H%M%S.ts"
