# Record Door Events

Home Assistant App for continuously recording an RTSP camera stream, maintaining a rolling video buffer, and creating video clips when a door event occurs.

The application is designed to work with Home Assistant automations. When an event occurs, it creates a video containing footage before and after the event and publishes the resulting file path through MQTT.

Telegram delivery is handled by Home Assistant automation.

## Features

- Continuous recording of an RTSP camera stream
- Rolling video buffer
- Configurable buffer duration
- Configurable video segment duration
- Configurable pre-event recording
- Configurable post-event recording
- Automatic event video creation
- MQTT notification when a video is ready
- Automatic cleanup of old videos
- Supports `amd64` and `aarch64`
- Runs as a Home Assistant App
- Telegram integration through Home Assistant automation

## How it works

The application continuously records the configured RTSP stream into short MPEG-TS segments.

The segments are stored in a rolling buffer. Old segments are automatically removed when the configured buffer duration is exceeded.

When Home Assistant detects a door event, it creates an event marker:

```text
/media/record-door-events/events/event_<timestamp>.evt
```

The application detects the event marker and waits for the configured post-event period.

It then creates a video containing:

- footage before the event
- the event itself
- footage after the event

The resulting MP4 file is placed in:

```text
/media/record-door-events/ready/
```

The application publishes the path of the generated video through MQTT:

```text
record_door_events/video_ready
```

Home Assistant can then use this message to send the video to Telegram or another service.

## Requirements

- Home Assistant
- Home Assistant App support
- An RTSP-compatible camera
- MQTT broker available in Home Assistant

Supported architectures:

- `amd64`
- `aarch64`

## Installation

Add the GitHub repository as a Home Assistant App repository.

Repository:

https://github.com/Ujimych/Record-door-events

After adding the repository, install **Record Door Events** from the Home Assistant App store.

## Configuration

The application is configured from the Home Assistant App configuration page.

Example:

```yaml
rtsp_url: "rtsp://username:password@192.168.1.100:554/stream"

buffer_seconds: 60
segment_seconds: 1

pre_event_seconds: 5
post_event_seconds: 10

ready_retention_days: 3

mqtt_host: "core-mosquitto"
mqtt_port: 1883
mqtt_username: ""
mqtt_password: ""
```

### Configuration options

| Option | Description | Default |
|---|---|---:|
| `rtsp_url` | RTSP URL of the camera stream | — |
| `buffer_seconds` | Duration of the rolling video buffer in seconds | `60` |
| `segment_seconds` | Duration of each video segment in seconds | `1` |
| `pre_event_seconds` | Number of seconds recorded before the event | `5` |
| `post_event_seconds` | Number of seconds recorded after the event | `10` |
| `ready_retention_days` | Number of days to keep generated videos in `ready/` | `3` |
| `mqtt_host` | MQTT broker hostname | `core-mosquitto` |
| `mqtt_port` | MQTT broker port | `1883` |
| `mqtt_username` | MQTT username | — |
| `mqtt_password` | MQTT password | — |

### Parameter limits

| Option | Allowed range |
|---|---:|
| `buffer_seconds` | `30–300` |
| `segment_seconds` | `1–2` |
| `pre_event_seconds` | `0–60` |
| `post_event_seconds` | `1–120` |
| `ready_retention_days` | `1–30` |

## Home Assistant automation

The application does not detect the door state itself.

Home Assistant is responsible for detecting the event and creating an event marker.

Add the following to `configuration.yaml`:

```yaml
shell_command:
  record_door_events: >-
    touch "/media/record-door-events/events/event_{{ now().timestamp() }}.evt"

  record_door_events_delete_video: >-
    rm -f "{{ filename }}"
```

The `record_door_events_delete_video` command is optional and can be used by Home Assistant to manually delete a generated video.

Example automation:

```yaml
- id: security_door_recorder_event_open_door
  alias: "Security. Door Recorder — door opened"
  description: "Start recording when the door is opened"
  mode: parallel
  max: 20

  triggers:
    - trigger: state
      entity_id: binary_sensor.0x00158d0004245710_contact
      from: "off"
      to: "on"

  actions:
    - action: shell_command.record_door_events
```

Replace the `entity_id` with the door sensor used in your Home Assistant installation.

## MQTT

When a video has been successfully created, the application publishes an MQTT message to:

```text
record_door_events/video_ready
```

The MQTT payload contains the full path to the generated video.

Example payload:

```text
/media/record-door-events/ready/event_20260823_120000_a1b2c3.mp4
```

## Telegram

Telegram delivery is intentionally handled by Home Assistant rather than by the application.

This keeps the application independent from Telegram and allows Home Assistant to control the destination, caption, and delivery logic.

Example automation:

```yaml
- id: security_door_recorder_send_video_telegram
  alias: "Security. Door Recorder — send video to Telegram"

  triggers:
    - trigger: mqtt
      topic: record_door_events/video_ready

  actions:
    - action: telegram_bot.send_video
      data:
        chat_id:
          - 0123456
        file: "{{ trigger.payload }}"
        caption: >-
          🚪 Door opened
          {{ now().strftime('%d.%m.%Y %H:%M:%S') }}

    - action: shell_command.record_door_events_delete_video
      data:
        filename: "{{ trigger.payload }}"

  mode: queued
  max: 20
```

Replace the `chat_id` with the appropriate Telegram chat ID.

## Storage

The application uses the following directories:

```text
/media/record-door-events/
├── buffer/
├── events/
├── recordings/
├── ready/
└── tmp/
```

### `buffer/`

Contains the rolling RTSP video buffer.

Old segments are automatically removed according to the configured buffer duration.

### `events/`

Contains event marker files created by Home Assistant.

### `recordings/`

Temporary storage for newly generated event videos before they are moved to `ready/`.

### `ready/`

Contains generated videos that are ready for processing by Home Assistant.

Videos are published through MQTT when they become available.

After successful processing, Home Assistant can delete the video using the `record_door_events_delete_video` shell command.

Files that remain in `ready/` are automatically deleted after `ready_retention_days`.

The default retention period is:

```text
3 days
```

### `tmp/`

Temporary files used while creating event videos.

Temporary files are removed after processing.

## Video processing

The application uses FFmpeg to:

1. record the RTSP stream;
2. maintain the rolling buffer;
3. combine the required segments;
4. create the final MP4 event video;
5. validate the generated video.

The generated video uses H.264 video encoding and MP4 container format.

## Versioning

The application version is defined in one place:

```yaml
version: "0.7.5"
```

in `config.yaml`.

When releasing a new version, update this value and commit the change.

GitHub Actions automatically builds and publishes the corresponding container images.

## Project structure

```text
record_door_events/
├── config.yaml
├── Dockerfile
├── event_processor.py
├── run.sh
└── icon.png
```

### `config.yaml`

Home Assistant App configuration and application version.

### `Dockerfile`

Defines the application container and installs the required software.

### `run.sh`

Starts the RTSP recording process and maintains the rolling buffer.

### `event_processor.py`

Processes event markers, creates event videos, validates generated files, manages retention, and publishes MQTT notifications.

### `icon.png`

Application icon used by Home Assistant.

## Building

Docker images are built automatically using GitHub Actions.

Supported architectures:

```text
amd64
aarch64
```

Images are published to GitHub Container Registry (GHCR).

## License

This project is released under the MIT License.

See [LICENSE](LICENSE) for details.

## Repository

https://github.com/Ujimych/Record-door-events
