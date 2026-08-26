# Application Note: Retrieving Plan Files from Crosswork Network Controller

## Overview

This application note describes how to use the `get_plan.py` script to retrieve network plan files from **Cisco Crosswork Network Controller (CNC)**. The script provides a simple command-line interface to authenticate with CNC and download the current network model as a plan file in either `.pln` (binary) or `.txt` (text) format.

## About Cisco Crosswork Network Controller

Cisco Crosswork Network Controller (CNC) is a unified platform for network automation that provides end-to-end network management and orchestration capabilities for service provider and enterprise networks. It delivers:

| Capability | Description |
|------------|-------------|
| **Network Visualization** | Real-time topology views showing physical and logical network layers, including IGP, SR, and segment routing paths |
| **Path Computation** | Centralized path computation element (PCE) functionality for traffic engineering and SR-TE policy management |
| **Network Optimization** | Crosswork Optimization Engine integration for bandwidth optimization, tactical TE, and local congestion mitigation |
| **Provisioning & Automation** | Network service provisioning with model-driven configuration and closed-loop automation |
| **Analytics & Assurance** | Health monitoring, performance analytics, and fault correlation across the network |

CNC maintains a comprehensive real-time network model that includes:

- **Topology**: Nodes, interfaces, links, and their attributes (IGP metrics, TE metrics, bandwidth, SRLGs)
- **Traffic Engineering**: SR-TE policies, RSVP-TE LSPs, and their path information
- **Traffic Demands**: Measured traffic flows and demand matrices
- **Configuration State**: Device configurations and operational status

The network model can be exported as a **plan file** using the Optimization Engine's REST API, which is exactly what `get_plan.py` accomplishes.

## Script Purpose

`get_plan.py` is a standalone command-line utility that:

1. **Authenticates** to Crosswork Network Controller using SSO (Single Sign-On)
2. **Retrieves** the current network plan file via the CNC RESTCONF API
3. **Saves** the plan file locally in the specified format (`.pln` or `.txt`)

This enables network operators and planning engineers to:

- Export the live network model for offline analysis
- Archive network snapshots at specific points in time
- Use CNC data as input for external planning tools
- Perform what-if simulations using Cisco WAE Design or Crosswork Planning

## Script Architecture

### Authentication Flow

The script implements CNC's two-step SSO authentication process:

```
┌─────────────────┐     ┌─────────────────────────────────┐
│   get_plan.py   │────▶│  CNC SSO Endpoint               │
│                 │     │  https://<IP>:30603/crosswork/  │
│                 │     │  sso/v1/tickets                 │
└─────────────────┘     └─────────────────────────────────┘
         │                           │
         │  1. POST username/password│
         │◀──────────────────────────│
         │     Returns: TGT ticket   │
         │                           │
         │  2. POST TGT + service    │
         │◀──────────────────────────│
         │     Returns: JWT token    │
         ▼
```

**Step 1**: Submit credentials to obtain a Ticket Granting Ticket (TGT)
**Step 2**: Exchange TGT for a JWT (JSON Web Token) for API access

### Plan Retrieval Flow

```
┌─────────────────┐     ┌─────────────────────────────────┐
│   get_plan.py   │────▶│  CNC Optimization Engine API    │
│  (with JWT)     │     │  /crosswork/nbi/optima/v2/      │
│                 │     │  restconf/operations/get-plan   │
└─────────────────┘     └─────────────────────────────────┘
         │                           │
         │  POST: get-plan request   │
         │  - version: "7.10"        │
         │  - format: "pln" or "txt" │
         │◀──────────────────────────│
         │  Response: base64 content │
         ▼
┌─────────────────┐
│  Local File     │
│  (output.pln    │
│   or output.txt)│
└─────────────────┘
```

## Command Line Interface

### Usage

```bash
python get_plan.py --ip <CNC_IP> --username <USER> --password <PASS> --planfile <OUTPUT_FILE>
```

### Arguments

| Argument | Short | Required | Description |
|----------|-------|----------|-------------|
| `--ip` | | Yes | Crosswork Network Controller IP address |
| `--username` | `-u` | No | CNC username for authentication |
| `--password` | `-p` | No | CNC password for authentication |
| `--jwt` | `-j` | No | Path to a JWT file. When `--username`, `--password`, and `--jwt` are omitted, the script uses `~/.crosswork/<ip>.jwt` if it exists. |
| `--planfile` | `-f` | Yes | Output file name (must end with `.txt` or `.pln`) |
| `--version` | `-v` | No | Planfile schema version (default: `7.10`) |

### Output Format Selection

The script automatically determines the plan file format based on the output filename extension:

| Extension | Format | Description |
|-----------|--------|-------------|
| `.pln` | Binary | Compressed binary format, smaller file size |
| `.txt` | Text | Human-readable text format, useful for debugging |

### Example Usage

```bash
# Retrieve plan file in binary format
python get_plan.py --ip 198.18.134.219 -u admin -p mypassword -f network.pln

# Retrieve plan file in text format
python get_plan.py --ip 198.18.134.219 -u admin -p mypassword -f network.txt

# Specify a different planfile schema version
python get_plan.py --ip 198.18.134.219 -u admin -p mypassword -f network.pln -v 7.6

# Use the default JWT created by cw_get_jwt.py
python get_plan.py --ip 198.18.134.219 -f network.pln
```

### Sample Output

```
Authenticating to 198.18.134.219...
Retrieving plan: network.pln...
Plan saved to: network.pln
```

## Key Functions

### `get_auth_ticket(ip, username, password)`

Performs two-step SSO authentication against CNC:

```python
def get_auth_ticket(ip: str, username: str, password: str) -> str:
    # Step 1: POST credentials → TGT ticket
    # Step 2: POST TGT + service URL → JWT token
    return jwt_token
```

**Endpoint**: `https://<ip>:30603/crosswork/sso/v1/tickets`

### `get_plan(ip, ticket, plan_name, format, version)`

Retrieves the plan file from CNC's Optimization Engine API:

```python
def get_plan(ip: str, ticket: str, plan_name: str, format: str, version: str) -> bytes:
    # POST request to get-plan endpoint
    # Decode base64 response content
    return plan_content_bytes
```

**Endpoint**: `https://<ip>:30603/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-operations:get-plan`

**Request Payload**:
```json
{
  "input": {
    "version": "7.10",
    "format": "pln"
  }
}
```

### `main()`

Orchestrates the complete workflow:
1. Parse command-line arguments
2. Validate output file extension
3. Authenticate to CNC
4. Retrieve plan file
5. Save to local file

## API Details

### Get-Plan REST API

| Property | Value |
|----------|-------|
| **Method** | POST |
| **Endpoint** | `/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-operations:get-plan` |
| **Content-Type** | `application/yang-data+json` |
| **Accept** | `application/yang-data+json` |
| **Authorization** | Bearer token (JWT) |

### Response Format

The API returns a JSON response with base64-encoded plan file content:

```json
{
  "cisco-crosswork-optimization-engine-operations:output": {
    "planfile-content": "<base64-encoded-content>"
  }
}
```

The script handles both `planfile-content` and `plan-file-content` response keys for compatibility across CNC versions.

## Dependencies

### Python Packages

| Package | Purpose |
|---------|---------|
| `requests` | HTTP client for REST API calls |
| `urllib3` | SSL warning suppression for self-signed certificates |
| `argparse` | Command-line argument parsing |
| `base64` | Decoding API response content |
| `os` | File path operations |

### Installation

```bash
pip install requests urllib3
```

## Error Handling

The script handles common error scenarios with informative messages:

| Error Type | Scenario | Message |
|------------|----------|---------|
| `HTTPError` | Invalid credentials, API errors | Displays HTTP status and response body |
| `ConnectionError` | Network unreachable, CNC down | "Connection Error: Could not connect to <IP>" |
| File format error | Invalid extension | "Error: --planfile must have .txt or .pln extension" |

### Example Error Output

```bash
# Authentication failure
HTTP Error: 401 Client Error: Unauthorized
Response: {"error": "Invalid credentials"}

# Connection failure
Connection Error: Could not connect to 198.18.134.219

# Invalid file extension
Error: --planfile must have .txt or .pln extension, got '.csv'
```

## Security Considerations

### SSL/TLS Verification

The script disables SSL certificate verification (`verify=False`) to support self-signed certificates commonly used in lab environments:

```python
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
response = requests.post(url, verify=False)
```

> **Production Note**: For production deployments with valid certificates, remove `verify=False` or set `verify='/path/to/ca-bundle.crt'` to enable certificate validation.

### Credential Handling

- Credentials are passed via command-line arguments
- For production use, consider:
  - Environment variables
  - Secure credential stores
  - Integration with enterprise secrets management

### AAA Session Limits

When running the script frequently or from multiple sources, be aware of CNC's AAA session limits:

- Navigate to **Admin > AAA Settings** in CNC
- Ensure **No. of parallel sessions** is higher than **No. of parallel sessions per user**
- Recommended: Use a dedicated service account for automated plan retrieval

## Use Cases

### 1. Network Snapshot Archive

Create periodic snapshots of the network model for historical analysis:

```bash
# Create timestamped backup
python get_plan.py --ip 198.18.134.219 -u admin -p pass -f "network_$(date +%Y%m%d_%H%M%S).pln"
```

### 2. Offline Planning Analysis

Export the network model for use in WAE Design or Crosswork Planning:

```bash
# Export for WAE Design
python get_plan.py --ip 198.18.134.219 -u admin -p pass -f network.pln
mate_convert -plan-file network.pln -out-file network.db
```

### 3. CI/CD Integration

Integrate with automation pipelines for network validation:

```bash
# In CI/CD pipeline
python get_plan.py --ip $CNC_IP -u $CNC_USER -p $CNC_PASS -f baseline.pln
# Run validation scripts against baseline.pln
```

### 4. Multi-Domain Export

Export from multiple CNC instances for multi-domain analysis:

```bash
# Domain 1
python get_plan.py --ip 10.0.1.100 -u admin -p pass1 -f domain1.pln

# Domain 2
python get_plan.py --ip 10.0.2.100 -u admin -p pass2 -f domain2.pln
```

## Comparison with get_plan_cp_cw.py

| Feature | get_plan.py | get_plan_cp_cw.py |
|---------|-------------|-------------------|
| **Purpose** | Standalone plan retrieval | Crosswork Planning startup script |
| **Output Format** | `.pln` or `.txt` | `.db` (converted) |
| **Integration** | Command-line only | Crosswork Planning Collector |
| **mate_convert** | Not included | Automatic conversion |
| **Credentials** | Command-line args | Hardcoded/configurable |

## Troubleshooting

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| Connection refused | CNC not reachable | Verify IP, port 30603, and firewall rules |
| 401 Unauthorized | Invalid credentials | Verify username/password |
| 403 Forbidden | Insufficient permissions | Ensure user has Optimization Engine access |
| Empty plan file | Schema version mismatch | Try different `--version` values |
| Timeout | Large network model | Increase timeout in requests call |

### Debugging Tips

1. **Test connectivity**:
   ```bash
   curl -k https://<CNC_IP>:30603/crosswork/sso/v1/tickets
   ```

2. **Use text format for debugging**:
   ```bash
   python get_plan.py --ip 198.18.134.219 -u admin -p pass -f debug.txt
   cat debug.txt | head -50
   ```

3. **Check CNC logs** for API errors on the CNC server

## References

- [Cisco Crosswork Network Controller Documentation](https://www.cisco.com/c/en/us/support/cloud-systems-management/crosswork-network-controller/series.html)
- [Crosswork Network Controller API Documentation](https://developer.cisco.com/docs/crosswork/)
- [Crosswork Optimization Engine REST API Guide](https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-optimization-engine/api-guide.html)

---

*Document Version: 1.0*  
*Script: get_plan.py*  
*Platform: Cisco Crosswork Network Controller*
