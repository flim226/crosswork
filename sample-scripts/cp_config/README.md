# cp_config

Get or set collector configuration for Cisco Crosswork Planning.

Connection options come first, then exactly one of `--get` or `--set`.

## Requirements

- Platform: Cisco Crosswork Planning 8.0.0 or later
- Python 3.10+
- `requests` library (`pip install requests`)

## Usage

### Get the collector configuration

```bash
python cp_config.py --ip <CW_IP> --get <FILE>
```

The file holds the full envelope from `GET /cp/apiGateway/collector/v1/configurations`: SNMP groups, auth groups, node profiles, agents, networks, and schedulers. A count for each list is printed after the write.

The file contains encrypted SNMP and login fields returned by the controller, so it is written with owner-only permissions (`0600`).

```bash
python cp_config.py --ip <CW_IP> -k --get cp-configurations.json
```

### Set the collector configuration

```bash
python cp_config.py --ip <CW_IP> --set <FILE>
```

Pass a file written by `--get`, or a JSON object holding a subset of its lists. Lists omitted from the file are left untouched. Objects that already exist on the controller, matched by name, are never modified.

A new network is created from the collectors listed for it, so the file can describe part of a collection, for example a single `TOPO_IGP` collector. A scheduler is created only for a network created by the same `--set` run. When a scheduler sets `active` to `false`, it is paused after create. If a scheduler task archives plans and the archive is missing, the archive is created first.

```bash
python cp_config.py --ip <CW_IP> -k --set cpconfig-test-subset.json
```

The run prints a JSON summary of unchanged and created objects.

### Limits of `--set`

The public API cannot create SNMP groups, auth groups, node profiles, or traffic agents. If the file names one that is not on the controller, `--set` fails before it writes anything. A new network must reference an existing node profile.

Collectors that reference a traffic agent are rejected, because attaching an existing agent retargets the current traffic poller.

If a network or scheduler create fails partway, the incomplete network is removed.

## Options

| Flag | Description |
|------|-------------|
| `--ip` | Crosswork controller IP or hostname |
| `--port` | HTTPS port (default: `30603`) |
| `--username`, `-u` | Username (or set `CW_USERNAME` env var) |
| `--password`, `-p` | Password (or set `CW_PASSWORD` env var; prompts if omitted) |
| `--jwt` | Path to a JWT file (default when no credentials are given: `~/.crosswork/<ip>.jwt`) |
| `--get FILE` | Write the collector configuration to `FILE` |
| `--set FILE` | Create objects from `FILE` that are not already on the controller |
| `-k`, `--insecure` | Disable TLS certificate verification |
| `--timeout` | HTTP timeout in seconds (default: 60) |

`--get` and `--set` cannot be combined. Connections use TLS 1.3 only.

## Credential Resolution

When `--username`, `--password`, and `--jwt` are omitted, a non-empty `~/.crosswork/<ip>.jwt` (the file written by `cw_get_jwt.py`) is used. Otherwise credentials are resolved in order: **`--jwt` → CLI flags → environment variables → interactive prompt**.

A password passed with `--password` is visible in the process list. Prefer the environment variable or the prompt.

```bash
export CW_USERNAME=admin
read -rs CW_PASSWORD && export CW_PASSWORD
python cp_config.py --ip 10.0.0.1 --get cp-configurations.json
```
