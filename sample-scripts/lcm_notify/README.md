# Application Note: LCM Recommendation Event Listener

## Overview

This application note describes `lcm_notify.py`, a single-file Python script that subscribes to and listens for **LCM Recommendation Events** from **Cisco Crosswork Network Controller (CNC)**. The script authenticates via CNC SSO, creates a RESTCONF notification stream for LCM recommendation events, and prints each notification as it arrives. It is intended for operators and integrators who need real-time visibility into Link Capacity Management (LCM) recommendation activity—such as new recommendations, updates, or lifecycle changes—without polling the CNC REST API.

The script also supports optional **recommendation detail retrieval** (`--get-rec`), **pretty-printed or verbose HTTP-style output** (`--pretty`, `--verbose`), **environment-variable credentials**, **HTTP/HTTPS proxy servers** via `http_proxy` and `https_proxy`, and **secure-by-default SSL verification** (with an explicit opt-out via `-k`/`--insecure`). It runs indefinitely until the user presses **Ctrl+C**, and automatically reconnects if the notification stream is closed by the server.

## Background: LCM Recommendation Notifications

Crosswork Optimization Engine (COE) publishes **LCM Recommendation Events** when recommendation-related state changes occur in the network. These notifications are defined in the YANG model `cisco-crosswork-optimization-engine-lcm-recommendation-operations` and use the common grouping `lcm-rec-notification`.

CNC exposes notification delivery through **RESTCONF notification streams**. The mechanism differs slightly between CNC releases:

| CNC Release | API Prefix | Subscription Model |
|-------------|------------|-------------------|
| **7.2 and later** | `/crosswork/nbi/optimization/v3/restconf` | POST `sal-remote:create-notification-stream`, then listen on `/streams/json/{uuid}` (Server-Sent Events) |
| **7.0 and earlier** | `/crosswork/nbi/optima/v2/restconf` | GET stream enable/subscribe endpoints, then listen on `/notif/notification-stream/...` |

`lcm_notify.py` supports both models. By default (`--api auto`), it prefers the **optimization v3** API used on CNC 7.2+ and falls back to the legacy **optima v2** API only when v3 stream creation fails for reasons other than authorization (for example, the v3 RPC is absent). **HTTP 401/403 responses are not treated as API-unavailable**; they indicate insufficient RBAC and the script exits with a clear hint instead of attempting the v2 fallback.

When `--get-rec` is enabled, the script additionally calls LCM RESTCONF operations on the **optimization v3** API to fetch full recommendation and preview details. Preview retrieval tries `get-lcm-msl-recommendation-preview` first and falls back to legacy `get-lcm-recommendation-preview` when the MSL RPC is not registered (typical on CNC 7.1).

**Note: The role associated with the apiuser must have write permissions for Crosswork Optimization Engine > Optimization Engine RESTCONF to subscribe to the RESTCONF notification streams**

For API details, see:

- [Crosswork Optimization Engine RESTCONF Notifications (7.2+)](https://developer.cisco.com/docs/crosswork/network-controller/crosswork-optimization-engine-restconf-notifications/)
- [Crosswork Optimization Engine RESTCONF Notifications (7.0 and earlier)](https://developer.cisco.com/docs/crosswork/network-controller/7-0/crosswork-optimization-engine-restconf-notifications/)
- [Retrieve an LCM recommendation](https://developer.cisco.com/docs/crosswork/network-controller/retrieve-an-lcm-recommendation/)
- [Preview LCM MSL recommendation](https://developer.cisco.com/docs/crosswork/network-controller/preview-lcm-msl-recommendation/)
- [Preview an LCM recommendation (deprecated)](https://developer.cisco.com/docs/crosswork/network-controller/7-2/preview-an-lcm-recommendation-will-be-deprecated/)

For a detailed comparison of CNC 7.1 vs 7.2 notification, recommendation, and preview behaviour, see `lcm-notify-cnc7.1-cnc7.2.md`.

## Script Purpose

`lcm_notify.py`:

1. **Authenticates** to Crosswork Network Controller via SSO (TGT → JWT)
2. **Subscribes** to the `lcm-recommendation-event` notification type
3. **Listens** on a long-lived HTTP stream for incoming events
4. **Prints** each event with a local timestamp and `<<<` / `>>>` direction markers
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
| `--get-rec` | On each `lcm-recommendation-event`, calls `get-lcm-recommendation` and preview RPCs via optimization v3 (`get-lcm-msl-recommendation-preview`, with legacy `get-lcm-recommendation-preview` fallback on CNC 7.1) |
| `--pretty` | Pretty-print notifications and recommendation RPCs to stdout (indented JSON after a single `<<<` line per section) |
| `--verbose` / `-v` | Log setup API traffic to stderr; show notifications/RPCs in HTTP trace style (or pretty style when combined with `--pretty`) |
| `-k` / `--insecure` | Opt out of SSL certificate verification (verification is **enabled by default**) |
| `CW_USERNAME` / `CW_PASSWORD` | Environment variables for credentials; interactive prompt if omitted |
| `http_proxy` / `https_proxy` | Environment variables for HTTP/HTTPS proxy servers (uppercase variants also supported) |
| `RecommendationClient` | Encapsulates LCM retrieval RPCs and per-solution preview fan-out with MSL/legacy fallback |
| RBAC-aware stream setup | HTTP 401/403 on v3 stream creation raises `CrossworkAccessDeniedError` with policy guidance; no v2 fallback |
| Request/response logging | `>>>` for outbound requests, `<<<` for inbound responses and notifications |

## Script Structure

`lcm_notify.py` is a single self-contained script organised into named sections:

| Section | Contents |
|---------|----------|
| **Constants** | Ports, API paths, YANG identifiers, environment variable names, reconnect defaults, log markers (`>>>`, `<<<`) |
| **Output formatting** | `ExchangeDisplay`, `local_timestamp()`, `print_exchange()`, `format_exchange_line()` |
| **Exceptions** | `CrossworkAuthError`, `CrossworkAccessDeniedError` |
| **HTTP adapters and session factory** | `_TimeoutAdapter`, `_VerboseAdapter`, `_apply_proxy_config()`, `_create_session()` |
| **HTTP response helpers** | `response_text()`, `check_response()`, `response_json()`, `stream_log()`, `stream_chunk_log()` |
| **Credentials** | `_resolve_credentials()`, `load_token_from_file()` |
| **Configuration and authentication** | `ClientConfig`, `_get_ticket()`, `_get_token()`, `authenticate()` |
| **Event parsing** | `ParserMode`, `StreamingEventParser`, `extract_lcm_recommendation_event()`, `format_notification()`, `emit_notification_event()` |
| **Notification stream management** | `NotificationStreamClient`, `StreamSession` |
| **Recommendation retrieval** | `RecommendationClient` |
| **Notification listener** | `NotificationListener`, `ListenOptions` |
| **CLI** | `build_parser()`, `_build_config()`, `_obtain_token()`, `_resolve_api_mode()`, `_listen_v3()`, `_listen_legacy()`, `main()` |

## Script Architecture

### Authentication Flow

The authentication flow matches other CNC API scripts such as `get_plan.py`:

```
┌─────────────────────┐    ┌─────────────────────────────────┐
│  lcm_notify.py      │───▶│  CNC SSO Endpoint               │
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

### Proxy Configuration

When CNC is reachable only through a corporate or lab HTTP proxy, set standard proxy environment variables before running the script. All HTTP traffic—SSO authentication, stream setup, the long-lived notification listen connection, and optional recommendation RPCs—uses the configured proxies.

| Variable | Purpose |
|----------|---------|
| `http_proxy` | Proxy URL for HTTP traffic (also used for HTTPS when `https_proxy` is unset) |
| `https_proxy` | Proxy URL for HTTPS traffic to CNC |
| `HTTP_PROXY` / `HTTPS_PROXY` | Uppercase variants; used when lowercase names are not set |
| `no_proxy` / `NO_PROXY` | Comma-separated hosts or CIDR ranges to reach directly (honoured via `requests` environment handling) |

Proxy URLs use the usual form, for example `http://proxy.example.com:8080` or `http://user:pass@proxy.example.com:8080`.

With `--verbose`, the script prints the effective proxy configuration to stderr at startup:

```
Using proxy: http_proxy=http://proxy.example.com:8080, https (via http_proxy)=http://proxy.example.com:8080
```

If no proxy variables are set, verbose mode reports `Using proxy: none (direct connection)`.

### Notification Flow (CNC 7.2+ / Optimization v3)

```
┌─────────────────────┐    ┌──────────────────────────────────────────────┐
│  lcm_notify.py      │───▶│  CNC Optimization Engine API (v3)            │
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
    [stdout / stderr / --output file]
```

If the SSE connection closes, the script waits 3 seconds, creates a **new** notification stream, and resumes listening.

### Notification Flow (Legacy optima v2)

```
┌─────────────────────┐    ┌──────────────────────────────────────────────┐
│  lcm_notify.py      │───▶│  CNC Optimization Engine API (v2)            │
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
    [stdout / stderr / --output file]
```

### Recommendation Retrieval Flow (`--get-rec`)

When `--get-rec` is enabled, each parsed `lcm-recommendation-event` triggers additional RESTCONF RPCs on the **optimization v3** API:

```
┌─────────────────────┐    ┌──────────────────────────────────────────────┐
│  lcm_notify.py      │───▶│  CNC Optimization Engine API (v3)            │
│  RecommendationClient│    │  /crosswork/nbi/optimization/v3/restconf/    │
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
         │     (once per solution;      │
         │     legacy preview fallback  │
         │     on CNC 7.1)              │
         │◀─────────────────────────────│
         │     TTE policy preview       │
         ▼
    [recommendation-details in output envelope]
```

**Preview RPC fallback** (`RecommendationClient.get_preview()`):

1. Try `get-lcm-msl-recommendation-preview` on optimization v3 (CNC 7.2+)
2. On HTTP 409 `data-missing`, fall back to `get-lcm-recommendation-preview` on optimization v3 (CNC 7.1)
3. If that is also missing, try `get-lcm-recommendation-preview` on optima v2 as a last resort

A one-time stderr warning is emitted when legacy preview fallback is used. Each preview response records the operation actually used in `preview-operation`.

If a retrieval call fails, the notification is still emitted; stderr reports the error and `recommendation-details` is omitted for that event.

When `--get-rec` is combined with `--pretty` or `--verbose`, the underlying HTTP adapter suppresses duplicate logging of recommendation RPC traffic (the structured event output already includes those calls).

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
python lcm_notify.py --ip <CNC_HOST> [options]
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
| `--pretty` | Pretty-print notifications and recommendation RPCs to stdout (indented JSON body after a single `<<<` line) |
| `--output`, `-o` | Append received notifications to this file (one JSON object per line) |
| `--max-events` | Stop after receiving this many events (default: listen until interrupted) |
| `--get-rec` | Fetch recommendation details and per-interface previews for each LCM event (MSL preview with legacy fallback) |
| `--verbose`, `-v` | Print setup API traffic to stderr; show notifications/RPCs in HTTP trace style |

### Output destinations

| Mode | Notifications | Setup / status messages |
|------|---------------|-------------------------|
| Default | stdout | stderr |
| `--pretty` | stdout (structured sections) | stderr |
| `--verbose` | stderr (HTTP trace style) | stderr (includes setup API traffic) |
| `--pretty --verbose` | stdout (pretty sections) | stderr (setup API traffic only; recommendation RPCs suppressed from adapter) |

> **Security Note**: Avoid passing real passwords on the command line in shared or production environments (credentials may be visible in process listings). Prefer `--jwt` with a file obtained from `cw_get_jwt.py`, or set `CW_USERNAME` and `CW_PASSWORD` in the environment.

### Example Usage

```bash
# Basic usage — listen until Ctrl+C
python lcm_notify.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' -k

# Use environment variables for credentials
export CW_USERNAME=<USERNAME>
export CW_PASSWORD='<PASSWORD>'
python lcm_notify.py --ip <CNC_HOST> -k

# Connect via an HTTP/HTTPS proxy
export http_proxy=http://proxy.example.com:8080
export https_proxy=http://proxy.example.com:8080
python lcm_notify.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' --verbose -k

# Use a saved JWT (from cw_get_jwt.py)
python lcm_notify.py --ip <CNC_HOST> -j <JWT_FILE> -k

# Fetch full recommendation details on each event
python lcm_notify.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' --get-rec -k

# Pretty-print, save events, and trace HTTP traffic
python lcm_notify.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' \
  --pretty --get-rec --verbose -o lcm_events.jsonl -k

# Capture only the first N events (useful for testing)
python lcm_notify.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' --max-events 5 -k

# Force legacy optima v2 API (CNC 7.0 and earlier)
python lcm_notify.py --ip <CNC_HOST> -u <USERNAME> -p '<PASSWORD>' --api legacy -k

# Reattach to an existing v3 stream
python lcm_notify.py --ip <CNC_HOST> -j <JWT_FILE> \
  --stream-id 'urn:uuid:<STREAM_UUID>' -k
```

> **Note**: The `-k` flag is shown in examples because CNC lab deployments typically use self-signed certificates. Omit `-k` in environments with valid TLS certificates.

### Example Session Output

Default mode (compact JSON envelope on stdout):

```
Authenticating to <CNC_HOST>...
Creating optimization v3 notification stream...
Stream identifier: urn:uuid:<STREAM_UUID>
Press Ctrl+C to stop.
Listening on https://<CNC_HOST>:30603/crosswork/nbi/optimization/v3/restconf/streams/json/urn:uuid:<STREAM_UUID>
2026-07-21 19:48:01.123 <<< notification
{"received-at":"2026-07-21 19:48:01.123","notification":{...}}
^C
Received signal 2, stopping...
Received 1 event(s).
```

With `--get-rec`:

```
Recommendation retrieval enabled via optimization v3 (get-lcm-recommendation + preview RPC with legacy fallback).
```

On CNC 7.1, preview fallback may also print:

```
get-lcm-msl-recommendation-preview is unavailable on this controller; falling back to legacy get-lcm-recommendation-preview via optimization v3.
```

With `--verbose` and proxy environment variables set:

```
Using proxy: http_proxy=http://proxy.example.com:8080, https_proxy=http://proxy.example.com:8080
Authenticating to <CNC_HOST>...
```

When no LCM activity is occurring, the v3 SSE stream still stays open. The server sends periodic `: ping` keepalives; these are not printed as events. With `--verbose`, keepalive chunks appear on stderr as:

```
2026-07-21 19:48:05.000 <<< chunk (8 bytes): : ping
```

## Output Format

### Log markers

All structured output uses direction markers with a millisecond-resolution local timestamp:

- `>>>` — outbound request (HTTP method + URL, optional body)
- `<<<` — inbound response, notification, or stream chunk

### JSON envelope (default and `--output`)

Each received notification is wrapped in a JSON envelope:

```json
{
  "received-at": "2026-07-21 19:48:01.123",
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

When `--get-rec` is enabled, a `recommendation-details` object is added with full RPC metadata (request, response, HTTP status, and timestamps):

```json
{
  "received-at": "2026-07-21 19:48:01.123",
  "notification": { "...": "..." },
  "recommendation-details": {
    "get-lcm-recommendation": {
      "request": {
        "method": "POST",
        "url": "https://<CNC_HOST>:30603/crosswork/nbi/optimization/v3/restconf/operations/...",
        "body": { "input": { "domain-id": "<DOMAIN_ID>" } }
      },
      "response": {
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
      "status": { "code": 200, "reason": "OK", "size": 1234 },
      "requested-at": "2026-07-21 19:48:01.200",
      "responded-at": "2026-07-21 19:48:01.350"
    },
    "get-lcm-msl-recommendation-preview": [
      {
        "lcm-int": { "node": "node-2", "interface": "GigabitEthernet0/0/0/6" },
        "preview-operation": "cisco-crosswork-optimization-engine-lcm-recommendation-operations:get-lcm-msl-recommendation-preview",
        "request": { "method": "POST", "url": "...", "body": { "input": { "...": "..." } } },
        "response": {
          "cisco-crosswork-optimization-engine-lcm-recommendation-operations:output": {
            "rec-id-check": "accepted",
            "response-result": "valid",
            "tte-policy-preview": [ "..." ]
          }
        },
        "status": { "code": 200, "reason": "OK", "size": 567 },
        "requested-at": "2026-07-21 19:48:01.400",
        "responded-at": "2026-07-21 19:48:01.500"
      }
    ]
  }
}
```

Field descriptions:

- `received-at` — local timestamp when the script received the event (`YYYY-MM-DD HH:MM:SS.mmm`)
- `notification` — the raw notification payload from CNC
- `recommendation-details` — (optional) full recommendation and per-interface previews fetched via optimization v3 RPCs, including request/response pairs, HTTP status, timestamps, and `preview-operation` (MSL or legacy)

On CNC 7.2+, preview responses use the MSL schema (`tte-policy-preview[].segment-list[]`). On CNC 7.1, legacy preview responses use a flat schema (`tte-policy-preview[].segment-list-hop[]`). See `lcm-notify-cnc7.1-cnc7.2.md` for examples.

With `--output`, the envelope is written to the file using the same `<<< notification` line format as console output. Compact JSON is embedded in the line body regardless of `--pretty`.

### Compact console output (default)

```
2026-07-21 19:48:01.123 <<< notification
{"received-at":"2026-07-21 19:48:01.123","notification":{...},"recommendation-details":{...}}
```

### Pretty output (`--pretty`)

Events are printed as labelled sections with indented JSON bodies:

```
2026-07-21 19:48:01.123 <<< notification
{
  "ietf-restconf:notification": {
    "cisco-crosswork-optimization-engine-lcm-recommendation-operations:lcm-recommendation-event": {
      "domain-id": "<DOMAIN_ID>",
      "recommendation-id": "<RECOMMENDATION_UUID>"
    }
  }
}

2026-07-21 19:48:01.200 >>> POST https://<CNC_HOST>:30603/crosswork/nbi/optimization/v3/restconf/operations/...
{
  "input": {
    "domain-id": "<DOMAIN_ID>"
  }
}

2026-07-21 19:48:01.350 <<< get-lcm-recommendation
{
  "cisco-crosswork-optimization-engine-lcm-recommendation-operations:output": {
    "response-result": "valid",
    "recommendation-id": "<RECOMMENDATION_UUID>",
    "solutions": [ "..." ]
  }
}

2026-07-21 19:48:01.500 <<< get-lcm-msl-recommendation-preview (node-2/GigabitEthernet0/0/0/6)
{
  "cisco-crosswork-optimization-engine-lcm-recommendation-operations:output": {
    "rec-id-check": "accepted",
    "response-result": "valid",
    "tte-policy-preview": [ "..." ]
  }
}
```

The preview section label reflects the operation used (`get-lcm-msl-recommendation-preview` or `get-lcm-recommendation-preview`) and the target interface.

With `--get-rec`, the notification and each RPC are shown as separate sections instead of one nested JSON object.

### Verbose output (`--verbose`)

At startup, verbose mode prints the configured proxy servers (or `Using proxy: none (direct connection)` when unset). Setup API calls (authentication, stream creation, legacy subscribe) are then logged to stderr with full request/response details. Notification and RPC output follows the HTTP trace style (status code and byte count in the `<<<` headline) unless `--pretty` is also set.

## Key Functions and Classes

### `ClientConfig`

Frozen dataclass holding connection settings: `base_url`, `verify_ssl`, `timeout`, `verbose`, `pretty`, `suppress_rec_rpc`, and an optional shared `requests.Session`.

- `suppress_rec_rpc` — when `True` (set automatically with `--get-rec --pretty` or `--get-rec --verbose`), the verbose HTTP adapter skips duplicate logging of recommendation/preview RPC traffic because structured event output already includes those calls
- `get_session()` — returns the configured session or creates one via `_create_session()`

### `ExchangeDisplay` / `print_exchange()`

`ExchangeDisplay` groups formatting options (timestamp, output file, leading/trailing blank lines) for `print_exchange()` and related helpers. All structured log output uses `>>>` / `<<<` markers with millisecond-resolution local timestamps.

### `CrossworkAccessDeniedError`

Subclass of `CrossworkAuthError` raised when authentication succeeded but the user lacks permission for an API call (HTTP 401/403). Stream creation failures with this exception do not trigger legacy v2 fallback in `--api auto` mode.

### `authenticate(config, username, password)`

Performs two-step SSO authentication:

1. Obtains TGT (Ticket Granting Ticket) from `/crosswork/sso/v1/tickets`
2. Exchanges TGT for JWT via `/crosswork/sso/v2/tickets/jwt`

### `NotificationStreamClient`

Encapsulates RESTCONF stream setup for both API variants:

| Method | Purpose |
|--------|---------|
| `create_v3_stream()` | POST `sal-remote:create-notification-stream` for LCM events; raises `CrossworkAccessDeniedError` on HTTP 401/403 |
| `v3_listen_session(stream_id)` | Build SSE listen URL and headers |
| `_setup_v2_stream()` | Legacy enable/subscribe GET sequence |
| `v2_listen_session()` | Build legacy listen URL and headers |

### `RecommendationClient`

Fetches LCM recommendation details via optimization v3 RESTCONF operations:

| Method | Purpose |
|--------|---------|
| `get_recommendation(domain_id)` | POST `get-lcm-recommendation` for a domain |
| `get_preview(domain_id, recommendation_id, node, interface)` | POST preview RPC for one interface, with MSL → legacy v3 → legacy v2 fallback |
| `fetch_details_for_event(event)` | Orchestrates recommendation fetch + preview fan-out per solution |

### `StreamSession`

Frozen dataclass with `url` and `headers` for an active listen session.

### `StreamingEventParser`

Incrementally parses notification payloads from a chunked HTTP response body:

- **`ParserMode.SSE`** — parses Server-Sent Events (`data:` lines), ignores `: ping` comments
- **`ParserMode.JSON`** — parses concatenated JSON objects (legacy v2 streams)
- **`reset()`** — discards buffered partial stream data

### `extract_lcm_recommendation_event(notification)`

Extracts the `lcm-recommendation-event` payload from a RESTCONF notification envelope, handling both fully qualified and suffix-matched YANG keys.

### `emit_notification_event()`

Renders a notification and optional recommendation RPCs as labelled `>>>` / `<<<` sections. Preview section labels include the operation name and target interface (for example, `get-lcm-msl-recommendation-preview (node-2/GigabitEthernet0/0/0/6)`).

### `NotificationListener`

Manages the long-running listen loop:

- Opens a streaming HTTP GET with no read timeout
- Optionally fetches recommendation details before emitting each event
- Emits formatted events to stdout or stderr depending on `--pretty` / `--verbose`
- Appends envelope JSON to `--output` file via a context-managed append handle when specified
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

### Optimization v3 (CNC 7.2+) — Recommendation Retrieval (`--get-rec`)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/crosswork/nbi/optimization/v3/restconf/operations/cisco-crosswork-optimization-engine-lcm-recommendation-operations:get-lcm-recommendation` | POST | Fetch recommendation for a domain |
| `/crosswork/nbi/optimization/v3/restconf/operations/cisco-crosswork-optimization-engine-lcm-recommendation-operations:get-lcm-msl-recommendation-preview` | POST | Fetch MSL/TTE policy preview for an interface (CNC 7.2+) |
| `/crosswork/nbi/optimization/v3/restconf/operations/cisco-crosswork-optimization-engine-lcm-recommendation-operations:get-lcm-recommendation-preview` | POST | Legacy preview fallback (CNC 7.1; deprecated) |

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

Defaults are defined at the top of `lcm_notify.py`:

```python
BASE_PORT = 30603
CONNECT_TIMEOUT = 20
DEFAULT_AUTH_TIMEOUT = 30
DEFAULT_RECONNECT_DELAY = 3
CHUNK_SIZE = 4096

ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"
ENV_HTTP_PROXY = "http_proxy"
ENV_HTTPS_PROXY = "https_proxy"
```

Port, timeout, and SSL verification may be overridden via `--port`, `--timeout`, and `-k`/`--insecure` on the command line. SSL verification is **enabled by default**; use `-k` for self-signed lab certificates. Proxy servers are configured only through environment variables (there is no `--proxy` CLI flag).

## Dependencies

- **Python packages**: `requests`, `urllib3`
- **Standard library**: `argparse`, `contextlib`, `getpass`, `json`, `os`, `signal`, `sys`, `time`, `datetime`, `dataclasses`, `enum`, `typing`
- **External tools**: None

Install dependencies:

```bash
pip install requests urllib3
```

## Error Handling

The script handles common error scenarios:

- **HTTP errors**: Authentication failures, stream creation failures, listen endpoint errors — reported with HTTP status and response body snippet
- **Authorization errors (HTTP 401/403)**: Raised as `CrossworkAccessDeniedError` with an RBAC hint; **not** treated as API-unavailable in `--api auto` mode
- **Connection errors**: Network unreachability, stream disconnects — triggers automatic reconnection (v3 and legacy modes)
- **Recommendation fetch errors** (`--get-rec`): Logged to stderr; the notification is still emitted without `recommendation-details`
- **JSON parse errors**: Malformed notification payloads raise `json.JSONDecodeError` and terminate the script with exit code `1`
- **Signal handling**: SIGINT (Ctrl+C) and SIGTERM set a stop flag on `NotificationListener`; the script exits cleanly after the current read completes
- **API fallback (`--api auto`)**: Failure to create a v3 stream falls back to legacy v2 with a stderr warning, **except** for `CrossworkAccessDeniedError` (insufficient RBAC)

## Troubleshooting

### SSL certificate verification errors

CNC lab deployments often use self-signed certificates. Either install the controller CA in your trust store, or pass `-k`/`--insecure`:

```
Error: ... SSLError ... certificate verify failed
```

### HTTP 403 Forbidden on v3 stream creation

Authentication succeeded but the user lacks permission to call `sal-remote:create-notification-stream`. The script exits immediately with `CrossworkAccessDeniedError` and does **not** fall back to optima v2.

```
Error: create_notification_stream_v3 returned HTTP 403 Forbidden: ...
Hint: The authenticated user lacks permission to create optimization v3 notification
streams (sal-remote:create-notification-stream). Grant RESTCONF notification-stream
access in the user's CNC policy, or use an account with sufficient RBAC.
```

Grant the CNC policy permissions required for optimization v3 RESTCONF notification streams, or authenticate with an account that has them (for example, `admin` rather than a read-only NOC role). On CNC 7.2+, legacy v2 fallback will not help because the v2 streams model is typically absent.

### Script exits immediately with HTTP 404 on listen (v2 path)

The target CNC likely requires the **optimization v3** API. Use `--api v3` or leave `--api` at the default `auto` value.

```
Error: listen_notification_stream returned HTTP 404 Not Found: ...
```

### Script exits immediately with HTTP 409 on streams (v2 enable)

A `data-missing` (HTTP 409) response on the v2 streams endpoint indicates the legacy monitoring model is not populated on this CNC release (typical on CNC 7.2+). Use `--api v3` with an account that has v3 notification-stream permissions. If v3 stream creation previously failed with HTTP 403, fix RBAC first — v2 fallback cannot substitute for missing authorization.

### `--get-rec` preview fallback warning (CNC 7.1)

On CNC 7.1, stderr may show:

```
get-lcm-msl-recommendation-preview is unavailable on this controller; falling back to legacy get-lcm-recommendation-preview via optimization v3.
```

This is expected. Preview responses use the legacy flat schema; see `lcm-notify-cnc7.1-cnc7.2.md`.

### No events received, but script keeps running

This is expected when no LCM recommendation activity is occurring. The v3 SSE stream sends `: ping` keepalives to maintain the connection. Trigger an LCM event on the controller (for example, a threshold crossing or recommendation generation) to verify end-to-end delivery.

### JWT expires during a long listen session

JWT tokens have a finite lifetime. If the stream fails with authentication errors after extended runtime, obtain a fresh JWT with `cw_get_jwt.py` and restart the script.

### Reconnection loop

If the server repeatedly closes the stream, stderr will show:

```
closed by server.
reconnecting in 3 second(s)...
Creating new optimization v3 notification stream...
```

This is normal behaviour. Investigate CNC platform health and optima-lcm service logs if reconnections are frequent. Note that each v3 reconnect creates a **new** notification stream; orphaned streams may accumulate on the controller.

### `--get-rec` fails but notifications still appear

Recommendation retrieval uses optimization v3 RPCs. Verify that the JWT has permissions for `get-lcm-recommendation` and `get-lcm-msl-recommendation-preview`, and that the event contains a valid `domain-id`.

### Verbose mode shows no stream body for listen GET

For streaming responses, `--verbose` logs only the HTTP status line (not the full SSE body) to avoid flooding stderr. Stream chunk logging (`<<< chunk ...`) appears on stderr when non-notification chunks arrive.

### Proxy connection failures

If CNC is unreachable through the proxy, authentication or stream setup fails with connection or proxy errors. Verify `http_proxy` / `https_proxy` values, ensure the proxy allows long-lived HTTPS CONNECT tunnels (required for the notification stream), and confirm CNC is not listed in `no_proxy` when it should be proxied.

```
Error: ... ProxyError ... Cannot connect to proxy ...
```

## Limitations and Considerations

1. **Long-running process**: The script is designed to run as a foreground listener, not as a daemon. Use `systemd`, `screen`, or `tmux` for persistent background operation.
2. **JWT lifetime**: The script does not refresh JWT tokens automatically during a long listen session.
3. **Single notification type**: Only `lcm-recommendation-event` is subscribed. Other COE notification types (SR policy, topology, RSVP) require separate stream creation with different notification URIs.
4. **SSL verification**: Verification is enabled by default. Use `-k`/`--insecure` only for lab or development environments with self-signed certificates.
5. **API version**: On CNC 7.2+, both notifications and recommendation retrieval use optimization v3. Legacy optima v2 is used only for notifications when v3 stream creation fails for non-authorization reasons (`--api auto`) or when `--api legacy` is specified. HTTP 401/403 on v3 stream creation always fails fast with an RBAC hint.
6. **Preview schema**: CNC 7.2+ returns MSL previews with `segment-list[]`; CNC 7.1 uses legacy preview with flat `segment-list-hop[]`. Integrations should handle both shapes or inspect `preview-operation`.
7. **AAA session limits**: Use a dedicated API user for automated listeners to avoid exhausting shared AAA session limits on the controller.
8. **Event volume**: High LCM activity can produce a large volume of notifications. With `--get-rec`, each event triggers one `get-lcm-recommendation` call plus one preview call per solution interface. Use `--output` with log rotation in production integrations.
9. **Orphaned v3 streams**: Reconnect-on-v3 creates a new stream each time. The script does not list or delete old streams.
10. **v3 SSE framing**: The script assumes Server-Sent Events framing on v3 listen endpoints. This is consistent with observed controller behaviour but is not explicitly documented for LCM streams.
11. **Proxy configuration**: Proxies are read from environment variables only. Long-lived SSE streams require a proxy that supports persistent HTTPS tunneling; some proxies may time out idle connections and trigger reconnect loops.

## Relationship to Other Scripts

| Script | Relationship |
|--------|--------------|
| `get_plan.py` | Shares the same SSO authentication flow and CLI conventions (`--ip`, `-u`, `-p`, `-j`) |
| `cw_get_jwt.py` | Can be used to obtain a JWT file for `--jwt` authentication |
| `lcm-notify-cnc7.1-cnc7.2.md` | Application note comparing CNC 7.1 vs 7.2 LCM notify and preview behaviour |

## References

- [Crosswork Optimization Engine RESTCONF Notifications (7.2+)](https://developer.cisco.com/docs/crosswork/network-controller/crosswork-optimization-engine-restconf-notifications/)
- [Crosswork Optimization Engine RESTCONF Notifications (7.0 and earlier)](https://developer.cisco.com/docs/crosswork/network-controller/7-0/crosswork-optimization-engine-restconf-notifications/)
- [Retrieve an LCM recommendation](https://developer.cisco.com/docs/crosswork/network-controller/retrieve-an-lcm-recommendation/)
- [Preview LCM MSL recommendation](https://developer.cisco.com/docs/crosswork/network-controller/preview-lcm-msl-recommendation/)
- [Preview an LCM recommendation (deprecated)](https://developer.cisco.com/docs/crosswork/network-controller/7-2/preview-an-lcm-recommendation-will-be-deprecated/)
- [Cisco Crosswork Network Controller API Documentation](https://developer.cisco.com/docs/crosswork/)

---

*Document Version: 1.3*  
*Script: lcm_notify.py*  
*Platform: Cisco Crosswork Network Controller 7.2+ (with legacy 7.0 v2 fallback for notifications; legacy preview fallback for `--get-rec` on CNC 7.1)*
