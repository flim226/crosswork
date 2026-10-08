# One-shot prompt: minimal Crosswork Planning MCP server

You are a Python engineer familiar with FastMCP and Cisco Crosswork Planning.
Build a minimal, usable MCP server in one file. Keep the implementation small
and direct. Do not add features that are not listed here.

## Deliverable

Write one executable file, `cp_mcp.py`, for Python 3.11 or newer.

- Start with `#!/usr/bin/env python3` and a short module docstring that shows
  how to run stdio and authenticated HTTP.
- Use `fastmcp.FastMCP`, Pydantic v2, and type hints.
- Return JSON-serializable `{"ok": True, ...}` results from every tool.
- Raise FastMCP `ToolError` or `ResourceError` for expected failures. Include a
  short hint and do not return stack traces, secrets, or certificate contents.
- Send logs and startup messages to stderr. Leave stdout for MCP stdio traffic.
- Do not emit TODOs, pseudocode, mocks, or extra modules.

Before writing Cisco calls, inspect the installed SDK under `CARIDEN_HOME` and
its local documentation. Use only APIs you can verify there, especially
`open_plan`, route and traffic simulation, LSP and segment-list access,
`SimulationAnalysis`, and the Design RPC `create_growth_plans` tool. If a
required API is absent, do not invent a substitute and do not write the file.
Return `status: blocked` with the paths and symbols you checked. If an API
exists but DesignAPI is unreachable or unlicensed at runtime, keep the tool and
return a clear capability error.

## Runtime

Resolve `CARIDEN_HOME` from `--cariden-home`, then the environment, then a
local `cw-planning` directory. Require `<CARIDEN_HOME>/lib/python`, add it to
the Python path, and set the native library path before importing the SDK.
Fail startup with an actionable message when it is missing.

Read configuration from the environment:

- `CW_DESIGNAPI_HOST`, `CW_DESIGNAPI_PORT`, `CW_DESIGNAPI_PROTOCOL`
  (default `ssl`), `CW_DESIGNAPI_TIMEOUT_S`
- `CW_DESIGNAPI_CA_FILE`, `CW_DESIGNAPI_CLIENT_CERT_FILE`,
  `CW_DESIGNAPI_CLIENT_KEY_FILE`
- `CW_DESIGNAPI_USERNAME` and `CW_DESIGNAPI_PASSWORD_FILE`, or
  `CW_DESIGNAPI_TOKEN_FILE`, only when the verified SDK requires them
- `CW_DEFAULT_PLAN`, `CW_PLAN_ROOT`, `CW_STAGING_DIR`, `CW_MAX_PLAN_BYTES`
- `MCP_HOST` (default `127.0.0.1`), `MCP_PORT`, `MCP_PATH`, `MCP_API_TOKEN`

Never put secrets, certificates, or private keys in source code or CLI
arguments. Use the SDK’s documented connection settings. Do not disable TLS
verification.

`main()` accepts `--transport {stdio,http}` (default `stdio`), `--host`,
`--port`, `--path`, `--allow-remote`, `--designapi-host`, and `--cariden-home`.
HTTP must refuse to start without `MCP_API_TOKEN`. Compare that token in
constant time. Binding outside loopback requires `--allow-remote`; document
that remote use belongs behind TLS. Stdio uses one fixed local owner. HTTP
uses one owner derived from the configured token.

Check configured certificate files for presence without printing their
contents. Tell operators to inspect each public certificate with:

```bash
openssl x509 -text -noout -in <certificate_file>
```

They must check validity dates, RSA 2048-bit or P-256-or-stronger keys, a SHA-2
signature, and whether a self-signed certificate is intentional and limited to
private use.

## Uploaded plans

Keep a JSON index in the staging directory, guarded by a lock. Store plan ID,
safe filename, path, size, SHA-256 digest, upload time, and owner. Accept UTF-8
text or strict base64, enforce the size limit, and allow only safe filename
characters. Generate plan IDs on the server. Large uploads and deletions return
a preview until the caller retries with `confirm=true`.

References use `upload:<plan_id>`. List, get, and delete only the current
owner’s plans, using the same not-found response for missing and unauthorized
IDs. A configured default plan is available only to the local stdio owner and
must stay inside `CW_PLAN_ROOT`. Reject path traversal and symlinks. HTTP
callers cannot pass filesystem paths.

Serialize all SDK work with one lock. Open a fresh plan context per operation
and do not modify the stored upload.

## MCP surface

Server instructions must say: upload a plan before simulation; use resources
for unfiltered reads; call `failure_sim` only for a known circuit failure; call
`get_wc_traffic` alone for worst-case analysis; call `list_lsps` before
`get_lsp_path`; use `resolved_ordered_path` for ordered LSP hops because the
route-simulation interface set is unordered.

Resources, all JSON:

1. `server://health` — SDK path, DesignAPI endpoint, certificate-file presence, and reachability.
2. `plan://uploads` — current owner’s plans.
3. `plan://{plan_id}/summary` — node, circuit, demand, and LSP counts, including active counts.
4. `plan://{plan_id}/circuits` — bounded active circuits.
5. `plan://{plan_id}/demands` — bounded active demands.

Prompts:

1. `link_failure_workflow(node_a, node_b)` — upload, inspect circuits, run `failure_sim`, review reroutes and new oversubscriptions, then inspect affected demand paths.
2. `capacity_planning_workflow(growth_percent=33.0)` — upload, read the summary, run growth, run circuit worst-case analysis, and summarize oversubscribed and near-capacity circuits.

Tools:

1. `upload_plan(name, content, encoding="text", confirm=False)` — store the plan, open it with OPM, and return its ID, `upload:` reference, metadata, and model counts. Delete the staged files if opening fails.
2. `list_uploaded_plans()`.
3. `delete_uploaded_plan(plan_id, confirm=False)`.
4. `designapi_health_check(plan_ref=None)`.
5. `get_plan_summary(plan_ref=None)`.
6. `list_circuits(plan_ref=None, node_filter=None, active_only=True, limit=100)` — endpoints, interfaces, capacity, and active state. Cap `limit` and report truncation.
7. `list_demands(plan_ref=None, source=None, destination=None, active_only=True, limit=100)` — name, endpoints, service class, traffic, and active state.
8. `get_demand_path(source, destination, plan_ref=None, demand_name=None, service_class="Default")` — clear failures, recompute the baseline, and return routed state, traffic, metric, latency, ECMP percentage, interfaces, traffic shares, and hop count.
9. `get_igp_path(source, destination, plan_ref=None, metric="igp")` — allow only metrics supported by the installed SDK and return the shortest-path interfaces.
10. `list_lsps(plan_ref=None, source=None, destination=None, active_only=True, limit=100)` — type, setup bandwidth, color, state, and path options.
11. `get_lsp_path(source, plan_ref=None, lsp_name=None, destination=None, color=None, destination_ip=None, path_option=None)` — resolve one RSVP-TE LSP by source and name, or one SR policy by source, destination, and color. Recompute the baseline. Return route metrics and these separate fields: configured `segment_list_hops`, ordered `resolved_ordered_path` (interface SID directly, node SID filled by IGP, then IGP fill to the destination), unordered `interface_set`, and comparison-only `reference_igp_shortest_path`. Warn on unresolved segment steps. Do not present an unordered set as hop order.
12. `failure_sim(plan_ref=None, node_a=None, node_b=None, failed_circuits=None, top_n=25, min_traffic_delta_mbps=1.0, sample_reroutes=5)` — fail one uniquely matched circuit between two nodes, or the listed circuits. Capture the baseline, apply those failures, recompute, and return failed IDs, rerouted and unrouted counts, a bounded reroute sample, the largest interface traffic changes, and new oversubscriptions. This is a specific what-if, not worst-case analysis.
13. `get_wc_traffic(plan_ref=None, failure_sets="nodes,circuits", max_fail_per_int=10, limit=15)` — validate failure-set names, run Cisco `SimulationAnalysis`, and return the top interfaces by vendor worst-case utilization. Do not loop over `failure_sim`.
14. `get_traffic_growth(plan_ref=None, growth_percent=33.0, num_periods=1, period_inc=1, growth_method="COMPOUND", threshold=100.0, near_capacity=80.0)` — record baseline circuit utilization, run documented `create_growth_plans`, and report oversubscribed and near-capacity circuits. If that tool succeeds without changing traffic, apply the compound multiplier through the documented demand-traffic API and say that the fallback was used. Reject `SIMPLE` unless its documented behavior is available. Clean up every temporary file and temporary Design plan on both success and failure.

Reset simulation state before each baseline and recompute after failures. Treat missing utilization as unknown. Compare both sides of a circuit and report the higher-utilization side. Cap list and sample sizes.

## Finish

If a required API was missing, return only the blocked report. Otherwise compile the file and confirm that all 5 resource URIs, 2 prompts, and 14 tools are registered, that stdout is unused for logging, and that the source contains no secrets or certificate material. Run any small offline checks you can. State which live DesignAPI checks were not run.
