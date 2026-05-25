# cw_get_jwt

Obtain or decode a JWT token from Cisco Crosswork Network Controller.

## Prerequisites

- Python 3.6+
- `requests` library (`pip install requests`)

## Usage

### Authenticate and save a JWT

```bash
python cw_get_jwt.py <CNC_IP> -u <username> -p <password>
```

The token is saved to `~/.crosswork/<ip>.jwt` by default.

### Decode an existing JWT

```bash
python cw_get_jwt.py -f ~/.crosswork/10.0.0.1.jwt
```

## Options

| Flag | Description |
|------|-------------|
| `ip` | CNC IP address or hostname (omit to decode only) |
| `-u, --username` | Username (or set `CW_USERNAME` env var) |
| `-p, --password` | Password (or set `CW_PASSWORD` env var; prompts if omitted) |
| `-f, --filename` | File to save token to or decode from |
| `-k, --insecure` | Disable SSL certificate verification |

## Credential Resolution

Credentials are resolved in order: **CLI flags → environment variables → interactive prompt**.

```bash
export CW_USERNAME=admin
export CW_PASSWORD=secret
python cw_get_jwt.py 10.0.0.1
```
