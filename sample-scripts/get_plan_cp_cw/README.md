# Application Note: Using Crosswork Planning Startup Script

## Overview

This application note provides an example on how an external script may be used within **Cisco Crosswork Planning 7.2** as a **Startup Script** in the data collection workflow. Our script `get_plan_cp_cw.py` retrieve the real time network model in the form of a planfile from **Crosswork Network Controller (CNC)** and converts it for use in Crosswork Planning. Since the startup script can be scheduled to execute at a regular cadence as part of a collection workflow, it allows for a automated archival of planfiles for offline planning use cases.

The script has been verified with **Cisco Crosswork Planning 7.2** and **Cisco Crosswork Planning 8.0**.

## Requirements

- Platform: Cisco Crosswork Planning 7.2 or later (startup script); verified on 7.2 and 8.0
- Source: Cisco Crosswork Network Controller 7.2 or later
- **Python packages**: `requests`, `urllib3`, `argparse`, `base64`, `os`, `subprocess`, `logging`, `sys`, `typing`  (Satisfied by Crosswork Planning)
- **External tools**: `mate_convert` (Cisco WAE/Crosswork Planning utility), `trim_nodes` (Cisco WAE/Crosswork Planning utility, used for optional node trimming)

## About Cisco Crosswork Planning

Cisco Crosswork Planning provides toolsets for network operators to create and maintain a model of the current network through the collection and analysis of the network and the traffic demands placed on it. At a given time, this network model contains all relevant information about a network, including topology, LSPs and traffic. You may use this as a basis for analyzing the impact on the network due to changes in traffic demands, paths, node and link failures, link metrics, or others. Crosswork Planning comprises two components: **Crosswork Planning Collector** (component that create, maintain, and archive a network model) and **Crosswork Planning Design** (component for predictive what-if simulation analysis, growth planning, and network optimization and design).

## New Features in Crosswork Planning 7.2 (Startup Script Related)

Cisco Crosswork Planning 7.2 introduces several new capabilities:

| Feature | Description |
|---------|-------------|
| **Startup Script Support** | You can now configure an external script as the first step in the collection configuration chain instead of the existing mandatory IGP/SR-PCE collectors. |
| **Dynamic Data File Access** | Upload data files dynamcially to the Collector. External scripts can access these files at runtime without requiring script repackaging or redeployment. |
| **Import .db Plan Files** | You can now import plan files with a `.db` extension into user space—the format produced by startup scripts. |

For more information, see the *"Run an external script as a startup script"* section in the [Cisco Crosswork Planning 7.2 Collection Setup and Administration](https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/setup-guide/cisco-crosswork-planning-7-2-collection-setup-and-administration/m-collectors-in-cp.html#run-startup-script) document.

## Background: Startup Scripts in Crosswork Planning

As documented in the [Cisco Crosswork Planning 7.2 Collection Setup Guide](https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/setup-guide/cisco-crosswork-planning-7-2-collection-setup-and-administration/m-collectors-in-cp.html#run-startup-script), Crosswork Planning supports running an **external script as the first step** in a collection configuration chain.

Key characteristics of startup scripts:

- Executed **before** any other collectors in the chain
- Only **one startup script** is allowed per collection chain
- When a startup script is configured, the IGP database or SR-PCE collector becomes **optional**
- The startup script output can serve as a **source** for downstream collectors
- Supported languages: **Python, Shell, Perl**
- Valid file formats: `.py`, `.sh`, `.pl`, `.zip`, `.tar`, `.gz`, `.tar.gz`
  - The .zip, .tar, .gz, and .tar.gz format are useful for packaging additional files required for script execution.
- Startup script must produce a valid database (.db) file for ingestion by Crosswork Planning

## Script Purpose

`get_plan_cp_cw.py` serves as a startup script that:

1. **Authenticates** to Crosswork Network Controller via SSO
2. **Retrieves** the current network plan file using the CNC RESTCONF API
3. Optionally retrieves node coordinates from DLM Inventory and adds them to the text plan's `<Nodes>` table
4. **Trims** the plan file by including or excluding nodes based on optional node trimming configuration files
5. **Converts** the plan file to `.db` format required by Crosswork Planning

This enables Crosswork Planning to use the live network model from CNC as its collection source, rather than performing independent topology discovery.

## Script Architecture

### Authentication Flow

```
┌─────────────────────┐    ┌─────────────────────────────────┐
│  get_plan_cp_cw.py  │───▶│  CNC SSO Endpoint               │
│                     │    │  https://<IP>:30603/crosswork/  │
│                     │    │  sso/v1/tickets                 │
└─────────────────────┘    └─────────────────────────────────┘
         │                              │
         │  1. POST username/password   │
         │◀─────────────────────────────│
         │     Returns: TGT ticket      │
         │                              │
         │  2. POST TGT + service URL   │
         │◀─────────────────────────────│
         │     Returns: JWT token       │
         ▼
```

### Plan Retrieval Flow

```
┌─────────────────────┐    ┌─────────────────────────────────┐
│  get_plan_cp_cw.py  │───▶│  CNC Optimization Engine API    │
│  (with JWT token)   │    │  /crosswork/nbi/optima/v2/      │
│                     │    │  restconf/operations/...        │
└─────────────────────┘    └─────────────────────────────────┘
         │                              │
         │  POST: get-plan request      │
         │  - version: "" (Default)     │
         │  - format: "pln" or "txt"    │
         │◀─────────────────────────────│
         │  Response: base64 planfile   │
         ▼
┌─────────────────────────┐
│   trim_nodes            │  (optional, if trim config files found)
│   -plan-file X.pln      │
│   -out-file X.plan.trim │
└─────────────────────────┘
         │
         ▼
┌─────────────────────┐
│   mate_convert      │
│   -plan-file X.pln  │
│   -out-file Y.db    │
└─────────────────────┘
         │
         ▼
    [Output .db file for Crosswork Planning]
```

## Command Line Interface

The script conforms to the executable script parameter conventions as defined in the [Cisco Crosswork Planning 7.2 Collection Setup and Administration](https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/setup-guide/cisco-crosswork-planning-7-2-collection-setup-and-administration/m-collectors-in-cp.html#run-external-scripts) documentation. When Crosswork Planning executes an external script, it provides command-line arguments in a predefined order:

| Argument | Description |
|----------|-------------|
| `argv[1]` | Source plan file (baseplan) |
| `argv[2]` | Output plan file (.db format) |
| `argv[3]` | Device access authentication file |
| `argv[4]` | Global network access configuration file |
| `argv[5]` | Home directory |
| `argv[6]` | Path where user uploaded external files are available |
| `argv[7]` | Path to access archive root directory |

The `get_plan_cp_cw.py` script accepts all seven positional arguments defined by the Crosswork Planning framework:

```bash
python get_plan_cp_cw.py <baseplan> <output_planfile> <device_auth_file> <network_access_config> <home_dir> <user_upload_dir> <archive_root_dir> [options]
```

> **Note**: Since this script retrieves a complete plan file directly from Crosswork Network Controller, it does not use the base planfile (`argv[1]`), device access authentication file (`argv[3]`), global network access configuration file (`argv[4]`), or archive root directory (`argv[7]`) parameters. The **output plan file** (`argv[2]`) specifies the output file name. The **home directory** (`argv[5]`) and **user upload directory** (`argv[6]`) are used to search for optional trim configuration files.

### Arguments

| Argument | Description |
|----------|-------------|
| `baseplan` | First parameter (base planfile) — ignored, required by CP framework |
| `planfile` | Output plan file name (typically `.db` format) |
| `device_auth_file` | Device access authentication file — accepted but not used |
| `network_access_config` | Global network access configuration file — accepted but not used |
| `home_dir` | Home directory — searched for trim configuration files |
| `user_upload_dir` | Path where user uploaded external files are available — searched for trim configuration files |
| `archive_root_dir` | Path to access archive root directory — accepted but not used |
| `--tmpfile` | Intermediate plan file path; extension determines download format. By default, `TMP_PLANFILE` (`planfile.pln`) is created in a private per-run temporary directory (see [Temporary files](#temporary-files)) |
| `--ip` | Crosswork Network Controller IP address *(optional, for testing only)* |
| `--username`, `-u` | CNC username *(optional, for testing only)* |
| `--password`, `-p` | CNC password *(optional, for testing only)* |
| `--jwt`, `-j` | Path to a JWT file. When `--username`, `--password`, and `--jwt` are omitted, a non-empty `~/.crosswork/<ip>.jwt` is used. A missing or empty file falls back to CLI flags, then `CW_USERNAME` / `CW_PASSWORD`, then the script constants |
| `-k`, `--insecure` | Disable SSL certificate verification (default: `INSECURE`, `False`) |
| `--version`, `-v` | Planfile version (default: `PLAN_VERSION`, empty string) |
| `--geoloc` | Populate node `Longitude` and `Latitude` in the `<Nodes>` table from DLM Inventory (default: `GEOLOC`, `False`). It changes the default intermediate file from `planfile.pln` to `planfile.txt`; when supplying `--tmpfile`, it must end in `.txt`. |
| `--debug` | Enable DEBUG logging: per-request HTTP lines, trim file paths, unmatched geoloc node names, and Python tracebacks. Overrides `CW_LOG_LEVEL` (see [Troubleshooting](#troubleshooting)) |

> **Note**: The `--ip`, `--username`, `--password`, `--version`, `-k` and `--geoloc` parameters are optional and intended for quick testing purposes only. For production deployments, configure these values as constants within the script (`CROSSWORK_IP`, `PLAN_VERSION`, `INSECURE`, `GEOLOC`, etc.) or use environment variables for credentials. Because `-k` and `--geoloc` are on/off flags, setting `INSECURE` or `GEOLOC` to `True` in the script cannot be overridden back to `False` from the command line.

### Example Usage

```bash
# As standalone script (with required positional args)
python get_plan_cp_cw.py ignored output.db devauth.txt netaccess.txt /home/user /uploads /archive --ip 10.58.239.120 -u admin -p mypassword

# Add node coordinates before conversion to the output database
python get_plan_cp_cw.py ignored output.db devauth.txt netaccess.txt /home/user /uploads /archive --ip 10.58.239.120 --geoloc

# The script internally:
# 1. Downloads plan as <private temp dir>/planfile.pln (planfile.txt with --geoloc)
# 2. Optionally trims nodes (if trim config files exist in home_dir or user_upload_dir)
# 3. Converts planfile.pln → output.db using mate_convert, then removes the temp directory
```

With `--geoloc`, the script calls `POST /crosswork/inventory/v1/nodes/query` after retrieving the plan. It matches each plan-node `Name` to the DLM Inventory `host_name` case-insensitively, then writes `geo_info.coordinates.longitude.value` and `latitude.value` into the text plan's `<Nodes>` table. Nodes with no match or no complete coordinate pair remain unchanged.

## Integration with Crosswork Planning Collector

### Configuration Steps

1. **Create API user** on Crosswork Network Controller
   - Although not strictly necessary, using separate API credentials can help limit the impact of excessive script invocation on shared resources (e.g. hitting AAA maximum session limits)
   
3. **Configure Parameters**
   - Modify the hardcoded defaults in the script 
```
CROSSWORK_IP = "198.18.134.219"
CROSSWORK_USERNAME = "admin"
CROSSWORK_PASSWORD = "PASSWORD"
PLAN_VERSION = ""
TMP_PLANFILE = "planfile.pln"
INSECURE = False
GEOLOC = False
```
4. **Create Node trimming configuration files (Optional)**

5. **Navigate to Collection Configuration**
   - In Crosswork Planning, create a new collection or edit an existing one

6. **Enable Startup Script**
   - In the **Startup script** section, select **Script**

7. **Upload the Script**
   - **Input file**: Upload `get_plan_cp_cw.py` (or as part of a `.zip` archive if node triimming config files are used)
   - **Executable script**: `get_plan_cp_cw.py`
   - **Script language**: Python

8. **Configure Downstream Collectors (Optional)**
   - Use the startup script output as **Source** for other collectors (LSP, BGP, VPN, etc.)
   - The IGP database or SR-PCE collector becomes optional when a startup script produces a valid network model

### Collection Chain Example

```
┌──────────────────────┐
│   Startup Script     │
│  get_plan_cp_cw.py   │
│  (retrieves plan,    │
│   trims nodes,       │
│   produces .db)      │
└──────────┬───────────┘
           │ Source
           ▼
┌──────────────────────┐
│   LSP Collector      │
│  (optional)          │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│   Traffic Collector  │
│  (optional)          │
└──────────────────────┘
```

## Key Functions

### `CrossworkAuthError`

Custom exception class raised when authentication or API calls fail.

### `_TimeoutAdapter`

An `HTTPAdapter` subclass that applies a default connect timeout (`CONNECT_TIMEOUT`) to all requests made through the session.

### `_create_session()`

Creates a `requests.Session` with SSL verification controlled by `-k`/`--insecure` (default from the `INSECURE` constant). Mounts the `_TimeoutAdapter` for both `https://` and `http://` schemes so all HTTP calls share the session for connection pooling and consistent settings.

### `check_response(resp, context)`

Inspects an HTTP response and raises `CrossworkAuthError` with the status code, reason, and truncated response body when the request was not successful.

### `get_ticket(session, base_url, username, password)`

Obtains a Ticket Granting Ticket (TGT) from the CNC SSO endpoint (`/crosswork/sso/v1/tickets`) by posting username and password.

### `get_token(session, base_url, ticket)`

Exchanges a TGT for a JWT bearer token via the CNC SSO v2 endpoint (`/crosswork/sso/v2/tickets/jwt`).

### `get_plan(session, base_url, token, plan_format, version)`

Calls the CNC Optimization Engine REST API to retrieve the plan file:
- **Endpoint**: `/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-operations:get-plan`
- **Payload**: `{ "input": { "version": "<version>", "format": "<pln|txt>" } }`
- **Authorization**: Bearer token obtained from `get_token()`
- **Returns**: Base64-decoded plan file content

### `plan_inventory(path, plan_format)`

Counts nodes, circuits, interfaces, SR policies, RSVP LSPs, demands, flex-algos, and SRLGs in a `.txt` or `.pln` plan using only the Python standard library. See [Plan inventory](#plan-inventory).

### `run_command(cmd, description)`

Runs a subprocess command, logs its command line, stdout/stderr, return code, and duration, and raises `CommandError` (a `RuntimeError`) if the command is not found or exits with a non-zero return code.

### `find_trim_file(filename, search_dirs, dir_labels)`

Searches for a node trimming configuration file across the given directories. Returns a `(path, label)` tuple if found, or `(None, None)` otherwise.

### `find_and_apply_trim(tmpfile, search_dirs, dir_labels)`

Discovers node trimming configuration files, builds the appropriate `trim_nodes` arguments, and executes the trim_nodes command if any config files are found. Returns the path to the (possibly trimmed) plan file for downstream conversion.

### `main()`

Orchestrates the workflow:
1. Configures logging
2. Parses command-line arguments
3. Deduces plan file format from `--tmpfile` extension (`.pln` or `.txt`)
4. Creates an HTTP session and authenticates to CNC (ticket + token exchange)
5. Retrieves plan file
6. Saves intermediate file (`.pln` or `.txt`)
7. Logs the plan inventory of the received plan (see [Plan inventory](#plan-inventory))
8. Delegates node trimming to `find_and_apply_trim()` (see [Node Trimming](#node-trimming)), then logs the trimmed inventory
9. Converts to `.db` format using `mate_convert` via `run_command()` and checks the output is non-empty
10. Cleans up temporary files (downloaded plan file and trimmed output) in a `finally` block and logs the summary line

## Node Trimming

The script supports optional node trimming to reduce the plan file to a subset of nodes before conversion. Node Trimming configuration files are searched for in the **home directory** (`argv[5]`) and **user upload directory** (`argv[6]`), in that order. 

A supported usage scenario is to create a zip archive which comprises of get_plan_cp_cw.py and a trim configuration file (e.g. trim_exclude.txt), uploaded under the Startup script configuration > Input file parameter.

### Node Trimming Configuration Files

| File Name | Description | Trim Argument |
|-----------|-------------|---------------|
| `trim_include.txt` | Node table of nodes to **include** (all others removed) | `-node-table <file> -exclude-node-table false` |
| `trim_exclude.txt` | Node table of nodes to **exclude** (all others kept) | `-node-table <file> -exclude-node-table true` |
| `trim_include_regex.txt` | Regex pattern — **include** matching nodes | `-include-nodes-regex <pattern>` |
| `trim_exclude_regex.txt` | Regex pattern — **exclude** matching nodes | `-exclude-nodes-regex <pattern>` |

- If **no trim files** are found, the downloaded plan file is passed directly to `mate_convert`.
- If **any trim files** are found, `trim_nodes` is invoked to produce a trimmed `.trim.pln` file, which is then passed to `mate_convert`.
- Trim config files can also be uploaded to the Collector using the **Dynamic Data File Access** feature, making them available in the user upload directory 

### Node Trimming Flow

```
┌──────────────────────────────┐
│  Search for trim config files│
│  in home_dir, user_upload_dir│
└──────────────┬───────────────┘
               │
        Found? │
       ┌───────┴───────┐
       │ Yes           │ No
       ▼               ▼
┌──────────────┐  ┌──────────────────┐
│ trim_nodes   │  │ Skip trimming    │
│ → X.trim.pln │  │ Use original pln │
└──────┬───────┘  └────────┬─────────┘
       │                   │
       └─────────┬─────────┘
                 ▼
        ┌──────────────┐
        │ mate_convert │
        │ → output.db  │
        └──────────────┘
```

## Configuration Constants

The script contains hardcoded defaults that should be modified for your environment:

```python
CROSSWORK_IP = "198.18.134.219"      # CNC IP address
CROSSWORK_USERNAME = "admin"         # CNC username  
CROSSWORK_PASSWORD = "PASSWORD"      # CNC password (update for production!)
PLAN_VERSION = ""                    # Plan version (empty = latest); default for --version
TMP_PLANFILE = "planfile.pln"        # Intermediate plan file name; extension (.pln/.txt) sets download format
INSECURE = False                     # True disables SSL verification; default for -k
GEOLOC = False                       # True adds DLM Inventory coordinates; default for --geoloc
CONNECT_TIMEOUT = 20                 # HTTP connect timeout in seconds
SCRIPT_VERSION = "1.5"               # Logged on the start line of every run
JWT_EXPIRY_WARNING_SECONDS = 300     # Warn when the JWT expires within this many seconds
```

When `GEOLOC = True` (or `--geoloc` is passed) and `--tmpfile` is not supplied, the script replaces the extension of `TMP_PLANFILE` with `.txt` and downloads a text plan (for example, `planfile.pln` becomes `planfile.txt`). An explicit `--tmpfile` must already end in `.txt`.

### Temporary files

When `--tmpfile` is not supplied, each run creates a private directory with `tempfile.mkdtemp()` (for example `/tmp/get_plan_cp_cw-k3j9x2ab/`, under `$TMPDIR` if set). The directory is readable only by the account running the script (mode `0700`) and has a unique name, so:

- The downloaded plan, which describes the full network topology, is not readable by other local users.
- Another user cannot pre-create or symlink the path to redirect the write.
- Concurrent collections do not overwrite each other's intermediate files.

The downloaded plan and the trimmed plan are written inside that directory, and the whole directory is removed at the end of the run, on success or failure. An explicit `--tmpfile` path is used as given and only the files are removed; choose a location that is not shared with other users.

> **Security Note**: For production deployments, consider using environment variables or a secure credential store instead of hardcoded credentials.

## Error Handling

Every failure is assigned an `error_class`, logged with a hint, and mapped to a distinct exit code:

| Exit | `error_class` | Typical cause | First check |
|------|---------------|---------------|-------------|
| 0 | — | Success | — |
| 1 | `unknown` | Unexpected exception | Re-run with `--debug` for the traceback |
| 2 | `config` | Invalid `--tmpfile` extension, `--geoloc` with a `.pln` file, or bad arguments | Script constants and arguments |
| 3 | `auth` | HTTP 401/403, SSO ticket/token failure, empty JWT file | Credentials, JWT expiry (`jwt_exp` on the `auth` line), AAA session limits |
| 4 | `connect` | Connection refused, DNS failure, TLS verification failure | CNC IP, port 30603, firewall, CA certificate or `-k` |
| 5 | `timeout` | CNC did not respond within `CONNECT_TIMEOUT` | CNC load, network latency, `CONNECT_TIMEOUT` |
| 6 | `http` | Other non-2xx responses from CNC | Response body in the `error=` field |
| 7 | `parse` | Invalid JSON, empty plan, missing `<Nodes>` table for `--geoloc` | CNC version and get-plan response |
| 8 | `subprocess` | `trim_nodes` or `mate_convert` missing, non-zero exit, or no output `.db` | `phase=subprocess` stdout/stderr lines |
| 9 | `io` | File read/write errors | Paths and permissions |

Intermediate files (downloaded plan file and trimmed output) are removed in a `finally` block regardless of success or failure.

## Troubleshooting

For troubleshooting script execution issues, please refer to the logs on the Crosswork Planning server at:

```
/mnt/cw_logfs/external-executor-service/1/external-executor.log
```

This log file contains output from the script execution, including any error messages or exceptions that may help diagnose issues with authentication, API calls, or file conversion.

### Log format

Each line has a UTC timestamp, a level, a per-run ID, and `key=value` fields. Every step logs one `phase=<name> status=ok|failed duration_ms=...` line, and the run always ends with a `phase=summary` line, even on failure. Credentials, tickets, and tokens are never logged; only the JWT subject and expiry are decoded.

Example run (paths shortened):

```
2026-11-14T07:38:20.293Z INFO  run=45180697 phase=start script=get_plan_cp_cw.py script_version=1.5 python=3.11.4 requests=2.31.0 host=cp-collector pid=812 cwd=/app/... base_url=https://10.58.239.120:30603 out=/app/.../21_EXTERNAL_SCRIPT.db tmpfile=auto home_dir=/app/... user_upload_dir=/app/... plan_version=default geoloc=false insecure=false timeout_s=20
2026-11-14T07:38:20.299Z INFO  run=45180697 phase=auth status=ok duration_ms=85 source=credentials user=cpuser user_source=script_constant password_source=env jwt_sub=cpuser jwt_exp=2026-11-14T08:38:20Z jwt_ttl_s=3599
2026-11-14T07:38:21.610Z INFO  run=45180697 phase=get_plan status=ok duration_ms=1310 format=pln plan_version=default http=200 http_ms=1290 resp_bytes=75556 bytes=56639
2026-11-14T07:38:21.611Z INFO  run=45180697 phase=write_tmp status=ok duration_ms=0 path=/tmp/get_plan_cp_cw-k3j9x2ab/planfile.pln bytes=56639
2026-11-14T07:38:21.619Z INFO  run=45180697 phase=plan_inventory status=ok duration_ms=7 stage=received nodes=19 circuits=32 interfaces=64 sr_policies=85 sr_policies_srv4=73 sr_policies_srv6=12 cs_sr_policies=3 rsvp_lsps=2 lsps_total=87 demands=2 flex_algos=5 node_flex_algos=32 srlgs=0 pln_format_version=7.10.-1
2026-11-14T07:38:21.619Z INFO  run=45180697 phase=trim_config searched=home_dir,user_upload_dir found=trim_exclude.txt@home_dir not_found=trim_include.txt,trim_include_regex.txt,trim_exclude_regex.txt
2026-11-14T07:38:21.619Z INFO  run=45180697 phase=subprocess tool=trim_nodes description="Trimming nodes" cmd="trim_nodes -plan-file /tmp/get_plan_cp_cw-k3j9x2ab/planfile.pln -out-file /tmp/get_plan_cp_cw-k3j9x2ab/planfile.trim.pln -node-table /app/.../trim_exclude.txt -exclude-node-table true"
2026-11-14T07:38:22.330Z INFO  run=45180697 phase=subprocess tool=trim_nodes stream=stdout Notice [30]: 6 nodes trimmed, 13 nodes in plan post trimming
2026-11-14T07:38:22.330Z INFO  run=45180697 phase=subprocess tool=trim_nodes rc=0 duration_ms=711
2026-11-14T07:38:22.330Z INFO  run=45180697 phase=trim status=ok duration_ms=713 applied=true
2026-11-14T07:38:22.347Z INFO  run=45180697 phase=plan_inventory status=ok duration_ms=16 stage=trimmed nodes=13 circuits=20 interfaces=40 ... delta_nodes=-6 delta_circuits=-12 delta_interfaces=-24 ...
2026-11-14T07:38:22.347Z INFO  run=45180697 phase=subprocess tool=mate_convert description="Converting plan" cmd="mate_convert -plan-file /tmp/get_plan_cp_cw-k3j9x2ab/planfile.trim.pln -out-file /app/.../21_EXTERNAL_SCRIPT.db"
2026-11-14T07:38:22.654Z INFO  run=45180697 phase=subprocess tool=mate_convert rc=0 duration_ms=305
2026-11-14T07:38:22.654Z INFO  run=45180697 phase=convert status=ok duration_ms=306 input=/tmp/get_plan_cp_cw-k3j9x2ab/planfile.trim.pln out=/app/.../21_EXTERNAL_SCRIPT.db out_bytes=88112
2026-11-14T07:38:22.655Z INFO  run=45180697 phase=cleanup removed=/tmp/get_plan_cp_cw-k3j9x2ab/planfile.trim.pln
2026-11-14T07:38:22.655Z INFO  run=45180697 phase=cleanup removed=/tmp/get_plan_cp_cw-k3j9x2ab/planfile.pln
2026-11-14T07:38:22.655Z INFO  run=45180697 phase=cleanup removed=/tmp/get_plan_cp_cw-k3j9x2ab
2026-11-14T07:38:22.655Z INFO  run=45180697 phase=summary status=ok exit=0 total_ms=2362 auth_ms=85 get_plan_ms=1310 write_tmp_ms=0 trim_ms=713 convert_ms=306 plan_bytes=56639 nodes=19 circuits=32 interfaces=64 sr_policies=85 rsvp_lsps=2 demands=2 flex_algos=5 srlgs=0 trim_applied=true out_bytes=88112
```

Example failure (expired stored JWT):

```
2026-11-14T07:38:21.706Z WARNING run=2260d57a phase=auth warning="JWT is expired" jwt_exp=2026-11-14T07:37:21Z source=stored_jwt
2026-11-14T07:38:21.712Z ERROR run=2260d57a phase=get_plan status=failed error_class=auth duration_ms=5 format=pln plan_version=default http=401 error="get_plan /crosswork/nbi/optima/v2/restconf/operations/...:get-plan returned HTTP 401 Unauthorized: ..."
2026-11-14T07:38:21.712Z ERROR run=2260d57a phase=error error_class=auth hint="JWT may be expired or invalid; refresh it or remove it to fall back to credentials"
2026-11-14T07:38:21.712Z ERROR run=2260d57a phase=summary status=failed exit=3 error_class=auth total_ms=7 auth_ms=0 get_plan_ms=5 plan_bytes=n/a nodes=n/a ...
```

### Log verbosity

- Default level is `INFO`.
- `--debug` (or `CW_LOG_LEVEL=DEBUG`) adds a line per HTTP request (method, path, status, time, size), the path of each trim file found, the first 20 plan nodes that did not match DLM Inventory with `--geoloc`, and the Python traceback on failure.
- `CW_LOG_LEVEL=WARNING` keeps only warnings, errors, and failed phases.

### Useful searches

```bash
LOG=/mnt/cw_logfs/external-executor-service/1/external-executor.log
grep 'phase=summary' $LOG                      # one line per run: status, timings, counts
grep 'phase=summary status=failed' $LOG        # failed runs only
grep 'run=45180697' $LOG                       # every line of a single run
grep 'phase=plan_inventory stage=received' $LOG  # plan size trend over time
grep -E ' (WARNING|ERROR) run=' $LOG          # warnings and errors from this script
```

### Plan inventory

After the plan is written to the temporary file, the script counts the main objects in the plan received from CNC and logs them on a `phase=plan_inventory stage=received` line. When trimming runs, a second `stage=trimmed` line logs the trimmed counts and `delta_<counter>` values relative to the received plan. The main counts are repeated on the summary line.

| Counter | Plan table | Notes |
|---------|------------|-------|
| `nodes` | `Nodes` | |
| `circuits` | `Circuits` | |
| `interfaces` | `Interfaces` | |
| `sr_policies` | `LSPs` | Rows with `Type` `SRv4` or `SRv6`; also split into `sr_policies_srv4` and `sr_policies_srv6` |
| `cs_sr_policies` | `CircuitStyleSRPolicies` | Circuit-style SR policies |
| `rsvp_lsps` | `LSPs` | Rows with `Type` `RSVP` |
| `other_lsps` | `LSPs` | Any other `Type`; only logged when non-zero |
| `lsps_total` | `LSPs` | All rows |
| `demands` | `Demands` | |
| `flex_algos` | `FlexAlgorithms` | Flex-algorithm definitions |
| `node_flex_algos` | `NodeFlexAlgorithms` | Per-node flex-algorithm participation |
| `srlgs` | `SRLGs` | |
| `pln_format_version` | `version.dat` | `.pln` only |

- `.txt` plans are counted by reading the data rows under each `<Table>` header.
- `.pln` plans are zip archives containing a SQLite `network.db`; the script extracts it to a temporary directory and counts rows with read-only SQL queries.
- A table that is not present in the plan is reported as `n/a`, which is different from `0`.
- Warnings are logged when the plan has no nodes, when it has nodes but no interfaces (an empty or partial model from CNC), or when trimming removes every node.
- Counting is diagnostic only: if it fails, a warning with `error_class=parse` is logged and the collection continues.

## Relationship to Crosswork Planning Documentation

Per Cisco documentation:

> "You can provide an external script as the initial step in a data collection chain. The startup script is executed before any other collectors in the chain."

This script fulfills that role by providing the network model from CNC, enabling scenarios where:

- Network topology is already managed in Crosswork Network Controller
- You want to synchronize Crosswork Planning with the live CNC network state
- You prefer using CNC's optimization engine data rather than independent SNMP/IGP discovery

## Limitations and Considerations

1. **Single startup script**: Only one startup script per collection chain
2. **Database file requirement**: Downstream collectors fail if the script doesn't produce a valid `.db` file
3. **Credential management**: Hardcoded credentials should be externalized for security
4. **SSL verification**: Script verifies SSL certificates by default; set `INSECURE = True` or pass `-k` for self-signed certificates
5. **AAA session Limits**: As a safeguard against resource exhaustion, it is preferred to use separate CNC credentials for get-plan. Under Admin > AAA Settings, No. of parallel sessions should be set orders higher than No. of parallel sessions per user (e.g. 200 vs 50). 

## References

- [Cisco Crosswork Planning 7.2 Collection Setup Guide - Startup Scripts](https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/setup-guide/cisco-crosswork-planning-7-2-collection-setup-and-administration/m-collectors-in-cp.html#run-startup-script)
- [Cisco Crosswork Network Controller API Documentation](https://developer.cisco.com/docs/crosswork/)

---

*Document Version: 1.5*  
*Script: get_plan_cp_cw.py*  
*Platform: Cisco Crosswork Planning 7.2 or later (verified on 7.2 and 8.0)*
