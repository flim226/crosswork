# Application Note: Using Crosswork Planning Startup Script

## Overview

This application note provides an example on how an external script may be used within **Cisco Crosswork Planning 7.2** as a **Startup Script** in the data collection workflow. Our script `get_plan_cp_cw.py` retrieve the real time network model in the form of a planfile from **Crosswork Network Controller (CNC)** and converts it for use in Crosswork Planning. Since the startup script can be scheduled to execute at a regular cadence as part of a collection workflow, it allows for a automated archival of planfiles for offline planning use cases.

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
3. **Trims** the plan file by including or excluding nodes based on optional node trimming configuration files
4. **Converts** the plan file to `.db` format required by Crosswork Planning

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
| `--tmpfile` | Intermediate plan file name (default: `planfile.pln`); extension determines download format |
| `--ip` | Crosswork Network Controller IP address *(optional, for testing only)* |
| `--username`, `-u` | CNC username *(optional, for testing only)* |
| `--password`, `-p` | CNC password *(optional, for testing only)* |
| `--version`, `-v` | Planfile version (default: empty string) |

> **Note**: The `--ip`, `--username`, `--password` and `--version` parameters are optional and intended for quick testing purposes only. For production deployments, configure these values as constants within the script or use environment variables.

### Example Usage

```bash
# As standalone script (with required positional args)
python get_plan_cp_cw.py ignored output.db devauth.txt netaccess.txt /home/user /uploads /archive --ip 10.58.239.120 -u admin -p mypassword

# The script internally:
# 1. Downloads plan as planfile.pln
# 2. Optionally trims nodes (if trim config files exist in home_dir or user_upload_dir)
# 3. Converts planfile.pln → output.db using mate_convert
```

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

Creates a `requests.Session` with SSL verification controlled by the `VERIFY_SSL` constant. Mounts the `_TimeoutAdapter` for both `https://` and `http://` schemes so all HTTP calls share the session for connection pooling and consistent settings.

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

### `run_command(cmd, description)`

Runs a subprocess command, logs its stdout/stderr via the `logging` module, and raises `RuntimeError` if the command exits with a non-zero return code.

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
7. Delegates node trimming to `find_and_apply_trim()` (see [Node Trimming](#node-trimming))
8. Converts to `.db` format using `mate_convert` via `run_command()`
9. Cleans up temporary files (downloaded plan file and trimmed output) in a `finally` block

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
PLAN_VERSION = ""                    # Plan version (empty = latest)
TMP_PLANFILE = "planfile.pln"        # Intermediate plan file name
CONNECT_TIMEOUT = 20                 # HTTP connect timeout in seconds
VERIFY_SSL = False                   # Set to True for production with valid certs
```

> **Security Note**: For production deployments, consider using environment variables or a secure credential store instead of hardcoded credentials.

## Dependencies

- **Python packages**: `requests`, `urllib3`, `argparse`, `base64`, `os`, `subprocess`, `logging`, `sys`, `typing`  (Satisfied by Crosswork Planning)
- **External tools**: `mate_convert` (Cisco WAE/Crosswork Planning utility), `trim_nodes` (Cisco WAE/Crosswork Planning utility, used for optional node trimming)

## Error Handling

The script handles common error scenarios:
- **HTTP errors**: Authentication failures, API errors
- **Connection errors**: Network unreachability
- **File format errors**: Invalid planfile extensions
- **Subprocess failures**: Non-zero return codes from `trim_nodes` or `mate_convert` raise `RuntimeError`
- **Temporary file cleanup**: Intermediate files (downloaded plan file and trimmed output) are removed in a `finally` block regardless of success or failure

## Troubleshooting

For troubleshooting script execution issues, please refer to the logs on the Crosswork Planning server at:

```
/mnt/cw_logfs/external-executor-service/1/external-executor.log
```

This log file contains output from the script execution, including any error messages or exceptions that may help diagnose issues with authentication, API calls, or file conversion.

Example invocation:

```
026-03-22 03:53:15,061 | DEBUG | src.common.utils | 126 | Startup script get_plan_cp_cw.py is initializing...
Authenticating to Crosswork at 10.58.239.120...
Retrieving plan using get-plan: planfile.pln...
  Retrieved 52446 bytes
Plan saved to: planfile.pln
Checking for trim config files in ['home_dir', 'user_upload_dir']...
  Not found: trim_include.txt (include node table) - skipped
  Found trim_exclude.txt in home_dir (exclude node table)
  Not found: trim_include_regex.txt (include nodes regex) - skipped
  Not found: trim_exclude_regex.txt (exclude nodes regex) - skipped
Trimming nodes: trim_nodes -plan-file planfile.pln -out-file planfile.trim.pln -node-table /app/external-executor/linux/work/data/7_NETWORK/21_EXTERNAL_SCRIPT/input/trim_exclude.txt -exclude-node-table true
22-3-2026::3:53:14.530918  Notice [30]: 12 nodes trimmed, 13 nodes in plan post trimming

Converting plan: planfile.trim.pln -> /app/external-executor/linux/work/data/7_NETWORK/21_EXTERNAL_SCRIPT/sys-out/21_EXTERNAL_SCRIPT.db: mate_convert -plan-file planfile.trim.pln -out-file /app/external-executor/linux/work/data/7_NETWORK/21_EXTERNAL_SCRIPT/sys-out/21_EXTERNAL_SCRIPT.db
Cleaned up temporary file: planfile.pln
Cleaned up temporary file: planfile.trim.pln
```

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
4. **SSL verification**: Script disables SSL verification by default (`VERIFY_SSL = False`) for self-signed certificates
5. **AAA session Limits**: As a safeguard against resource exhaustion, it is preferred to use separate CNC credentials for get-plan. Under Admin > AAA Settings, No. of parallel sessions should be set orders higher than No. of parallel sessions per user (e.g. 200 vs 50). 

## References

- [Cisco Crosswork Planning 7.2 Collection Setup Guide - Startup Scripts](https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/setup-guide/cisco-crosswork-planning-7-2-collection-setup-and-administration/m-collectors-in-cp.html#run-startup-script)
- [Cisco Crosswork Network Controller API Documentation](https://developer.cisco.com/docs/crosswork/)

---

*Document Version: 1.3*  
*Script: get_plan_cp_cw.py*  
*Platform: Cisco Crosswork Planning 7.2*
