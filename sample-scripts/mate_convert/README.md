# mate_convert.py

A wrapper that executes the `mate_convert` CLI tool as a Job Manager job, submitted through the Crosswork Planning 8.0 REST API. It converts plan files between `.txt`, `.pln`, and `.db` and between schema or release versions.

## Overview

Plan files move between text, binary, and database formats, and between Crosswork Planning schema or release versions. That conversion is performed by the `mate_convert` CLI on the Design engine. This script wraps that tool for use from a workstation: it stages the source plan in Design userspace, submits a Job Manager job over the CP 8.0 REST API (`jobType` `CLI_JOB`, `script=mate_convert`), polls until the job finishes, and writes the converted file locally.

Tool options match Crosswork Planning 8.0 Build 385 `mate_convert -help`. The engine injects `-plan-file` and `-out-file` from the staged plan (`inPlanFileId`) and `jobConfig.outFile`. Every other flag is passed in `jobConfig.options`. Omitting a flag leaves the tool default in effect.

Credentials are never stored in the script. When `--username`, `--password`, and `--jwt` are all omitted, the script uses `~/.crosswork/<ip>.jwt` if that file exists (the path `cw_get_jwt.py` writes). Otherwise it uses `--jwt`, the CLI flags, `CW_USERNAME` / `CW_PASSWORD`, or an interactive prompt.

## Requirements

- Python 3.10+
- `requests` (`pip install requests`)
- Crosswork Planning 8.0, with the Design REST API reachable (HTTPS port `30603` by default)
- A local plan file, or a plan already stored in Design userspace as a `PLANFILE` with the same basename

## How It Works

1. **Resolve credentials** — Loads a JWT from `--jwt` or from `~/.crosswork/<ip>.jwt`, or authenticates with username and password through Crosswork SSO and exchanges the ticket for a JWT.
2. **Resolve the source plan** — If `-plan-file` is a local file, imports it into Design userspace as a `PLANFILE`. Otherwise looks up that basename in userspace. The script exits with an error when the path is not a local file and that basename is not a userspace `PLANFILE`. Stdin (`-plan-file -`) is not supported.
3. **Stage an options file** — When `-options-file` names a local file, imports it as category `OTHERS` so the job can read it. The flag sent to `mate_convert` is the file's basename.
4. **Stage job input** — Calls `GET /cp/apiGateway/design/v1/jobs/input` with the plan file id (and the options file id, when present) and reads the returned `fsId`.
5. **Submit the Job Manager job** — Posts the job through the CP 8.0 REST API. The body uses `jobType` `CLI_JOB`. `jobConfig` selects `script=mate_convert`, sets `outFile` from the output basename, and carries the remaining `mate_convert` flags in `options`.
6. **Poll until completion** — Searches jobs by id until the status is `COMPLETED`, `FAILED`, `ABORTED`, or `CANCELLED`, or until `--timeout` elapses.
7. **Export the result** — On `COMPLETED`, downloads the job result tar (`outputTarFileId`) and extracts the member whose basename matches the engine output file.
8. **Write the converted plan** — Saves that member to `-out-file`. `-out-file -` writes the bytes to stdout.

Progress is printed to stderr:

```
Using JWT from /Users/operator/.crosswork/198.18.134.229.jwt
Importing plan us_wan.txt to userspace...
Staging job input (fileId=42)...
Submitting CLI_JOB mate_convert (fsId=fs-9c1e)...
Submitted job 1842
Job 1842 status: COMPLETED
Exporting result tar fileId=9102...
Wrote converted plan to us_wan.pln (4821104 bytes)
```

A failed or timed-out job exits with status 1 and an error that includes the job id, final status, and `jobResult` when the API returned one.

## Usage

```bash
# Text plan to binary .pln, using the JWT written by cw_get_jwt.py
python mate_convert.py --ip "$CW_HOST" -k \
  -plan-file us_wan.txt -out-file /tmp/us_wan.pln

# Keep the plan's existing version instead of converting to current
python mate_convert.py --ip 198.18.134.229 -k \
  -plan-file us_wan.txt -out-file us_wan.pln -to-version keep

# Convert to a specific release, authenticating with an explicit JWT file
python mate_convert.py --ip 198.18.134.229 \
  --jwt ~/.crosswork/198.18.134.229.jwt \
  -plan-file us_wan.txt -out-file us_wan_7.9.txt -to-version 7.9
```

The output extension selects the format (`.txt`, `.pln`, or `.db`). When `-out-file` is `-`, the engine member is `<plan-stem>_converted<plan-suffix>` and those bytes are written to stdout.

A plan that is already in Design userspace can be named by basename. The script imports a file only when that path exists locally.

```bash
python mate_convert.py --ip "$CW_HOST" \
  -plan-file us_wan.pln -out-file ./us_wan.db
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
| `--job-name` | Job name (default: `CLI: mate_convert <plan> -> <out>`) |
| `--profile` | Engine profile (default: `4000m vCPU, 16Gi Mem`) |
| `--job-priority` | `LOW`, `MEDIUM`, or `HIGH` (default: `HIGH`) |
| `--poll-interval` | Seconds between status polls (default: `5`) |
| `--timeout` | Max seconds to wait for completion (default: `300`) |
| `--keep-tar` | Save the job result tar to this path |

`--profile` must match `<N>m vCPU, <N>Gi Mem`.

### mate_convert

These flags are forwarded in `jobConfig.options`. They are sent only when you pass them; otherwise the tool default applies.

| Flag | Description |
|------|-------------|
| `-plan-file` | Local plan path (imported) or userspace `PLANFILE` name. Required. Stdin `-` is rejected. |
| `-out-file` | Local output path. Extension selects `.pln`, `.txt`, or `.db`. Use `-` for stdout. Required. |
| `-to-version` | `current` (tool default, `7.10.-1` on Build 385), `keep`, or a release/schema id such as `7.9` |
| `-version-type` | How `-to-version` is interpreted: `release` (tool default) or `schema` |
| `-include-netint` | Include `<NetInt*>` tables (`true` or `false`; tool default `true`) |
| `-simple-txt-out-file` | Drop empty tables and columns from `.txt` output (`true` or `false`; tool default `false`) |
| `-verbosity` | Log verbosity from `1` to `60` (tool default `30`) |
| `-log-file` | Log file basename created on the engine |
| `-suppress-progress` | Suppress progress output (`true` or `false`; tool default `true`) |
| `-options-file` | Extra options file. A local file is imported and staged; the tool receives its basename. |
| `-no-global-options` | Skip the global options file (`true` or `false`) |

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
python mate_convert.py --ip "$CW_HOST" -plan-file us_wan.txt -out-file us_wan.pln
```

Obtain the default JWT file with `cw_get_jwt.py`. The token is then picked up automatically for that host.

## API reference

| Call | Purpose |
|------|---------|
| `POST /crosswork/sso/v1/tickets` | Exchange username and password for a ticket-granting ticket |
| `POST /crosswork/sso/v2/tickets/jwt` | Exchange the ticket for a JWT (`service` is `<base>/app-dashboard`) |
| `POST /cp/apiGateway/design/v1/file/import` | Upload the plan (category `PLANFILE`) or a local options file (category `OTHERS`) |
| `GET /cp/apiGateway/design/v1/file/search` | Find an existing userspace `PLANFILE` by exact `fileName` |
| `GET /cp/apiGateway/design/v1/jobs/input` | Stage `fileId` values and return `fsId` |
| `POST /cp/apiGateway/design/v1/jobs` | Submit the Job Manager job (`jobType` `CLI_JOB`) |
| `POST /cp/apiGateway/design/v1/jobs/search` | Poll `jobStatus` for the submitted job id |
| `GET /cp/apiGateway/design/v1/file/export` | Download the result tar by `outputTarFileId` |

The CP 8.0 submit body includes `jobName`, `jobType` (`CLI_JOB`), `fsId`, `jobConfig`, `inPlanFileId` (the stored plan file name), `requestedProfile`, and `jobPriority`. `jobConfig` is a JSON string:

```json
{
  "scriptType": "CLI",
  "script": "mate_convert",
  "outFile": "us_wan.pln",
  "options": "-to-version keep"
}
```

`options` is empty when no extra `mate_convert` flags are passed. `-plan-file` and `-out-file` are not placed in `options`; the engine supplies them.

## Notes

- TLS certificate verification is on unless `-k` / `--insecure` is set. Use `-k` only for lab controllers with a private or self-signed certificate. For a production controller, leave verification enabled so the client checks the server certificate.
- Passwords and tokens are read from a JWT file, environment variables, CLI flags, or a prompt. Do not commit them, and prefer `cw_get_jwt.py` plus the default `~/.crosswork/<ip>.jwt` path for repeat runs.
- Re-running against the same local plan imports that file into userspace again under the same basename.
- A job that ends in `FAILED`, `ABORTED`, or `CANCELLED`, or that completes without an `outputTarFileId`, is reported as an error. The converted file is written only after a `COMPLETED` job and a successful tar extract.
- `--timeout` bounds how long the script waits for the job. `--http-timeout` bounds each HTTP call. Increase `--timeout` for large plans.
- `--keep-tar` writes the result tar locally and still extracts the converted plan to `-out-file`.
