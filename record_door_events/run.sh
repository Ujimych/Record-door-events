#!/bin/bash

echo "======================================"
echo " Record door events"
echo "======================================"

OPTIONS="/data/options.json"
RTSP_URL="$(python3 -c "import json; print(json.load(open('$OPTIONS'))['rtsp_url'])")"
RTSP_TRANSPORT="$(python3 -c "import json; print(json.load(open('$OPTIONS')).get('rtsp_transport', 'tcp'))")"
BUFFER_SECONDS="$(python3 -c "import json; print(json.load(open('$OPTIONS'))['buffer_seconds'])")"
SEGMENT_SECONDS="$(python3 -c "import json; print(json.load(open('$OPTIONS'))['segment_seconds'])")"
WATCHDOG_TIMEOUT="$(python3 -c "import json; print(json.load(open('$OPTIONS')).get('watchdog_timeout', 20))")"

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
STARTUP_GRACE=30

echo "RTSP: configured"
echo "RTSP transport: $RTSP_TRANSPORT"
echo "Buffer: ${BUFFER_SECONDS}s"
echo "Segment: ${SEGMENT_SECONDS}s"
echo "Maximum segments: $MAX_SEGMENTS"
echo "Watchdog timeout: ${WATCHDOG_TIMEOUT}s"

cleanup() {
    echo "Cleanup process started."
    while true; do
        python3 - "$BUFFER_DIR" "$MAX_SEGMENTS" <<'PY'
import sys
from pathlib import Path

directory = Path(sys.argv[1])
limit = int(sys.argv[2])
files = sorted(directory.glob("segment_*.ts"), key=lambda p: p.name)

for path in files[:-limit]:
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
        -rtsp_transport "$RTSP_TRANSPORT" \
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

        NEWEST_SEGMENT=$(python3 - "$BUFFER_DIR" <<'PY'
import sys
from pathlib import Path

newest = None
newest_time = 0

for path in Path(sys.argv[1]).glob("segment_*.ts"):
    try:
        if path.stat().st_size <= 0:
            continue
        mtime = path.stat().st_mtime
        if mtime > newest_time:
            newest = path
            newest_time = mtime
    except FileNotFoundError:
        continue

if newest:
    print(f"{newest_time} {newest}")
PY
)

        NOW=$(date +%s)
        UPTIME=$((NOW - START_TIME))

        if [ -z "$NEWEST_SEGMENT" ]; then
            if [ "$UPTIME" -gt "$STARTUP_GRACE" ]; then
                echo "WATCHDOG: no segments after ${UPTIME}s. Restarting FFmpeg..."
                kill "$FFMPEG_PID" 2>/dev/null
                sleep 2

                if kill -0 "$FFMPEG_PID" 2>/dev/null; then
                    kill -9 "$FFMPEG_PID" 2>/dev/null
                fi

                break
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
