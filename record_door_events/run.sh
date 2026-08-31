#!/bin/bash

echo "======================================"
echo " Record door events"
echo "======================================"

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

if [ -z "$RTSP_URL" ]; then
    echo "ERROR: rtsp_url is empty!"
    exit 1
fi

BUFFER_DIR="/media/record-door-events/buffer"
EVENT_DIR="/media/record-door-events/events"
RECORD_DIR="/media/record-door-events/recordings"
READY_DIR="/media/record-door-events/ready"

mkdir -p "$BUFFER_DIR"
mkdir -p "$EVENT_DIR"
mkdir -p "$RECORD_DIR"
mkdir -p "$READY_DIR"

MAX_SEGMENTS=$((BUFFER_SECONDS / SEGMENT_SECONDS + 10))

echo "RTSP: configured"
echo "Buffer: ${BUFFER_SECONDS}s"
echo "Segment: ${SEGMENT_SECONDS}s"
echo "Maximum segments: ${MAX_SEGMENTS}"

echo "Buffer directory: $BUFFER_DIR"
echo "Event directory: $EVENT_DIR"
echo "Recording directory: $RECORD_DIR"
echo "Ready directory: $READY_DIR"


# ==================================================
# Ring buffer cleanup
# ==================================================

cleanup() {

    echo "Cleanup process started."

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

PY

        sleep 2

    done
}

cleanup &


# ==================================================
# Event processor
# ==================================================

python3 /event_processor.py &


# ==================================================
# FFmpeg
# ==================================================

while true; do

    echo "Starting FFmpeg..."

    # Remove incomplete zero-byte segments left by a previous FFmpeg run
    find "$BUFFER_DIR" -name "segment_*.ts" -type f -size 0 -delete

    ffmpeg \
        -hide_banner \
        -loglevel error \
        -rtsp_transport tcp \
        -i "$RTSP_URL" \
        -an \
        -c:v copy \
        -f segment \
        -segment_time "$SEGMENT_SECONDS" \
        -strftime 1 \
        -segment_format mpegts \
        "$BUFFER_DIR/segment_%Y%m%d_%H%M%S.ts"

    EXIT_CODE=$?

    echo "FFmpeg stopped. Exit code: $EXIT_CODE"
    echo "Restarting FFmpeg in 3 seconds..."

    sleep 3

done
