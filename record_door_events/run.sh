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
READY_DIR="/media/record-door-events/ready"

mkdir -p "$BUFFER_DIR" "$EVENT_DIR" "$READY_DIR"

BUFFER_RETENTION_SECONDS=$((BUFFER_SECONDS + 10))
STARTUP_GRACE=30

echo "RTSP: configured"
echo "RTSP transport: $RTSP_TRANSPORT"
echo "Buffer: ${BUFFER_SECONDS}s"
echo "Segment: ${SEGMENT_SECONDS}s (actual split follows camera keyframes)"
echo "Buffer retention: ${BUFFER_RETENTION_SECONDS}s"
echo "Watchdog timeout: ${WATCHDOG_TIMEOUT}s"

cleanup() {
    echo "Cleanup process started."
    while true; do
        python3 - "$BUFFER_DIR" "$BUFFER_RETENTION_SECONDS" <<'PY'
import sys
import time
from datetime import datetime
from pathlib import Path

directory = Path(sys.argv[1])
retention = float(sys.argv[2])
cutoff = time.time() - retention
files = sorted(directory.glob("segment_*.ts"), key=lambda p: p.name)

# The newest segment may still be written by FFmpeg; never remove it.
for path in files[:-1]:
    try:
        timestamp = datetime.strptime(
            path.stem.replace("segment_", ""), "%Y%m%d_%H%M%S"
        ).timestamp()
    except ValueError:
        try:
            timestamp = path.stat().st_mtime
        except FileNotFoundError:
            continue

    if timestamp >= cutoff:
        continue

    try:
        path.unlink()
        print(f"Removed old buffer segment: {path.name}")
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

    FFmpeg_START_TIME=$(date +%s)

    ffmpeg \
        -hide_banner \
        -loglevel warning \
        -rtsp_transport "$RTSP_TRANSPORT" \
        -i "$RTSP_URL" \
        -an \
        -c:v copy \
        -f segment \
        -segment_time "$SEGMENT_SECONDS" \
        -reset_timestamps 1 \
        -strftime 1 \
        -segment_format mpegts \
        "$BUFFER_DIR/segment_%Y%m%d_%H%M%S.ts" &

    FFMPEG_PID=$!

    echo "FFmpeg started. PID: $FFMPEG_PID"

    while kill -0 "$FFMPEG_PID" 2>/dev/null; do
        sleep 5

        NOW=$(date +%s)
        UPTIME=$((NOW - FFmpeg_START_TIME))

        NEWEST_SEGMENT=$(python3 - "$BUFFER_DIR" "$FFmpeg_START_TIME" <<'PY'
import sys
from pathlib import Path

directory = Path(sys.argv[1])
start_time = float(sys.argv[2])

newest = None
newest_time = 0

for path in directory.glob("segment_*.ts"):
    try:
        stat = path.stat()

        if stat.st_size <= 0:
            continue

        if stat.st_mtime < start_time:
            continue

        if stat.st_mtime > newest_time:
            newest = path
            newest_time = stat.st_mtime

    except FileNotFoundError:
        continue

if newest:
    print(f"{newest_time} {newest}")
PY
)

        if [ -z "$NEWEST_SEGMENT" ]; then
            if [ "$UPTIME" -gt "$STARTUP_GRACE" ]; then
                echo "WATCHDOG: no new segments after ${UPTIME}s. Restarting FFmpeg..."

                kill "$FFMPEG_PID" 2>/dev/null
                sleep 2

                if kill -0 "$FFMPEG_PID" 2>/dev/null; then
                    echo "WATCHDOG: FFmpeg did not stop, killing..."
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
            echo "WATCHDOG: newest new segment is ${AGE}s old. Restarting FFmpeg..."
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
