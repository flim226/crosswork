# metric_opt.py

A wrapper that executes the `metric_opt` CLI tool as a Job Manager job, submitted through the Crosswork Planning 8.0 REST API. It runs Metric Optimization on a plan file and writes the optimized plan locally.

## Overview

Metric Optimization selects IGP metrics that reduce interface utilization under normal operation, under failure scenarios, or both. That optimization is performed by the `metric_opt` CLI on the Design engine. This script wraps that tool for use from a workstation: it stages the source plan in Design userspace, submits a Job Manager job over the CP 8.0 REST API (`jobType` `CLI_JOB`, `script=metric_opt`), polls until the job finishes, and writes the optimized plan locally.

Tool options match the CP 8.0 Metric Optimization configuration (`JobConfigurations_MetricOptimizer`) plus the generic CLI flags shared with other Design tools. The engine injects `-plan-file` and `-out-file` from the staged plan (`inPlanFileId`) and `jobConfig.outFile`. Every other flag is passed in `jobConfig.options`. Omitting a flag leaves the tool default in effect.

Credentials are never stored in the script. When `--username`, `--password`, and `--jwt` are all omitted, the script uses `~/.crosswork/<ip>.jwt` if that file exists (the path `cw_get_jwt.py` writes). Otherwise it uses `--jwt`, the CLI flags, `CW_USERNAME` / `CW_PASSWORD`, or an interactive prompt.

## Requirements

- Platform: Cisco Crosswork Planning 8.0.0 or later
- Python 3.10+
- `requests` (`pip install requests`)
- Design REST API reachable (HTTPS port `30603` by default)
- A local plan file, or a plan already stored in Design userspace as a `PLANFILE` with the same basename

## How It Works

1. **Resolve credentials**: Loads a JWT from `--jwt` or from `~/.crosswork/<ip>.jwt`, or authenticates with username and password through Crosswork SSO and exchanges the ticket for a JWT.
2. **Resolve the source plan**: If `-plan-file` is a local file, imports it into Design userspace as a `PLANFILE`. Otherwise looks up that basename in userspace. Stdin (`-plan-file -`) is not supported.
3. **Stage input tables**: When `-opt-interfaces-table`, `-set-interfaces-table`, or `-options-file` names a local file, imports it as category `OTHERS` so the job can read it. The flag sent to `metric_opt` is the file's basename.
4. **Stage job input**: Calls `GET /cp/apiGateway/design/v1/jobs/input` with the plan file id (and any staged table ids) and reads the returned `fsId`.
5. **Submit the Job Manager job**: Posts the job with `jobType` `CLI_JOB`. `jobConfig` selects `script=metric_opt`, sets `outFile` from the output basename, and carries the remaining `metric_opt` flags in `options`.
6. **Poll until completion**: Searches jobs by id until the status is `COMPLETED`, `FAILED`, `ABORTED`, or `CANCELLED`, or until `--timeout` elapses.
7. **Export the result**: On `COMPLETED`, downloads the job result tar (`outputTarFileId`) and extracts the member whose basename matches the engine output plan.
8. **Write the output plan**: Saves that member to `-out-file`. `-out-file -` writes the bytes to stdout.

`-report-file` and `-log-file` are created on the engine and returned inside the same tar. Pass `--keep-tar` to save the archive. When either flag is set and `--keep-tar` is omitted, the script prints a reminder on stderr before submitting the job.

Progress is printed to stderr:

```
Using JWT from /Users/operator/.crosswork/198.18.134.229.jwt
Importing plan us_wan.txt to userspace...
Staging job input (fileId=42)...
Submitting CLI_JOB metric_opt (fsId=fs-9c1e)...
Submitted job 1843
Job 1843 status: RUNNING
Job 1843 status: COMPLETED
Exporting result tar fileId=9103...
Wrote optimized plan to us_wan_opt.txt (4821104 bytes)
```

## Usage

```bash
# Default metric optimization, using the JWT written by cw_get_jwt.py
python metric_opt.py --ip "$CW_HOST" -k \
  -plan-file us_wan.txt -out-file /tmp/us_wan_opt.txt

# Optimize for circuit and node failures while holding normal utilization under 80%
python metric_opt.py --ip "$CW_HOST" -k \
  -plan-file us_wan.txt -out-file /tmp/us_wan_opt.txt \
  -min-max-failure-util true -failure-sets circuits,nodes \
  -nr-util-percent-bound 80

# Incremental, symmetric metrics on a selected set of interfaces; keep the report
python metric_opt.py --ip 198.18.134.229 \
  --jwt ~/.crosswork/198.18.134.229.jwt \
  -plan-file us_wan.txt -out-file us_wan_opt.txt \
  -optimization-type incremental -enforce-symmetric-metrics true \
  -opt-interfaces-table core_intfs.txt \
  -report-file metric_opt_report.txt --keep-tar /tmp/metric_opt.tar
```

```bash
# Print the Metric-Opt report to stdout after the job (progress stays on stderr)
python metric_opt.py --ip "$CW_HOST" -k \
  -plan-file us_wan.txt -out-file us_wan_opt.txt \
  -min-max-failure-util true -failure-sets circuits,srlgs --report > metric_opt_report.txt
```

`--report` reads the output plan's `<ReportSections>` table, takes the `Contents` of the last row named `Metric-Opt`, decodes the escaped newlines and `\<` / `\>` markers, and prints the result to stdout. It needs a text plan, so it cannot be combined with a `.pln` or `.db` `-out-file`, or with `-out-file -`.

When `-out-file` is `-`, the engine member is `<plan-stem>_opt<plan-suffix>` and those bytes are written to stdout.

## Options

### Crosswork connection and job control

These match `sim_analysis.py`: `--ip`, `--port`, `--username`/`-u`, `--password`/`-p`, `--jwt`/`-j`, `--http-timeout`, `-k`/`--insecure`, `--job-name`, `--profile`, `--job-priority`, `--poll-interval`, `--timeout` (default `3600` seconds), and `--keep-tar`. `--report` prints the Metric-Opt report from the output plan to stdout.

### metric_opt

These flags are forwarded in `jobConfig.options`. They are sent only when you pass them; otherwise the tool default applies.

| Flag | Description |
|------|-------------|
| `-plan-file` | Local plan path (imported) or userspace `PLANFILE` name. Required. |
| `-out-file` | Local path for the optimized plan. Use `-` for stdout. Required. |
| `-report-file` | Engine filename for the optimization report. Retrieve it with `--keep-tar`. |
| `-opt-interfaces-table` | Interfaces table to optimize utilization on (default: all interfaces) |
| `-set-interfaces-table` | Interfaces table `metric_opt` may change metrics on (default: core) |
| `-traffic-level` | Traffic level to optimize over (for example `Default`) |
| `-non-opt-interfaces` | `ignore`, `increment`, or `bound` |
| `-non-opt-increment` | Percent above current utilization. Requires `-non-opt-interfaces increment`. |
| `-non-opt-bound` | Utilization bound in percent. Requires `-non-opt-interfaces bound`. |
| `-min-max-nofailure-util` | Minimize maximum interface utilization (`true` or `false`) |
| `-min-nofailure-links-above` | Minimize the number of interfaces with utilization above this percent |
| `-enforce-latency-bounds` | Restrict demand paths to those within their latency bound |
| `-min-max-failure-util` | Minimize maximum utilization over all failure scenarios |
| `-min-failure-links-above` | Minimize the number of interfaces above this percent under any failure |
| `-nr-util-percent-bound` | No-failure utilization bound enforced while optimizing for failures |
| `-failure-sets` | Comma-separated failure sets (same tokens as `sim_analysis.py`) |
| `-set-inactive` | Set objects inactive for each failure scenario |
| `-optimization-type` | `global`, `incremental`, or `none` |
| `-target-metrics` | `current` or `none` |
| `-default-target-metric` | Target metric for interfaces without a current metric |
| `-enforce-symmetric-metrics` | Set both metrics on each circuit equal |
| `-prevent-edge-leakage` | Keep core-routed demands from transiting the edge |
| `-intersite-metric-lower-bound`, `-intersite-metric-upper-bound` | Metric range on inter-site interfaces |
| `-intrasite-metric-lower-bound`, `-intrasite-metric-upper-bound` | Metric range on intra-site interfaces |
| `-intrasite-metrics-below-intersite` | Keep intra-site metrics below inter-site metrics |
| `-incremental-time-limit` | Time limit for the incremental phase, in seconds |
| `-max-iterations-no-improv` | Stop after this many iterations without improvement |
| `-min-usage-improvement` | Minimum utilization improvement to accept a change |
| `-latency-correction-bound`, `-latency-rounding` | Latency tuning |
| `-num-threads` | Maximum worker threads, `1`–`1024` |
| `-verbosity`, `-log-file`, `-suppress-progress`, `-options-file`, `-no-global-options` | Generic CLI options |

`-failure-sets` accepts `circuits`, `nodes`, `sites`, `ports`, `portcircuits`, `external_endpoint_members`, `l1nodes`, `l1links`, `srlgs`, and `parallel_circuits`. Swagger spellings such as `PORT_CIRCUITS` and `SR_LGS` are normalized to those tokens.

The script rejects a lower metric bound above its upper bound, and rejects `-non-opt-increment` or `-non-opt-bound` without the matching `-non-opt-interfaces` mode. Basenames used by `-out-file`, `-report-file`, `-log-file`, `-options-file`, and the two interfaces tables must be distinct.

## API reference

The REST calls are identical to `sim_analysis.py` (SSO ticket and JWT, `file/import`, `file/search`, `jobs/input`, `jobs`, `jobs/search`, `file/export`). `jobConfig` is a JSON string:

```json
{
  "scriptType": "CLI",
  "script": "metric_opt",
  "outFile": "us_wan_opt.txt",
  "options": "-optimization-type incremental -enforce-symmetric-metrics true"
}
```

## Notes

- Flag names come from the `JobConfigurations_MetricOptimizer` schema in the CP 8.0 REST API specification. Enum values (`-optimization-type`, `-non-opt-interfaces`, `-target-metrics`) are sent in lower case. The schema fields `new-plan`, `default-opt-interfaces-table-value`, and `default-set-interfaces-table-value` appear to be GUI-only settings and are not exposed.
- TLS certificate verification is on unless `-k` / `--insecure` is set. The client allows TLS 1.2 and TLS 1.3 and does not follow HTTP redirects.
- Uploads, the result tar, and the extracted plan are limited to 1 GiB. Empty files are not uploaded.
- Global optimization over large failure sets can take a long time. Raise `--timeout` accordingly.
