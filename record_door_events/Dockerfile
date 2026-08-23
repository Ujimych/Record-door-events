FROM alpine:3.22

RUN apk add --no-cache \
    bash \
    ffmpeg \
    python3 \
    py3-pip \
    tzdata

RUN pip3 install \
    --no-cache-dir \
    --break-system-packages \
    paho-mqtt

COPY run.sh /run.sh
COPY event_processor.py /event_processor.py

RUN chmod 755 /run.sh
RUN chmod 755 /event_processor.py

ENTRYPOINT ["/run.sh"]
