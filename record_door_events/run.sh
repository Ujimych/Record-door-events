#!/bin/bash

echo "======================================"
echo " Record door events"
echo "======================================"

RTSP_URL="$(python3 -c "import json; print(json.load(open('/data/options.json'))['rtsp_url'])")"
BUFFER_SECONDS="$(python3 -c "import json; print(json.load(open('/data/options.json'))['buffer_seconds'])")"
SEGMENT_SECONDS="$(python3 -c "import json; print(json.load(open('/data/options.json'))['segment_seconds'])")"

if [ -z "$RTSP_URL" ]; then
    echo "ERROR: rtsp_url is empty!"
    exit 1
fi

BUFFER_DIR="/media/record-door-events/buffer"
EVENT_DIR="/media/record-door-events/events"
RECORD_DIR="/media/record-door-events/recordings"
READY_DIR="/media/record-door-events/ready"

mkdir -p "$BUFFER_DIR" "$EVENT_DIR" "$RECORD_DIR" "$READY_DIR"

MAX_SEGMENTS=$((BUFFER_SECONDS / SEGMENT_SECONDS + 10))
WATCHDOG_TIMEOUT=20
STARTUP_GRACE=30

echo "RTSP: configured"
echo "Buffer: ${BUFFER_SECONDS}s"
echo "Segment: ${SEGMENT_SECONDS}s"
echo "Maximum segments: ${MAX_SEGMENTS}"
echo "Watchdog timeout: ${WATCHDOG_TIMEOUT}s"

cleanup() {
    echo "Cleanup process started."
    while true; do
        python3 - "$BUFFER_DIR" "$MAX_SEGMENTS" <<'PY'
import sys
from pathlib import Path

buffer_dir = Path(sys.argv[1])
max_segments = int(sys.argv[2])
files = sorted(buffer_dir.glob("segment_*.ts"), key=lambda p: p.name)

if len(files) > max_segments:
    for path in files[:len(files) - max_segments]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
PY
        sleep 2
    done
}

cleanup &

python3 /event_processor.py &

while true; do
    echo "Starting FFmpeg..."

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
        "$BUFFER_DIR/segment_%Y%m%d_%H%M%S.ts" &

    FFMPEG_PID=$!
    START_TIME=$(date +%s)

    echo "FFmpeg started. PID: $FFMPEG_PID"

    while kill -0 "$FFMPEG_PID" 2>/dev/null; do
        sleep 5

        NOW=$(date +%s)
        UPTIME=$((NOW - START_TIME))

        NEWEST_SEGMENT=$(find "$BUFFER_DIR" -name "segment_*.ts" -type f -size +0c -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1)

        if [ -z "$NEWEST_SEGMENT" ]; then
            if [ "$UPTIME" -gt "$STARTUP_GRACE" ]; then
                echo "WATCHDOG: no segments after ${UPTIME}s. Restarting FFmpeg..."
                kill "$FFMPEG_PID" 2>/dev/null
                sleep 2

                if kill -0 "$FFMPEG_PID" 2>/dev/null; then
                    kill -9 "$FFMPEG_PID" 2>/dev/null
                fi
            fi
            continue
        fi

        LAST_UPDATE="${NEWEST_SEGMENT%% *}"
        AGE=$(python3 - "$LAST_UPDATE" <<'PY'
import sys
import time
print(int(time.time() - float(sys.argv[1])))
PY
)

        if [ "$AGE" -gt "$WATCHDOG_TIMEOUT" ]; then
            echo "WATCHDOG: newest segment is ${AGE}s old. Restarting FFmpeg..."
            echo "WATCHDOG: $NEWEST_SEGMENT"

            kill "$FFMPEG_PID" 2>/dev/null
            sleep 2

            if kill -0 "$FFMPEG_PID" 2>/dev/null; then
                echo "WATCHDOG: FFmpeg did not stop, killing..."
                kill -9 "$FFMPEG_PID" 2>/dev/null
            fi

            break
        fi
    done

    wait "$FFMPEG_PID" 2>/dev/null
    EXIT_CODE=$?

    echo "FFmpeg stopped. Exit code: $EXIT_CODE"
    echo "Restarting FFmpeg in 3 seconds..."

    sleep 3
done
