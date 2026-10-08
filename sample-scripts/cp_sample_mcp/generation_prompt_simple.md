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

The Python docstrings are incomplete and sometimes wrong. Treat the
**Verified OPM/Design operational contract** section below as mandatory
ground truth for this release even when it contradicts an `open_plan`
docstring. GenericTool JSON option names are not Python symbols; they are
still required and must use the kebab-case names listed there (also present
as strings in `libdesignservice.so`). Do not invent camelCase tool options.

## Runtime

Resolve `CARIDEN_HOME` from `--cariden-home`, then the environment, then a
local `cw-planning` directory. Require `<CARIDEN_HOME>/lib/python`, add it to
`sys.path`, and put both `<CARIDEN_HOME>/lib` and `<CARIDEN_HOME>/lib/python`
on `LD_LIBRARY_PATH`. Setting `os.environ["LD_LIBRARY_PATH"]` in an already
running process does **not** load `libIceDiscovery.so`. After updating native
paths, if `import Ice` / `IcePy` would fail, re-exec the same interpreter
with `os.execv` (preserving argv and the new env) **before** any Cisco
import. Fail startup with an actionable message when `lib/python` is missing.

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

IceSSL (protocol `ssl`) loads mTLS exclusively from
`CARIDEN_HOME/etc/certs/{ca_cert.pem,designapi_user_cert.pem,designapi_user_key.pem}`
via `ServiceConnectionManager`. Do not pass PEM contents into `open_plan`.
`ServiceConnectionManager.newServiceConnection(host, port, protocol)` accepts
`ssl` or `tcp`, not the `open_plan` docstring values `http`/`https`. Default
`CW_DESIGNAPI_PROTOCOL` is `ssl`.

Check configured certificate files for presence without printing their
contents. If the IceSSL private key exists and is owner-readable but not
mode `0600`/`0400`, warn on stderr and continue; refuse only if it is
missing, unreadable, or world-writable. Tell operators to inspect each
public certificate with:

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
and do not modify the stored upload. Do not wrap every SDK exception as a
certificate/DesignAPI connectivity error. Include the real exception class and
a short message (never a stack trace). Ice `ValueError` from a bad argument
is a programming/API-usage error, not a missing cert.

## Verified OPM/Design operational contract

Use these patterns. They are required for a functional server on DesignAPI
IceSSL.

**Open and simulate**

```python
with open_plan(path, host=host, port=port, protocol="ssl", linger=False) as network:
    model = network.model
    model.route_simulation = []          # empty failure set; never assign None
    model.traffic_simulation = None
    model.route_simulation.recompute()
```

Assigning `model.route_simulation = None` leaves `_failed_objects` as a list.
Then `circuit.failed = True` crashes (`list` has no `.add`). Do not use
`.failed = True` for what-if. Apply failures with:

```python
model.route_simulation = [circuit, ...]   # OPM circuit objects
model.route_simulation.recompute()
_ = model.traffic_simulation              # trigger traffic sim
```

**IGP / shortest path — Node objects, not name strings**

`RouteSimulation.shortest_path` requires OPM `Node` objects. Passing a
string makes Ice raise `ValueError: invalid value for argument 1 in
operation getShortestPathRouteRecord` and can poison the pooled Ice
connection for later tools.

```python
src = model.nodes[source_name]
dst = model.nodes[destination_name]
route = model.route_simulation.shortest_path(src, dst, "igp")  # or te/latency/bgp
```

**Order hops from topology, not from a Python set**

`route.interfaces` is unordered. Reconstruct hop order by walking from the
source node: pick the interface whose `node.name` is the current node, then
advance to `opposite_interface.node` (or the other circuit endpoint). Use
the same walk for IGP fill between node SIDs and from the last SID to the
destination. Return that walk as `resolved_ordered_path` (list of
`"{node}:{interface}"` strings). Do not skip IGP fill just because the SDK
collection is unordered.

**Circuit identity**

Accept all of: unique `node_a`+`node_b` (reject if parallel circuits), the
SDK key `ckt{n1|if1|n2|if2}`, and the shorthand `n1-n2` / `n2-n1`.

**Worst-case Simulation Analysis**

Index by **model** interface, not by iterating SA wrapper objects as if they
were model interfaces:

```python
sa = SimulationAnalysis(model, failure_types=["circuits"], max_fail_per_int=10)
sa.cache_valid = True
for iface in model.interfaces:
    rec = sa.interfaces[iface]
    util = rec.worst_case_utilization   # e.g. 113.39
```

Return a stable `interface_id` such as `if{node|name}` or `{node}:{name}`,
never a Python `repr` of a wrapper object.

**Traffic growth (`create_growth_plans`)**

JSON options are kebab-case. Use the DesignAPI filesystem round-trip:

1. Set `demand.growth_percent` on active demands.
2. `network.rpc_plan.serializeToFileSystem("/tmp/mcp_growth_<unique>.txt")`
   on the DesignAPI host (a unique path each call).
3. `newPlanFromFileSystem` that path; `newGenericTool().runTool(net,
   "create_growth_plans", json.dumps({...}))` with:

```json
{
  "plan-file": "<same server path>",
  "create-plans-from": "DEMANDS",
  "num-periods": 1,
  "period-inc": 1,
  "growth-method": "COMPOUND",
  "run-sim-analysis": false
}
```

4. If the tool **raises** or succeeds without changing demand traffic, apply
   compound `(1 + growth/100) ** (period_inc * num_periods)` through the
   documented demand-traffic API (`Demand.traffic` or `DemandTrafficKey`) and
   set `fallback_used: true`. Do not treat a tool exception as a hard failure
   when the fallback can still produce a correct COMPOUND result.
5. Delete local and server-side temp plans in `finally`.
6. Reject `SIMPLE` unless you implement its documented linear math; never
   label compound arithmetic as SIMPLE.

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
9. `get_igp_path(source, destination, plan_ref=None, metric="igp")` — allow only metrics supported by the installed SDK. Resolve endpoints with `model.nodes[name]` and call `shortest_path` with those Node objects. Return interfaces and hop count.
10. `list_lsps(plan_ref=None, source=None, destination=None, active_only=True, limit=100)` — type, setup bandwidth, color, state, and path options.
11. `get_lsp_path(source, plan_ref=None, lsp_name=None, destination=None, color=None, destination_ip=None, path_option=None)` — resolve one RSVP-TE LSP by source and name, or one SR policy by source, destination, and color. Recompute the baseline. Return route metrics and these separate fields: configured `segment_list_hops`, ordered `resolved_ordered_path` as `"{node}:{iface}"` strings (interface SID directly; node SID filled by IGP using Node-object `shortest_path` plus the topology walk; then IGP fill to the destination), unordered `interface_set`, and comparison-only `reference_igp_shortest_path` (also Node-object `shortest_path`). Warn on unresolved segment steps. Do not present an unordered set as hop order; do not omit IGP fill because the SDK collection is unordered.
12. `failure_sim(plan_ref=None, node_a=None, node_b=None, failed_circuits=None, top_n=25, min_traffic_delta_mbps=1.0, sample_reroutes=5)` — fail one uniquely matched circuit between two nodes, or the listed circuits (`ckt{...}` or `nodeA-nodeB`). Capture the baseline, apply failures with `model.route_simulation = [circuits]`, recompute, and return failed IDs, rerouted and unrouted counts, a bounded reroute sample, the largest interface traffic changes, and new oversubscriptions. This is a specific what-if, not worst-case analysis.
13. `get_wc_traffic(plan_ref=None, failure_sets="nodes,circuits", max_fail_per_int=10, limit=15)` — validate failure-set names, run Cisco `SimulationAnalysis`, index `sa.interfaces[model_iface]`, and return the top interfaces by vendor worst-case utilization with stable interface ids. Do not loop over `failure_sim`.
14. `get_traffic_growth(plan_ref=None, growth_percent=33.0, num_periods=1, period_inc=1, growth_method="COMPOUND", threshold=100.0, near_capacity=80.0)` — record baseline circuit utilization, run kebab-case `create_growth_plans` via the filesystem round-trip above, and report oversubscribed and near-capacity circuits. If that tool raises or leaves traffic unchanged, apply the compound multiplier through the documented demand-traffic API and set `fallback_used`. Reject `SIMPLE` unless its documented linear math is implemented. Clean up every temporary file and temporary Design plan on both success and failure.

Reset simulation state before each baseline and recompute after failures. Treat missing utilization as unknown. Compare both sides of a circuit and report the higher-utilization side. Cap list and sample sizes.

## Finish

If a required API was missing, return only the blocked report. Otherwise compile the file and confirm that all 5 resource URIs, 2 prompts, and 14 tools are registered, that stdout is unused for logging, and that the source contains no secrets or certificate material. Confirm `os.execv` (or equivalent) is used so `python3 cp_mcp.py` works without a pre-exported `LD_LIBRARY_PATH`. Run any small offline checks you can.

If `CW_DESIGNAPI_HOST` is set and `us_wan.txt` is next to the script, you **must** run live DesignAPI checks before finishing: upload the plan; `get_demand_path` er1.sjc→er1.mia (expect hop_count 6, metric ~2169); `get_igp_path` cr1.sjc→cr1.kcy (non-empty, no Ice ValueError); `failure_sim` node pair cr1.sjc/cr1.kcy (rerouted_count 10); `failure_sim` `failed_circuits=["cr1.sjc-cr1.kcy"]`; `get_wc_traffic` circuits (top util ~113, human-readable interface ids); `get_traffic_growth` 33% one period (`ok` true, either tool growth or `fallback_used`). Fix failures before answering. State any live checks that still could not run.
