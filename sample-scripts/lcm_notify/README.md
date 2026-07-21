# Application Note: LCM Recommendation Event Listener

## Overview

This application note describes `lcm_notify_v2.py`, a single-file Python script that subscribes to and listens for **LCM Recommendation Events** from **Cisco Crosswork Network Controller (CNC)**. The script authenticates via CNC SSO, creates a RESTCONF notification stream for LCM recommendation events, and prints each notification as it arrives. It is intended for operators and integrators who need real-time visibility into Link Capacity Management (LCM) recommendation activity—such as new recommendations, updates, or lifecycle changes—without polling the CNC REST API.

The script also supports optional **recommendation detail retrieval** (`--get-rec`), **verbose HTTP tracing** (`--verbose`), **environment-variable credentials**, and **secure-by-default SSL verification** (with an explicit opt-out via `-k`/`--insecure`). It runs indefinitely until the user presses **Ctrl+C**, and automatically reconnects if the notification stream is closed by the server.

## Background: LCM Recommendation Notifications

Crosswork Optimization Engine (COE) publishes **LCM Recommendation Events** when recommendation-related state changes occur in the network. These notifications are defined in the YANG model `cisco-crosswork-optimization-engine-lcm-recommendation-operations` and use the common grouping `lcm-rec-notification`.

CNC exposes notification delivery through **RESTCONF notification streams**. The mechanism differs slightly between CNC releases:

| CNC Release | API Prefix | Subscription Model |
|-------------|------------|-------------------|
| **7.2 and later** | `/crosswork/nbi/optimization/v3/restconf` | POST `sal-remote:create-notification-stream`, then listen on `/streams/json/{uuid}` (Server-Sent Events) |
| **7.0 and earlier** | `/crosswork/nbi/optima/v2/restconf` | GET stream enable/subscribe endpoints, then listen on `/notif/notification-stream/...` |

`lcm_notify_v2.py` supports both models. By default (`--api auto`), it prefers the **optimization v3** API used on CNC 7.2+ and falls back to the legacy **optima v2** API if stream creation fails.

When `--get-rec` is enabled, the script additionally calls LCM RESTCONF operations on the **optima v2** API to fetch full recommendation and MSL preview details. This is a deliberate cross-API design: notifications are delivered on optimization v3, while LCM retrieval RPCs remain on optima v2 (consistent with the Crosswork OE Postman collection).

For API details, see:

- [Crosswork Optimization Engine RESTCONF Notifications (7.2+)](https://developer.cisco.com/docs/crosswork/network-controller/crosswork-optimization-engine-restconf-notifications/)
- [Crosswork Optimization Engine RESTCONF Notifications (7.0 and earlier)](https://developer.cisco.com/docs/crosswork/network-controller/7-0/crosswork-optimization-engine-restconf-notifications/)
- [Retrieve LCM recommendation policies](https://developer.cisco.com/docs/crosswork/network-controller/retrieve-lcm-recommendation-policies/)

## Script Purpose

`lcm_notify_v2.py`:

1. **Authenticates** to Crosswork Network Controller via SSO (TGT → JWT)
2. **Subscribes** to the `lcm-recommendation-event` notification type
3. **Listens** on a long-lived HTTP stream for incoming events
4. **Prints** each event to stdout as JSON (with a local timestamp envelope)
5. **Optionally fetches** full recommendation and MSL preview details for each event (`--get-rec`)
6. **Reconnects** automatically if the stream drops, until the user interrupts the process

Typical use cases:

- Monitor LCM recommendation activity during lab testing or customer PoCs
- Feed events into a downstream log aggregator or automation pipeline (`--output`)
- Debug LCM behaviour alongside CNC platform logs
- Capture complete recommendation context (solutions, TTE policy previews) in a single event envelope (`--get-rec`)
- Trace RESTCONF request/response flow during integration troubleshooting (`--verbose`)

## Key Features

| Feature | Description |
|---------|-------------|
| `--get-rec` | On each `lcm-recommendation-event`, calls `get-lcm-recommendation` and `get-lcm-msl-recommendation-preview` via optima v2 |
| `--verbose` / `-v` | Logs HTTP requests and responses to stderr (Authorization header truncated) |
| `-k` / `--insecure` | Opt out of SSL certificate verification (verification is **enabled by default**) |
| `CW_USERNAME` / `CW_PASSWORD` | Environment variables for credentials; interactive prompt if omitted |
| `RecommendationClient` | Encapsulates LCM retrieval RPCs and MSL preview fan-out per solution |
| Structured sections | Code organised into named sections with HTTP adapter classes |

## Script Structure

`lcm_notify_v2.py` is a single self-contained script organised into named sections:

| Section | Contents |
|---------|----------|
| **Constants** | Ports, API paths, YANG identifiers, environment variable names, reconnect defaults |
| **Exceptions** | `CrossworkAuthError` |
| **HTTP adapters and session factory** | `_TimeoutAdapter`, `_VerboseAdapter`, `_create_session()` |
| **HTTP response helpers** | `response_text()`, `check_response()`, `response_json()`, `stream_log()` |
| **Credentials** | `_resolve_credentials()`, `load_token_from_file()` |
| **Configuration and authentication** | `ClientConfig`, `_get_ticket()`, `_get_token()`, `authenticate()` |
| **Event parsing** | `ParserMode`, `StreamingEventParser`, `extract_lcm_recommendation_event()`, `format_notification()` |
| **Notification stream management** | `NotificationStreamClient`, `StreamSession` |
| **Recommendation retrieval** | `RecommendationClient` |
| **Notification listener** | `NotificationListener`, `ListenOptions` |
| **CLI** | `build_parser()`, `_build_config()`, `_obtain_token()`, `_resolve_api_mode()`, `listen_v3()`, `listen_legacy()`, `main()` |

## Script Architecture

### Authentication Flow

The authentication flow matches other CNC API scripts such as `get_plan.py`:

```
┌─────────────────────┐    ┌─────────────────────────────────┐
│  lcm_notify_v2.py   │───▶│  CNC SSO Endpoint               │
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

Credentials are resolved in this order:

1. `--jwt` file (skips username/password entirely)
2. `--username` / `--password` CLI arguments
3. `CW_USERNAME` / `CW_PASSWORD` environment variables
4. Interactive prompt (`Username:` / `Password:` via `getpass`)

Alternatively, a pre-obtained JWT may be supplied with `--jwt` to skip username/password authentication. The companion script `cw_get_jwt.py` can be used to obtain and save a JWT file.

### Notification Flow (CNC 7.2+ / Optimization v3)

```
┌─────────────────────┐    ┌──────────────────────────────────────────────┐
│  lcm_notify_v2.py   │───▶│  CNC Optimization Engine API (v3)            │
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
│  lcm_notify_v2.py   │───▶│  CNC Optimization Engine API (v2)            │
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

### Recommendation Retrieval Flow (`--get-rec`)

When `--get-rec` is enabled, each parsed `lcm-recommendation-event` triggers additional RESTCONF RPCs on the **optima v2** API (independent of whether notifications arrive via v3 or legacy):

```
┌─────────────────────┐    ┌──────────────────────────────────────────────┐
│  lcm_notify_v2.py   │───▶│  CNC Optimization Engine API (v2)            │
│  RecommendationClient│    │  /crosswork/nbi/optima/v2/restconf/          │
└─────────────────────┘    └──────────────────────────────────────────────┘
         │                              │
         │  1. POST get-lcm-recommendation
         │     input: { domain-id }     │
         │◀─────────────────────────────│
         │     solutions[] with node,    │
         │     interface per solution   │
         │                              │
         │  2. POST get-lcm-msl-        │
         │     recommendation-preview   │
         │     (once per solution)      │
         │◀─────────────────────────────│
         │     TTE policy preview       │
         ▼
    [recommendation-details in output envelope]
```

If a retrieval call fails, the notification is still emitted; stderr reports the error and `recommendation-details` is omitted for that event.

### Runtime Flow

```
┌──────────────┐
│   main()     │
└──────┬───────┘
       │
       ├─▶ _build_config() / _obtain_token()
       │
       ├─▶ RecommendationClient (if --get-rec)
       │
       ├─▶ NotificationStreamClient
       │      ├─ create_v3_stream()
       │      ├─ v3_listen_session()
       │      └─ v2_listen_session()
       │
       └─▶ NotificationListener.listen()
              ├─ StreamingEventParser
              ├─ extract_lcm_recommendation_event() + fetch_details_for_event()
              └─ on_reconnect (re-subscribe on disconnect)
```

## Command Line Interface

The CLI follows the same conventions as `get_plan.py` for CNC connectivity and authentication.

### Usage

```bash
python lcm_notify_v2.py --ip <CNC_HOST> [options]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `--ip` | Crosswork Network Controller IP address or hostname *(required)* |
| `--port` | Crosswork HTTPS port (default: `30603`) |
| `-k`, `--insecure` | Disable SSL certificate verification (not recommended) |
| `--username`, `-u` | CNC username (or set `CW_USERNAME`) |
| `--password`, `-p` | CNC password (or set `CW_PASSWORD`; prompts if omitted) |
| `--jwt`, `-j` | Path to a JWT file; skips username/password authentication |
| `--timeout` | HTTP timeout in seconds for auth and setup requests (default: `30`) |
| `--api` | Notification API variant: `auto`, `v3`, or `legacy` (default: `auto`) |
| `--stream-id` | Existing optimization v3 stream UUID; skips initial stream creation |
| `--pretty` | Pretty-print each notification as indented JSON |
| `--output`, `-o` | Append received notifications to this file (one JSON object per line) |
| `--max-events` | Stop after receiving this many events (default: listen until interrupted) |
| `--get-rec` | Fetch recommendation details and MSL previews for each LCM event |
| `--verbose`, `-v` | Print API requests and responses to stderr |

> **Note**: Status messages (authentication progress, stream identifiers, reconnect notices, verbose HTTP traces) are written to **stderr**. Notification payloads are written to **stdout** prefixed with `<<<< `, making it straightforward to filter event output from diagnostic messages.

> **Security Note**: Avoid passing real passwords on the command line in shared or production environments (credentials may be visible in process listings). Prefer `--jwt` with a file obtained from `cw_get_jwt.py`, or set `CW_USERNAME` and `CW_PASSWORD` in the environment.

### Example Usage

```bash
# Basic usage — listen until Ctrl+C
python lcm_notify_v2.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' -k

# Use environment variables for credentials
export CW_USERNAME=<USERNAME>
export CW_PASSWORD='<PASSWORD>'
python lcm_notify_v2.py --ip <CNC_HOST> -k

# Use a saved JWT (from cw_get_jwt.py)
python lcm_notify_v2.py --ip <CNC_HOST> -j <JWT_FILE> -k

# Fetch full recommendation details on each event
python lcm_notify_v2.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' --get-rec -k

# Pretty-print, save events, and trace HTTP traffic
python lcm_notify_v2.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' \
  --pretty --get-rec --verbose -o lcm_events.jsonl -k

# Capture only the first N events (useful for testing)
python lcm_notify_v2.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' --max-events 5 -k

# Force legacy optima v2 API (CNC 7.0 and earlier)
python lcm_notify_v2.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' --api legacy -k

# Reattach to an existing v3 stream
python lcm_notify_v2.py --ip <CNC_HOST> -j <JWT_FILE> \
  --stream-id 'urn:uuid:<STREAM_UUID>' -k
```

> **Note**: The `-k` flag is shown in examples because CNC lab deployments typically use self-signed certificates. Omit `-k` in environments with valid TLS certificates.

### Example Session Output

```
Authenticating to <CNC_HOST>...
Creating optimization v3 notification stream...
Stream identifier: urn:uuid:<STREAM_UUID>
Recommendation retrieval enabled (get-lcm-recommendation + get-lcm-msl-recommendation-preview).
Press Ctrl+C to stop.
<<<< Listening on https://<CNC_HOST>:30603/crosswork/nbi/optimization/v3/restconf/streams/json/urn:uuid:<STREAM_UUID>
<<<< {"received-at":"<ISO8601_TIMESTAMP>","notification":{...},"recommendation-details":{...}}
^C
Received signal 2, stopping...
Received 1 event(s).
```

When no LCM activity is occurring, the v3 SSE stream still stays open. The server sends periodic `: ping` keepalives; these are not printed as events. With `--verbose`, keepalive chunks appear on stderr as `<<<< chunk (8 bytes): : ping`.

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

When `--get-rec` is enabled, a `recommendation-details` object is added:

```json
{
  "received-at": "<ISO8601_TIMESTAMP>",
  "notification": { "...": "..." },
  "recommendation-details": {
    "get-lcm-recommendation": {
      "cisco-crosswork-optimization-engine-lcm-recommendation-operations:output": {
        "urgency": "medium",
        "recommendation-id": "<RECOMMENDATION_UUID>",
        "response-result": "valid",
        "solutions": [
          {
            "node": "node-2",
            "interface": "GigabitEthernet0/0/0/6",
            "recommended-action": "create-set",
            "lcm-state": "congested"
          }
        ]
      }
    },
    "get-lcm-msl-recommendation-preview": [
      {
        "lcm-int": { "node": "node-2", "interface": "GigabitEthernet0/0/0/6" },
        "response": {
          "cisco-crosswork-optimization-engine-lcm-recommendation-operations:output": {
            "rec-id-check": "accepted",
            "response-result": "valid",
            "tte-policy-preview": [ "..." ]
          }
        }
      }
    ]
  }
}
```

Field descriptions:

- `received-at` — UTC timestamp when the script received the event locally
- `notification` — the raw notification payload from CNC
- `recommendation-details` — (optional) full recommendation and per-interface MSL previews fetched via optima v2 RPCs

Each event line on stdout is prefixed with `<<<< ` to distinguish event output from stderr diagnostics. With `--output`, the same prefixed line is appended (JSONL format), regardless of `--pretty`.

## Key Functions and Classes

### `ClientConfig`

Frozen dataclass holding connection settings: `base_url`, `verify_ssl`, `timeout`, `verbose`, and an optional shared `requests.Session`.

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
| `_setup_v2_stream()` | Legacy enable/subscribe GET sequence |
| `v2_listen_session()` | Build legacy listen URL and headers |

### `RecommendationClient`

Fetches LCM recommendation details via optima v2 RESTCONF operations:

| Method | Purpose |
|--------|---------|
| `get_recommendation(domain_id)` | POST `get-lcm-recommendation` for a domain |
| `get_msl_preview(domain_id, recommendation_id, node, interface)` | POST `get-lcm-msl-recommendation-preview` for one interface |
| `fetch_details_for_event(event)` | Orchestrates recommendation fetch + MSL preview fan-out per solution |

### `StreamSession`

Frozen dataclass with `url` and `headers` for an active listen session.

### `StreamingEventParser`

Incrementally parses notification payloads from a chunked HTTP response body:

- **`ParserMode.SSE`** — parses Server-Sent Events (`data:` lines), ignores `: ping` comments
- **`ParserMode.JSON`** — parses concatenated JSON objects (legacy v2 streams)

### `extract_lcm_recommendation_event(notification)`

Extracts the `lcm-recommendation-event` payload from a RESTCONF notification envelope, handling both fully qualified and suffix-matched YANG keys.

### `NotificationListener`

Manages the long-running listen loop:

- Opens a streaming HTTP GET with no read timeout
- Optionally fetches recommendation details before emitting each event
- Emits formatted events to stdout and optional output file
- Handles SIGINT/SIGTERM via `request_stop()`
- Reconnects after stream errors or server-side disconnects when an `on_reconnect` callback is provided

### `main()`

Orchestrates the workflow:

1. Parses command-line arguments
2. Builds `ClientConfig` with optional verbose HTTP adapter
3. Authenticates (or loads JWT from file)
4. Resolves API mode via `_resolve_api_mode()` (`auto`, `v3`, or `legacy`)
5. Creates or reuses a notification stream
6. Delegates to `_listen_v3()` or `_listen_legacy()`
7. Prints total event count on exit; returns exit code `0` on success, `1` on error

## API Endpoints Used

### Optimization v3 (CNC 7.2+) — Notifications

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

### Legacy optima v2 (CNC 7.0 and earlier) — Notifications

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/crosswork/nbi/optima/v2/restconf/data/ietf-restconf-monitoring:restconf-state/streams` | GET | Enable notification streams |
| `/crosswork/nbi/optima/v2/restconf/data/ietf-restconf-monitoring:restconf-state/streams/stream=lcm-recommendation-event/access=JSON/location` | GET | Subscribe to LCM stream |
| `/crosswork/nbi/optima/v2/restconf/notif/notification-stream/cisco-crosswork-optimization-engine-lcm-recommendation-operations:lcm-recommendation-event/JSON` | GET | Listen for notifications |

### Optima v2 — Recommendation Retrieval (`--get-rec`)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-lcm-recommendation-operations:get-lcm-recommendation` | POST | Fetch recommendation for a domain |
| `/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-lcm-recommendation-operations:get-lcm-msl-recommendation-preview` | POST | Fetch MSL/TTE policy preview for an interface |

**get-lcm-recommendation payload:**

```json
{
  "input": {
    "domain-id": "<DOMAIN_ID>"
  }
}
```

**get-lcm-msl-recommendation-preview payload:**

```json
{
  "input": {
    "domain-id": "<DOMAIN_ID>",
    "recommendation-id": "<RECOMMENDATION_UUID>",
    "lcm-int": {
      "node": "<NODE>",
      "interface": "<INTERFACE>"
    }
  }
}
```

## Configuration Constants

Defaults are defined at the top of `lcm_notify_v2.py`:

```python
BASE_PORT = 30603
CONNECT_TIMEOUT = 20
DEFAULT_AUTH_TIMEOUT = 30
DEFAULT_RECONNECT_DELAY = 3
CHUNK_SIZE = 4096

ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"
```

Port, timeout, and SSL verification may be overridden via `--port`, `--timeout`, and `-k`/`--insecure` on the command line. SSL verification is **enabled by default**; use `-k` for self-signed lab certificates.

## Dependencies

- **Python packages**: `requests`, `urllib3`
- **Standard library**: `argparse`, `getpass`, `json`, `logging`, `os`, `signal`, `sys`, `time`, `datetime`, `dataclasses`, `enum`, `typing`
- **External tools**: None

Install dependencies:

```bash
pip install requests urllib3
```

## Error Handling

The script handles common error scenarios:

- **HTTP errors**: Authentication failures, stream creation failures, listen endpoint errors — reported with HTTP status and response body snippet
- **Connection errors**: Network unreachability, stream disconnects — triggers automatic reconnection (v3 and legacy modes)
- **Recommendation fetch errors** (`--get-rec`): Logged to stderr; the notification is still emitted without `recommendation-details`
- **JSON parse errors**: Malformed notification payloads raise `json.JSONDecodeError` and terminate the script with exit code `1`
- **Signal handling**: SIGINT (Ctrl+C) and SIGTERM set a stop flag on `NotificationListener`; the script exits cleanly after the current read completes
- **API fallback**: In `--api auto` mode, failure to create a v3 stream falls back to legacy v2 with a stderr warning

## Troubleshooting

### SSL certificate verification errors

CNC lab deployments often use self-signed certificates. Either install the controller CA in your trust store, or pass `-k`/`--insecure`:

```
Error: ... SSLError ... certificate verify failed
```

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
<<<< closed by server.
<<<< reconnecting in 3 second(s)...
Creating new optimization v3 notification stream...
```

This is normal behaviour. Investigate CNC platform health and optima-lcm service logs if reconnections are frequent. Note that each v3 reconnect creates a **new** notification stream; orphaned streams may accumulate on the controller.

### `--get-rec` fails but notifications still appear

Recommendation retrieval uses optima v2 RPCs independently of the notification API. Verify that the JWT has permissions for `get-lcm-recommendation` and `get-lcm-msl-recommendation-preview`, and that the event contains a valid `domain-id`.

### Verbose mode shows no stream body for listen GET

For streaming responses, `--verbose` logs only the HTTP status line (not the full SSE body) to avoid flooding stderr. Use stream chunk logging (`<<<< chunk ...`) which appears when chunks arrive.

## Limitations and Considerations

1. **Long-running process**: The script is designed to run as a foreground listener, not as a daemon. Use `systemd`, `screen`, or `tmux` for persistent background operation.
2. **JWT lifetime**: The script does not refresh JWT tokens automatically during a long listen session.
3. **Single notification type**: Only `lcm-recommendation-event` is subscribed. Other COE notification types (SR policy, topology, RSVP) require separate stream creation with different notification URIs.
4. **SSL verification**: Verification is enabled by default. Use `-k`/`--insecure` only for lab or development environments with self-signed certificates.
5. **Mixed API usage**: With `--get-rec` on CNC 7.2+, notifications arrive via optimization v3 while recommendation fetches use optima v2. Both require a valid JWT with appropriate permissions.
6. **AAA session limits**: Use a dedicated API user for automated listeners to avoid exhausting shared AAA session limits on the controller.
7. **Event volume**: High LCM activity can produce a large volume of notifications. With `--get-rec`, each event triggers one `get-lcm-recommendation` call plus one `get-lcm-msl-recommendation-preview` call per solution interface. Use `--output` with log rotation in production integrations.
8. **Orphaned v3 streams**: Reconnect-on-v3 creates a new stream each time. The script does not list or delete old streams.
9. **v3 SSE framing**: The script assumes Server-Sent Events framing on v3 listen endpoints. This is consistent with observed controller behaviour but is not explicitly documented for LCM streams.

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

*Document Version: 1.0*  
*Script: lcm_notify_v2.py*  
*Platform: Cisco Crosswork Network Controller 7.2+ (with legacy 7.0 v2 fallback)*
