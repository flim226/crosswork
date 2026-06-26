# Application Note: LCM Recommendation Event Listener

## Overview

This application note describes `lcm_notify.py`, a single-file Python script that subscribes to and listens for **LCM Recommendation Events** from **Cisco Crosswork Network Controller (CNC)**. The script authenticates via CNC SSO, creates a RESTCONF notification stream for LCM recommendation events, and prints each notification as it arrives. It is intended for operators and integrators who need real-time visibility into Link Capacity Management (LCM) recommendation activity—such as new recommendations, updates, or lifecycle changes—without polling the CNC REST API.

The script runs indefinitely until the user presses **Ctrl+C**, and automatically reconnects if the notification stream is closed by the server.

## Background: LCM Recommendation Notifications

Crosswork Optimization Engine (COE) publishes **LCM Recommendation Events** when recommendation-related state changes occur in the network. These notifications are defined in the YANG model `cisco-crosswork-optimization-engine-lcm-recommendation-operations` and use the common grouping `lcm-rec-notification`.

CNC exposes notification delivery through **RESTCONF notification streams**. The mechanism differs slightly between CNC releases:

| CNC Release | API Prefix | Subscription Model |
|-------------|------------|-------------------|
| **7.2 and later** | `/crosswork/nbi/optimization/v3/restconf` | POST `sal-remote:create-notification-stream`, then listen on `/streams/json/{uuid}` (Server-Sent Events) |
| **7.0 and earlier** | `/crosswork/nbi/optima/v2/restconf` | GET stream enable/subscribe endpoints, then listen on `/notif/notification-stream/...` |

`lcm_notify.py` supports both models. By default (`--api auto`), it prefers the **optimization v3** API used on CNC 7.2+ and falls back to the legacy **optima v2** API if stream creation fails.

For API details, see:

- [Crosswork Optimization Engine RESTCONF Notifications (7.2+)](https://developer.cisco.com/docs/crosswork/network-controller/crosswork-optimization-engine-restconf-notifications/)
- [Crosswork Optimization Engine RESTCONF Notifications (7.0 and earlier)](https://developer.cisco.com/docs/crosswork/network-controller/7-0/crosswork-optimization-engine-restconf-notifications/)

## Script Purpose

`lcm_notify.py`:

1. **Authenticates** to Crosswork Network Controller via SSO (TGT → JWT)
2. **Subscribes** to the `lcm-recommendation-event` notification type
3. **Listens** on a long-lived HTTP stream for incoming events
4. **Prints** each event to stdout as JSON (with a local timestamp envelope)
5. **Reconnects** automatically if the stream drops, until the user interrupts the process

Typical use cases:

- Monitor LCM recommendation activity during lab testing or customer PoCs
- Feed events into a downstream log aggregator or automation pipeline (`--output`)
- Debug LCM behaviour alongside CNC platform logs

## Script Structure

`lcm_notify.py` is a single self-contained script organised into named sections:

| Section | Contents |
|---------|----------|
| **Constants** | Ports, API paths, reconnect defaults |
| **Errors** | `CrossworkAuthError` |
| **HTTP helpers** | `response_text()`, `check_response()`, `response_json()` |
| **Configuration and authentication** | `ClientConfig`, `get_ticket()`, `get_token()`, `authenticate()` |
| **Event parsing** | `ParserMode`, `StreamingEventParser`, `format_notification()` |
| **Notification stream setup** | `NotificationStreamClient`, `StreamSession` |
| **Notification listener** | `NotificationListener`, `ListenOptions` |
| **CLI** | `build_parser()`, `resolve_api_mode()`, `listen_v3()`, `listen_legacy()`, `main()` |

## Script Architecture

### Authentication Flow

The authentication flow matches other CNC API scripts such as `get_plan.py`:

```
┌─────────────────────┐    ┌─────────────────────────────────┐
│    lcm_notify.py    │───▶│  CNC SSO Endpoint               │
│                     │    │  https://<CNC_HOST>:30603/      │
│                     │    │  crosswork/sso/v1/tickets       │
└─────────────────────┘    └─────────────────────────────────┘
         │                              │
         │  1. POST username/password   │
         │◀─────────────────────────────│
         │     Returns: TGT ticket      │
         │                              │
         │  2. POST TGT + service URL   │
         │     /crosswork/sso/v2/      │
         │     tickets/jwt              │
         │◀─────────────────────────────│
         │     Returns: JWT token       │
         ▼
```

Alternatively, a pre-obtained JWT may be supplied with `--jwt` to skip username/password authentication. The companion script `cw_get_jwt.py` can be used to obtain and save a JWT file.

### Notification Flow (CNC 7.2+ / Optimization v3)

```
┌─────────────────────┐    ┌──────────────────────────────────────────────┐
│    lcm_notify.py    │───▶│  CNC Optimization Engine API (v3)            │
│  (with JWT token)   │    │  /crosswork/nbi/optimization/v3/restconf/    │
└─────────────────────┘    └──────────────────────────────────────────────┘
         │                              │
         │  POST: create-notification-  │
         │  stream                      │
         │  - lcm-recommendation-event  │
         │  - output-type: JSON         │
         │◀─────────────────────────────│
         │  Returns: stream UUID        │
         │                              │
         │  GET: /streams/json/{uuid}   │
         │  Accept: text/event-stream   │
         │◀─────────────────────────────│
         │  SSE stream (events + pings) │
         ▼
    [stdout / --output file]
```

If the SSE connection closes, the script waits 3 seconds, creates a **new** notification stream, and resumes listening.

### Notification Flow (Legacy optima v2)

```
┌─────────────────────┐    ┌──────────────────────────────────────────────┐
│    lcm_notify.py    │───▶│  CNC Optimization Engine API (v2)            │
│  (with JWT token)   │    │  /crosswork/nbi/optima/v2/restconf/          │
└─────────────────────┘    └──────────────────────────────────────────────┘
         │                              │
         │  GET: restconf-state/streams │
         │◀─────────────────────────────│
         │                              │
         │  GET: stream location for    │
         │  lcm-recommendation-event    │
         │◀─────────────────────────────│
         │                              │
         │  GET: /notif/notification-   │
         │  stream/.../JSON             │
         │◀─────────────────────────────│
         │  Chunked JSON notifications  │
         ▼
    [stdout / --output file]
```

### Runtime Flow

```
┌──────────────┐
│   main()     │
└──────┬───────┘
       │
       ├─▶ authenticate() / load JWT from file
       │
       ├─▶ NotificationStreamClient
       │      ├─ create_v3_stream()
       │      ├─ v3_listen_session()
       │      └─ v2_listen_session()
       │
       └─▶ NotificationListener.listen()
              ├─ StreamingEventParser
              └─ on_reconnect (re-subscribe on disconnect)
```

## Command Line Interface

The CLI follows the same conventions as `get_plan.py` for CNC connectivity and authentication.

### Usage

```bash
python lcm_notify.py --ip <CNC_HOST> [options]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `--ip` | Crosswork Network Controller IP address or hostname *(required)* |
| `--port` | Crosswork HTTPS port (default: `30603`) |
| `--username`, `-u` | CNC username (default: `admin`) |
| `--password`, `-p` | CNC password (default: `admin`) |
| `--jwt`, `-j` | Path to a JWT file; skips username/password authentication |
| `--timeout` | HTTP timeout in seconds for auth and setup requests (default: `30`) |
| `--api` | Notification API variant: `auto`, `v3`, or `legacy` (default: `auto`) |
| `--stream-id` | Existing optimization v3 stream UUID; skips initial stream creation |
| `--pretty` | Pretty-print each notification as indented JSON |
| `--output`, `-o` | Append received notifications to this file (one JSON object per line) |
| `--max-events` | Stop after receiving this many events (default: listen until interrupted) |

> **Note**: Status messages (authentication progress, stream identifiers, reconnect notices) are written to **stderr**. Notification payloads are written to **stdout**, making it straightforward to pipe or redirect output.

> **Security Note**: Avoid passing real passwords on the command line in shared or production environments (credentials may be visible in process listings). Prefer `--jwt` with a file obtained from `cw_get_jwt.py`.

### Example Usage

```bash
# Basic usage — listen until Ctrl+C
python lcm_notify.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>'

# Use a saved JWT (from cw_get_jwt.py)
python lcm_notify.py --ip <CNC_HOST> -j <JWT_FILE>

# Pretty-print and save events to a file
python lcm_notify.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' --pretty -o lcm_events.jsonl

# Capture only the first N events (useful for testing)
python lcm_notify.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' --max-events 5

# Force legacy optima v2 API (CNC 7.0 and earlier)
python lcm_notify.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' --api legacy

# Reattach to an existing v3 stream
python lcm_notify.py --ip <CNC_HOST> -j <JWT_FILE> \
  --stream-id 'urn:uuid:<STREAM_UUID>'
```

### Example Session Output

```
Authenticating to <CNC_HOST>...
Creating optimization v3 notification stream...
Stream identifier: urn:uuid:<STREAM_UUID>
Press Ctrl+C to stop.
Listening on https://<CNC_HOST>:30603/crosswork/nbi/optimization/v3/restconf/streams/json/urn:uuid:<STREAM_UUID>
{"received-at":"<ISO8601_TIMESTAMP>","notification":{...}}
^C
Received signal 2, stopping...
Received 1 event(s).
```

When no LCM activity is occurring, the v3 SSE stream still stays open. The server sends periodic `: ping` keepalives; these are not printed as events.

## Output Format

Each received notification is wrapped in a JSON envelope:

```json
{
  "received-at": "<ISO8601_TIMESTAMP>",
  "notification": {
    "ietf-restconf:notification": {
      "event-time": "<ISO8601_TIMESTAMP>",
      "cisco-crosswork-optimization-engine-lcm-recommendation-operations:lcm-recommendation-event": {
        "timestamp": "<ISO8601_TIMESTAMP>",
        "urgency": "low",
        "recommendation-id": "<RECOMMENDATION_UUID>",
        "domain-id": "<DOMAIN_ID>"
      }
    }
  }
}
```

- `received-at` — UTC timestamp when the script received the event locally
- `notification` — the raw notification payload from CNC

With `--output`, each envelope is appended as a single line of JSON (JSONL format), regardless of `--pretty`.

## Key Functions and Classes

### `ClientConfig`

Frozen dataclass holding connection settings: `base_url`, `verify_ssl`, and `timeout`.

### `authenticate(config, username, password)`

Performs two-step SSO authentication:

1. Obtains TGT (Ticket Granting Ticket) from `/crosswork/sso/v1/tickets`
2. Exchanges TGT for JWT via `/crosswork/sso/v2/tickets/jwt`

### `NotificationStreamClient`

Encapsulates RESTCONF stream setup for both API variants:

| Method | Purpose |
|--------|---------|
| `create_v3_stream()` | POST `sal-remote:create-notification-stream` for LCM events |
| `v3_listen_session(stream_id)` | Build SSE listen URL and headers |
| `setup_v2_stream()` | Legacy enable/subscribe GET sequence |
| `v2_listen_session()` | Build legacy listen URL and headers |

### `StreamSession`

Frozen dataclass with `url` and `headers` for an active listen session.

### `StreamingEventParser`

Incrementally parses notification payloads from a chunked HTTP response body:

- **`ParserMode.SSE`** — parses Server-Sent Events (`data:` lines), ignores `: ping` comments
- **`ParserMode.JSON`** — parses concatenated JSON objects (legacy v2 streams)

### `NotificationListener`

Manages the long-running listen loop:

- Opens a streaming HTTP GET with no read timeout
- Emits formatted events to stdout and optional output file
- Handles SIGINT/SIGTERM via `request_stop()`
- Reconnects after stream errors or server-side disconnects when an `on_reconnect` callback is provided

### `main()`

Orchestrates the workflow:

1. Parses command-line arguments
2. Authenticates (or loads JWT from file)
3. Resolves API mode via `resolve_api_mode()` (`auto`, `v3`, or `legacy`)
4. Creates or reuses a notification stream
5. Delegates to `listen_v3()` or `listen_legacy()`
6. Prints total event count on exit

## API Endpoints Used

### Optimization v3 (CNC 7.2+)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/crosswork/sso/v1/tickets` | POST | Obtain TGT |
| `/crosswork/sso/v2/tickets/jwt` | POST | Exchange TGT for JWT |
| `/crosswork/nbi/optimization/v3/restconf/operations/sal-remote:create-notification-stream` | POST | Create notification stream |
| `/crosswork/nbi/optimization/v3/restconf/streams/json/{uuid}` | GET | Listen for SSE notifications |

**Stream creation payload:**

```json
{
  "input": {
    "notifications": [
      "(urn:com:cisco:crosswork:optimization-engine:lcm-recommendation:operations?revision=2021-05-06)lcm-recommendation-event"
    ],
    "notification-output-type": "JSON"
  }
}
```

### Legacy optima v2 (CNC 7.0 and earlier)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/crosswork/sso/v1/tickets` | POST | Obtain TGT |
| `/crosswork/sso/v2/tickets/jwt` | POST | Exchange TGT for JWT |
| `/crosswork/nbi/optima/v2/restconf/data/ietf-restconf-monitoring:restconf-state/streams` | GET | Enable notification streams |
| `/crosswork/nbi/optima/v2/restconf/data/ietf-restconf-monitoring:restconf-state/streams/stream=lcm-recommendation-event/access=JSON/location` | GET | Subscribe to LCM stream |
| `/crosswork/nbi/optima/v2/restconf/notif/notification-stream/cisco-crosswork-optimization-engine-lcm-recommendation-operations:lcm-recommendation-event/JSON` | GET | Listen for notifications |

## Configuration Constants

Defaults are defined at the top of `lcm_notify.py`:

```python
DEFAULT_PORT = 30603
DEFAULT_AUTH_TIMEOUT = 30
DEFAULT_RECONNECT_DELAY = 3
VERIFY_SSL = False
CHUNK_SIZE = 4096
```

Port and timeout may be overridden via `--port` and `--timeout` on the command line.

## Dependencies

- **Python packages**: `requests`, `urllib3`
- **Standard library**: `argparse`, `json`, `signal`, `time`, `datetime`, `dataclasses`, `enum`, `typing`
- **External tools**: None

Install dependencies:

```bash
pip install requests urllib3
```

## Error Handling

The script handles common error scenarios:

- **HTTP errors**: Authentication failures, stream creation failures, listen endpoint errors — reported with HTTP status and response body snippet
- **Connection errors**: Network unreachability, stream disconnects — triggers automatic reconnection (v3 and legacy modes)
- **JSON parse errors**: Malformed notification payloads raise `json.JSONDecodeError` and terminate the script
- **Signal handling**: SIGINT (Ctrl+C) and SIGTERM set a stop flag on `NotificationListener`; the script exits cleanly after the current read completes
- **API fallback**: In `--api auto` mode, failure to create a v3 stream falls back to legacy v2 with a stderr warning

## Troubleshooting

### Script exits immediately with HTTP 404 on listen (v2 path)

The target CNC likely requires the **optimization v3** API. Use `--api v3` or leave `--api` at the default `auto` value.

```
Error: listen_notification_stream returned HTTP 404 Not Found: ...
```

### Script exits immediately with HTTP 409 on streams (v2 enable)

A `data-missing` (HTTP 409) response on the v2 streams endpoint indicates the legacy monitoring model is not populated on this CNC release. The v3 API should be used instead.

### No events received, but script keeps running

This is expected when no LCM recommendation activity is occurring. The v3 SSE stream sends `: ping` keepalives to maintain the connection. Trigger an LCM event on the controller (for example, a threshold crossing or recommendation generation) to verify end-to-end delivery.

### JWT expires during a long listen session

JWT tokens have a finite lifetime. If the stream fails with authentication errors after extended runtime, obtain a fresh JWT with `cw_get_jwt.py` and restart the script.

### Reconnection loop

If the server repeatedly closes the stream, stderr will show:

```
Stream closed by server.
Reconnecting in 3 second(s)...
Creating new optimization v3 notification stream...
```

This is normal behaviour. Investigate CNC platform health and optima-lcm service logs if reconnections are frequent.

## Limitations and Considerations

1. **Long-running process**: The script is designed to run as a foreground listener, not as a daemon. Use `systemd`, `screen`, or `tmux` for persistent background operation.
2. **JWT lifetime**: The script does not refresh JWT tokens automatically during a long listen session.
3. **Single notification type**: Only `lcm-recommendation-event` is subscribed. Other COE notification types (SR policy, topology, RSVP) require separate stream creation with different notification URIs.
4. **SSL verification**: The script disables SSL verification (`verify=False`) for self-signed certificates, consistent with `get_plan.py`.
5. **AAA session limits**: Use a dedicated API user for automated listeners to avoid exhausting shared AAA session limits on the controller.
6. **Event volume**: High LCM activity can produce a large volume of notifications. Use `--output` with log rotation in production integrations.

## Relationship to Other Scripts

| Script | Relationship |
|--------|--------------|
| `get_plan.py` | Shares the same SSO authentication flow and CLI conventions (`--ip`, `-u`, `-p`, `-j`) |
| `cw_get_jwt.py` | Can be used to obtain a JWT file for `--jwt` authentication |

## References

- [Crosswork Optimization Engine RESTCONF Notifications (7.2+)](https://developer.cisco.com/docs/crosswork/network-controller/crosswork-optimization-engine-restconf-notifications/)
- [Crosswork Optimization Engine RESTCONF Notifications (7.0 and earlier)](https://developer.cisco.com/docs/crosswork/network-controller/7-0/crosswork-optimization-engine-restconf-notifications/)
- [Retrieve LCM recommendation policies](https://developer.cisco.com/docs/crosswork/network-controller/retrieve-lcm-recommendation-policies/)
- [Cisco Crosswork Network Controller API Documentation](https://developer.cisco.com/docs/crosswork/)

---

*Document Version: 2.1*  
*Script: lcm_notify.py*  
*Platform: Cisco Crosswork Network Controller 7.2+ (with legacy 7.0 v2 fallback)*
