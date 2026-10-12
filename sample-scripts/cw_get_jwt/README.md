# Application Note: Obtaining and Decoding JWT Tokens from Crosswork Network Controller

## Overview

This application note describes how to use the `cw_get_jwt.py` script to obtain a JSON Web Token (JWT) from **Cisco Crosswork Network Controller (CNC)** and to inspect the claims of an existing token. The script authenticates with CNC's Single Sign-On (SSO) service, saves the token to a local file with owner-only permissions, and can decode a saved token to show its subject, issuer, and expiry.

Other scripts in this repository reuse the saved token, so you can authenticate once and run several scripts without entering credentials again.

## Requirements

- Platform: Cisco Crosswork Network Controller or Crosswork Planning 7.2 or later
- Python 3.6 or later

### Python Packages

| Package | Purpose |
|---------|---------|
| `requests` | HTTP client for the SSO REST API calls |
| `urllib3` | HTTP transport used by `requests`; imported directly only to suppress `InsecureRequestWarning` when `-k` / `--insecure` is used |
| `argparse` | Command-line argument parsing |
| `getpass` | Password prompt without echo |
| `base64` | Decoding JWT header and payload segments |
| `json` | Parsing API responses and JWT segments |
| `os` | File paths, permissions, and environment variables |
| `datetime` | Converting JWT timestamps to readable dates |

### Installation

```bash
pip install requests urllib3
```

## About JWT Authentication in Crosswork

CNC REST APIs accept a JWT as a bearer token in the `Authorization` header. A JWT has three dot-separated, base64url-encoded parts:

| Part | Contents |
|------|----------|
| **Header** | Token type and signing algorithm |
| **Payload** | Claims such as subject (`sub`), issuer (`iss`), issued-at (`iat`), and expiry (`exp`) |
| **Signature** | Server signature that CNC uses to validate the token |

CNC issues the JWT through a two-step SSO exchange. `cw_get_jwt.py` performs that exchange and stores the result so other tools can call CNC APIs directly.

## Script Purpose

`cw_get_jwt.py` is a standalone command-line utility with two modes:

1. **Authenticate mode** (an IP is given): obtains a JWT from CNC and saves it to `~/.crosswork/<CW_IP>.jwt` or to a file you choose
2. **Decode mode** (no IP, `-f` given): reads a saved JWT and prints its header, payload, and a summary of key claims

This enables network operators and automation engineers to:

- Authenticate once and share the token across multiple scripts
- Keep passwords out of scripts and command history
- Check who a token belongs to and when it expires
- Import `get_jwt()` into other Python tools

## Script Architecture

### Authentication Flow

The script implements CNC's two-step SSO authentication process. Both endpoints are relative to the base URL `https://<CW_IP>:30603`:

```
┌─────────────────┐  1. POST username/password  ┌──────────────────────────────┐
│  cw_get_jwt.py  │────────────────────────────▶│  /crosswork/sso/v1/tickets   │
│                 │◀────────────────────────────│                              │
│                 │     Returns: TGT ticket     └──────────────────────────────┘
│                 │
│                 │  2. POST TGT + service      ┌──────────────────────────────┐
│                 │────────────────────────────▶│  /crosswork/sso/v2/tickets/  │
│                 │◀────────────────────────────│  jwt                         │
│                 │     Returns: JWT token      └──────────────────────────────┘
└─────────────────┘
         │
         ▼
┌──────────────────────────┐
│  Local File (mode 0600)  │
│ ~/.crosswork/<CW_IP>.jwt │
└──────────────────────────┘
```

**Step 1**: Submit credentials to `/crosswork/sso/v1/tickets` to obtain a Ticket Granting Ticket (TGT)
**Step 2**: Exchange the TGT for a JWT at `/crosswork/sso/v2/tickets/jwt`, using `https://<CW_IP>:30603/app-dashboard` as the service URL

### Decode Flow

```
┌─────────────────┐     ┌─────────────────────────────────┐
│  JWT File       │────▶│  cw_get_jwt.py (decode mode)    │
│  (-f FILENAME)  │     │  base64url-decode header and    │
│                 │     │  payload, print key claims      │
└─────────────────┘     └─────────────────────────────────┘
```

Decode mode runs locally and does not contact CNC.

## Command Line Interface

### Usage

```bash
# Authenticate and save a JWT
python cw_get_jwt.py --ip <CW_IP> [-u <USER>] [-p <PASS>] [-f <OUTPUT_FILE>] [-k]

# Decode an existing JWT
python cw_get_jwt.py -f <JWT_FILE>
```

### Arguments

| Argument | Short | Required | Description |
|----------|-------|----------|-------------|
| `--ip` | | Authenticate mode | CNC IP address or hostname. Omit it to use decode mode. |
| `IP` (positional) | | No | Legacy positional form of `--ip`. Supplying both forms is an error. |
| `--username` | `-u` | No | CNC username. Falls back to `CW_USERNAME`, then a prompt. |
| `--password` | `-p` | No | CNC password. Falls back to `CW_PASSWORD`, then a hidden prompt. |
| `--filename` | `-f` | Decode mode | In authenticate mode, the file to write the token to (default: `~/.crosswork/<CW_IP>.jwt`). In decode mode, the token file to read. |
| `--insecure` | `-k` | No | Disable SSL certificate verification. Prints a warning to stderr. |

### Mode Selection

| Arguments given | Mode | Behavior |
|-----------------|------|----------|
| IP (with or without `-f`) | Authenticate | Calls CNC SSO and writes the token |
| `-f` only | Decode | Reads and decodes the token file |
| Neither | Error | `either provide an IP to authenticate, or -f FILENAME to decode a JWT` |

### Credential Resolution

Credentials are resolved in this order:

1. Command-line flag (`-u`, `-p`)
2. Environment variable (`CW_USERNAME`, `CW_PASSWORD`)
3. Interactive prompt (the password prompt does not echo)

The password is never required on the command line. Prefer the prompt or environment variables, because `-p` values are visible in the process list and shell history.

### Example Usage

```bash
# Authenticate and be prompted for username and password
python cw_get_jwt.py --ip 198.18.134.219

# Supply the username and be prompted for the password
python cw_get_jwt.py --ip 198.18.134.219 -u admin

# Use environment variables
export CW_USERNAME=<USER>
export CW_PASSWORD=<PASS>
python cw_get_jwt.py --ip 198.18.134.219

# Save the token to a custom file
python cw_get_jwt.py --ip 198.18.134.219 -u admin -f lab.jwt

# Lab system with a self-signed certificate
python cw_get_jwt.py --ip 198.18.134.219 -u admin -k

# Legacy positional IP form
python cw_get_jwt.py 198.18.134.219 -u admin

# Decode a saved token
python cw_get_jwt.py -f ~/.crosswork/198.18.134.219.jwt
```

### Sample Output

Authenticate mode:

```
Authenticating to 198.18.134.219...
JWT written to /Users/<you>/.crosswork/198.18.134.219.jwt
```

Decode mode (values are illustrative):

```
=== JWT Header ===
{
  "alg": "RS256",
  "typ": "JWT"
}

=== JWT Payload ===
{
  "sub": "admin",
  "iss": "<issuer>",
  "iat": 1791810000,
  "exp": 1791838800
}

=== Token Details ===
  Subject:    admin
  Issuer:     <issuer>
  Issued At:  2026-10-12T13:00:00+00:00 (1791810000)
  Expires:    2026-10-12T21:00:00+00:00 (1791838800) [expires in 7:59:12]
  Signature:  present
```

The `Token Details` section shows only the claims present in the token: `sub`, `iss`, `aud`, `iat`, `exp`, `nbf`, `scope`, and `roles`. An expired token is marked `[EXPIRED]`.

## Key Functions

### `get_jwt(ip, username, password, verify_ssl=True)`

Top-level function that other scripts can import. Creates an HTTP session, obtains a ticket, and exchanges it for a JWT:

```python
from cw_get_jwt import get_jwt

token = get_jwt("198.18.134.219", username, password)
headers = {"Authorization": f"Bearer {token}"}
```

It raises `CrossworkAuthError` on API failures and `requests.RequestException` on network failures. It does not print or exit.

### `get_ticket(session, base_url, username, password)`

Posts form-encoded credentials and returns the TGT.

**Endpoint**: `POST /crosswork/sso/v1/tickets`

The ticket is read from the `Location` header if present, otherwise from a JSON `ticket` field, otherwise from the plain-text body.

### `get_token(session, base_url, ticket)`

Exchanges the TGT for a JWT.

**Endpoint**: `POST /crosswork/sso/v2/tickets/jwt`

The token is read from a JSON `token` field, otherwise from the plain-text body.

### `decode_jwt(token)`

Decodes and prints the header, payload, and a summary of key claims. It does not verify the signature.

### `main()`

Orchestrates the workflow:
1. Parse command-line arguments and resolve the IP from `--ip` or the positional form
2. If no IP is given, decode the file named by `-f` and exit
3. Resolve credentials
4. Create `~/.crosswork` with mode `0700`
5. Authenticate to CNC
6. Write the token with mode `0600`

## API Details

All endpoints are relative to the base URL `https://<CW_IP>:30603`. Every request uses a 20-second timeout.

### Step 1: Get Ticket

| Property | Value |
|----------|-------|
| **Method** | POST |
| **Endpoint** | `/crosswork/sso/v1/tickets` |
| **Content-Type** | `application/x-www-form-urlencoded` |
| **Body** | `username=<USER>&password=<PASS>` |
| **Response** | TGT in `Location` header, JSON `ticket`, or plain text |

### Step 2: Get JWT

| Property | Value |
|----------|-------|
| **Method** | POST |
| **Endpoint** | `/crosswork/sso/v2/tickets/jwt` |
| **Content-Type** | `application/x-www-form-urlencoded` |
| **Body** | `tgt=<TICKET>&service=https://<CW_IP>:30603/app-dashboard` |
| **Response** | JWT in JSON `token` or plain text |

## Error Handling

Errors are printed to stderr as `Error: <message>`, and the script exits with status 1.

| Error Type | Scenario | Message |
|------------|----------|---------|
| `CrossworkAuthError` | Non-2xx SSO response | `<step> returned HTTP <code> <reason>: <body>` (body truncated to 500 characters) |
| `CrossworkAuthError` | No ticket or token in response | `Could not extract ticket from response: ...` or `Could not extract token from response: ...` |
| `requests.RequestException` | Network unreachable, timeout, or SSL failure | The underlying `requests` error |
| `OSError` | Token file cannot be written | The underlying OS error |
| Missing file | Decode mode with a path that does not exist | `file not found: <path>` |
| Invalid JWT | Decode mode with a malformed token | `not a valid JWT (expected 2 or 3 dot-separated parts)` |
| Argument error | Both IP forms, or neither IP nor `-f` | `argparse` usage error |

### Example Error Output

```bash
# Authentication failure
Authenticating to 198.18.134.219...
Error: get_ticket returned HTTP 401 Unauthorized: <response body>

# Self-signed certificate without -k
Authenticating to 198.18.134.219...
Error: HTTPSConnectionPool(host='198.18.134.219', port=30603): ... CERTIFICATE_VERIFY_FAILED ...

# Decode a missing file
Error: file not found: missing.jwt
```

## Security Considerations

### SSL/TLS Verification

SSL certificate verification is **enabled by default**. Use `-k` / `--insecure` only for lab systems with self-signed certificates. When it is set, the script suppresses `InsecureRequestWarning` and prints:

```
WARNING: SSL verification disabled
```

> **Production Note**: Install a CA-signed certificate on CNC, or add the CNC CA to your system trust store, instead of using `-k`.

### Credential Handling

- No credentials are stored in the script
- The password can come from `CW_PASSWORD` or a hidden prompt, so it never needs to appear on the command line
- The password is not written to disk or printed

### Token Storage

- `~/.crosswork` is created with mode `0700`
- Token files are written with mode `0600` (owner read/write only)
- A token is a bearer credential: anyone with the file can call CNC APIs as that user until it expires. Do not commit token files or share them.

### Decoding Is Not Validation

`decode_jwt` reads the claims but does not verify the signature. Use decode output to check the subject and expiry, not to prove that a token is genuine.

### AAA Session Limits

Each authentication creates a CNC session. If you authenticate frequently or from multiple sources:

- Navigate to **Admin > AAA Settings** in CNC
- Ensure **No. of parallel sessions** is higher than **No. of parallel sessions per user**
- Recommended: Reuse the saved token instead of authenticating on every run, and use a dedicated service account for automation

## Use Cases

### 1. Authenticate Once for Multiple Scripts

These scripts read `~/.crosswork/<CW_IP>.jwt` when no credentials are given: `get_plan`, `get_plan_cp_cw`, `get_topo`, `get_inventory`, `get_sr_policies`, `get_ecmp_paths`, `tag_manage`, `lcm_notify`, `sim_analysis`, `mate_convert`, and `cp_archive`.

```bash
python cw_get_jwt.py --ip 198.18.134.219 -u admin
python get_plan.py --ip 198.18.134.219 -f network.pln
```

### 2. Check Token Expiry

```bash
python cw_get_jwt.py -f ~/.crosswork/198.18.134.219.jwt | grep Expires
```

### 3. CI/CD Integration

Supply credentials from pipeline secrets through environment variables:

```bash
export CW_USERNAME="$CNC_USER"
export CW_PASSWORD="$CNC_PASS"
python cw_get_jwt.py --ip "$CW_IP" -f "$RUNNER_TEMP/cnc.jwt"
```

### 4. Multiple CNC Instances

Each IP gets its own default token file:

```bash
python cw_get_jwt.py --ip 10.0.1.100 -u admin   # ~/.crosswork/10.0.1.100.jwt
python cw_get_jwt.py --ip 10.0.2.100 -u admin   # ~/.crosswork/10.0.2.100.jwt
```

### 5. Use in Your Own Python Tools

```python
import os
from cw_get_jwt import get_jwt

token = get_jwt(os.environ["CW_IP"], os.environ["CW_USERNAME"], os.environ["CW_PASSWORD"])
```

## Troubleshooting

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| Connection refused or timeout | CNC not reachable | Verify the IP, port 30603, and firewall rules |
| `CERTIFICATE_VERIFY_FAILED` | Self-signed or untrusted certificate | Trust the CNC CA, or use `-k` in a lab |
| `get_ticket returned HTTP 401` | Invalid credentials | Verify the username and password |
| `get_token returned HTTP 4xx` | Ticket rejected or expired | Rerun the script to get a fresh ticket |
| Other scripts return 401 with a saved token | Token expired | Decode the token to check `Expires`, then rerun `cw_get_jwt.py` |
| Python traceback in decode mode | File is not a JWT (for example, an HTML error page) | Re-authenticate to replace the file |

### Debugging Tips

1. **Test connectivity**:
   ```bash
   curl -k https://<CW_IP>:30603/crosswork/sso/v1/tickets
   ```

2. **Check the saved token**:
   ```bash
   ls -l ~/.crosswork/
   python cw_get_jwt.py -f ~/.crosswork/<CW_IP>.jwt
   ```

3. **Check CNC logs** for SSO errors on the CNC server

## References

- [Cisco Crosswork Network Controller Documentation](https://www.cisco.com/c/en/us/support/cloud-systems-management/crosswork-network-controller/series.html)
- [Crosswork Network Controller API Documentation](https://developer.cisco.com/docs/crosswork/)
- [RFC 7519: JSON Web Token (JWT)](https://www.rfc-editor.org/rfc/rfc7519)

---

*Document Version: 1.0*  
*Script: cw_get_jwt.py*  
*Platform: Cisco Crosswork Network Controller or Crosswork Planning 7.2 or later*
