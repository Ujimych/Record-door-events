# Record Door Events

Home Assistant App for continuously recording an RTSP camera stream, maintaining a rolling video buffer, and creating event clips when a door event occurs.

The application is designed to work with Home Assistant automations and can publish the path of a generated video through MQTT. The video can then be sent to Telegram or any other service supported by Home Assistant.

## Features

- Continuous recording of an RTSP camera stream
- Rolling video buffer
- Configurable buffer duration
- Configurable video segment duration
- Capture video before an event
- Capture video after an event
- Automatic event video creation
- MQTT notification when a video is ready
- Automatic cleanup of old files in the `ready` directory
- Supports `amd64` and `aarch64`
- Designed to run as a Home Assistant App
- Telegram integration is handled by Home Assistant automation

## How it works

The application continuously records the configured RTSP stream into short MPEG-TS segments.

The segments are stored in a rolling buffer. Old segments are automatically removed when the configured buffer size is exceeded.

When Home Assistant detects a door event, it creates an event marker:

```text
/media/record-door-events/events/event_<timestamp>.evt
```

The application detects the event marker and creates a video containing:

- video before the event
- the event itself
- video after the event

The resulting MP4 file is placed in:

```text
/media/record-door-events/ready/
```

The application then publishes the file path through MQTT:

```text
record_door_events/video_ready
```

Home Assistant can use this MQTT message to send the video to Telegram or another service.

## Requirements

- Home Assistant
- Home Assistant App support
- An RTSP-compatible camera
- MQTT broker available in Home Assistant

The application currently supports:

- `amd64`
- `aarch64`

## Installation

Add the GitHub repository as a Home Assistant App repository.

Repository:

https://github.com/Ujimych/Record-door-events

After adding the repository, install **Record Door Events** from the Home Assistant App store.

## Configuration

The application can be configured from the Home Assistant App configuration page.

Example configuration:

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
| `rtsp_url` | RTSP camera stream URL | — |
| `buffer_seconds` | Duration of the rolling buffer | `60` |
| `segment_seconds` | Duration of each video segment | `1` |
| `pre_event_seconds` | Video captured before the event | `5` |
| `post_event_seconds` | Video captured after the event | `10` |
| `ready_retention_days` | Number of days to keep generated videos in `ready/` | `3` |
| `mqtt_host` | MQTT broker hostname | `core-mosquitto` |
| `mqtt_port` | MQTT broker port | `1883` |
| `mqtt_username` | MQTT username | — |
| `mqtt_password` | MQTT password | — |

## Home Assistant automation

The application does not detect the door state itself.

Home Assistant is responsible for detecting the event and creating an event marker.

Example `configuration.yaml`:

```yaml
shell_command:
  record_door_events: >-
    touch "/media/record-door-events/events/event_{{ now().timestamp() }}.evt"

  record_door_events_delete_video: >-
    rm -f "{{ filename }}"
```

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

## MQTT notification

When a video is successfully created, the application publishes its file path to:

```text
record_door_events/video_ready
```

The MQTT payload contains the full path to the generated video.

Example:

```text
/media/record-door-events/ready/event_20260823_120000_a1b2c3.mp4
```

## Telegram example

Telegram delivery is intentionally handled by Home Assistant rather than by the application.

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
          - 1254456
        file: "{{ trigger.payload }}"
        caption: >-
          🚪 Door opened
          {{ now().strftime('%d.%m.%Y %H:%M:%S') }}

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

Old segments are automatically removed.

### `events/`

Contains temporary event marker files created by Home Assistant.

### `recordings/`

Contains generated event videos.

### `ready/`

Contains generated videos waiting to be processed by Home Assistant.

Files older than the configured `ready_retention_days` are automatically deleted.

The default retention period is:

```text
3 days
```

### `tmp/`

Temporary files used while creating event videos.

Temporary files are removed after processing.

## Versioning

The application version is defined in one place only:

```yaml
version: "0.7.5"
```

in `config.yaml`.

When releasing a new version, update this value and commit the change.

GitHub Actions builds and publishes the corresponding container images automatically.

## Architecture

The application consists of four main files:

```text
record_door_events/
├── config.yaml
├── Dockerfile
├── event_processor.py
└── run.sh
```

### `config.yaml`

Home Assistant App configuration and application version.

### `Dockerfile`

Builds the application container and installs:

- FFmpeg
- Python
- Paho MQTT

### `run.sh`

Starts the RTSP recording process and maintains the rolling buffer.

### `event_processor.py`

Processes door events, creates event videos, performs validation, manages the `ready` directory, and publishes MQTT notifications.

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

GitHub:

https://github.com/Ujimych/Record-door-events
