# fake_cnc_server.py

A mock Crosswork Network Controller (CNC) server for local testing of `get_plan.py` and related API clients.

## Overview

This script creates a local HTTPS server that simulates the Crosswork CNC authentication and plan retrieval APIs. It's useful for testing client scripts without needing access to a real Crosswork environment.

## Features

- **SSO Authentication Flow**: Implements the full TGT → JWT token exchange
- **Plan File Serving**: Serves plan files in both `txt` and `pln` formats
- **HTTPS Support**: Auto-generates self-signed certificates (or can run in HTTP mode)
- **Configurable Plan Content**: Load custom plan files or use built-in fallback data

## Requirements

- Python 3.6+
- OpenSSL (for certificate generation when using HTTPS)

## Usage

### Basic Usage

```bash
python fake_cnc_server.py
```

Starts the server on port 30603 with HTTPS enabled, serving `sample_plan.txt`.

### Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `--port PORT` | Port to listen on | `30603` |
| `--no-ssl` | Disable SSL (use HTTP instead of HTTPS) | Disabled (HTTPS enabled) |
| `--plan-file FILE` | Path to plan file to serve | `sample_plan.txt` |

### Examples

```bash
# Start with default settings (HTTPS on port 30603)
python fake_cnc_server.py

# Use custom port
python fake_cnc_server.py --port 8443

# Run without SSL (HTTP mode)
python fake_cnc_server.py --no-ssl

# Serve a custom plan file
python fake_cnc_server.py --plan-file /path/to/my_plan.txt
```

## Testing with get_plan.py

Once the server is running, test it with `get_plan.py`:

```bash
python get_plan.py --ip 127.0.0.1 -u admin -p admin -f plan.txt
```

**Note**: Any username/password combination is accepted for testing purposes.

## API Endpoints

The server implements the following CNC API endpoints:

### POST `/crosswork/sso/v1/tickets`

Initial authentication endpoint. Accepts form-encoded credentials and returns a TGT (Ticket Granting Ticket).

**Request Body** (form-encoded):
```
username=admin&password=admin
```

**Response**: TGT string (e.g., `TGT-abc123...`)

### POST `/crosswork/sso/v1/tickets/{TGT}`

Token exchange endpoint. Exchanges a TGT for a JWT token.

**Response**: JWT token string

### POST `/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-operations:get-plan`

Plan retrieval endpoint. Returns base64-encoded plan content.

**Headers**:
```
Authorization: Bearer <JWT_TOKEN>
Content-Type: application/json
```

**Request Body**:
```json
{
  "input": {
    "format": "txt",
    "version": "7.10"
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

## Plan File Format

The server serves plan files in the Crosswork network plan format. If no plan file is specified or the file doesn't exist, a minimal fallback plan is used:

```
<Network>
Property    Value
Title       Fake Network
Version     7.10.-1

<NetworkOptions>
Option      Value
IGP_Protocol    ISIS

<Nodes>
Name    Site    Function    Protected    Active    Type    AS    IPAddress
node-1          core        F            T         physical 65000 198.19.1.1
node-2          core        F            T         physical 65000 198.19.1.2
```

## Architecture

```
┌─────────────────┐     1. POST /tickets       ┌─────────────────┐
│                 │  ─────────────────────────>│                 │
│   get_plan.py   │     (username/password)    │                 │
│                 │  <─────────────────────────│                 │
│   (or other     │          TGT               │                 │
│    client)      │                            │  fake_cnc_server│
│                 │     2. POST /tickets/TGT   │                 │
│                 │  ─────────────────────────>│                 │
│                 │  <─────────────────────────│                 │
│                 │          JWT               │                 │
│                 │                            │                 │
│                 │     3. POST /get-plan      │                 │
│                 │  ─────────────────────────>│                 │
│                 │     (Bearer JWT)           │                 │
│                 │  <─────────────────────────│                 │
└─────────────────┘     (base64 plan)          └─────────────────┘
```

## Notes

- The server binds to `0.0.0.0`, making it accessible from other machines on the network
- Self-signed certificates are generated in temporary files and cleaned up on exit
- All authentication is accepted (no real credential validation) for testing purposes
- Press `Ctrl+C` to stop the server
