# import_igp.py

A wrapper that executes the `parse_igp` CLI tool as a Job Manager job, submitted through the Crosswork Planning 8.0 REST API. It builds a plan file from an OSPF or IS-IS link-state database captured from a router.

## Overview

A plan file can be seeded from the IGP link-state database of a single router: the routers, adjacencies, interfaces, metrics, and IP addressing advertised in OSPF or IS-IS. That parsing is performed by the `parse_igp` CLI on the Design engine. This script wraps that tool for use from a workstation: it stages the database file in Design userspace, submits a Job Manager job over the CP 8.0 REST API (`jobType` `CLI_JOB`, `script=parse_igp`), polls until the job finishes, and writes the generated plan locally.

Tool options match `parse_igp -help`. The engine injects `-plan-file` from the staged plan (`inPlanFileId`) and `-out-file` from `jobConfig.outFile`. `-database-file` and every other flag are passed in `jobConfig.options`. Omitting a flag leaves the tool default in effect.

Credentials are never stored in the script. When `--username`, `--password`, and `--jwt` are all omitted, the script uses `~/.crosswork/<ip>.jwt` if that file exists (the path `cw_get_jwt.py` writes). Otherwise it uses `--jwt`, the CLI flags, `CW_USERNAME` / `CW_PASSWORD`, or an interactive prompt.

## Requirements

- Platform: Cisco Crosswork Planning 8.0.0 or later (tested on 8.0.0)
- Python 3.10+ (3.10 is in security-only support through October 2026)
- `requests` (`pip install requests`)
- Design REST API reachable (HTTPS port `30603` by default)
- A database file captured from a router, either local or already stored in Design userspace as an `OTHERS` file with the same basename

## Capturing the database

Save the output of one of these commands to a text file:

| Vendor | IGP | Command |
|--------|-----|---------|
| Cisco | OSPF | `show ip ospf database router` |
| Cisco | IS-IS | `show isis database verbose` |
| Juniper | OSPF | `show ospf database extensive` |
| Juniper | IS-IS | `show isis database extensive` |

`-igp-protocol` must match the database: `ospf`, `ospfv3`, `isis`, or `isisv6`.

## How It Works

1. **Resolve credentials** — Loads a JWT from `--jwt` or from `~/.crosswork/<ip>.jwt`, or authenticates with username and password through Crosswork SSO and exchanges the ticket for a JWT.
2. **Resolve the database** — If `-database-file` is a local file, imports it into Design userspace as category `OTHERS`. Otherwise looks up that basename in userspace. The script exits with an error when the path is not a local file and that basename is not a userspace `OTHERS` file. Stdin (`-database-file -`) is not supported.
3. **Resolve the base plan** — If `-plan-file` is given, it is imported (local file) or looked up (userspace `PLANFILE`), and `parse_igp` merges the database into it. If `-plan-file` is omitted, the script imports an empty plan named `<database-stem>_igp_base.txt`. A `CLI_JOB` without a staged plan fails with `Plan file does not exist`, so a base plan is always staged.
4. **Stage an options file** — When `-options-file` names a local file, imports it as category `OTHERS` so the job can read it. The flag sent to `parse_igp` is the file's basename.
5. **Stage job input** — Calls `GET /cp/apiGateway/design/v1/jobs/input` with the database, plan, and options file ids and reads the returned `fsId`.
6. **Submit the Job Manager job** — Posts the job through the CP 8.0 REST API. The body uses `jobType` `CLI_JOB`. `jobConfig` selects `script=parse_igp`, sets `outFile` from the output basename, and carries `-igp-protocol`, `-database-file` (the stored userspace name), and the remaining flags in `options`.
7. **Poll until completion** — Searches jobs by id until the status is `COMPLETED`, `FAILED`, `ABORTED`, or `CANCELLED`, or until `--timeout` elapses. A poll that times out or loses its connection is retried until `--timeout`.
8. **Export the result** — On `COMPLETED`, downloads the job result tar (`outputTarFileId`) and extracts the member whose basename matches the engine output file.
9. **Write the plan** — Saves that member to `-out-file`. `-out-file -` writes the bytes to stdout.

Progress is printed to stderr:

```
Using JWT from /Users/operator/.crosswork/198.18.134.229.jwt
Importing database lab_ospf_db.txt to userspace...
Importing empty base plan lab_ospf_db_igp_base.txt to userspace...
Staging job input (fileId=18,19)...
Submitting CLI_JOB parse_igp (fsId=admin20261107041229852)...
Submitted job 3
Job 3 status: QUEUED
Job 3 status: COMPLETED
Exporting result tar fileId=20...
Wrote plan to /tmp/lab_ospf_plan.txt (39132 bytes)
```

A failed or timed-out job exits with status 1 and an error that includes the job id, final status, and `jobResult` when the API returned one.

## Usage

```bash
# OSPF area 0 from a Cisco router, using the JWT written by cw_get_jwt.py
python import_igp.py --ip "$CW_HOST" -k \
  -igp-protocol ospf -database-file lab_ospf_db.txt -out-file /tmp/lab_ospf_plan.txt

# IS-IS level 1 and 2 with Segment Routing data, written as a binary .pln
python import_igp.py --ip 198.18.134.229 -k \
  -igp-protocol isis -database-file isis_db.txt -out-file isis_plan.pln \
  -isis-level both -get-segments true

# All OSPF areas merged into an existing plan, tagging every discovered node
python import_igp.py --ip 198.18.134.229 \
  --jwt ~/.crosswork/198.18.134.229.jwt \
  -igp-protocol ospf -database-file ospf_db.txt -area all \
  -plan-file us_wan.txt -out-file us_wan_ospf.txt -add-node-tag igp,lab
```

The output extension selects the format (`.txt`, `.pln`, or `.db`). When `-out-file` is `-`, the engine member is `<database-stem>_igp.txt` and those bytes are written to stdout.

A database that is already in Design userspace can be named by basename. The script imports a file only when that path exists locally.

```bash
python import_igp.py --ip "$CW_HOST" \
  -igp-protocol isis -database-file isis_db.txt -out-file ./isis_plan.txt
```

## Options

### Crosswork connection

| Flag | Description |
|------|-------------|
| `--ip` | Controller IP or hostname (or set `CW_HOST`) |
| `--port` | HTTPS port (default: `30603`) |
| `--username`, `-u` | Username (or set `CW_USERNAME`) |
| `--password`, `-p` | Password (or set `CW_PASSWORD`; prompts if omitted) |
| `--jwt`, `-j` | Path to a JWT file |
| `--http-timeout` | HTTP timeout in seconds (default: `30`) |
| `-k`, `--insecure` | Disable TLS certificate verification (lab only) |

### Job control

| Flag | Description |
|------|-------------|
| `--job-name` | Job name (default: `CLI: parse_igp <protocol> <database> -> <out>`) |
| `--profile` | Engine profile (default: `4000m vCPU, 16Gi Mem`) |
| `--job-priority` | `LOW`, `MEDIUM`, or `HIGH` (default: `HIGH`) |
| `--poll-interval` | Seconds between status polls (default: `5`) |
| `--timeout` | Max seconds to wait for completion (default: `300`) |
| `--keep-tar` | Save the job result tar to this path |

`--profile` must match `<N>m vCPU, <N>Gi Mem`.

### parse_igp

These flags are forwarded in `jobConfig.options`. Except for `-igp-protocol` and `-database-file`, they are sent only when you pass them; otherwise the tool default applies.

| Flag | Description |
|------|-------------|
| `-igp-protocol` | IGP of the database: `ospf`, `ospfv3`, `isis`, or `isisv6`. Required. |
| `-database-file` | Local database file (imported) or userspace `OTHERS` name. Required. Stdin `-` is rejected. |
| `-out-file` | Local output path. Extension selects `.pln`, `.txt`, or `.db`. Use `-` for stdout. Required. |
| `-plan-file` | Existing plan to merge with: local path (imported) or userspace `PLANFILE` name. When omitted, an empty base plan is staged. |
| `-isis-level` | IS-IS level to read: `1`, `2`, or `both` (tool default `2`). IS-IS protocols only. |
| `-area` | Area to read: a number, a dotted-quad area id, or `all` (tool default `0`) |
| `-lic-product` | Base product license to check out: `Collector` (tool default) or `Design` |
| `-use-dns` | Resolve router IP addresses with DNS (`true` or `false`; tool default `false`) |
| `-find-ip-manage` | Find management IP addresses in the database (`true` or `false`; tool default `true`) |
| `-create-subnet-links` | Create links by subnet matching even when adjacencies are missing (`true` or `false`; tool default `true`) |
| `-get-segments` | Collect Segment Routing data (`true` or `false`; tool default `false`). IS-IS on Cisco IOS XR only. |
| `-add-node-tag` | Comma-separated tags appended to every discovered node |
| `-simple-txt-out-file` | Drop empty tables and columns from `.txt` output (`true` or `false`; tool default `false`) |
| `-verbosity` | Log verbosity from `1` to `60` (tool default `30`) |
| `-log-file` | Log file basename created on the engine |
| `-suppress-progress` | Suppress progress output (`true` or `false`; tool default `true`) |
| `-options-file` | Extra options file. A local file is imported and staged; the tool receives its basename. |
| `-no-global-options` | Skip the global options file (`true` or `false`) |

`-database-dir` is not supported over REST. Pass a single `-database-file` per job.

The script rejects `-isis-level` and `-get-segments true` with an OSPF protocol before contacting the controller.

`-log-file` is not downloaded on its own. Use `--keep-tar` when you need the full result archive.

## Credential resolution

Resolution order:

1. `--jwt`, when that flag is set.
2. `~/.crosswork/<ip>.jwt`, when `--username`, `--password`, and `--jwt` are all omitted and that file exists and is non-empty.
3. `--username` / `--password`.
4. `CW_USERNAME` / `CW_PASSWORD`.
5. An interactive username prompt and a hidden password prompt.

```bash
export CW_HOST=198.18.134.229
export CW_USERNAME=admin
export CW_PASSWORD='your-password'
python import_igp.py --ip "$CW_HOST" \
  -igp-protocol ospf -database-file ospf_db.txt -out-file ospf_plan.txt
```

Obtain the default JWT file with `cw_get_jwt.py`. The token is then picked up automatically for that host.

## API reference

| Call | Purpose |
|------|---------|
| `POST /crosswork/sso/v1/tickets` | Exchange username and password for a ticket-granting ticket |
| `POST /crosswork/sso/v2/tickets/jwt` | Exchange the ticket for a JWT (`service` is `<base>/app-dashboard`) |
| `POST /cp/apiGateway/design/v1/file/import` | Upload the database or options file (category `OTHERS`) and the base plan (category `PLANFILE`) |
| `GET /cp/apiGateway/design/v1/file/search` | Find an existing userspace file by exact `fileName` and category |
| `GET /cp/apiGateway/design/v1/jobs/input` | Stage `fileId` values and return `fsId` |
| `POST /cp/apiGateway/design/v1/jobs` | Submit the Job Manager job (`jobType` `CLI_JOB`) |
| `POST /cp/apiGateway/design/v1/jobs/search` | Poll `jobStatus` for the submitted job id |
| `GET /cp/apiGateway/design/v1/file/export` | Download the result tar by `outputTarFileId` |

The CP 8.0 submit body includes `jobName`, `jobType` (`CLI_JOB`), `fsId`, `jobConfig`, `inPlanFileId` (the stored base plan name), `requestedProfile`, and `jobPriority`. `jobConfig` is a JSON string:

```json
{
  "scriptType": "CLI",
  "script": "parse_igp",
  "outFile": "lab_ospf_plan.txt",
  "options": "-igp-protocol ospf -database-file lab_ospf_db.txt"
}
```

`-plan-file` and `-out-file` are not placed in `options`; the engine supplies them. `-database-file` carries the name userspace stored the file under, which can differ from the local name (for example `lab_ospf_db_2.txt` after repeated imports).

## Notes

- TLS certificate verification is on unless `-k` / `--insecure` is set. Use `-k` only for lab controllers with a private or self-signed certificate. For a production controller, leave verification enabled so the client checks the server certificate. The client allows TLS 1.2 and TLS 1.3 and does not follow HTTP redirects.
- Names sent to the engine (`-database-file`, `-plan-file`, `-out-file`, `-log-file`, `-options-file`) must be 1–255 characters of letters, digits, `.`, `_`, or `-`, and must start with a letter or digit. Paths are reduced to that basename. Node tags must be 1–64 characters from the same set. Uploads, the result tar, and the extracted plan are limited to 1 GiB. Empty files are not uploaded.
- `--password` prints a warning because the value is visible in the process list. A group- or world-readable JWT file also prints a warning. Prefer `chmod 600` on `~/.crosswork/<ip>.jwt`.
- Passwords and tokens are read from a JWT file, environment variables, CLI flags, or a prompt. Do not commit them, and prefer `cw_get_jwt.py` plus the default `~/.crosswork/<ip>.jwt` path for repeat runs.
- Each run imports the database, and the empty base plan when `-plan-file` is omitted, into userspace again. Userspace keeps earlier copies under suffixed names such as `lab_ospf_db_1.txt`; remove them from Design userspace when no longer needed.
- The empty base plan has no `Version` row, so `parse_igp` logs `No version information found in the file`. The warning is harmless; the output plan carries the engine's current version.
- A job that ends in `FAILED`, `ABORTED`, or `CANCELLED`, or that completes without an `outputTarFileId`, is reported as an error. The plan is written only after a `COMPLETED` job and a successful tar extract.
- `--timeout` bounds how long the script waits for the job. `--http-timeout` bounds each HTTP call. Increase either for large databases or a busy controller.
- `--keep-tar` writes the result tar locally and still extracts the plan to `-out-file`.
