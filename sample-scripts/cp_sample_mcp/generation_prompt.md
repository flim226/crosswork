# Generation Prompt: Crosswork Planning Simulation MCP Server

## How to use this prompt

Copy everything under **Prompt** into your coding agent. Replace any bracketed
placeholders if your environment differs. Give the agent access to the Cisco
Crosswork Planning OPM SDK documentation and installed SDK so it can verify API
names rather than inventing them.

## Prompt

You are a senior Python engineer experienced with:

- Model Context Protocol (MCP) and FastMCP
- Cisco Crosswork Planning
- The OPM Python Library and DesignAPI
- Secure local and remote service design
- Pydantic validation and typed Python

Build a production-quality, single-file Python MCP server named
`cp_sample_mcp.py`. Its purpose is to let an MCP client upload a Crosswork
Planning plan and perform topology inspection, route simulation, explicit link
failure analysis, worst-case Simulation Analysis, LSP/SR-policy path inspection,
and traffic-growth forecasting.

Use the implementation at
`https://github.com/flim226/crosswork/blob/main/sample-scripts/cp_sample_mcp/cp_sample_mcp.py`
as the behavioral reference. Produce an independently written implementation
with comparable scope and quality. Do not merely summarize, omit major
capabilities, or return pseudocode.

### Deliverable

Return the complete contents of one executable Python file,
`cp_sample_mcp.py`. It must be runnable after its declared dependencies and the
Crosswork Planning SDK are installed. Do not split the implementation into
multiple modules.

At the top of the file, include:

- `#!/usr/bin/env python3`
- A concise module docstring
- Local stdio and remote HTTP launch examples
- Required environment-variable names, but no real credentials or tokens

Target Python 3.11 or newer. Use type hints throughout. Keep imports, formatting,
and naming compatible with Ruff/Black-style conventions.

### Technology and SDK constraints

Use:

- `fastmcp.FastMCP`
- Pydantic v2 strict models (`extra="forbid"`) for sensitive or complex inputs
- Cisco OPM APIs such as `open_plan`
- Cisco Design model/RPC APIs where required for growth-plan generation
- `SimulationAnalysis` for worst-case failure analysis
- stdlib facilities for CLI parsing, logging, files, locking, hashing, UUIDs,
  temporary files, and time handling

Before importing Cisco SDK modules:

1. Resolve `CARIDEN_HOME` from the environment or a configurable local default.
2. Verify that its `lib/python` directory exists.
3. Add the SDK Python path to `sys.path` and configure required runtime library
   paths.
4. Raise an actionable startup error when the SDK is unavailable.

Do not invent Cisco API methods. Verify all OPM and Design RPC calls against the
installed SDK or official documentation. If an API differs by supported
Crosswork release, isolate that compatibility logic and explain it in comments.

### Configuration

Provide a central configuration structure with environment-variable overrides
for at least:

- Crosswork/DesignAPI host, port, protocol, and timeout
- `CARIDEN_HOME`
- Default plan
- Allowed local plan root
- Plan staging directory
- Maximum upload size
- Large-upload confirmation threshold
- Uploaded-plan TTL
- MCP host, port, and path
- Rate-limit rate and burst
- HTTP bearer token

Use safe defaults:

- stdio transport by default
- HTTP bound to `127.0.0.1` by default
- DesignAPI protocol set to TLS/SSL
- no embedded password, token, private key, client secret, or certificate

Configuration errors must produce actionable messages.

### MCP server instructions

Create one `FastMCP` server with strict input validation and server instructions
that tell clients:

- Upload a user-provided plan before simulation.
- Pass the returned plan ID/reference to later calls.
- Use MCP resources for unfiltered, read-only data.
- Use `failure_sim` only for a specific known circuit/link failure.
- Use `get_wc_traffic` alone for worst-case or Simulation Analysis questions.
- Call `list_lsps` before `get_lsp_path`.
- Treat an LSP route simulation interface set as unordered; use the resolved
  segment-list expansion for ordered hops.

### Uploaded-plan registry

Implement a persistent, server-side plan registry:

- Store uploaded plans beneath a staging directory.
- Persist metadata in a JSON index.
- Record plan ID, safe filename, resolved path, byte size, upload time, encoding,
  and owner ID.
- Accept UTF-8 text and validated base64 binary content.
- Allow only safe filename and plan-ID characters.
- Enforce upload-size limits.
- Require an explicit `confirm=true` retry for large uploads.
- Support lookup, list, delete, and TTL purge.
- Prevent path traversal.
- Restrict direct filesystem plan references to a configured allowed root.
- Scope uploaded plans to the current authenticated HTTP client; use a stable
  local owner for stdio.
- Do not disclose another owner's plan existence or metadata.
- Require an explicit confirmation retry before deletion.

Accept plan references in a clear form such as `upload:<plan_id>`, while also
allowing an authorized bare ID. Support a configured default plan and an
optional local sample-plan fallback.

### Concurrency and response conventions

Assume the OPM client is not thread-safe. Serialize OPM/DesignAPI operations
with a process-level lock.

Return JSON-serializable dictionaries from all tools. Use a consistent success
envelope:

```python
{"ok": True, ...}
```

Convert expected user/configuration/SDK failures into concise FastMCP
`ToolError` or `ResourceError` messages with a useful hint. Do not leak stack
traces, tokens, certificate contents, or sensitive filesystem details to remote
clients.

### MCP resources

Implement these read-only JSON resources:

1. `server://health`
   - SDK path
   - configured DesignAPI endpoint
   - required certificate-file presence
   - DesignAPI reachability when a plan can be opened
   - actionable error/hint fields

2. `plan://uploads`
   - plans owned by the current client only

3. `plan://{plan_id}/summary`
   - node, circuit, active-circuit, demand, active-demand, LSP, active-LSP, and
     LSP-path counts

4. `plan://{plan_id}/circuits`
   - bounded active-circuit catalog

5. `plan://{plan_id}/demands`
   - bounded active-demand catalog

### MCP prompts

Implement:

1. `link_failure_workflow(node_a, node_b)`
   - upload if needed
   - inspect circuits
   - run the explicit failure simulation
   - review reroutes and new oversubscriptions
   - inspect affected demand paths

2. `capacity_planning_workflow(growth_percent=33.0)`
   - upload if needed
   - inspect baseline summary
   - run traffic growth
   - run worst-case circuit-failure Simulation Analysis
   - summarize oversubscribed and near-capacity circuits

### MCP tools

Implement all tools below with detailed docstrings, bounded inputs, and
structured outputs.

#### Plan lifecycle and catalog

1. `upload_plan(name, content, encoding="text", plan_id=None, validate=True, confirm=False)`
   - stage and register content
   - optionally open it through OPM to validate it
   - return `plan_id`, canonical `plan_ref`, file metadata, and model counts

2. `list_uploaded_plans()`

3. `delete_uploaded_plan(plan_id, confirm=False)`

4. `designapi_health_check(plan_ref=None)`

5. `get_plan_summary(plan_ref=None)`

6. `list_circuits(plan_ref=None, node_filter=None, active_only=True, limit=100)`
   - include circuit identity, endpoints, interfaces, capacity, and active state
   - cap the limit and report truncation

7. `list_demands(plan_ref=None, source=None, destination=None, active_only=True, limit=100)`
   - include name, endpoints, service class, traffic, and active state

#### Route and LSP inspection

8. `get_demand_path(source, destination, plan_ref=None, demand_name=None, service_class="Default")`
   - reset failures
   - recompute baseline routing
   - return routed state, traffic, metric, latency, ECMP percentage, interfaces,
     traffic shares, and hop count

9. `get_igp_path(source, destination, plan_ref=None, metric="igp")`
   - allow only documented metrics such as IGP, BGP, TE, and latency
   - return the shortest-path interface sequence

10. `list_lsps(plan_ref=None, source=None, destination=None, active_only=True, limit=100)`
    - include type, setup bandwidth, color, state, and path options

11. `get_lsp_path(source, plan_ref=None, lsp_name=None, destination=None, color=None, destination_ip=None, path_option=None)`
    - resolve a named RSVP-TE LSP or an SR policy identified by source,
      destination, and color
    - support a specific path option
    - recompute baseline routing
    - return routed state, type, endpoints, setup bandwidth, color, metric,
      latency, and ECMP data
    - return configured segment-list hops
    - expand interface SIDs directly
    - expand node SIDs using IGP shortest-path fill
    - append IGP fill from the final segment anchor to the destination
    - return warnings for unsupported or unresolved segment steps
    - clearly distinguish:
      - `resolved_ordered_path`
      - unordered route-simulation `interface_set`
      - unconstrained `reference_igp_shortest_path`

#### Explicit failure simulation

12. `failure_sim(plan_ref=None, node_a=None, node_b=None, failed_circuits=None, top_n=25, min_traffic_delta_mbps=1.0, sample_reroutes=5)`
    - accept either a circuit list or a node pair
    - first compute baseline demand paths and per-interface traffic/utilization
    - apply the requested failed circuits and recompute
    - return:
      - failed circuit IDs
      - active-demand count
      - rerouted and unrouted counts
      - bounded sample reroutes with before/after paths
      - largest bounded interface traffic changes
      - utilization before/after
      - newly oversubscribed interfaces
      - baseline oversubscription count

This tool is for deterministic what-if analysis, not worst-case analysis.

#### Worst-case Simulation Analysis

13. `get_wc_traffic(plan_ref=None, failure_sets="nodes,circuits", max_fail_per_int=10, limit=15)`
    - validate failure-set names against an explicit allowlist
    - support string or list input
    - use Cisco `SimulationAnalysis`
    - rank interfaces by worst-case utilization
    - return failure sets, scenario count, and bounded top interfaces

Do not implement worst-case analysis by repeatedly calling `failure_sim`.

#### Traffic growth

14. `get_traffic_growth(plan_ref=None, growth_percent=33.0, num_periods=1, period_inc=1, growth_method="COMPOUND", threshold=100.0, near_capacity=80.0)`
    - collect baseline per-circuit utilization
    - set demand growth where required by the SDK
    - invoke the documented `create_growth_plans` GenericTool through Design RPC
    - inspect the generated plan
    - detect known releases where the GenericTool reports success but does not
      apply growth
    - in that compatibility case, apply a clearly reported compound-growth
      fallback to demand traffic using the documented RPC API
    - serialize to a unique temporary file and reopen via OPM
    - always clean up temporary local files
    - return multiplier, whether tool growth or fallback was applied, baseline,
      after-growth oversubscriptions, and near-capacity circuits

For `SIMPLE` growth, either implement its mathematically correct behavior or
reject it explicitly if the installed SDK cannot support it. Never silently
apply compound growth while labeling it simple.

### Security requirements

Implement all of the following:

- Never hardcode credentials, bearer tokens, private keys, certificates, or
  connection strings containing credentials.
- Read HTTP authentication material only from environment variables or a
  secret provider.
- Require bearer-token authentication for HTTP transport.
- Compare tokens in constant time.
- Refuse to start HTTP transport without authentication.
- Keep stdio as the default local mode.
- Require an explicit flag before binding HTTP to `0.0.0.0`.
- Enable host/origin protection for HTTP.
- Recommend a TLS reverse proxy for remote deployment.
- Add per-client rate limiting.
- Add structured audit logging for tools, resources, and prompts, including
  target, owner/client identifier, duration, result, and exception class, but
  never log plan contents or credentials.
- Send all logs and startup diagnostics to stderr so stdout remains valid MCP
  protocol output in stdio mode.
- Validate all enum-like inputs through allowlists.
- Bound list sizes, samples, and other potentially expensive parameters.
- Use unique temporary names and deterministic cleanup.

The health check must verify the presence of expected certificate files without
reading or returning their contents. Add comments/documentation requiring
operators to inspect each X.509 certificate with:

```bash
openssl x509 -text -noout -in <certificate_file>
```

Operators must verify:

- validity dates
- RSA keys are at least 2048 bits or EC keys use P-256 or stronger
- signatures use SHA-2 or stronger, not MD5/SHA-1
- whether the certificate is self-signed and, if so, that this is intentional
  and not for public production use

Do not weaken TLS, disable certificate verification, use deprecated crypto, or
generate custom cryptographic algorithms.

### CLI

Implement `main()` with:

- `--transport {stdio,http}`, default `stdio`
- `--host`
- `--port`
- `--path`
- `--allow-remote`
- `--cw-host`
- `--cariden-home`

On startup:

- purge expired plans
- print sanitized configuration diagnostics to stderr
- warn about missing certificate files
- for stdio, run FastMCP without a stdout banner
- for HTTP, enforce authentication, safe binding behavior, and host/origin
  protection
- return meaningful process exit codes

### Correctness details

- Reset route and traffic simulation state before baseline calculations.
- Recompute routing after setting failures.
- Include only active objects where appropriate.
- Handle `None` utilization/traffic/capacity values safely.
- Compare both sides of a circuit and report the bottleneck side.
- Sort utilization and delta results deterministically.
- Never claim that an unordered interface collection is a hop sequence.
- Keep simulation state local to one opened plan context.
- Catch expected SDK, input, lookup, and filesystem errors at each MCP boundary.
- Avoid a blanket `except Exception` except in middleware that logs and
  immediately re-raises.

### Final validation before answering

Before returning the file:

1. Parse or compile it with Python.
2. Confirm every resource, prompt, and tool listed above is registered.
3. Confirm all referenced helper functions and imports exist.
4. Confirm stdout is unused for logging in stdio mode.
5. Confirm there are no embedded secrets, private keys, certificates, or
   production credentials.
6. Confirm direct plan paths cannot escape the allowed root.
7. Confirm one authenticated HTTP client cannot access another client's plans.
8. Confirm destructive deletion and large upload require explicit confirmation.
9. Confirm worst-case questions route to `get_wc_traffic`, not `failure_sim`.
10. Confirm temporary files are cleaned up even when an SDK call fails.
11. Report any SDK-dependent assumptions after the code, briefly and explicitly.

Output the complete Python source in one fenced code block, followed only by a
short list of SDK/version assumptions. Do not replace implementation sections
with TODOs or ellipses.
