# Retrieve SR Policies

`get_sr_policies.py` retrieves Segment Routing policy operational data from Cisco Crosswork Network Controller (CNC).

It calls:

```text
GET /crosswork/nbi/optima/v2/restconf/data/cisco-crosswork-segment-routing-policy:sr-policies
```

The complete RESTCONF JSON response is written to standard output, so it can be redirected to a file or piped to `jq`.

## Usage

```bash
python get_sr_policies.py --ip <CNC_HOSTNAME_OR_IP> [options]
```

| Option | Description |
|---|---|
| `--ip` | Required CNC IP address or hostname. |
| `--port` | CNC HTTPS port; defaults to `30603`. |
| `-u`, `--username` | CNC username; alternatively set `CW_USERNAME`. |
| `-p`, `--password` | CNC password; alternatively set `CW_PASSWORD`. |
| `-j`, `--jwt` | Path to a JWT file; skips username/password authentication. |
| `-k`, `--insecure` | Disable TLS certificate verification. |
| `--timeout` | HTTP timeout in seconds; defaults to `20`. |

If `--username`, `--password`, and `--jwt` are all omitted, the script uses the default JWT file created by `cw_get_jwt.py`:

```text
~/.crosswork/<ip>.jwt
```

If that file does not exist, credentials are resolved from CLI options, then `CW_USERNAME` / `CW_PASSWORD`, then interactive prompts.

## Examples

```bash
# Use the saved JWT automatically
python get_sr_policies.py --ip tme7-cnc.cisco.com -k > sr-policies.json

# Supply a JWT explicitly
python get_sr_policies.py --ip 192.0.2.10 --jwt ~/.crosswork/192.0.2.10.jwt -k

# Authenticate with environment variables
CW_USERNAME=admin CW_PASSWORD=secret python get_sr_policies.py --ip 192.0.2.10 -k
```
