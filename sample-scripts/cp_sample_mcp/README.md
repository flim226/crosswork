# Crosswork Planning Simulation MCP Server

**Automation tutorial and reference guide**

HTTP- and stdio-based [FastMCP](https://gofastmcp.com) server that exposes Crosswork Planning OPM/Design RPC simulation capabilities to LLM clients (Cursor, Claude Desktop, custom agents).

Crosswork Planning exposes powerful simulation and analysis through the OPM Python Library and DesignAPI. This guide teaches you how to establish connectivity from an external Linux host, build and connect an [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server, and use an LLM client for network planning tasks — link failure what-if analysis, simulation analysis, and traffic growth forecasting.

**Single file:** [`cp_sample_mcp.py`](cp_sample_mcp.py)

**Tags:** crosswork planning, MCP, OPM Python Library, DesignAPI, automation

---

## Table of contents

1. [Overview](#overview)
2. [Background](#background)
3. [Architecture](#architecture)
4. [Prerequisites](#prerequisites)
5. [Part 1 — Establish OPM/Python Library connectivity](#part-1--establish-opmpython-library-connectivity)
6. [Part 2 — Build the MCP server](#part-2--build-the-mcp-server)
7. [Installation](#installation)
8. [Configuration](#configuration)
9. [Part 3 — Configure and connect (stdio)](#part-3--configure-and-connect-stdio)
10. [Running the server](#running-the-server)
11. [Workflow: upload then simulate](#workflow-upload-then-simulate)
12. [Tool reference](#tool-reference)
13. [MCP resources and prompts](#mcp-resources-and-prompts)
14. [Example sessions](#example-sessions)
15. [Remote deployment](#remote-deployment)
16. [Security](#security)
17. [Troubleshooting](#troubleshooting)
18. [Known limitations](#known-limitations)
19. [Related files and documentation](#related-files-and-documentation)

---

## Overview

This MCP server lets an LLM:

- **Upload** a Crosswork plan file from the user's machine (no pre-staging on the server)
- **Inspect** topology (nodes, circuits, demands)
- **Simulate routing** for demands and IGP shortest paths
- **Simulate link failures** and report interface traffic/utilization changes
- **Run Simulation Analysis** (worst-case failure scenarios)
- **Forecast traffic growth** via Create Growth Plans and find oversubscribed circuits

All simulation runs against the **Crosswork Planning DesignAPI** (port **30744**, mTLS) using the OPM Python library — the same engine as the Design GUI.

This guide provides a complete end-to-end workflow in four parts:

1. **Establish OPM/Python Library connectivity** — SDK setup, CA-signed mTLS certificates (CP 8.0), DesignAPI verification
2. **Build the MCP server** — structure a FastMCP application, bootstrap OPM, implement plan upload and simulation tools
3. **Configure and connect the MCP server** — lay out files locally and register stdio transport in Cursor
4. **Use the MCP server for network planning** — upload a plan, run simulations, and interpret results

As a running example, we use the sample WAN plan **`us_wan.txt`** (33 nodes, 50 circuits, 95 demands) and the scenario: *"What happens if the link between San Jose and Kansas City fails?"*

---

## Background

### What Is the OPM Python Library?

The OPM (Operations and Planning Model) Python Library provides a high-level Python API to open, read, and simulate Crosswork Planning network models. Instead of manipulating raw plan file tables, you work with objects such as `Node`, `Interface`, `Circuit`, and `Demand`. The library connects to DesignAPI on the Crosswork Planning VM; simulation computation runs on the server, not on your laptop.

Typical operations include:

- Opening a local plan file: `open_plan(path, host, port, protocol)`
- Running route simulation: `model.route_simulation.recompute()` then reading `demand.route`
- Running traffic simulation after failures: set `model.route_simulation = [failed_circuit]`
- Running Simulation Analysis: `SimulationAnalysis(model, failure_types=[...])`

The same library powers external scripts in the Crosswork Planning Collector framework and customization scripts run from Job Manager. See the [Crosswork Planning Design/OPM Library Package](https://developer.cisco.com/docs/crosswork/planning/customization-scripts-using-crosswork-planning-designopm-library-package/) documentation on Cisco DevNet.

### What Is DesignAPI?

DesignAPI (`designapid`) is the IceSSL/mTLS service on the Crosswork Planning VM that executes Design RPC and OPM operations remotely. The OPM Python library on a Linux host connects to it at:

| Setting | Typical value |
|---------|---------------|
| Host | Crosswork Planning VM IP |
| Port | **30744** |
| Protocol | `ssl` (mTLS) |

Authentication uses **client certificates** onboarded through the OPM REST API (`POST /cp/opm-service/api/v1/certs`). This is separate from the JWT Bearer token used for the Crosswork Planning web gateway on port 30603.

> **CP 8.0 note:** Build 385 requires **CA-signed** client certificates. The SDK's `./generate_client_certs` script produces self-signed certificates that are rejected during onboard (HTTP 400/422). See [Step 3: Generate CA-signed client certificates](#step-3-generate-ca-signed-client-certificates-cp-80) below.

### What Is MCP and Why Use It with Crosswork Planning?

MCP defines how AI clients discover and invoke **tools** (functions with JSON schemas) over a standard transport. The server also exposes **resources** (read-only data such as plan summaries) and **prompts** (workflow templates). An MCP server advertises tools such as `failure_sim` and `get_wc_traffic`; the LLM client decides when to call them based on the user's question.

### What Is stdio (Stream-Based) MCP Transport?

MCP supports several transports. The recommended pattern for Cursor uses **stdio** — the client **spawns** the MCP server as a child process and exchanges JSON-RPC messages over **stdin** and **stdout**. No HTTP port is opened; there is no separate "start the server" step for daily use.

| Transport | How it works | Typical use |
|-----------|--------------|-------------|
| **stdio** (recommended) | Client launches `python3 cp_sample_mcp.py` (stdio is default); messages flow on stdin/stdout | Cursor, Claude Desktop — local IDE integration |
| Streamable HTTP | Client connects to `http://host:8080/mcp` | Remote shared server, multiple clients |

stdio is the recommended pattern for Cursor: the IDE manages the server lifecycle, credentials stay on your workstation, and you avoid exposing an HTTP endpoint on the network.

Compared to ad-hoc OPM automation or REST batch jobs:

| Approach | Strength | Limitation |
|----------|----------|------------|
| Collector external scripts | Integrated into collection chains | Runs only during collection; file-path based |
| Job Manager / REST batch jobs | Scheduled, UI-visible jobs | CP 8.0 userspace import gap on some builds |
| **MCP server (stdio)** | Natural-language interface; upload-from-client; immediate what-if; no HTTP listener; MCP resources for read-only catalog | MCP client and OPM SDK must run on a host with network access to DesignAPI |

The MCP server in this repo (`cp_sample_mcp.py`) runs **locally on the same machine as your MCP client** (e.g. Cursor). Plan files are uploaded at runtime via tool calls — you do not need to pre-copy plans into a staging directory yourself.

### Example Use Case: LLM-Assisted Failure Simulation

Consider a capacity planner investigating a dual-homed WAN. A fiber cut between **cr1.sjc** (San Jose) and **cr1.kcy** (Kansas City) may reroute traffic through Chicago and New York, oversubscribing the NYC–Washington DC link.

Manually, this requires opening the plan in Design, configuring a failure set, running route and traffic simulation, and reading utilization tables. With MCP:

1. The user asks Cursor: *"Upload my plan and simulate failure of the SJC–KCY link."*
2. The LLM calls `upload_plan` with the plan content from the workspace.
3. The LLM calls `failure_sim(node_a="cr1.sjc", node_b="cr1.kcy")`.
4. The tool returns structured JSON: reroute count, interface traffic deltas, and newly oversubscribed links.

Verified result on `us_wan.txt`: **10 reroutes**; **cr2.wdc:to_cr1.nyc** reaches **113.39%** utilization.

---

## Architecture

### stdio (local — recommended)

```
┌─────────────────────────────────────────────────────────────────┐
│  Workstation (same host as Cursor)                              │
│                                                                 │
│  ┌─────────────┐   stdin/stdout    ┌──────────────────────────┐ │
│  │ Cursor      │ ◄──────────────► │ cp_sample_mcp.py │ │
│  │ MCP client  │  JSON-RPC stream  │  --transport stdio        │ │
│  └─────────────┘                   │  .plan_staging/          │ │
│                                    │  CARIDEN_HOME=cw-planning│ │
│                                    └────────────┬─────────────┘ │
└─────────────────────────────────────────────────┼───────────────┘
                                                  │ mTLS :30744
                                                  ▼
                                       ┌──────────────────────┐
                                       │ Crosswork Planning VM │
                                       │ designapid            │
                                       └──────────────────────┘
```

When you send a message in Cursor, the client launches (or reuses) the Python process, calls tools such as `upload_plan` or `failure_sim`, and reads JSON responses from stdout. Diagnostic startup lines go to **stderr** so they do not interfere with the MCP protocol stream.

### HTTP (remote — optional)

```
┌─────────────────┐     Streamable HTTP      ┌──────────────────────────┐
│  LLM / Cursor   │ ───────────────────────► │ cp_sample_mcp.py │
│  MCP client     │      /mcp on :8080       │  (remote host)            │
└─────────────────┘                          └───────────┬──────────────┘
                                                         │
                    upload_plan(content) ──► .plan_staging/
                    plan_ref ──► open_plan() ──► mTLS :30744
                                                         │
                                                         ▼
                                              ┌──────────────────────┐
                                              │ Crosswork Planning VM │
                                              │ designapid (30744)    │
                                              └──────────────────────┘
```

**Plan provision:** The client sends plan **content** via `upload_plan`. The server writes it to a local staging directory and returns a `plan_id`. All other tools accept `plan_ref` pointing at that id.

**No REST gateway required** for simulation — only mTLS client certs for DesignAPI.

---

## Prerequisites

Before you begin, ensure you have:

| Requirement | Notes |
|-------------|-------|
| **Cisco Crosswork Planning 8.0** (or later) deployment | Tested on release-8.0.0 Build 385 |
| **Crosswork Planning VM** reachable on port **30744** | From the machine that runs Cursor / the MCP client |
| **Linux or macOS workstation** | Same machine as the MCP client; Python 3.10+; OPM SDK is Linux-oriented — Linux lab VM or WSL recommended |
| **Design/OPM SDK** (`cp_design_opm_sdk.tgz`) | Extracted to `cw-planning/` (sets `CARIDEN_HOME`) |
| **CA-signed client certificate** + issuing CA PEM | See certificate section; **not** SDK self-signed certs |
| **JWT credentials** (admin or service account) | For OPM REST cert onboard only (`30603`) |
| **Sample plan file** | e.g. `us_wan.txt` exported from Design user space |
| **MCP client** | Cursor, Claude Desktop, or any FastMCP-compatible client |
| **`cp_sample_mcp.py`** | Single-file MCP server (provided in lab files) |
| **`requirements-mcp.txt`** | `fastmcp` + `pydantic` dependencies |

### On the MCP server host

| Requirement | Notes |
|-------------|-------|
| Python 3.10+ | Tested on 3.12 |
| `fastmcp` | `pip install -r requirements-mcp.txt` |
| OPM SDK | `cw-planning/` directory (CARIDEN_HOME) with `lib/python` |
| mTLS certs | CA-signed client cert onboarded on Crosswork; matching files in `$CARIDEN_HOME/etc/certs/` |
| Network | TCP reachability to Crosswork VM port **30744** |

### On the Crosswork Planning VM

| Requirement | Notes |
|-------------|-------|
| DesignAPI running | `POST /cp/opm-service/api/v1/start` |
| Client cert onboarded | `cert` + `ca_cert` + `title` via OPM REST ([Step 4](#step-4-onboard-certificates-on-crosswork)) |
| Lab defaults | Host `198.18.134.229`, DesignAPI `30744` |

### Certificate files (local)

These must match what was uploaded to Crosswork:

```
cw-planning/etc/certs/
├── designapi_user_cert.pem
├── designapi_user_key.pem
└── ca_cert.pem
```

Lab source: copy CA-signed files from `opm-certs-test/` into `cw-planning/etc/certs/` ([Step 3](#step-3-generate-ca-signed-client-certificates-cp-80) and [Step 5](#step-5-sync-local-certificate-files)).

For the full OpenSSL CA workflow, see [Step 3: Generate CA-signed client certificates](#step-3-generate-ca-signed-client-certificates-cp-80).
---

## Part 1 — Establish OPM/Python Library connectivity

This section connects an external Linux host to a running Crosswork Planning instance so the OPM library can open plans and run simulations via DesignAPI.

### OPM connectivity architecture

```
┌─────────────────────┐         mTLS :30744          ┌──────────────────────────┐
│  Linux host         │ ─────────────────────────────► │ Crosswork Planning VM    │
│  CARIDEN_HOME=      │                                │ designapid (DesignAPI)   │
│    cw-planning/     │ ◄───────────────────────────── │ OPM / Design RPC engine  │
│  open_plan(...)     │                                └──────────────────────────┘
└─────────────────────┘
         │
         │  JWT :30603 (cert onboard only)
         ▼
   POST /cp/opm-service/api/v1/certs
   POST /cp/opm-service/api/v1/start
```

### Step 1: Verify Crosswork Planning and DesignAPI

Log in to the Crosswork Planning UI and confirm the deployment is healthy. Obtain a JWT bearer token from Crosswork SSO (via the UI or your organization's token workflow), then configure OPM REST access:

```bash
export TOKEN=<your-jwt-bearer-token>
export OPM="https://<CW_HOST>:30603/cp/opm-service/api/v1"
```

Check DesignAPI status and start it if needed:

```bash
curl -sk -H "Authorization: Bearer $TOKEN" "$OPM/status" | python3 -m json.tool
curl -sk -X POST -H "Authorization: Bearer $TOKEN" "$OPM/start"
```

Expected: DesignAPI reports **running** and listens on port **30744**.

### Step 2: Download and Extract the Design/OPM SDK

Download `cp_design_opm_sdk.tgz` from your Crosswork Planning installation or Cisco software portal. Extract it:

```bash
mkdir -p ~/cw-planning-work
cd ~/cw-planning-work
tar xzf cp_design_opm_sdk.tgz
# Produces cw-planning/ with lib/python, docs, etc/certs/, generate_client_certs, ...
export CARIDEN_HOME=$PWD/cw-planning
```

Verify the Python library path exists:

```bash
ls "$CARIDEN_HOME/lib/python/com/cisco/wae/opm/network"
```

> **Do not rely on `./generate_client_certs` for CP 8.0 onboard.** That script creates a self-signed client certificate. CP 8.0 Build 385 rejects self-signed clients during `POST /certs`.

### Step 3: Generate CA-Signed Client Certificates (CP 8.0)

CP 8.0 validates two PEM files at onboard time:

| Field | Requirement |
|-------|-------------|
| `cert` | Client certificate, **CA-signed** (`issuer` ≠ `subject`) |
| `ca_cert` | Issuing CA with `BasicConstraints: critical, CA:TRUE` |

#### Lab workflow — private OpenSSL CA

Create a CA configuration file **`openssl-ca.cnf`**:

```ini
[ req ]
default_bits       = 4096
prompt             = no
default_md         = sha256
distinguished_name = dn
x509_extensions    = v3_ca

[ dn ]
CN = PLAN274 Test CA
O  = Lab
C  = US

[ v3_ca ]
basicConstraints = critical, CA:TRUE
keyUsage         = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
```

Create a client configuration file **`openssl-client.cnf`**:

```ini
[ req ]
default_bits       = 2048
prompt             = no
default_md         = sha256
distinguished_name = dn
req_extensions     = v3_client

[ dn ]
CN = plan274-design-client
O  = Lab
C  = US

[ v3_client ]
basicConstraints = critical, CA:FALSE
keyUsage         = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
subjectKeyIdentifier = hash
```

Generate the CA and client:

```bash
mkdir -p opm-certs-test && cd opm-certs-test

openssl genrsa -out ca_key.pem 4096
openssl req -x509 -new -nodes -key ca_key.pem -days 3650 \
  -out test_ca.pem -config ../openssl-ca.cnf

openssl genrsa -out client_key.pem 2048
openssl req -new -key client_key.pem -out client.csr -config ../openssl-client.cnf
openssl x509 -req -in client.csr -CA test_ca.pem -CAkey ca_key.pem -CAcreateserial \
  -out client_cert.pem -days 825 -sha256 -extensions v3_client -extfile ../openssl-client.cnf
```

Verify before upload:

```bash
openssl x509 -in test_ca.pem -noout -ext basicConstraints    # CA:TRUE
openssl x509 -in client_cert.pem -noout -issuer -subject       # issuer ≠ subject
openssl verify -CAfile test_ca.pem client_cert.pem             # OK
```

#### Production workflow

Request a **client authentication certificate** from your enterprise PKI. Obtain the **issuing CA PEM** (`BasicConstraints: CA:TRUE`). The upload format is identical; only the issuance step differs.

### Step 4: Onboard Certificates on Crosswork

Upload the CA-signed pair via OPM REST:

```bash
curl -sk -X POST "$OPM/certs" \
  -H "Authorization: Bearer $TOKEN" \
  -F "cert=@opm-certs-test/client_cert.pem" \
  -F "ca_cert=@opm-certs-test/test_ca.pem" \
  -F "title=dev-vm-cert"
```

Expected: HTTP **200** — `{"message": "Certificates imported successfully"}`.

List onboarded certs:

```bash
curl -sk -H "Authorization: Bearer $TOKEN" "$OPM/certs" | python3 -m json.tool
```

### Step 5: Sync Local Certificate Files

The onboard upload and local IceSSL truststore serve different roles. Copy files into the SDK cert directory:

```bash
cp opm-certs-test/client_cert.pem  $CARIDEN_HOME/etc/certs/designapi_user_cert.pem
cp opm-certs-test/client_key.pem   $CARIDEN_HOME/etc/certs/designapi_user_key.pem
cp opm-certs-test/test_ca.pem      $CARIDEN_HOME/etc/certs/ca_cert.pem

# Append DesignAPI server certificate to local trust bundle
curl -sk -H "Authorization: Bearer $TOKEN" \
  "$OPM/certs/server" >> "$CARIDEN_HOME/etc/certs/ca_cert.pem"
```

Restart DesignAPI after any certificate change:

```bash
curl -sk -X POST -H "Authorization: Bearer $TOKEN" "$OPM/stop"
curl -sk -X POST -H "Authorization: Bearer $TOKEN" "$OPM/start"
```

Your local cert directory should contain:

```
cw-planning/etc/certs/
├── designapi_user_cert.pem   ← onboarded client cert
├── designapi_user_key.pem    ← matching private key (never uploaded)
└── ca_cert.pem               ← issuing CA + DesignAPI server cert
```

### Step 6: Configure Shell Environment

Set these variables before running the MCP server or any direct OPM library calls:

```bash
export CARIDEN_HOME=/path/to/cw-planning
export PYTHONPATH=$PYTHONPATH:$CARIDEN_HOME/lib/python
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$CARIDEN_HOME/lib:$CARIDEN_HOME/lib/python

# If the client key is encrypted:
# export CW_CLIENT_KEY_PASS=<password>
```

The MCP server sets these automatically via `_bootstrap_opm_env()` on startup.

### Step 7: Verify Connectivity

From your MCP server directory, confirm DesignAPI reachability with the built-in health tool:

```bash
cd ~/crosswork-mcp
export CARIDEN_HOME=$PWD/cw-planning
export CW_HOST=198.18.134.229

python3 -c "
import cp_sample_mcp as m
print(m.designapi_health_check())
"
```

Expected output includes `certs_ok: true`, `design_api_reachable: true`, and node/circuit/demand counts when a default plan is available (for example `us_wan.txt` next to the server).

You may see benign IceSSL deprecation warnings; they do not affect simulation results.

### How OPM Simulation Works

| Step | Code | Purpose |
|------|------|---------|
| 1 | `open_plan(path, host, 30744, "ssl")` | Open plan via DesignAPI; returns `Network` context manager |
| 2 | `model = network.model` | Access nodes, circuits, demands, interfaces |
| 3 | `model.route_simulation = []` | Clear failure set (baseline) |
| 4 | `model.route_simulation.recompute()` | Run route simulation on server |
| 5 | `demand.route` | Read computed path for a demand |
| 6 | `model.route_simulation = [circuit]` | Simulate circuit failure |
| 7 | `iface.simulated_utilization` | Read post-failure interface utilization |

This is the same engine used by the Crosswork Planning Design GUI.

---

## Part 2 — Build the MCP Server

This section explains **how to construct** the Crosswork Planning MCP server. The reference implementation is [`cp_sample_mcp.py`](cp_sample_mcp.py) (~1718 lines, single file). You can use it as-is, or follow these steps to understand — and extend — the design.

### Design goals

Before writing code, fix the requirements that shaped the server:

| Requirement | Implementation |
|-------------|----------------|
| LLM client provides the plan | `upload_plan` accepts file **content**, not a server-side path |
| Same simulation engine as Design UI | OPM `open_plan()` → DesignAPI on port 30744 |
| Cursor-friendly transport | **stdio** (default) — client spawns the process |
| Portable lab config | Embedded `CONFIG` dict + environment overrides |
| Safe concurrent access | `_opm_lock` serializes Ice/OPM calls |
| Predictable tool output | Tools return `{"ok": true, ...}` JSON; failures raise `ToolError` |
| Input validation | Pydantic models with `extra="forbid"` on upload/delete/analysis args |
| Read-only catalog via MCP resources | `server://health`, `plan://uploads`, `plan://{plan_id}/summary`, etc. |
| Workflow guidance for LLMs | MCP prompts: `link_failure_workflow`, `capacity_planning_workflow` |
| HTTP hardening (optional remote) | Bearer `MCP_API_TOKEN`, rate limiting, audit logging, owner-scoped plans |
| Destructive-op guardrails | `confirm=true` required for large uploads (>10 MiB) and plan deletion |

### Server module layout

The monolith is organized top-to-bottom in dependency order:

```
cp_sample_mcp.py
├── CONFIG + allowlists + _opm_lock
├── _apply_env_overrides() + _bootstrap_opm_env()   ← must run before OPM imports
├── OPM / FastMCP imports
├── Pydantic arg models (UploadPlanArgs, DeletePlanArgs, SimulationAnalysisArgs)
├── AuditMiddleware + RateLimitingMiddleware
├── PlanRegistry (owner-scoped)                   ← upload staging + index.json
├── Helpers: ok(), _fail(), _resolve_plan_ref(), _open_model()
├── Payload builders (_health_payload, _plan_summary_payload, …)
├── Simulation helpers                            ← circuit lookup, iface snapshots, growth math
│   (_collect_link_failure_reroutes, _execute_traffic_growth, …)
├── _create_mcp_server()                          ← auth, middleware, strict_input_validation
├── @mcp.resource (×5)                            ← read-only health and plan catalog
├── @mcp.prompt (×2)                              ← workflow templates for LLMs
├── @mcp.tool functions (×12)                     ← simulation and mutation tools
└── main()                                        ← argparse + mcp.run(transport=…)
```

### Step 1: Create the project skeleton

Create a working directory and install dependencies:

```bash
mkdir -p ~/crosswork-mcp
cd ~/crosswork-mcp
pip install -r requirements-mcp.txt
touch cp_sample_mcp.py
```

`requirements-mcp.txt` pins `fastmcp` and `pydantic` (used for strict tool argument validation).

Minimum imports and paths at the top of the file:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

SCRIPT_DIR = Path(__file__).resolve().parent
_opm_lock = threading.Lock()
```

### Step 2: Add configuration and bootstrap OPM

OPM native libraries must be on `PYTHONPATH` and `LD_LIBRARY_PATH` **before** importing `com.cisco.wae.*`. The bootstrap function validates `CARIDEN_HOME` and patches the environment:

```python
CONFIG: dict[str, Any] = {
    "crosswork": {"host": "198.18.134.229", "design_api_port": 30744, "protocol": "ssl"},
    "opm": {"cariden_home": None, "design_api_timeout_s": 120},
    "plan": {
        "staging_dir": None,
        "max_bytes": 52_428_800,
        "default_plan": None,
        "allowed_root": None,
        "ttl_hours": None,
        "large_upload_confirm_bytes": 10_485_760,
    },
    "mcp": {
        "host": "127.0.0.1",
        "port": 8080,
        "path": "/mcp",
        "rate_limit_rps": 5.0,
        "rate_limit_burst": 10,
    },
}

def _cariden_home() -> Path:
    raw = CONFIG["opm"]["cariden_home"]
    return Path(raw).resolve() if raw else (SCRIPT_DIR / "cw-planning")

def _bootstrap_opm_env() -> Path:
    cariden = _cariden_home()
    if not (cariden / "lib" / "python").is_dir():
        raise RuntimeError(f"CARIDEN_HOME not found: {cariden}")
    os.environ["CARIDEN_HOME"] = str(cariden)
    py_lib = str(cariden / "lib" / "python")
    sys.path.insert(0, py_lib)
    os.environ["PYTHONPATH"] = os.pathsep.join(
        p for p in [os.environ.get("PYTHONPATH", ""), py_lib] if p
    )
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(
        p for p in [str(cariden / "lib"), py_lib, os.environ.get("LD_LIBRARY_PATH", "")] if p
    )
    return cariden

_bootstrap_opm_env()

from com.cisco.wae.opm.network import open_plan
from com.cisco.wae.opm.network.tools.simulation_analysis import SimulationAnalysis
from fastmcp import FastMCP
```

Apply environment overrides (`CW_HOST`, `CARIDEN_HOME`, etc.) in `_apply_env_overrides()` before calling `_bootstrap_opm_env()` — see the full file for the complete override list.

| Step | Code | Purpose |
|------|------|---------|
| 1 | `CONFIG` dict | Default Crosswork host, port, upload limits |
| 2 | `_apply_env_overrides()` | Let MCP client `env` block override defaults |
| 3 | `_bootstrap_opm_env()` | Set paths before any `com.cisco.wae` import |
| 4 | `from ... import open_plan` | Safe only after bootstrap |

### Step 3: Standardize tool responses and errors

LLMs parse structured JSON more reliably than raw exceptions. Successful tool calls return a dict with `"ok": true`:

```python
def ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}

def _fail(message: str, hint: str | None = None) -> None:
    detail = f"{message} Hint: {hint}" if hint else message
    raise ToolError(detail)
```

Wrap tool bodies in `try/except` and call `_fail(str(exc))` on failure. FastMCP converts `ToolError` into a structured MCP error response without crashing the server process. Read-only **resources** (`@mcp.resource`) use `ResourceError` via `_resource_fail()` for the same pattern.

### Step 4: Implement plan upload (`PlanRegistry`)

Because the LLM reads plans from the user's workspace, the server must accept **content** and stage it locally:

1. Sanitize the filename (alphanumeric, `.`, `-`, `_` only)
2. Decode `encoding="text"` (UTF-8) or `encoding="base64"` (binary `.pln`/`.db`)
3. Enforce `max_bytes` (default 50 MB)
4. Write to `.plan_staging/{plan_id}/{name}`
5. Persist metadata in `.plan_staging/index.json`

Core upload logic (with owner isolation and large-upload confirmation):

```python
@dataclass
class UploadedPlan:
    plan_id: str
    name: str
    path: str
    bytes: int
    uploaded_at: str
    encoding: str
    owner_id: str = ""   # scopes plans per HTTP Bearer token or local stdio session

class PlanRegistry:
    def upload(self, name, content, encoding="text", plan_id=None, owner_id=LOCAL_OWNER_ID):
        safe_name = Path(name).name  # basename only — path traversal guard
        raw = content.encode("utf-8") if encoding == "text" else base64.b64decode(content, validate=True)
        if len(raw) > CONFIG["plan"]["max_bytes"]:
            raise ValueError("Plan exceeds max size")
        # ... write to staging, persist index.json, return UploadedPlan
```

The `upload_plan` tool validates arguments through a Pydantic `UploadPlanArgs` model. Uploads above **10 MiB** (`large_upload_confirm_bytes`) return a preview response and require a second call with `confirm=true`.

Resolve `plan_ref` in simulation tools with `_resolve_plan_ref()` (owner-aware):

1. Look up `plan_id` in `PlanRegistry` for the current client (`_owner_id()`)
2. Else treat as a filesystem path under `CW_PLAN_ROOT` (path traversal guard)
3. Else fall back to `CW_DEFAULT_PLAN` or `us_wan.txt`

### Step 5: Wrap OPM model access

Every simulation tool opens and closes a plan per request, under the global lock:

```python
def _cw_connection() -> tuple[str, int, str]:
    c = CONFIG["crosswork"]
    return c["host"], int(c["design_api_port"]), c["protocol"]

@contextmanager
def _open_model(plan_path: Path) -> Iterator[Any]:
    host, port, protocol = _cw_connection()
    with _opm_lock:
        with open_plan(str(plan_path), host, port, protocol) as network:
            yield network.model
```

This pattern ensures IceSSL sessions do not overlap across concurrent MCP tool calls.

### Step 6: Create the FastMCP server instance

Build the server with auth, middleware, and strict validation:

```python
def _create_mcp_server() -> FastMCP:
    return FastMCP(
        "cp-sample-mcp",
        instructions=(
            "Crosswork Planning network simulation. "
            "Read-only data: use resources (server://health, plan://uploads, plan://{plan_id}/summary). "
            "Mutations and simulation: use tools. "
            "Always upload_plan first when the user provides a plan file, "
            "then pass the returned plan_id as plan_ref to simulation tools."
        ),
        auth=_build_auth_provider(),       # Bearer MCP_API_TOKEN for HTTP; None for stdio
        middleware=[AuditMiddleware(), RateLimitingMiddleware(...)],
        strict_input_validation=True,
    )
```

Register **resources** for read-only catalog data (preferred over tools for passive reads):

```python
@mcp.resource("server://health", mime_type="application/json")
def health_resource() -> str:
    return json.dumps(_health_payload())

@mcp.resource("plan://{plan_id}/summary", mime_type="application/json")
def plan_summary_resource(plan_id: str) -> str:
    return json.dumps(_plan_summary_payload(f"upload:{plan_id}"))
```

Register **prompts** as workflow templates the LLM can pull into context:

```python
@mcp.prompt
def link_failure_workflow(node_a: str, node_b: str) -> str:
    return "1. upload_plan … 2. failure_sim …"
```

Register tools with `@mcp.tool`. FastMCP derives JSON schema from type hints and docstrings:

```python
@mcp.tool
def designapi_health_check(plan_ref: str | None = None) -> dict[str, Any]:
    """Verify MCP server config, OPM certs, and DesignAPI connectivity. Prefer server://health."""
    payload = _health_payload(plan_ref)
    if payload.get("ok"):
        return payload
    _fail(payload.get("error", "health check failed"), hint=payload.get("hint"))
```

### Step 7: Port OPM simulation logic into tools

Each `@mcp.tool` function follows the same template:

1. Validate inputs (Pydantic models where applicable; allowlists; numeric bounds)
2. `path = _resolve_plan_ref(plan_ref)`
3. `with _open_model(path) as model:` — run OPM operations
4. `return ok(...)` with structured results, or `_fail(...)` on error

#### Example — baseline demand routing

```python
@mcp.tool
def get_demand_path(
    source: str,
    destination: str,
    plan_ref: str | None = None,
    demand_name: str | None = None,
    service_class: str = "Default",
) -> dict[str, Any]:
    """Simulate how a demand is routed (baseline, no failures)."""
    try:
        path = _resolve_plan_ref(plan_ref)
        with _open_model(path) as model:
            demand = model.demands[{
                "name": demand_name or f"{source}_{destination}",
                "source": source,
                "destination": destination,
                "service_class": service_class,
            }]
            model.route_simulation = []
            model.traffic_simulation = None
            model.route_simulation.recompute()

            if not demand.routed:
                return ok(routed=False, demand=demand.name)

            route = demand.route
            return ok(
                routed=True,
                path_metric=route.total_path_metric,
                latency_ms={"avg": route.average_latency},
                interfaces=[f"{i.node.name}:{i.name}" for i in route.interfaces],
                hop_count=len(route.interfaces),
            )
    except Exception as exc:
        _fail(str(exc))
```

#### Example — link failure with before/after comparison

The failure tool runs simulation **twice** — baseline, then with `model.route_simulation = [failed_circuit]`:

```python
@mcp.tool
def failure_sim(
    plan_ref: str | None = None,
    node_a: str | None = None,
    node_b: str | None = None,
    failed_circuits: list[str] | None = None,
) -> dict[str, Any]:
    """Simulate circuit failure(s) and report traffic changes."""
    try:
        with _open_model(_resolve_plan_ref(plan_ref)) as model:
            failed = _resolve_failed_circuits(model, failed_circuits, node_a, node_b)

            # Baseline
            model.route_simulation = []
            model.traffic_simulation = None
            model.route_simulation.recompute()
            before_paths = {str(d): _path_of(d) for d in model.demands if d.active}
            before_ifaces = _iface_snapshot(model)

            # Failure
            model.route_simulation = failed
            model.route_simulation.recompute()
            after_ifaces = _iface_snapshot(model)

            # Compare reroutes and utilization deltas ...
            return ok(rerouted_count=..., new_oversubscribed=...)
    except Exception as exc:
        _fail(str(exc))
```

#### Additional tools (same pattern)

| Tool | OPM source / API |
|------|------------------|
| `upload_plan` | `PlanRegistry` + Pydantic validation; `confirm=true` for uploads >10 MiB |
| `delete_uploaded_plan` | `PlanRegistry.delete()`; requires `confirm=true` |
| `get_plan_summary`, `list_circuits`, `list_demands` | Iterate `model.*`; prefer matching `plan://` resources for unfiltered reads |
| `get_igp_path` | `model.route_simulation.shortest_path(src, dst, metric)` |
| `get_wc_traffic` | `SimulationAnalysis(model, failure_types=[...])`; `failure_sets` as list or comma-separated string |
| `get_traffic_growth` | `demand.growth_percent` + GenericTool `create_growth_plans` + compound fallback |

See [How OPM Simulation Works](#how-opm-simulation-works) and [`get_traffic_growth`](#get_traffic_growth) for OPM patterns and Build 385 growth-plan limitations.

### Step 8: Wire transport in `main()`

The entry point parses CLI flags and starts FastMCP. **stdio is the default** — ideal for Cursor:

```python
def main() -> int:
    parser = argparse.ArgumentParser(description="Crosswork Planning Simulation MCP Server")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--allow-remote", action="store_true")  # HTTP: bind 0.0.0.0
    parser.add_argument("--cw-host", help="Crosswork VM IP")
    parser.add_argument("--cariden-home", help="Path to cw-planning SDK")
    args = parser.parse_args()

    if args.transport == "http" and not os.environ.get("MCP_API_TOKEN"):
        print("ERROR: MCP_API_TOKEN is required for HTTP transport.", file=sys.stderr)
        return 1

    # ... apply CLI overrides, purge expired uploads, print diagnostics to stderr ...

    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        mcp.run(transport="http", host=..., port=..., path=...,
                host_origin_protection=True, allowed_hosts=[...])
    return 0
```

**Important:** All startup diagnostics and audit logs go to **stderr** so stdout remains a clean MCP JSON-RPC stream in stdio mode.

For optional **remote HTTP** deployment, set `MCP_API_TOKEN` and run with `--transport http`. See [Remote deployment](#remote-deployment).

### Step 9: Test tools without Cursor

Before wiring Cursor, verify tools via direct Python import (same pattern as Part 1's connectivity check):

```bash
cd ~/crosswork-mcp
export CARIDEN_HOME=$PWD/cw-planning
export CW_HOST=198.18.134.229

python3 -c "
import cp_sample_mcp as m
up = m.upload_plan('us_wan.txt', open('us_wan.txt').read())
print('upload:', up)
print('health:', m.designapi_health_check(plan_ref=up['plan_id']))
print('failure:', m.failure_sim(
    plan_ref=up['plan_id'], node_a='cr1.sjc', node_b='cr1.kcy'))
"
```

Expected: `upload` returns 33/50/95 counts; link failure returns `rerouted_count: 10`.

### Step 10: Extend the server

To add a new capability:

1. Prove the OPM logic in the Python REPL with `open_plan`, or add a draft `@mcp.tool` and test via direct import
2. Add a `@mcp.tool` function (or `@mcp.resource` for read-only data) with typed parameters and a clear docstring
3. Use Pydantic models for complex or security-sensitive inputs
4. Use `_resolve_plan_ref` + `_open_model` inside `try/except`; call `_fail(...)` on errors
5. Return `ok(...)` with flat, JSON-serializable fields (no OPM objects)
6. Re-run the direct import test above
7. Reload MCP in Cursor and exercise the new tool in chat

### Build checklist

| Item | Done when |
|------|-----------|
| OPM bootstrap before imports | `import com.cisco.wae...` succeeds on a clean shell |
| Dependencies installed | `pip install -r requirements-mcp.txt` succeeds |
| Certs in `cw-planning/etc/certs/` | `designapi_health_check()` or `server://health` → `certs_ok: true` |
| DesignAPI reachable | `designapi_health_check()` → `design_api_reachable: true` |
| Upload path works | `upload_plan` returns `plan_id` and node counts |
| Baseline routing | `get_demand_path` returns hops and metric |
| Failure simulation | `failure_sim` returns reroutes and oversubscription |
| Resources available | `plan://uploads` and `server://health` readable in MCP client |
| stdio transport | Cursor spawns process; 12 tools + 5 resources + 2 prompts appear in MCP panel |

Reference implementation: [`cp_sample_mcp.py`](cp_sample_mcp.py)

---

## Installation

```bash
# 1. Copy to remote host (minimum footprint)
scp cp_sample_mcp.py requirements-mcp.txt remote:/opt/cw-mcp/
scp -r cw-planning remote:/opt/cw-mcp/

# 2. Install Python dependency
pip install -r requirements-mcp.txt
# or: pip install fastmcp --break-system-packages   # if PEP 668 blocks system pip

# 3. Verify certs
ls /opt/cw-mcp/cw-planning/etc/certs/designapi_user_*.pem
```

On the workstation where Cursor runs:

```bash
mkdir -p ~/crosswork-mcp
cp cp_sample_mcp.py requirements-mcp.txt ~/crosswork-mcp/
cp -r cw-planning ~/crosswork-mcp/
pip install -r requirements-mcp.txt
```

Or install directly:

```
fastmcp>=3.0.0
pydantic>=2.0,<3
```

Lay out files on the **same machine as Cursor** (absolute paths are required in MCP config):

```
~/crosswork-mcp/
├── cp_sample_mcp.py
├── requirements-mcp.txt
├── cw-planning/              # full OPM SDK + etc/certs/
└── .plan_staging/            # created automatically on first upload
```

You do **not** need to copy plan files — the LLM uploads them via `upload_plan` at runtime.

Network requirement:

- **Outbound** TCP from the workstation → `{CW_HOST}:30744` (DesignAPI)
- For HTTP transport only: inbound TCP to MCP host port 8080 from MCP clients

There is **no inbound** MCP port for stdio — transport is local to the spawned process.

---

## Configuration

Configuration is **embedded** in `cp_sample_mcp.py` (`CONFIG` dict) and overridden by **environment variables** or **CLI flags**.

### Embedded defaults (lab)

| Setting | Default |
|---------|---------|
| Crosswork host | `198.18.134.229` |
| DesignAPI port | `30744` |
| Protocol | `ssl` (mTLS) |
| CARIDEN_HOME | `./cw-planning` (next to the MCP server) |
| MCP listen (HTTP) | `127.0.0.1:8080/mcp` (use `--allow-remote` for `0.0.0.0`) |
| Default transport | `stdio` |
| Max upload size | 50 MB |
| Staging dir | `./.plan_staging/` |
| Rate limit | 5 req/s per client (burst 10) |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `CW_HOST` | Crosswork VM IP |
| `CW_DESIGN_API_PORT` | DesignAPI port (default 30744) |
| `CW_DESIGN_API_TIMEOUT` | DesignAPI open warning threshold in seconds (default 120) |
| `CARIDEN_HOME` | Path to OPM SDK |
| `PYTHONPATH` | Must include `$CARIDEN_HOME/lib/python` |
| `LD_LIBRARY_PATH` | Must include `$CARIDEN_HOME/lib` and `$CARIDEN_HOME/lib/python` (Linux) |
| `CW_DEFAULT_PLAN` | Optional server-side default plan path |
| `CW_PLAN_ROOT` | Allowed root for server-path `plan_ref` (path traversal guard) |
| `CW_MAX_PLAN_BYTES` | Max upload size in bytes |
| `CW_PLAN_TTL_HOURS` | Auto-purge uploaded plans after N hours |
| `CW_CLIENT_KEY_PASS` | Password if client key is encrypted |
| `MCP_API_TOKEN` | **Required** for HTTP transport (Bearer auth) |
| `MCP_HOST` | HTTP bind address |
| `MCP_PORT` | HTTP port |
| `MCP_RATE_LIMIT_RPS` | Per-client rate limit (default 5.0) |

### CLI flags

```bash
python3 cp_sample_mcp.py \
  --transport http \
  --allow-remote \
  --port 8080 \
  --path /mcp \
  --cw-host 198.18.134.229 \
  --cariden-home /opt/cw-mcp/cw-planning
```

---

## Part 3 — Configure and connect (stdio)

With the server built (Part 2) and OPM connectivity verified (Part 1), register it in Cursor. The MCP **client** spawns the server process and talks to it over **stdin/stdout** — you do not run a long-lived HTTP service or a separate terminal session for daily use.

The reference server exposes **12 tools**, **5 resources**, and **2 prompts**.

### Configuration recap

| Design choice | Rationale |
|---------------|-----------|
| **stdio transport (default)** | Cursor spawns the process; no HTTP port or reverse proxy |
| **Upload-first plan model** | User provides plan content from their workspace; staged under `.plan_staging/` |
| **Embedded `CONFIG` dict** | Portable defaults; override via env vars in MCP client config |
| **`_opm_lock` threading lock** | Serializes OPM calls for Ice communicator safety |
| **`with _open_model(plan_path)`** | Opens and closes plan per tool call |
| **MCP resources for reads** | `server://health`, `plan://uploads` — lighter than tool calls for catalog data |
| **Pydantic + `strict_input_validation`** | Rejects unknown fields; validates upload/delete/analysis args |
| **Owner-scoped uploads** | HTTP clients isolated by Bearer token hash; stdio uses `local` owner |
| **JSON responses + ToolError** | Success: `{"ok": true, ...}`; failure: structured MCP error via `ToolError` |

### Test stdio transport manually (optional)

Confirm the server starts in stdio mode before wiring Cursor:

```bash
cd ~/crosswork-mcp
export CARIDEN_HOME=$PWD/cw-planning
export CW_HOST=198.18.134.229
export PYTHONPATH=$CARIDEN_HOME/lib/python
export LD_LIBRARY_PATH=$CARIDEN_HOME/lib:$CARIDEN_HOME/lib/python

python3 cp_sample_mcp.py --transport stdio
```

Expected startup on **stderr** (not stdout — stdout is reserved for MCP):

```
Crosswork Planning MCP Server
  CARIDEN_HOME: /home/user/crosswork-mcp/cw-planning
  DesignAPI:    198.18.134.229:30744
  Staging:      /home/user/crosswork-mcp/.plan_staging
  Transport:    stdio
```

The process waits on stdin; press Ctrl+C to exit.

### Configure Cursor to spawn the server

Add to MCP settings (`.cursor/mcp.json` in your project or user config, or **Settings → MCP**). Use **absolute paths**:

```json
{
  "mcpServers": {
    "cp-sample-mcp": {
      "command": "python3",
      "args": [
        "/home/user/crosswork-mcp/cp_sample_mcp.py",
        "--transport",
        "stdio",
        "--cw-host",
        "198.18.134.229",
        "--cariden-home",
        "/home/user/crosswork-mcp/cw-planning"
      ],
      "env": {
        "CARIDEN_HOME": "/home/user/crosswork-mcp/cw-planning",
        "CW_HOST": "198.18.134.229",
        "PYTHONPATH": "/home/user/crosswork-mcp/cw-planning/lib/python",
        "LD_LIBRARY_PATH": "/home/user/crosswork-mcp/cw-planning/lib:/home/user/crosswork-mcp/cw-planning/lib/python"
      }
    }
  }
}
```

Notes:

- **stdio is the default transport**; explicit `--transport stdio` is optional but recommended for clarity.
- Replace `/home/user/crosswork-mcp` with your actual directory.
- On **macOS**, set `DYLD_LIBRARY_PATH` instead of (or in addition to) `LD_LIBRARY_PATH` if native libraries fail to load; Linux lab VM or WSL is often simpler for the OPM SDK.
- Reload MCP servers in Cursor (**Settings → MCP → refresh**) after saving the config. Cursor spawns the process automatically — you do not run `python3 cp_sample_mcp.py` in a separate terminal for normal use.

#### Claude Desktop (stdio)

Claude Desktop uses the same spawn pattern under `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cp-sample-mcp": {
      "command": "python3",
      "args": [
        "/home/user/crosswork-mcp/cp_sample_mcp.py",
        "--transport",
        "stdio"
      ],
      "env": {
        "CARIDEN_HOME": "/home/user/crosswork-mcp/cw-planning",
        "CW_HOST": "198.18.134.229",
        "PYTHONPATH": "/home/user/crosswork-mcp/cw-planning/lib/python",
        "LD_LIBRARY_PATH": "/home/user/crosswork-mcp/cw-planning/lib:/home/user/crosswork-mcp/cw-planning/lib/python"
      }
    }
  }
}
```

#### Generic FastMCP client (stdio)

```python
import asyncio
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

transport = StdioTransport(
    command="python3",
    args=[
        "/home/user/crosswork-mcp/cp_sample_mcp.py",
        "--transport",
        "stdio",
    ],
    env={
        "CARIDEN_HOME": "/home/user/crosswork-mcp/cw-planning",
        "CW_HOST": "198.18.134.229",
        "PYTHONPATH": "/home/user/crosswork-mcp/cw-planning/lib/python",
        "LD_LIBRARY_PATH": "/home/user/crosswork-mcp/cw-planning/lib:/home/user/crosswork-mcp/cw-planning/lib/python",
    },
)

async def main():
    async with Client(transport) as client:
        result = await client.call_tool("designapi_health_check", {})
        print(result)

asyncio.run(main())
```

#### Generic FastMCP client (HTTP)

```python
from fastmcp import Client

async with Client("http://127.0.0.1:8080/mcp") as client:
    result = await client.call_tool("designapi_health_check", {})
    print(result)
```

### Verify the MCP server

#### In Cursor

Open the MCP tools panel (or ask the agent): *"Run designapi_health_check on the cp-sample-mcp server."*

Expected response (abbreviated):

```json
{
  "ok": true,
  "design_api_reachable": true,
  "certs_ok": true,
  "cariden_home": "/home/user/crosswork-mcp/cw-planning",
  "crosswork_host": "198.18.134.229",
  "design_api_port": 30744
}
```

#### Direct Python import (no MCP transport)

Useful when debugging OPM connectivity independent of Cursor:

```bash
cd ~/crosswork-mcp
export CARIDEN_HOME=$PWD/cw-planning
export CW_HOST=198.18.134.229
python3 -c "
import cp_sample_mcp as m
print(m.designapi_health_check())
"
```

If `designapi_health_check` succeeds here but Cursor shows no tools, check absolute paths and `--transport stdio` in `.cursor/mcp.json`.

---

## Running the server

### Local stdio (recommended — Cursor / Claude Desktop)

```bash
cd /opt/cw-mcp
export CARIDEN_HOME=/opt/cw-mcp/cw-planning   # optional if ./cw-planning exists
python3 cp_sample_mcp.py --transport stdio
```

### Remote HTTP

Requires `MCP_API_TOKEN`. Binds to `127.0.0.1` by default; use `--allow-remote` only behind a TLS reverse proxy.

```bash
export MCP_API_TOKEN=your-secret-token
python3 cp_sample_mcp.py --transport http --port 8080
```

Expected HTTP startup output:

```
Crosswork Planning MCP Server
  CARIDEN_HOME: /opt/cw-mcp/cw-planning
  DesignAPI:    198.18.134.229:30744
  Staging:      /opt/cw-mcp/.plan_staging
  Transport:    http
  Listening:    http://127.0.0.1:8080/mcp
  Auth:         Bearer MCP_API_TOKEN
```

The MCP endpoint is: **`http://<host>:8080/mcp`** with `Authorization: Bearer $MCP_API_TOKEN`

**Remote HTTP** (requires `MCP_API_TOKEN` on server; configure Bearer auth in client if supported):

```json
{
  "mcpServers": {
    "cp-sample-mcp": {
      "url": "http://<remote-host>:8080/mcp"
    }
  }
}
```

If the server is on the same machine as Cursor:

```json
{
  "mcpServers": {
    "cp-sample-mcp": {
      "url": "http://127.0.0.1:8080/mcp"
    }
  }
}
```

Restart Cursor or reload MCP servers after starting the Python process.

---

## Workflow: upload then simulate

This is the **recommended flow** when the user provides a plan file that lives on their machine (not on the MCP server).

```
1. Read the user's plan file from the workspace
2. upload_plan(name="plan.txt", content=<text>, encoding="text", validate=true)
   — for plans >10 MiB, first call returns preview; re-call with confirm=true
3. Pass returned plan_id as plan_ref to simulation tools
   — or read plan://{plan_id}/summary / plan://{plan_id}/circuits for catalog data
4. delete_uploaded_plan(plan_id, confirm=true)   # optional cleanup
```

### Step 1 — Upload the plan

The LLM reads the user's plan file and calls:

```json
{
  "tool": "upload_plan",
  "arguments": {
    "name": "us_wan.txt",
    "content": "<full plan file text>",
    "encoding": "text",
    "validate": true
  }
}
```

Response:

```json
{
  "ok": true,
  "plan_id": "a1b2c3d4e5f6",
  "plan_ref": "upload:a1b2c3d4e5f6",
  "name": "us_wan.txt",
  "bytes": 123456,
  "nodes": 33,
  "circuits": 50,
  "demands": 95
}
```

### Step 2 — Run simulations with `plan_ref`

Pass `plan_ref` as the plan id (with or without `upload:` prefix):

```json
{
  "tool": "failure_sim",
  "arguments": {
    "plan_ref": "a1b2c3d4e5f6",
    "node_a": "cr1.sjc",
    "node_b": "cr1.kcy"
  }
}
```

### Step 3 — Cleanup (optional)

```json
{
  "tool": "delete_uploaded_plan",
  "arguments": { "plan_id": "a1b2c3d4e5f6", "confirm": true }
}
```

### Binary plans (.pln / .db)

Base64-encode the file and set `encoding: "base64"`:

```python
import base64
content = base64.b64encode(open("plan.pln", "rb").read()).decode()
# upload_plan(name="plan.pln", content=content, encoding="base64")
```

---

## Tool reference

Successful tool calls return JSON with `"ok": true`. On failure, the server raises `ToolError` (structured MCP error with message and optional hint) — it does not return `"ok": false` in the response body.

### Tools summary

| Category | Tool | Purpose |
|----------|------|---------|
| Plan provision | `upload_plan` | Stage plan content; returns `plan_id`; `confirm=true` for uploads >10 MiB |
| | `list_uploaded_plans` | List staged plans (prefer resource `plan://uploads`) |
| | `delete_uploaded_plan` | Remove staged plan; requires `confirm=true` |
| Catalog | `get_plan_summary` | Node/circuit/demand counts (prefer `plan://{plan_id}/summary`) |
| | `list_circuits` | Filter circuits by node name (prefer `plan://{plan_id}/circuits`) |
| | `list_demands` | Filter demands by source/destination (prefer `plan://{plan_id}/demands`) |
| Route simulation | `get_demand_path` | Baseline demand path |
| | `get_igp_path` | IGP/BGP/TE/latency shortest path |
| | `failure_sim` | Failure + traffic delta report |
| Analysis | `get_wc_traffic` | Worst-case failure scenarios |
| | `get_traffic_growth` | Growth forecast + oversubscription |
| Health | `designapi_health_check` | Certs, config, DesignAPI reachability (prefer `server://health`) |

All simulation tools accept optional `plan_ref` (the `plan_id` from `upload_plan`).

### Plan provision

#### `upload_plan`

Upload plan content for simulation.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | yes | Filename with extension (`.txt`, `.pln`, `.db`) |
| `content` | string | yes | Plan body (UTF-8 text or base64) |
| `encoding` | string | no | `"text"` (default) or `"base64"` |
| `plan_id` | string | no | Custom id; auto-generated if omitted |
| `validate` | bool | no | Open via OPM to verify (default `true`) |
| `confirm` | bool | no | Required `true` for uploads >10 MiB |

**Returns:** `plan_id`, `plan_ref`, `bytes`, optional `nodes`/`circuits`/`demands`

---

#### `list_uploaded_plans`

List all staged uploaded plans.

**Returns:** `plans[]`, `count`

---

#### `delete_uploaded_plan`

| Parameter | Type | Required |
|-----------|------|----------|
| `plan_id` | string | yes |
| `confirm` | bool | yes (`true` to delete) |

---

### Health & catalog

#### `designapi_health_check`

Verify certs, config, and DesignAPI connectivity.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `plan_ref` | string | no | Plan to test open; uses default if omitted |

---

#### `get_plan_summary`

| Parameter | Type | Required |
|-----------|------|----------|
| `plan_ref` | string | no |

**Returns:** `nodes`, `circuits`, `active_circuits`, `demands`, `active_demands`

---

#### `list_circuits`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `plan_ref` | string | null | Plan handle |
| `node_filter` | string | null | Substring match on node names (e.g. `"sjc"`) |
| `active_only` | bool | true | Skip inactive circuits |
| `limit` | int | 100 | Max results (1–500) |

---

#### `list_demands`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `plan_ref` | string | null | Plan handle |
| `source` | string | null | Filter by source node substring |
| `destination` | string | null | Filter by destination node substring |
| `active_only` | bool | true | Skip inactive demands |
| `limit` | int | 100 | Max results (1–500) |

---

### Route simulation

#### `get_demand_path`

Baseline demand routing (no failures).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source` | string | yes | Source node (e.g. `er1.sjc`) |
| `destination` | string | yes | Destination node (e.g. `er1.mia`) |
| `plan_ref` | string | no | Plan handle |
| `demand_name` | string | no | Default: `{source}_{destination}` |
| `service_class` | string | no | Default: `Default` |

**Returns:** `routed`, `path_metric`, `latency_ms`, `interfaces[]`, `hop_count`

**Verified checkpoint (`us_wan.txt`):** `er1.sjc` → `er1.mia` — 6 hops, metric 2169, latency ~33.4 ms

---

#### `get_igp_path`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `source` | string | required | Source node |
| `destination` | string | required | Destination node |
| `plan_ref` | string | null | Plan handle |
| `metric` | string | `igp` | `igp`, `bgp`, `te`, or `latency` |

---

#### `failure_sim`

Simulate one or more circuit failures; compare baseline vs failure traffic sim.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `plan_ref` | string | no | Plan handle |
| `node_a` | string | * | End node of failed link |
| `node_b` | string | * | Other end node |
| `failed_circuits` | list[string] | * | Alternative: explicit circuit ids |
| `top_n` | int | 25 | Max interface changes returned |
| `min_traffic_delta_mbps` | float | 1.0 | Min delta to include |
| `sample_reroutes` | int | 5 | Example rerouted demands |

\* Provide **either** `failed_circuits` **or** both `node_a` and `node_b`.

**Returns:** `failed_circuits`, `active_demands`, `rerouted_count`, `unrouted_count`, `unrouted`, `sample_reroutes`, `interface_changes[]`, `new_oversubscribed[]`, `baseline_oversubscribed_count`

**Verified checkpoint (`us_wan.txt`, SJC–KCY failure):** 10 reroutes; `cr2.wdc:to_cr1.nyc` → 113.39%

---

### Simulation analysis

#### `get_wc_traffic`

Worst-case interface utilization across failure scenarios (Design UI Simulation Analysis).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `plan_ref` | string | null | Plan handle |
| `failure_sets` | string or list[string] | `nodes,circuits` | Comma-separated string or JSON list of failure types |
| `max_fail_per_int` | int | 10 | Max failures per interface |
| `limit` | int | 15 | Top N interfaces returned |

**Allowed failure_sets:** `nodes`, `sites`, `circuits`, `ports`, `portcircuits`, `srlgs`, `external_endpoint_members`, `parallel_circuits`

**Verified checkpoint (`us_wan.txt`, `circuits`):** 29 scenarios; top `cr2.wdc:to_cr1.nyc` 113.39%

---

### Traffic growth

#### `get_traffic_growth`

Create Growth Plans + oversubscription report.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `plan_ref` | string | null | Plan handle |
| `growth_percent` | float | 33.0 | Demand growth % |
| `num_periods` | int | 1 | Number of growth periods |
| `period_inc` | int | 1 | Period increment |
| `growth_method` | string | `COMPOUND` | `COMPOUND` or `SIMPLE` |
| `threshold` | float | 100.0 | Oversubscription threshold % |
| `near_capacity` | float | 80.0 | Near-capacity lower bound % |

**Returns:** `growth_percent`, `num_periods`, `period_inc`, `growth_method`, `traffic_multiplier`, `tool_applied_growth`, `compound_fallback_applied`, `baseline`, `after_growth`, `near_capacity`

**Verified checkpoint (`us_wan.txt`, +33%/1 period):** NYC–WDC 113.83% oversubscribed

---

## MCP resources and prompts

### MCP resources (read-only)

| URI | Description |
|-----|-------------|
| `server://health` | Server, cert, and DesignAPI status |
| `plan://uploads` | Plans uploaded by current client |
| `plan://{plan_id}/summary` | Node/circuit/demand counts |
| `plan://{plan_id}/circuits` | Active circuits (limit 100) |
| `plan://{plan_id}/demands` | Active demands (limit 100) |

### MCP prompts (workflow templates)

| Prompt | Purpose |
|--------|---------|
| `link_failure_workflow(node_a, node_b)` | Step-by-step link failure analysis |
| `capacity_planning_workflow(growth_percent)` | Growth forecast + simulation analysis workflow |

---

## Example sessions

### Example 1: Link failure what-if (SJC–KCY)

**User question:** *"What happens if the link between cr1.sjc and cr1.kcy fails?"*

```
1. upload_plan(name="us_wan.txt", content=<file>)
   → plan_id: abc123

2. list_circuits(plan_ref="abc123", node_filter="sjc")
   → find cr1.sjc ↔ cr1.kcy circuit

3. failure_sim(plan_ref="abc123", node_a="cr1.sjc", node_b="cr1.kcy")
   → rerouted_count: 10, new_oversubscribed: [cr2.wdc:to_cr1.nyc 113.39%, ...]

4. get_demand_path(plan_ref="abc123", source="er1.sjc", destination="er1.mia")
   → baseline path via KCY

5. (After failure context from step 3 sample_reroutes)
   → new path via CHI/NYC
```

Verified output on `us_wan.txt`:

| Metric | Baseline | After SJC–KCY failure |
|--------|----------|------------------------|
| Active demands | 95 | 95 |
| Rerouted | 0 | **10** |
| Unrouted | 0 | 0 |
| cr1.sjc:to_cr1.kcy traffic | 978.6 Mbps | **0** |
| cr2.wdc:to_cr1.nyc utilization | 85.6% | **113.39%** |
| cr1.nyc:to_cr2.wdc utilization | — | **100.02%** |

**Interpretation:** Traffic detours through the northern ring (SJC → CHI → NYC → WDC), oversubscribing the NYC–WDC corridor. This is the same result Design UI would show for an equivalent failure simulation.

Verified baseline routing: **6 hops**, path metric **2169**, latency **~33.4 ms** for `er1.sjc` → `er1.mia`.

---

### Example 2: Simulation analysis (worst-case failures)

**User question:** *"Which interfaces are most at risk under any single circuit failure?"*

```json
{
  "tool": "get_wc_traffic",
  "arguments": {
    "plan_ref": "a1b2c3d4e5f6",
    "failure_sets": "circuits",
    "max_fail_per_int": 10,
    "limit": 15
  }
}
```

Verified on `us_wan.txt`:

- **29 scenarios** evaluated
- Top worst-case: **cr2.wdc:to_cr1.nyc** at **113.39%**

> **Performance note:** Simulation Analysis typically takes **10–30 seconds**. This is normal — the computation runs on DesignAPI.

---

### Example 3: Traffic growth forecasting

**User question:** *"If demand grows 33% over one period, which links become oversubscribed?"*

```json
{
  "tool": "get_traffic_growth",
  "arguments": {
    "plan_ref": "a1b2c3d4e5f6",
    "growth_percent": 33.0,
    "num_periods": 1,
    "period_inc": 1,
    "growth_method": "COMPOUND",
    "threshold": 100.0
  }
}
```

Verified on `us_wan.txt`: NYC–WDC reaches **113.83%** after 33% compound growth.

> **Build 385 note:** The `create_growth_plans` GenericTool may register a growth plan without updating demand traffic. The MCP server detects this and applies a documented compound formula fallback (`compound_fallback_applied: true` in the response).

---

### Example 4: Capacity planning (combined workflow)

```
1. upload_plan(...)
2. get_traffic_growth(plan_ref="abc123", growth_percent=33, num_periods=1)
   → after_growth.oversubscribed: NYC–WDC 113.83%
3. get_wc_traffic(plan_ref="abc123", failure_sets="circuits")
   → worst-case under any single circuit failure
```

---

### Example 5: Natural-language session in Cursor

A typical Cursor session combining multiple tools:

```
User:  I have us_wan.txt in my workspace. Upload it and tell me what happens
       if the SJC-KCY link fails. Also run simulation analysis on circuits.

Agent: 1. upload_plan(us_wan.txt) → plan_id abc123, 33/50/95
       2. failure_sim(node_a=cr1.sjc, node_b=cr1.kcy)
          → 10 reroutes; cr2.wdc:to_cr1.nyc oversubscribed at 113.39%
       3. get_wc_traffic(failure_sets=circuits)
          → 29 scenarios; same NYC-WDC link worst-case 113.39%
       4. Summarize findings for the user
```

The LLM chooses tool order and parameters; you validate the structured JSON results.

---

### Example 6: Quick health check

```
designapi_health_check()
→ design_api_reachable: true, certs_ok: true, nodes: 33
```

---

### Verify results against Design UI

Cross-check MCP tool output against Crosswork Planning Design UI:

| MCP tool | Design UI equivalent |
|----------|---------------------|
| `get_demand_path` | Design → Simulate → Demand routing |
| `failure_sim` | Design → Simulate → Failure simulation (circuit) |
| `get_wc_traffic` | Design → Simulation Analysis |
| `get_traffic_growth` | Design → Create Growth Plans |

For the SJC–KCY scenario, confirm in Design:

1. Open `us_wan.txt` in user space
2. Fail circuit between **cr1.sjc** and **cr1.kcy**
3. Run route + traffic simulation
4. Verify **10 reroutes** and NYC–WDC oversubscription ~113%

---

## Remote deployment

Minimum files on the remote MCP host:

```
/opt/cw-mcp/
├── cp_sample_mcp.py
├── requirements-mcp.txt
├── cw-planning/              # full OPM SDK + etc/certs/
└── .plan_staging/            # created automatically on first upload
```

**You do not need to copy plan files** — clients upload them at runtime.

Network requirements:

- Outbound TCP from MCP host → `{CW_HOST}:30744`
- Inbound TCP to MCP host port 8080 (or your chosen port) from MCP clients

Optional systemd unit:

```ini
[Unit]
Description=Crosswork Planning MCP Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/cw-mcp
Environment=CARIDEN_HOME=/opt/cw-mcp/cw-planning
Environment=CW_HOST=198.18.134.229
Environment=MCP_API_TOKEN=your-secret-token
ExecStart=/usr/bin/python3 /opt/cw-mcp/cp_sample_mcp.py --transport http --allow-remote --port 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

For a shared remote MCP service, use `--transport http --allow-remote` behind a TLS reverse proxy.

---

## Security

| Topic | Guidance |
|-------|----------|
| **stdio transport (default)** | No network listener; server runs as a local child process of the MCP client |
| **HTTP transport (optional)** | Requires `MCP_API_TOKEN` Bearer auth; binds `127.0.0.1` by default; use `--allow-remote` only behind TLS reverse proxy |
| **Host validation** | HTTP mode enables DNS rebinding / Host header protection |
| **Process isolation** | Each Cursor session spawns its own server; staged plans live under `.plan_staging/` on disk |
| **Plan isolation** | Uploaded plans scoped per Bearer token (HTTP) or local session (stdio) |
| **Owner-scoped uploads** | HTTP clients see only their own uploaded plans (scoped by Bearer token hash) |
| **Rate limiting** | Default 5 req/s per client, burst 10 (`MCP_RATE_LIMIT_RPS` to override rate; burst is CONFIG-only) |
| **Audit logging** | Structured logs to stderr (tool/resource/prompt invocations, redacted) |
| **Destructive ops** | `delete_uploaded_plan` and large uploads require `confirm=true` |
| **MCP HTTP exposure** | Never expose unauthenticated HTTP; prefer stdio locally |
| **Upload DoS** | Default 50 MB cap (`CW_MAX_PLAN_BYTES`); confirm required above 10 MiB |
| **Path traversal** | Server-path `plan_ref` restricted to `CW_PLAN_ROOT` |
| **Filename injection** | Basename only + regex allowlist (`SAFE_NAME_RE`) |
| **Input validation** | Pydantic models reject unknown fields; allowlists on failure sets and metrics |
| **Secrets** | Never returned in tool responses; certs stay on disk; pass via `env` in MCP config, not in chat |
| **mTLS** | DesignAPI auth uses client certs, not JWT passwords |
| **Staging TTL** | Set `CW_PLAN_TTL_HOURS` to auto-purge expired uploads |
| **Staging cleanup** | Delete uploaded plans when done; set `CW_PLAN_TTL_HOURS` for auto-purge |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Cursor shows no MCP tools | Use absolute paths; reload MCP in Settings |
| MCP server exits immediately | Run manually with `--transport stdio`; read stderr for `CARIDEN_HOME` or cert errors |
| `ImportError` / `lib*.so` not found | Set `LD_LIBRARY_PATH` (Linux) or `DYLD_LIBRARY_PATH` (macOS) in `env` |
| `ModuleNotFoundError: pydantic` or `fastmcp` | `pip install -r requirements-mcp.txt` in the Python env used by `command` |
| `CARIDEN_HOME not found or invalid` | Set `CARIDEN_HOME` in MCP config `env`; ensure `cw-planning/lib/python` exists |
| `Missing mTLS certificate files` | Sync certs from `opm-certs-test/` to `cw-planning/etc/certs/`; onboard via OPM REST ([Step 4](#step-4-onboard-certificates-on-crosswork)) |
| `design_api_reachable: false` | Confirm DesignAPI is running: `POST .../opm-service/api/v1/start`; check firewall to `:30744`; restart after cert change |
| `POST /certs` HTTP 422 | Missing `ca_cert` field (CP 8.0) |
| `POST /certs` HTTP 400 self-signed | Use CA-signed client cert, not SDK `generate_client_certs` |
| `No plan specified` | Call `upload_plan` first and pass returned `plan_id` as `plan_ref`; or set `CW_DEFAULT_PLAN` |
| `plan_id not found or not authorized` | Plan owned by another HTTP client; re-upload or use correct Bearer token |
| Upload returns `confirm_required: true` | Re-call `upload_plan` with `confirm=true` (plans >10 MiB) |
| Delete returns preview without deleting | Re-call `delete_uploaded_plan` with `confirm=true` |
| IceSSL warnings (safe to ignore) | Benign per Cisco DevNet — deprecated IceSSL property warnings do not affect simulation results |
| Slow SA/growth responses | Normal (10–30s) — simulation runs on DesignAPI |
| Growth traffic unchanged | Build 385 limitation; check `compound_fallback_applied: true` |

---

## Known limitations

| Limitation | Notes |
|------------|-------|
| stdio requires local OPM SDK | OPM SDK must be on the **same machine as the MCP client** unless using HTTP transport |
| No REST batch jobs | Userspace import broken on Build 385; use this MCP server instead |
| No plan save to userspace | Read/simulate only |
| Single OPM lock | Concurrent tool calls serialized for Ice safety |
| Text plans preferred | `.txt` upload tested; binary `.pln`/`.db` via base64 |
| No WMD / modeling daemon | Out of scope |
| HTTP transport | Requires `MCP_API_TOKEN`; never expose unauthenticated HTTP to the network |

---

## Related files and documentation

### Files in this repo

| File | Purpose |
|------|---------|
| [`cp_sample_mcp.py`](cp_sample_mcp.py) | MCP server (this document) |
| [`requirements-mcp.txt`](requirements-mcp.txt) | Python dependencies (`fastmcp`, etc.) |

### External documentation

- [Crosswork Planning Design/OPM Library Package (DevNet)](https://developer.cisco.com/docs/crosswork/planning/customization-scripts-using-crosswork-planning-designopm-library-package/)
- [Authentication for Design RPC / OPM (DevNet)](https://developer.cisco.com/docs/crosswork/planning/authentication/#authentication-for-crosswork-planning-design-rpc-and-opm-python-library-package)
- [Using External Scripts to Manipulate Network Models in Crosswork Planning (XRdocs)](https://xrdocs.io/automation/tutorials/using-external-scripts-to-manipulate-network-models-in-crosswork-planning)
- [FastMCP documentation](https://gofastmcp.com)
- [Model Context Protocol specification](https://modelcontextprotocol.io)

---

*Verified against Cisco Crosswork Planning release-8.0.0 (Build 385), refactored `cp_sample_mcp.py` (FastMCP 3.x), and `us_wan.txt` sample plan, July–August 2026.*
