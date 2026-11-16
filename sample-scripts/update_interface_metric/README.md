# update_interface_metric.py

Copies IPv6 IGP and TE metric values to their corresponding IPv4 metric fields on all interfaces in a Crosswork Planning plan file, using the **Crosswork Planning OPM Python Library**.

## Overview

In dual-stack (IPv4/IPv6) networks, the SR-PCE collector populates IPv4 metrics in the IGP metric table and IPv6 metrics in the IPv6-IGP metric table. In some network designs the IPv6 metric is the authoritative metric, but downstream tools or analysis workflows only consume the IPv4 metric columns. This script bridges that gap by reading the IPv6 IGP and TE metric values from every interface and copying them into the IPv4 IGP metric and TE metric fields, ensuring that the plan file reflects the intended metric values for both address families.

The script is designed to run as a **Crosswork Planning external script** within the Collector framework. It can be attached to any collection chain to post-process the network model automatically on every scheduled collection run.

## Requirements

- Platform: Cisco Crosswork Planning 7.2 or later (external script, OPM Python Library)
- Python 3.10+

## How It Works

1. **Open Source Plan** — Uses `com.cisco.wae.opm.network.Network(src)` to load the source plan file.
2. **Iterate Interfaces** — Walks every node and interface in the network model.
3. **Read IPv6 Metrics** — For each interface, accesses the underlying RPC record (`interface.rpc_record`) to read the `ipv6IGPMetric` and `ipv6TEMetric` fields. These fields are not directly exposed on the OPM `interface` object, so the lower-level record is used.
4. **Copy if Different** — If the IPv6 IGP metric is a valid integer and differs from the current IPv4 IGP metric (`interface.igp_metric`), the IPv6 value overwrites the IPv4 value. The same logic applies to the TE metric (`interface.te_metric`).
5. **Write Output** — Saves the modified network model to the destination plan file via `network.write(dest)`.

## Logging

The script uses the same log format as `get_plan_cp_cw.py`. Each line has a UTC timestamp, a level, a per-run ID, and `key=value` fields. Every step logs one `phase=<name> status=ok|failed duration_ms=...` line. Each run ends with a `phase=summary` line, even when the run fails.

```
2026-11-16T08:28:53.722Z INFO  run=67d5edac phase=start script=update_interface_metric.py script_version=2.0 python=3.11.4 host=cp-collector pid=5906 cwd=/app/... src=/app/.../in.db out=/app/.../out.db extra_args=5
2026-11-16T08:28:53.736Z INFO  run=67d5edac phase=load_plan status=ok duration_ms=12 path=/app/.../in.db bytes=56639
2026-11-16T08:28:53.736Z INFO  run=67d5edac phase=copy_metrics node=pe1 interface=GigabitEthernet0/0/0/0 metric=igp old=10 new=100
2026-11-16T08:28:53.736Z INFO  run=67d5edac phase=copy_metrics node=pe1 interface=GigabitEthernet0/0/0/0 metric=te old=20 new=200
2026-11-16T08:28:53.736Z INFO  run=67d5edac phase=copy_metrics status=ok duration_ms=0 nodes=19 interfaces=64 v6_igp_present=64 v6_te_present=64 igp_updated=1 te_updated=1 interfaces_updated=1 interface_errors=0
2026-11-16T08:28:53.737Z INFO  run=67d5edac phase=write_plan status=ok duration_ms=40 path=/app/.../out.db out_bytes=88112
2026-11-16T08:28:53.737Z INFO  run=67d5edac phase=summary status=ok exit=0 total_ms=55 load_plan_ms=12 copy_metrics_ms=0 write_plan_ms=40 plan_bytes=56639 nodes=19 interfaces=64 igp_updated=1 te_updated=1 interfaces_updated=1 interface_errors=0 out_bytes=88112
```

- Each metric change logs one line with its `old` and `new` value. The first 50 change lines are logged at INFO. Any further change lines are logged only with `--debug`, and `change_lines_suppressed` on the `copy_metrics` line records how many were hidden.
- If reading or updating one interface fails, the script skips that interface and continues. It logs a warning for the first failure and counts all failures in `interface_errors`. In that case the summary reports `status=partial`, and the exit code is still 0.
- A warning is logged when the plan has no nodes, has no interfaces, or has no interface with IPv6 metrics.
- Log verbosity: `--debug`, or set `CW_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, `ERROR`).

| Exit | `error_class` | Cause |
|------|---------------|-------|
| 0 | | Success (`status=ok` or `status=partial`) |
| 1 | `unknown` | Unexpected exception; re-run with `--debug` for the traceback |
| 2 | `config` | Base plan file or output directory not found |
| 7 | `parse` | OPM could not load the base plan |
| 9 | `io` | OPM could not write the output plan, or the output is missing or empty |
| 10 | `dependency` | OPM Python library (`com.cisco.wae.opm`) not available |

## Deploying as a Crosswork Planning External Script

This script is intended to be deployed as an external script within a Crosswork Planning collection chain. The Collector framework automatically provides the source plan file and output plan file as command-line arguments (`argv[1]` and `argv[2]`), and may pass additional arguments (`argv[3]` through `argv[7]`) which this script ignores.

For full details on the external script framework, see [Run an external script against a network model](https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/setup-guide/cisco-crosswork-planning-7-2-collection-setup-and-administration/m-collectors-in-cp.html#run-external-scripts) in the *Cisco Crosswork Planning 7.2 Collection Setup and Administration Guide*.

### Collector Framework Arguments

When invoked by the Collector framework, the script receives these positional arguments:

| Argument | Description |
|----------|-------------|
| `argv[1]` | Source plan file |
| `argv[2]` | Output plan file |
| `argv[3]` | Device access authentication file *(ignored)* |
| `argv[4]` | Global network access configuration file *(ignored)* |
| `argv[5]` | Home directory *(ignored)* |
| `argv[6]` | Path to user-uploaded external files *(ignored)* |
| `argv[7]` | Path to archive root directory *(ignored)* |

### Step-by-Step Deployment

1. **Create or edit a collection** — In the Crosswork Planning UI, navigate to your collection configuration.

2. **Add an external script** — On the Configure page, click **+ Add external script** under the **Basic topology**, **Advanced modeling**, or **Traffic and Demands** section (depending on where in the chain you want this script to run).

3. **Configure the script parameters:**

   | Option | Value |
   |--------|-------|
   | **Collector name** | A descriptive name, e.g. `Copy IPv6 Metrics` |
   | **Is source a plan file?** | Leave unchecked (the source comes from an upstream collector) |
   | **Source** | Select the upstream collector whose output should be processed (e.g., the SR-PCE collector or an aggregator) |
   | **Input file** | Upload `update_interface_metric.py` (or a `.zip` archive if additional files are needed) |
   | **Executable script** | `update_interface_metric.py` |
   | **Script language** | Python |
   | **Timeout** | 30 minutes (default) or adjust as needed |

4. **Preview and create** — Click **Next**, review the configuration, and click **Create**.

5. **Schedule the collection** — Configure the collection schedule to run immediately or at specific intervals.

### Typical Collection Chain Placement

This script is most useful when placed **after the SR-PCE collector** (or after aggregation), since the SR-PCE collector populates both the IPv4 and IPv6 metric tables. A typical chain might look like:

```
SR-PCE Collector → PCEP LSP Collector → Copy IPv6 Metrics (this script) → DARE Aggregation
```

Or if using the IGP database collector with ISISv6:

```
IGP Database Collector → LSP Collector → Copy IPv6 Metrics (this script) → DARE Aggregation
```

## API Reference

This script uses the following components from the Crosswork Planning OPM Python Library (see [API documentation](https://developer.cisco.com/docs/crosswork/planning/)):

| OPM Class / Method | Purpose |
|---------------------|---------|
| `com.cisco.wae.opm.network.Network(plan_file)` | Opens and loads a plan file into an OPM Network object |
| `network.model.nodes` | Iterable collection of all node objects in the network model |
| `node.interfaces` | Iterable collection of all interface objects on a given node |
| `interface.rpc_record` | Accesses the underlying RPC record for fields not directly exposed in OPM |
| `rpc_record.ipv6IGPMetric` | IPv6 IGP metric value for the interface |
| `rpc_record.ipv6TEMetric` | IPv6 TE metric value for the interface |
| `interface.igp_metric` | Read/write property for the IPv4 IGP metric |
| `interface.te_metric` | Read/write property for the IPv4 TE metric |
| `network.write(dest_file)` | Writes the modified network model to a plan file |

## Notes

- The script only copies a metric value when the IPv6 value is a valid integer **and** differs from the current IPv4 value. Interfaces without IPv6 metrics are left unchanged.
- The `rpc_record` accessor is used because `ipv6IGPMetric` and `ipv6TEMetric` are not exposed as first-class properties on the OPM `interface` object.
- When running inside the Collector framework, stdout output (the log lines) is captured in the collection logs accessible via the Crosswork Planning UI under **Administration > Show Tech**.
- If migrating this script from Cisco WAE, verify that file path references are compatible with the Crosswork Planning architecture, as noted in the [external scripts documentation](https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/setup-guide/cisco-crosswork-planning-7-2-collection-setup-and-administration/m-collectors-in-cp.html#run-external-scripts).
