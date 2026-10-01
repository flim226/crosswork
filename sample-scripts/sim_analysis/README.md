# sim_analysis.py

A wrapper that executes the `sim_analysis` CLI tool as a Job Manager job, submitted through the Crosswork Planning 8.0 REST API. It runs Simulation Analysis on a plan file and writes the output plan locally.

## Overview

Simulation Analysis evaluates a plan under failure scenarios: circuits, nodes, sites, ports, and related sets, including pairwise combinations. That analysis is performed by the `sim_analysis` CLI on the Design engine. This script wraps that tool for use from a workstation: it stages the source plan in Design userspace, submits a Job Manager job over the CP 8.0 REST API (`jobType` `CLI_JOB`, `script=sim_analysis`), polls until the job finishes, and writes the output plan locally.

Tool options match the CP 8.0 Simulation Analysis configuration (`JobConfigurations_SimulationAnalysis`) plus the generic CLI flags shared with other Design tools. The engine injects `-plan-file` and `-out-file` from the staged plan (`inPlanFileId`) and `jobConfig.outFile`. Every other flag is passed in `jobConfig.options`. Omitting a flag leaves the tool default in effect.

Credentials are never stored in the script. When `--username`, `--password`, and `--jwt` are all omitted, the script uses `~/.crosswork/<ip>.jwt` if that file exists (the path `cw_get_jwt.py` writes). Otherwise it uses `--jwt`, the CLI flags, `CW_USERNAME` / `CW_PASSWORD`, or an interactive prompt.

## Requirements

- Python 3.10+ (3.10 is in security-only support through October 2026)
- `requests` (`pip install requests`)
- Crosswork Planning 8.0, with the Design REST API reachable (HTTPS port `30603` by default)
- A local plan file, or a plan already stored in Design userspace as a `PLANFILE` with the same basename

## How It Works

1. **Resolve credentials** — Loads a JWT from `--jwt` or from `~/.crosswork/<ip>.jwt`, or authenticates with username and password through Crosswork SSO and exchanges the ticket for a JWT.
2. **Resolve the source plan** — If `-plan-file` is a local file, imports it into Design userspace as a `PLANFILE`. Otherwise looks up that basename in userspace. The script exits with an error when the path is not a local file and that basename is not a userspace `PLANFILE`. Stdin (`-plan-file -`) is not supported.
3. **Stage an options file** — When `-options-file` names a local file, imports it as category `OTHERS` so the job can read it. The flag sent to `sim_analysis` is the file's basename.
4. **Stage job input** — Calls `GET /cp/apiGateway/design/v1/jobs/input` with the plan file id (and the options file id, when present) and reads the returned `fsId`.
5. **Submit the Job Manager job** — Posts the job through the CP 8.0 REST API. The body uses `jobType` `CLI_JOB`. `jobConfig` selects `script=sim_analysis`, sets `outFile` from the output basename, and carries the remaining `sim_analysis` flags in `options`.
6. **Poll until completion** — Searches jobs by id until the status is `COMPLETED`, `FAILED`, `ABORTED`, or `CANCELLED`, or until `--timeout` elapses.
7. **Export the result** — On `COMPLETED`, downloads the job result tar (`outputTarFileId`) and extracts the member whose basename matches the engine output plan.
8. **Write the output plan** — Saves that member to `-out-file`. `-out-file -` writes the bytes to stdout.

`-result-file`, `-demand-paths`, `-demand-latencies`, and `-log-file` are created on the engine and returned inside the same tar. They are not extracted on their own. Pass `--keep-tar` to save the archive. When any of those flags is set and `--keep-tar` is omitted, the script prints a reminder on stderr before submitting the job.

Progress is printed to stderr:

```
Using JWT from /Users/operator/.crosswork/198.18.134.229.jwt
Importing plan us_wan.txt to userspace...
Staging job input (fileId=42)...
Submitting CLI_JOB sim_analysis (fsId=fs-9c1e)...
Submitted job 1842
Job 1842 status: COMPLETED
Exporting result tar fileId=9102...
Wrote simulation output to us_wan_sim.txt (4821104 bytes)
```

A failed or timed-out job exits with status 1 and an error that includes the job id, final status, and `jobResult` when the API returned one.

## Usage

```bash
# Circuit and node failures, using the JWT written by cw_get_jwt.py
python sim_analysis.py --ip "$CW_HOST" -k \
  -plan-file us_wan.txt -out-file /tmp/us_wan_sim.txt \
  -failure-sets nodes,circuits

# Pairwise node failures (one or two failure sets)
python sim_analysis.py --ip "$CW_HOST" -k \
  -plan-file us_wan.txt -out-file /tmp/us_wan_pairs.txt \
  -failure-sets nodes -pairwise-failure distinct-sets

# Cap the scenarios recorded per interface
python sim_analysis.py --ip 198.18.134.229 -k \
  -plan-file us_wan.txt -out-file tool_out.txt \
  -failure-sets nodes -max-num-failures-per-interface 10

# Record worst-case demand latency and keep the result tar
# (the simulation report is an engine file, retrieved from the tar)
python sim_analysis.py --ip 198.18.134.229 \
  --jwt ~/.crosswork/198.18.134.229.jwt \
  -plan-file us_wan.txt -out-file us_wan_sim.txt \
  -failure-sets circuits -wc-latency true \
  -result-file sim_report.txt --keep-tar /tmp/sim_result.tar
```

`-out-file` is the simulated plan (`.txt`, `.pln`, or `.db`). When `-out-file` is `-`, the engine member is `<plan-stem>_sim<plan-suffix>` and those bytes are written to stdout.

A plan that is already in Design userspace can be named by basename. The script imports a file only when that path exists locally.

```bash
python sim_analysis.py --ip "$CW_HOST" \
  -plan-file us_wan.pln -out-file ./us_wan_sim.pln \
  -failure-sets circuits
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
| `--job-name` | Job name (default: `CLI: sim_analysis <plan> -> <out>`) |
| `--profile` | Engine profile (default: `4000m vCPU, 16Gi Mem`) |
| `--job-priority` | `LOW`, `MEDIUM`, or `HIGH` (default: `HIGH`) |
| `--poll-interval` | Seconds between status polls (default: `5`) |
| `--timeout` | Max seconds to wait for completion (default: `1800`) |
| `--keep-tar` | Save the job result tar to this path |

`--profile` must match `<N>m vCPU, <N>Gi Mem`.

### sim_analysis

These flags are forwarded in `jobConfig.options`. They are sent only when you pass them; otherwise the tool default applies.

| Flag | Description |
|------|-------------|
| `-plan-file` | Local plan path (imported) or userspace `PLANFILE` name. Required. Stdin `-` is rejected. |
| `-out-file` | Local path for the simulated plan (`.txt`, `.pln`, or `.db`). Use `-` for stdout. Required. |
| `-failure-sets` | Comma-separated failure sets. See the list below. |
| `-pairwise-failure` | Pairwise mode: `distinct-sets`, `all-sets`, `distinct-sets-and-single`, or `all-sets-and-single`. Requires one or two `-failure-sets` entries. |
| `-traffic-levels` | Comma-separated traffic-level names (for example `Default`). Each name is 1–64 characters of letters, digits, `.`, `_`, or `-`. |
| `-record-failures-within-bound` | Record failures whose utilization is within this percent of worst case (`0`–`100`). `0` records only the worst case. |
| `-max-num-failures-per-interface` | Maximum failure scenarios recorded per interface (tool default `10`) |
| `-wc-latency` | Record demand worst-case latency (`true` or `false`) |
| `-record-demand-failures-within-bound` | Record failures whose demand latency is within this percent of worst case (`0`–`100`). `0` records only the worst case. |
| `-max-num-failure-per-demand` | Maximum failure scenarios recorded per demand (tool default `1`) |
| `-wc-vpn` | Record VPN worst-case utilizations and latencies (`true` or `false`) |
| `-demand-paths` | Engine filename for demand paths. Retrieve it with `--keep-tar`. |
| `-no-failure-diff` | Omit the no-failure difference (`true` or `false`) |
| `-set-inactive` | Set objects inactive for each failure scenario (`true` or `false`; tool default `true`) |
| `-num-threads` | Maximum worker threads, `1`–`1024` (the tool chooses a default) |
| `-num-partitions` | Number of failure-scenario partitions, `1`–`10000` (tool default `1`) |
| `-partition-index` | Partition to simulate, from `0` through `num-partitions - 1` |
| `-result-file` | Engine filename for the simulation report (`.txt` or `.db`). Retrieve it with `--keep-tar`. |
| `-demand-latencies` | Engine filename for demand latencies. Retrieve it with `--keep-tar`. |
| `-verbosity` | Log verbosity from `1` to `60` (tool default `30`) |
| `-log-file` | Log file basename created on the engine. Retrieve it with `--keep-tar`. |
| `-suppress-progress` | Suppress progress output (`true` or `false`; tool default `true`) |
| `-options-file` | Extra options file. A local file is imported and staged; the tool receives its basename. |
| `-no-global-options` | Skip the global options file (`true` or `false`) |

`-failure-sets` accepts these tokens, comma-separated, with no duplicates:

`circuits`, `nodes`, `sites`, `ports`, `portcircuits`, `external_endpoint_members`, `l1nodes`, `l1links`, `srlgs`, `parallel_circuits`

Common UI spellings (`port_circuits`, `l1_nodes`, `l1_links`, `sr_lgs`, `parallelcircuits`, and similar) are normalized to those tokens. Pairwise mode names accept hyphens, underscores, or spaces (`distinct sets`, `distinct_sets`) and are sent as the hyphenated form above.

Basenames used by `-out-file`, `-result-file`, `-demand-paths`, `-demand-latencies`, `-log-file`, and `-options-file` must be distinct. The script rejects a second flag that would reuse the same engine filename.

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
python sim_analysis.py --ip "$CW_HOST" \
  -plan-file us_wan.txt -out-file us_wan_sim.txt -failure-sets circuits
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
  "script": "sim_analysis",
  "outFile": "us_wan_sim.txt",
  "options": "-failure-sets circuits -wc-latency true"
}
```

`options` is empty when no extra `sim_analysis` flags are passed. `-plan-file` and `-out-file` are not placed in `options`; the engine supplies them.

## Notes

- TLS certificate verification is on unless `-k` / `--insecure` is set. Use `-k` only for lab controllers with a private or self-signed certificate. For a production controller, leave verification enabled so the client checks the server certificate. The client allows TLS 1.2 and TLS 1.3 and does not follow HTTP redirects.
- Names sent to the engine (`-plan-file`, `-out-file`, `-log-file`, `-options-file`, `-result-file`, `-demand-paths`, `-demand-latencies`) must be 1–255 characters of letters, digits, `.`, `_`, or `-`, and must start with a letter or digit. Paths are reduced to that basename. Uploads, the result tar, and the extracted plan are limited to 1 GiB. Empty files are not uploaded.
- `--password` prints a warning because the value is visible in the process list. A group- or world-readable JWT file also prints a warning. Prefer `chmod 600` on `~/.crosswork/<ip>.jwt`.
- Passwords and tokens are read from a JWT file, environment variables, CLI flags, or a prompt. Do not commit them, and prefer `cw_get_jwt.py` plus the default `~/.crosswork/<ip>.jwt` path for repeat runs.
- Re-running against the same local plan imports that file into userspace again under the same basename.
- A job that ends in `FAILED`, `ABORTED`, or `CANCELLED`, or that completes without an `outputTarFileId`, is reported as an error. The output plan is written only after a `COMPLETED` job and a successful tar extract.
- `--timeout` bounds how long the script waits for the job (default `1800` seconds). `--http-timeout` bounds each HTTP call. Increase `--timeout` for large failure sets or pairwise runs.
- `--keep-tar` writes the result tar locally and still extracts the simulated plan to `-out-file`. The simulation report, demand paths, demand latencies, and log stay in that tar.
