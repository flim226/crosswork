# One-shot build prompt: Crosswork Planning MCP server

You are a senior Python platform engineer with deep experience in the Model
Context Protocol (MCP), FastMCP, Cisco Crosswork Planning, the OPM Python API,
the Design RPC API, secure service design, and Pydantic v2.

Build a production-quality Crosswork Planning simulation MCP server. The result
must be complete, runnable, secure by default, and useful to an MCP client
without manual code completion.

## Non-negotiable deliverable

Create exactly one implementation file named `cp_sample_mcp.py`.

- Target Python 3.11 or newer.
- Use `fastmcp.FastMCP` and Pydantic v2.
- Include `#!/usr/bin/env python3`, a concise module docstring, local stdio and
  authenticated HTTP launch examples, dependency expectations, and all
  environment-variable names.
- Use type hints throughout and Ruff/Black-compatible formatting.
- Do not emit pseudocode, TODOs, ellipses, mock production behavior, omitted
  handlers, or references to code outside the file.
- Do not create supporting Python modules. Runtime state files and temporary
  plan files are allowed.
- If operating in a coding workspace, write the file and validate it there. If
  only text output is possible, return the entire source in one fenced block.

This build has one prerequisite gate: the installed FastMCP and Cisco SDK must
expose enough documented API to implement all 14 tools without invented calls.
If that gate fails, do not create a partial or deceptive server. Return a
concise `status: blocked` evidence report naming the paths, package versions,
documentation, and required symbols checked. A capability that is present in
the SDK but unavailable at runtime because of licensing, credentials, or a
temporarily unreachable DesignAPI does not block generation; keep its tool
registered and return a safe `CAPABILITY_UNAVAILABLE` error at runtime.

## Establish API ground truth before coding

The Cisco APIs are release-sensitive. Inspect the installed SDK, its Python
source/type stubs, bundled examples, and locally available official
documentation before writing integration code. Training-memory guesses are not
acceptable evidence.

1. Determine `CARIDEN_HOME` in this precedence order: CLI override,
   environment variable, then a configurable local default.
2. Apply CLI overrides before importing any Cisco package.
3. Verify `<CARIDEN_HOME>/lib/python`, add it to `sys.path`, and put both
   `<CARIDEN_HOME>/lib` and `<CARIDEN_HOME>/lib/python` on `LD_LIBRARY_PATH`.
   Mutating `os.environ["LD_LIBRARY_PATH"]` in an already-started process does
   not load `libIceDiscovery.so`. After updating native paths, if `Ice`/`IcePy`
   cannot import, re-exec the same interpreter with `os.execv` (preserving argv
   and the new env) **before** any Cisco import. Then inspect the installed
   symbols.
4. Verify every imported Cisco class and every constructor, attribute, method,
   enum, RPC option, and return type used by the implementation.
5. At minimum, establish whether this release provides and how it uses:
   `open_plan`, `SimulationAnalysis`, `PlanKey`, `PlanFormat`,
   `DemandTrafficKey`, the Design plan manager, the Design generic-tool
   manager, plan serialization, route simulation, traffic simulation, LSP
   keys, LSP paths, and segment lists.
6. Keep all version-sensitive Cisco operations in a small, clearly marked
   compatibility-adapter section. Comments may state the installed versions
   and the verified alternatives, but must not claim support that was not
   checked.
7. Never invent an API or silently replace a missing Cisco analysis with a
   home-grown approximation. Apply the prerequisite gate above when an API is
   absent; use a runtime capability error only when the API exists but the
   deployed service cannot currently execute it.
8. Python docstrings in this SDK are incomplete and sometimes wrong.
   `open_plan`'s protocol text (`http`/`https`) does not match
   `ServiceConnectionManager.newServiceConnection`, which accepts `ssl` or
   `tcp`. Remote IceSSL DesignAPI uses `protocol="ssl"`. GenericTool JSON
   option names are not Python symbols; they appear as kebab-case strings in
   `libdesignservice.so`. Use the **Verified OPM/Design operational contract**
   below as mandatory ground truth for those items. Do not invent camelCase
   GenericTool options.

Use FastMCP APIs from one installed distribution/version only. Verify decorator,
middleware, authentication, lifespan, context, transport, and `run()` signatures
instead of mixing examples from different FastMCP releases.

## Server purpose and client instructions

Create one FastMCP server for uploading a Crosswork Planning plan and performing
topology inspection, baseline route inspection, LSP/SR-policy path inspection,
deterministic circuit-failure simulation, worst-case Simulation Analysis, and
traffic-growth forecasting.

The server instructions must tell clients to:

- upload a user-supplied plan first and pass the returned canonical plan
  reference to later operations;
- prefer MCP resources for unfiltered, read-only data;
- use `failure_sim` only for a specifically identified circuit/link failure;
- use `get_wc_traffic` alone for worst-case or Simulation Analysis questions;
- call `list_lsps` before `get_lsp_path`;
- treat a route-simulation interface collection as unordered unless the SDK
  explicitly guarantees order, and use resolved segment-list expansion for
  ordered LSP/SR-policy hops.

## Configuration and startup

Implement one typed central configuration model. Reject malformed values and
unknown keys only within the application's own config namespace; ignore
unrelated process environment variables. Support these environment variables:

- `CARIDEN_HOME`;
- `CW_DESIGNAPI_HOST`, `CW_DESIGNAPI_PORT`, `CW_DESIGNAPI_PROTOCOL`,
  `CW_DESIGNAPI_TIMEOUT_S`, `CW_DESIGNAPI_CA_FILE`,
  `CW_DESIGNAPI_CLIENT_CERT_FILE`, `CW_DESIGNAPI_CLIENT_KEY_FILE`, and
  `CW_DESIGNAPI_SERVER_NAME`;
- `CW_DESIGNAPI_USERNAME`, `CW_DESIGNAPI_PASSWORD_FILE`, and
  `CW_DESIGNAPI_TOKEN_FILE` when the verified SDK uses password or token
  authentication rather than mTLS; secret files must have private permissions,
  and mutually exclusive authentication modes must be rejected;
- `CW_DEFAULT_PLAN`, `CW_PLAN_ROOT`, `CW_STAGING_DIR`,
  `CW_MAX_PLAN_BYTES`, `CW_LARGE_UPLOAD_CONFIRM_BYTES`,
  `CW_OWNER_QUOTA_BYTES`, and `CW_PLAN_TTL_HOURS`;
- `MCP_HOST`, `MCP_PORT`, `MCP_PATH`, `MCP_RATE_LIMIT_RPS`, and
  `MCP_RATE_LIMIT_BURST`;
- `MCP_TOKEN_FILE`, a root/user-readable-only JSON secret file mapping opaque
  bearer tokens to stable owner IDs.

If the installed Cisco release obtains DesignAPI mTLS settings exclusively from
its own documented configuration, verify and use that mechanism instead of
inventing constructor arguments. This release loads IceSSL from
`CARIDEN_HOME/etc/certs/{ca_cert.pem,designapi_user_cert.pem,designapi_user_key.pem}`
inside `ServiceConnectionManager` (`IceSSL.VerifyPeer=2`). Environment values
above may point to trust and identity files but must never contain private-key
or certificate contents. Do not accept secrets through CLI arguments. Default
`CW_DESIGNAPI_PROTOCOL` is `ssl`.

Use safe defaults:

- stdio transport;
- HTTP bound to `127.0.0.1`;
- verified TLS for DesignAPI;
- no deployment-specific Crosswork address;
- private filesystem permissions;
- no embedded password, token, private key, certificate, secret, or credentialed
  connection string.

Provide `main()` with at least:

- `--transport {stdio,http}`, default `stdio`;
- `--host`, `--port`, and `--path`;
- `--allow-remote`;
- `--cw-host`;
- `--cariden-home`;
- a non-network `--check` or equivalent startup diagnostic mode.

Parse and apply arguments that affect SDK discovery before SDK imports. Startup
must validate configuration and required capabilities, reconcile/purge stale
uploads, and print only sanitized diagnostics to stderr. Stdio stdout must
remain exclusively available to the MCP protocol.

HTTP transport must fail closed unless `MCP_TOKEN_FILE` is configured, has
private permissions, contains at least one valid token-to-owner mapping, and is
handled by a production-capable verifier supported by the installed FastMCP
release. Accept tokens only through the Authorization Bearer scheme. Compare
candidate tokens in constant time and derive the stable owner ID only from the
matched server-side mapping, never from tool arguments. Do not log or echo the
mapping. A shared token intentionally represents one owner; different owners
must have different tokens. For stdio, use one explicit constant local
principal for that server process.

Require an explicit flag for non-loopback binding, enable host/origin
protection, apply per-owner rate limiting, and configure a concrete
request-body limit at the HTTP server or documented reverse-proxy boundary.
Non-loopback deployment must refuse to start unless direct TLS is configured
through supported FastMCP settings or an explicit trusted-reverse-proxy mode is
enabled. Do not trust forwarded identity headers. Do not use a debug
authentication provider in production.

## Plan registry and filesystem safety

Implement a persistent SQLite uploaded-plan registry under the staging
directory. Use transactions, foreign-key enforcement, busy timeouts, unique
constraints, and active-operation leases so publication, lookup, quota
accounting, deletion, and TTL cleanup are race-safe. Run one server process;
reject configurations that request multiple workers unless equivalent
cross-process vendor-operation coordination is implemented.

For every plan record store:

- server-generated unguessable plan ID;
- safe display filename;
- canonical path managed by the server;
- exact byte size and SHA-256 content digest;
- upload timestamp and expiry;
- input encoding;
- authenticated owner ID;
- validation state.

Required behavior:

- accept UTF-8 text and strict validated base64;
- validate decoded size, not encoded size;
- allow only an explicitly defined safe filename and plan-ID character set;
- reject path separators, traversal, NULs, control characters, symlinks,
  non-regular files, and collisions;
- reject impossible encoded lengths before decoding, strictly decode with a
  hard decoded-size cap, enforce the HTTP request-body limit above, and do not
  falsely claim handler-level decoding prevents the MCP framework from
  buffering its request;
- require an explicit `confirm=true` retry before a large upload;
- write privately to a unique temporary file, validate exact size/digest,
  fsync, then atomically publish;
- never expose a partial or failed upload;
- perform owner-scoped lookup, listing, deletion, and TTL cleanup without
  revealing whether another owner's ID exists;
- require explicit confirmation before deletion;
- make deletion and cleanup race-safe with active simulations;
- recover or safely remove stale temporary state after restart;
- never follow symlinks during cleanup.

Canonical uploaded references must use `upload:<plan_id>`; an authorized bare ID
may also be accepted. The optional `plan_id` argument may provide a custom ID
only for the trusted stdio principal; HTTP always receives a server-generated
ID. A collision must return the same generic response as any unusable ID so it
cannot reveal another owner's record. Never reuse a deleted ID.

A configured default plan is optional and available only to the trusted stdio
principal. HTTP operations must supply an owned upload reference.

Direct local filesystem references use an explicit `file:<path>` syntax and are
allowed only for the trusted local stdio principal. Open them relative to an
allowlisted root using `openat`/`O_NOFOLLOW` or an equivalent race-safe
descriptor-based traversal, verify the opened descriptor with `fstat`, and copy
it into managed storage before use. Reject traversal, symlinks, hard links,
non-regular files, and path-replacement races. HTTP callers must never access
arbitrary server paths. Do not include sensitive absolute paths in remote
responses.

## Concurrency, isolation, errors, and responses

Assume OPM and Design RPC objects are blocking and not thread-safe. Serialize
vendor operations with a lock and use one HTTP worker unless cross-process
coordination is implemented. Acquire an active-operation lease before opening a
private working copy of a plan; deletion and expiry must wait for or safely
reject active leases. Keep each simulation inside a newly opened working
context. Never give the SDK write access to the registered baseline artifact.

Use bounded inputs and outputs. Configure vendor-supported RPC/network
timeouts. Do not describe `Future.result(timeout=...)` or async cancellation as
stopping an in-flight native SDK call. If the SDK provides no cancellation,
report a soft caller timeout while retaining the operation lease and lock until
the worker really completes; use a killable subprocess only if SDK licensing
and object transfer are verified to support it. If blocking work is invoked
from an async handler, offload it without violating SDK thread affinity.

Return JSON-serializable dictionaries with a consistent success envelope:

```python
{"ok": True, ...}
```

Map expected validation, lookup, SDK, filesystem, timeout, and authorization
failures to concise FastMCP `ToolError` or `ResourceError` responses with stable
safe error codes and actionable hints. Do not leak stack traces, credentials,
certificate contents, another owner's metadata, or sensitive paths. Avoid
blanket `except Exception` except at a boundary that records a safe error and
immediately re-raises or maps it.

Add structured audit logging for tools, resources, and prompts. Include target,
authenticated owner/client identifier, correlation ID, duration, result, and
exception class. Never log plan content, bearer tokens, credentials, or
certificate contents.

## MCP resources

Register these read-only JSON resources:

1. `server://health`
   - sanitized SDK/runtime version and capability status;
   - sanitized DesignAPI endpoint;
   - required certificate-file presence;
   - DesignAPI reachability when a plan can be opened;
   - safe error code and actionable hint.
2. `plan://uploads`
   - only plans owned by the current principal.
3. `plan://{plan_id}/summary`
   - node, circuit, active-circuit, demand, active-demand, LSP, active-LSP, and
     LSP-path counts.
4. `plan://{plan_id}/circuits`
   - bounded active-circuit catalog.
5. `plan://{plan_id}/demands`
   - bounded active-demand catalog.

Resource URI IDs must receive the same character validation and ownership
authorization as tool inputs.

## MCP prompts

Register:

1. `link_failure_workflow(node_a, node_b)`
   - upload if needed;
   - inspect circuits;
   - run the explicit failure simulation;
   - review reroutes and new oversubscriptions;
   - inspect affected demand paths.
2. `capacity_planning_workflow(growth_percent=33.0)`
   - upload if needed;
   - inspect the baseline summary;
   - run traffic growth;
   - run worst-case circuit-failure Simulation Analysis;
   - summarize oversubscribed and near-capacity circuits.

## Verified OPM/Design operational contract

These patterns are mandatory. Do not replace them with docstring guesses.

**Native libs.** After setting `LD_LIBRARY_PATH` to
`<CARIDEN_HOME>/lib:<CARIDEN_HOME>/lib/python`, re-exec with `os.execv` if
`IcePy` is not yet importable. A wrapper script is allowed only as comments
in the module docstring; the Python file itself must start unaided.

**Open plan.** `open_plan(path, host=..., port=..., protocol="ssl", linger=False)`.
Never `https`. IceSSL files are not constructor arguments.

**Baseline reset.** Always:

```python
model.route_simulation = []     # empty failure list; never None
model.traffic_simulation = None
model.route_simulation.recompute()
```

`route_simulation = None` leaves `_failed_objects` as a list, so
`circuit.failed = True` raises `AttributeError: 'list' object has no attribute
'add'`. Do not use `.failed = True` for what-if.

**Apply circuit failures.**

```python
model.route_simulation = [circuit, ...]  # OPM circuit objects
model.route_simulation.recompute()
_ = model.traffic_simulation
```

**IGP.** `shortest_path` requires OPM Node objects:

```python
route = model.route_simulation.shortest_path(
    model.nodes[src_name], model.nodes[dst_name], "igp"
)
```

Passing a string yields Ice `ValueError` on `getShortestPathRouteRecord` and
can poison the pooled communicator. Map SDK exceptions to ToolError with the
real exception class name; never claim a type error is a missing certificate.

**Order hops.** `Route.interfaces` is unordered. Reconstruct order by walking
from the source node: choose the interface whose node name equals the current
node, then advance via `opposite_interface.node` (or the other circuit
endpoint). Use that walk for IGP fill between node SIDs and from the last
anchor to the destination. Emit `resolved_ordered_path` as `"{node}:{iface}"`
strings. This topology walk is required; skipping IGP fill because the SDK
collection is unordered is incorrect.

**Circuit IDs.** Accept unique `node_a`+`node_b` (reject parallels), SDK
`ckt{n1|if1|n2|if2}`, and shorthand `n1-n2` / `n2-n1`.

**Simulation Analysis.**

```python
sa = SimulationAnalysis(model, failure_types=names, max_fail_per_int=n)
sa.cache_valid = True
for iface in model.interfaces:
    rec = sa.interfaces[iface]   # index by model interface
    util = rec.worst_case_utilization
```

Stable `interface_id` (`if{node|name}` or `{node}:{name}`). Never a wrapper
`repr`. Do not iterate SA wrappers as if they were model interfaces.

**Growth.** kebab-case GenericTool JSON and DesignAPI filesystem round-trip:

1. Set `demand.growth_percent` on active demands.
2. `network.rpc_plan.serializeToFileSystem("/tmp/mcp_growth_<unique>.txt")`.
3. `pm.newPlanFromFileSystem(server_path)`; `newGenericTool().runTool(net,
   "create_growth_plans", json.dumps({
     "plan-file": server_path,
     "create-plans-from": "DEMANDS",
     "num-periods": num_periods,
     "period-inc": period_inc,
     "growth-method": "COMPOUND",
     "run-sim-analysis": False,
   }))`.
4. If the tool **raises** or reports success without changing demand traffic,
   apply compound `(1 + g/100) ** (period_inc * num_periods)` via
   `DemandTrafficKey` / `Demand.traffic` and set `fallback_used` /
   `compound_fallback_applied`. Do not fail the tool solely because
   `create_growth_plans` threw when fallback can still produce COMPOUND results.
5. `finally`: remove local temps and extra PlanManager keys.
6. SIMPLE: linear `1 + (g/100)*periods` through a verified API, or reject
   SIMPLE explicitly. Never label compound math as SIMPLE.

## MCP tools

Implement every tool below with detailed docstrings, validated bounded inputs,
deterministically ordered outputs, and the common response/error conventions.

### Plan lifecycle and catalogs

1. `upload_plan(name, content, encoding="text", plan_id=None, validate=True, confirm=False)`
   - stage and register content;
   - optionally open it with OPM before publishing it as ready;
   - remove all state if validation fails;
   - return plan ID, canonical reference, safe metadata, digest, validation
     state, and model counts;
   - if `validate=False`, set state to `unchecked`, return null model counts,
     and require successful validation before any analysis operation.
2. `list_uploaded_plans()`.
3. `delete_uploaded_plan(plan_id, confirm=False)`.
4. `designapi_health_check(plan_ref=None)`.
5. `get_plan_summary(plan_ref=None)`.
6. `list_circuits(plan_ref=None, node_filter=None, active_only=True, limit=100)`
   - include stable circuit identity, endpoints, interfaces, capacity, and
     active state;
   - cap the limit and report truncation based on whether additional matches
     actually exist.
7. `list_demands(plan_ref=None, source=None, destination=None, active_only=True, limit=100)`
   - include stable identity/name, endpoints, service class, traffic with
     units, and active state;
   - cap the limit and report true truncation.

### Route and LSP inspection

8. `get_demand_path(source, destination, plan_ref=None, demand_name=None, service_class="Default")`
   - reset failures and traffic state;
   - recompute baseline routing;
   - reject ambiguous demand matches;
   - return routed state, traffic and units, metric, latency, ECMP percentage,
     interfaces, traffic shares, and hop count.
9. `get_igp_path(source, destination, plan_ref=None, metric="igp")`
   - allow only metrics verified for the installed SDK, such as IGP, BGP, TE,
     and latency;
   - resolve endpoints with `model.nodes[name]` and call `shortest_path` with
     those Node objects;
   - return the SDK-provided path while explicitly stating whether ordering is
     guaranteed; if unordered, also return a topology-walk ordered list.
10. `list_lsps(plan_ref=None, source=None, destination=None, active_only=True, limit=100)`
    - include stable identity, type, setup bandwidth and units, color, state,
      and path options.
11. `get_lsp_path(source, plan_ref=None, lsp_name=None, destination=None, color=None, destination_ip=None, path_option=None)`
    - resolve a named RSVP-TE LSP or an SR policy identified by source,
      destination, and color;
    - reject incomplete or ambiguous identifiers;
    - support a specific path option;
    - reset simulation state and recompute baseline routing;
    - return routed state, type, endpoints, setup bandwidth, color, metric,
      latency, and ECMP data;
    - return configured segment-list steps;
    - expand interface SIDs directly;
    - expand node SIDs with SDK-provided IGP shortest-path fill using Node
      objects plus the topology walk in the operational contract;
    - append IGP fill from the final segment anchor to the destination using
      the same walk;
    - report unsupported or unresolved segment steps as warnings;
    - clearly distinguish `resolved_ordered_path` (`"{node}:{iface}"` strings),
      unordered simulation `interface_set`, and unconstrained
      `reference_igp_shortest_path` (also Node-object `shortest_path`);
    - never fabricate order by iterating a set. The topology walk above is the
      required reconstruction; do not omit IGP fill because the SDK collection
      is unordered.

### Explicit deterministic failure simulation

12. `failure_sim(plan_ref=None, node_a=None, node_b=None, failed_circuits=None, top_n=25, min_traffic_delta_mbps=1.0, sample_reroutes=5)`
    - accept exactly one selector form: a non-empty circuit-ID list or a
      complete node pair;
    - resolve every circuit uniquely before changing simulation state; reject
      a node pair matching parallel circuits and require explicit circuit IDs;
    - treat `ckt{...}`, `nodeA-nodeB`, and `nodeB-nodeA` as circuit-ID list
      forms;
    - compute baseline demand paths and per-interface traffic/utilization;
    - apply exactly the requested circuit failures with
      `model.route_simulation = [circuits]` (not `.failed = True`) and recompute;
    - return applied circuit IDs, active-demand count, rerouted and unrouted
      counts, bounded sample reroutes with before/after paths, largest bounded
      interface traffic changes, before/after utilization, newly
      oversubscribed interfaces, and baseline oversubscription count;
    - use deterministic tie-breaking and state units;
    - do not mutate the stored baseline.

This tool is only for a known deterministic what-if; it is not a worst-case
engine.

### Worst-case Simulation Analysis

13. `get_wc_traffic(plan_ref=None, failure_sets="nodes,circuits", max_fail_per_int=10, limit=15)`
    - normalize string or list input;
    - validate failure-set names against an explicit allowlist proven valid for
      the installed release;
    - invoke Cisco `SimulationAnalysis` using its verified API;
    - read worst-case utilization via `sa.interfaces[model_iface]` for each
      model interface; never use a wrapper object's `repr` as an id;
    - rank interfaces by vendor-produced worst-case utilization with stable
      tie-breaking;
    - return failure sets, units, bounded top interfaces, relevant vendor
      warnings, and scenario count only if the verified API exposes it; never
      infer a count by guessed enumeration.

Never implement this by repeatedly calling `failure_sim`, enumerating guessed
failures, or relabeling ordinary utilization as worst-case output. If the
installed SDK does not expose the required API, apply the prerequisite gate and
do not generate the server. If the verified API exists but is unlicensed or
unavailable in the deployed service, return a runtime capability error.

### Traffic growth

14. `get_traffic_growth(plan_ref=None, growth_percent=33.0, num_periods=1, period_inc=1, growth_method="COMPOUND", threshold=100.0, near_capacity=80.0)`
    - validate finite numeric values and sensible bounded ranges;
    - require `0 <= near_capacity <= threshold`;
    - collect baseline per-circuit utilization, checking both sides and
      reporting the bottleneck side;
    - set growth attributes required by the installed SDK;
    - use the verified Design plan-manager transfer lifecycle to create a
      uniquely named server-side working plan from the uploaded artifact,
      retain its returned key rather than guessing a server path, and retrieve
      the generated result; if the release supports only a documented
      server-visible filesystem path, validate that deployment prerequisite
      explicitly;
    - invoke `create_growth_plans` through Design RPC using the kebab-case
      options and `serializeToFileSystem` / `newPlanFromFileSystem` lifecycle
      in the operational contract (not camelCase, not `newPlanFromBytes` alone);
    - inspect the generated plan and verify whether expected demand growth was
      actually applied;
    - if the tool raises, returns no new plan key, or reports success without
      applying COMPOUND growth, a compatibility adapter must apply the
      mathematically correct compound multiplier through the documented
      demand-traffic RPC API and report that fallback;
    - for SIMPLE growth, implement the documented mathematics through a
      verified API or reject it explicitly; never use compound arithmetic while
      labeling it SIMPLE;
    - create local temporary files with mode `0600` inside the private staging
      filesystem, serialize and reopen through OPM, and clean both local and
      server-side temporary plans by their verified keys in `finally` blocks on
      success or exception; after a soft timeout, cleanup must occur when the
      underlying call actually returns rather than pretending it was cancelled;
    - return multiplier, method, periods, whether tool growth or a compatibility
      fallback was applied, baseline utilization summary, after-growth
      oversubscriptions, and near-capacity circuits.

## Simulation correctness

- Reset failure, route, and traffic state before every baseline with
  `model.route_simulation = []` (never `None`).
- Recompute after applying failures via `model.route_simulation = [circuits]`.
- Include only active objects where requested.
- Handle missing traffic, utilization, capacity, latency, and route values
  safely without converting “unknown” to zero unless that meaning is documented.
- Compare both circuit sides and report the actual bottleneck side.
- Sort every ranked or sampled result deterministically with a stable secondary
  key.
- Never claim an unordered collection is a hop sequence.
- Keep all mutable simulation state local to one opened plan context.
- Preserve and verify units in field names or adjacent metadata.
- Apply explicit hard caps to circuit IDs, catalog rows, path interfaces,
  segment steps, reroutes, utilization rows, warnings, audit-field lengths, and
  total serialized response size; include truncation metadata.

## Certificate and cryptographic requirements

Never disable TLS verification or use custom cryptography, MD5, SHA-1, DES,
3DES, RC4, Blowfish, AES-CBC, or AES-ECB. Use SHA-256 only for non-secret
content digests; token authentication must use a documented verifier and
constant-time comparison where applicable.

Do not embed or return certificate or private-key content. When expected X.509
certificate files are configured or discovered, flag them for operator
verification and document this command:

```bash
openssl x509 -text -noout -in <certificate_file>
```

The operator must verify:

- `notBefore` and `notAfter` validity;
- RSA keys are at least 2048 bits or EC keys use P-256 or stronger;
- signatures use SHA-2 or stronger, never MD5 or SHA-1;
- whether issuer and subject indicate a self-signed certificate and, if so,
  that its use is intentional and limited to private development/testing or an
  explicitly managed internal trust domain.

When configured certificate files are present, parse the public certificates
with a maintained library or documented standard-library facility and fail
closed on expiry/not-yet-valid status, RSA keys below 2048 bits, EC curves below
P-256, or MD5/SHA-1 signatures. Report self-signed status as an operator warning
and never expose certificate contents. `MCP_TOKEN_FILE` and other application
secret files must be mode `0600`/`0400` (fail closed). IceSSL private keys under
`CARIDEN_HOME/etc/certs`: if the file exists and is owner-readable, log a
warning to stderr when the mode is not `0600`/`0400`, but do **not** refuse
startup solely because a lab key is group-readable (`0664`). Still fail closed
if the IceSSL key is missing, unreadable by the process, or world-writable.
Do not read or log key content outside the TLS/SDK facility that consumes it.

## Required validation before completion

If the prerequisite gate failed, stop here and return only the `status:
blocked` evidence report; the file-specific checks below do not apply.

If the prerequisite gate passed, before presenting the generated file classify
checks as (a) mandatory offline,
(b) SDK-present offline, (c) live DesignAPI/licensing, or (d) deployment
operator checks. All applicable offline checks must pass. Run live/deployment
checks only when suitable services, identities, fixtures, and certificate files
are actually available; report the others as `not run`, never as passed.

Then:

1. Compile the file with the target Python interpreter.
2. Run Ruff or an equivalent static check if available.
3. Exercise a no-network `--check` path.
4. Enumerate registrations and confirm all 5 exact resource URI templates, 2
   prompts, and 14 tools above are registered.
5. Confirm all referenced functions, imports, and compatibility adapters exist.
6. Confirm stdio mode writes no banner or logs to stdout.
7. Search the file for embedded secrets, private keys, certificates, tokens,
   deployment addresses, credentialed URLs, weak cryptography, and disabled TLS
   verification.
8. Test traversal, symlink, oversized content, malformed base64, ID collision,
   failed validation cleanup, unauthorized cross-owner lookup, and deletion
   confirmation.
9. Test concurrent registry operations and analysis-versus-delete behavior.
10. Confirm direct plan paths cannot escape allowed roots and cannot be used by
    HTTP principals.
11. Confirm worst-case analysis calls the verified Cisco
    `SimulationAnalysis`, never `failure_sim`.
12. Confirm SIMPLE growth is correct or explicitly rejected.
13. Confirm temporary files and Design-side temporary plans are cleaned up when
    each intermediate operation fails.
14. Confirm HTTP cannot start without valid authentication and cannot bind
    non-loopback without explicit authorization.
15. Confirm one authenticated principal cannot infer, read, operate on, or
    delete another principal's plans.
16. Confirm `os.execv` (or equivalent) is used so `python3 cp_sample_mcp.py`
    works without a pre-exported `LD_LIBRARY_PATH`.
17. If `CW_DESIGNAPI_HOST` is set and `us_wan.txt` is available, run live
    DesignAPI checks and fix failures: upload (33/50/95); demand path
    er1.sjc→er1.mia hop_count 6 metric ~2169; IGP cr1.sjc→cr1.kcy with Node
    objects and no Ice ValueError; `failure_sim` node pair rerouted_count 10;
    `failure_sim` `failed_circuits=["cr1.sjc-cr1.kcy"]`; `get_wc_traffic`
    circuits with ~113% and human-readable interface ids; `get_traffic_growth`
    33% one period with `ok` true and either tool growth or fallback.

Fix all failures before answering. Do not claim checks that could not run.

End with a concise report containing:

- the file created;
- compile/static-check results;
- detected Python, FastMCP, and Cisco SDK versions;
- the evidence source used for each version-sensitive Cisco capability;
- tests actually run;
- any remaining deployment-only checks, especially live DesignAPI connectivity,
  licensing, and operator certificate verification.
