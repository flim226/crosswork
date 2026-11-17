# fake_cnc_server.py

A mock Crosswork Network Controller (CNC) server for local testing of `get_plan.py`, `cw_get_jwt.py`, and related API clients.

## Overview

This script creates a local HTTPS server that simulates the Crosswork CNC SSO, plan retrieval, and DLM Inventory APIs used by `get_plan.py`. It's useful for testing client scripts without needing access to a real Crosswork environment.

## Features

- **SSO Authentication Flow**: Implements the TGT → JWT exchange used by `get_plan.py` and `cw_get_jwt.py` (`/crosswork/sso/v2/tickets/jwt`)
- **Signed JWTs**: Issues HS256 JWTs with `sub`, `iat`, and `exp` claims that `cw_get_jwt.py --decode` can inspect. Tokens stay valid across server restarts, so a cached `~/.crosswork/<ip>.jwt` keeps working until it expires (8 hours)
- **Plan File Serving**: Serves the plan for both `txt` and `pln` requests
- **DLM Inventory**: Answers `get_plan.py --geoloc` with coordinates for every node in the plan's `<Nodes>` table, falling back to the node's site coordinates
- **HTTPS Support**: Auto-generates a throwaway self-signed certificate (or can run in HTTP mode)

## Requirements

- Platform: None. It emulates the Crosswork Network Controller 7.2 or later SSO, `optima/v2` plan, and DLM Inventory APIs for testing `get_plan.py`.
- Python 3.7+
- OpenSSL (for certificate generation when using HTTPS)

## Usage

### Basic Usage

```bash
python fake_cnc_server.py
```

Starts the server on port 30603 with HTTPS enabled, serving the embedded US WAN sample plan.

### Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `--port PORT` | Port to listen on | `30603` |
| `--no-ssl` | Disable SSL (use HTTP instead of HTTPS). `get_plan.py` always uses HTTPS, so do not use this with it | Disabled (HTTPS enabled) |
| `--plan-file FILE` | Path to plan file to serve | Embedded US WAN sample plan |

### Examples

```bash
# Start with default settings (HTTPS on port 30603)
python fake_cnc_server.py

# Use custom port
python fake_cnc_server.py --port 8443

# Serve a custom plan file
python fake_cnc_server.py --plan-file /path/to/my_plan.txt
```

## Testing with get_plan.py

Once the server is running, test it with `get_plan.py`. Pass `-k` because the certificate is self-signed:

```bash
# Text plan
python get_plan.py --ip 127.0.0.1 -u admin -p admin -k -f plan.txt

# Text plan with node coordinates from DLM Inventory
python get_plan.py --ip 127.0.0.1 -u admin -p admin -k -f plan.txt --geoloc

# Server on a non-default port
python get_plan.py --ip 127.0.0.1 --port 8443 -u admin -p admin -k -f plan.txt
```

**Notes**:

- Any non-empty username/password combination is accepted.
- The `pln` format returns the same bytes as `txt`; the server does not produce binary plans.
- `--geoloc` requires the served plan's `<Nodes>` table to include `Longitude` and `Latitude` columns. The embedded plan does not, so use `--plan-file` with a plan that has them.

## API Endpoints

### POST `/crosswork/sso/v1/tickets`

Initial authentication endpoint. Accepts form-encoded credentials and returns a TGT (Ticket Granting Ticket).

**Request Body** (form-encoded):
```
username=admin&password=admin
```

**Response**: `201 Created`, with the TGT as the plain-text body and in the `Location` header.

### POST `/crosswork/sso/v2/tickets/jwt`

Exchanges a TGT for a JWT.

**Request Body** (form-encoded):
```
tgt=TGT-...&service=https://<ip>:30603/app-dashboard
```

**Response**: JWT as plain text.

The legacy `POST /crosswork/sso/v1/tickets/{TGT}` exchange is also accepted.

### POST `/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-operations:get-plan`

Plan retrieval endpoint. Requires `Authorization: Bearer <JWT>`.

**Request Body**:
```json
{
  "input": {
    "format": "txt",
    "version": ""
  }
}
```

**Response**:
```json
{
  "cisco-crosswork-optimization-engine-operations:output": {
    "planfile-content": "<base64-encoded-plan>"
  }
}
```

### POST `/crosswork/inventory/v1/nodes/query`

DLM Inventory query used by `get_plan.py --geoloc`. Requires `Authorization: Bearer <JWT>`. The request body is ignored.

**Response**:
```json
{
  "data": [
    {
      "host_name": "node-1",
      "geo_info": {
        "coordinates": {
          "longitude": {"value": -104.638684},
          "latitude": {"value": 74.34176}
        }
      }
    }
  ]
}
```

Each node's coordinates are taken, in order of preference, from:

1. The node's own `Longitude` and `Latitude` columns in `<Nodes>`
2. The `Longitude` and `Latitude` of the node's `Site` in `<Sites>`
3. A deterministic hash of the node name, so they are stable across runs

At startup the server logs how many nodes used each source.

## Embedded Plan

Without `--plan-file`, the server serves a US WAN sample plan embedded in the script as `DEFAULT_PLAN`. It contains 33 nodes plus sites, circuits, interfaces, demands, and traffic tables. Coordinates live in the `<Sites>` table, so the inventory API reports each node at its site's location. Its `<Nodes>` table has no `Longitude` or `Latitude` columns, so `get_plan.py --geoloc` cannot write them into the plan.

## Architecture

```
┌─────────────────┐  1. POST /sso/v1/tickets      ┌─────────────────┐
│                 │  ────────────────────────────>│                 │
│   get_plan.py   │     (username/password)       │                 │
│                 │  <────────────────────────────│                 │
│   (or other     │            TGT                │                 │
│    client)      │                               │ fake_cnc_server │
│                 │  2. POST /sso/v2/tickets/jwt  │                 │
│                 │  ────────────────────────────>│                 │
│                 │  <────────────────────────────│                 │
│                 │            JWT                │                 │
│                 │                               │                 │
│                 │  3. POST /get-plan            │                 │
│                 │  ────────────────────────────>│                 │
│                 │  <──── base64 plan ───────────│                 │
│                 │                               │                 │
│                 │  4. POST /inventory/v1/nodes/ │                 │
│                 │     query (--geoloc only)     │                 │
│                 │  ────────────────────────────>│                 │
│                 │  <──── node coordinates ──────│                 │
└─────────────────┘                               └─────────────────┘
```

## Notes

- The server binds to `0.0.0.0`, making it accessible from other machines on the network
- The self-signed certificate is written to a temporary directory and deleted once loaded
- Requests are handled concurrently
- Press `Ctrl+C` to stop the server
