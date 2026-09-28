# cp_archive

Import or export plan files for a Cisco Crosswork Planning archive.

Connection options come first, then exactly one of `--import` or `--export`.

## Prerequisites

- Python 3.10+
- `requests` library (`pip install requests`)

## Usage

### Export plans from an archive

```bash
python cp_archive.py --ip <CNC_IP> --export --archive <NAME> --last 10
```

Plans are written to `./<CNC_IP>-<NAME>-export` by default, with a `download_manifest.json` listing each file.

Select a window with one of `--from`/`--to`, `--last`, or `--lastdays`. Omit all three to export every plan in the archive. Dates are UTC. A `--to` date without a time covers that whole UTC day.

```bash
python cp_archive.py --ip <CNC_IP> --export --archive sr_topo_igp --lastdays 7
python cp_archive.py --ip <CNC_IP> --export --archive sr_topo_igp --from 2026-09-17 --to 2026-09-25
python cp_archive.py --ip <CNC_IP> --export --archive sr_topo_igp --last 10 -o ./sr_topo_igp-export
```

### Import plans into an archive

```bash
python cp_archive.py --ip <CNC_IP> --import --archive <NAME> plan_a.pln plan_b.pln
```

Pass plan files, directories of `*.pln` files, or both. Directories are not searched recursively. If the archive is not on the server, it is created first.

Filenames of the form `YYYYMMDD_HHMM_UTC.pln` keep that minute as the archive timestamp.

```bash
python cp_archive.py --ip <CNC_IP> --import --archive sr_topo_igp sr_topo_igp/
```

## Options

| Flag | Description |
|------|-------------|
| `--ip` | Crosswork controller IP or hostname |
| `--port` | HTTPS port (default: `30603`) |
| `--username`, `-u` | Username (or set `CW_USERNAME` env var) |
| `--password`, `-p` | Password (or set `CW_PASSWORD` env var; prompts if omitted) |
| `--jwt` | Path to a JWT file (default when no credentials are given: `~/.crosswork/<ip>.jwt`) |
| `--import` | Import plan files into `--archive` |
| `--export` | Export plan files from `--archive` |
| `--archive` | Archive name (1–100 characters: `A-Z`, `a-z`, `0-9`, `_ @ . & + -`) |
| `-k`, `--insecure` | Disable TLS certificate verification |
| `--timeout` | HTTP timeout in seconds (default: 60 for import, 120 for export) |
| `--workers` | Parallel uploads or downloads (default: 8) |
| `--from` | Export window start, UTC (`YYYY-MM-DD` or RFC3339) |
| `--to` | Export window end, UTC (`YYYY-MM-DD` or RFC3339) |
| `--last` | Export the newest *N* plan files |
| `--lastdays` | Export plans from the most recent *N* days (UTC) |
| `-o`, `--output` | Export directory (default: `./IP-ARCHIVE-export`) |
| `FILE_OR_DIR` | Plan file or directory of `*.pln` files (required with `--import`) |

`--from`/`--to`, `--last`, and `--lastdays` cannot be combined.

## Credential Resolution

When `--username`, `--password`, and `--jwt` are omitted, a non-empty `~/.crosswork/<ip>.jwt` (the file written by `cw_get_jwt.py`) is used. Otherwise credentials are resolved in order: **`--jwt` → CLI flags → environment variables → interactive prompt**.

```bash
export CW_USERNAME=admin
export CW_PASSWORD=secret
python cp_archive.py --ip 10.0.0.1 --export --archive sr_topo_igp --last 10
```
