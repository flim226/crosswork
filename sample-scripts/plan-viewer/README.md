# Plan Viewer

A single-file, fully offline, browser-based viewer for Cisco Crosswork Planning / WAE plan files (`.pln` or `.txt`). The only exception is the opt-in OpenFreeMap background of the geographic topology.

## Usage

1. Open `plan-viewer.html` in Chrome, Edge, Firefox or Safari. No internet connection, server or install is needed.
2. Drop a `.pln` or `.txt` plan file onto the page, or click the drop zone to browse.
3. Use the tabs to explore the plan. The filter box applies to every table on the current tab; click a column header to sort; **⬇ CSV** exports a table.

The plan is parsed entirely in the browser and never leaves your machine. The SQLite engine ([sql.js](https://sql.js.org) 1.12.0, WebAssembly) and [D3](https://d3js.org) 7.9.2 are embedded in the HTML file.

## Supported files

| Format | Description |
|--------|-------------|
| Zipped plan (CP 8.x) | Zip archive containing `network.db` (SQLite) and `version.dat`. |
| Raw SQLite plan | Older plan files that are a SQLite database directly. |
| Text plan (`.txt`) | Tab-separated export: a `<Table>` line, a header row, data rows, and a blank line between tables. Lines starting with `#` are comments. It is converted in the browser into the same SQLite tables, so every view works the same. A blank cell becomes NULL and `''` an empty string. Columns whose values are all numbers become numeric, except identifier columns (names, nodes, sites, interfaces, AS, IGP process IDs, SRLGs, affinities and similar), which stay text as in a `.pln`. |

Accepted extensions are `.pln`, `.txt`, `.db`, `.sqlite` and `.sqlite3`; the content is also checked by its file signature (a text plan must start with a `<Table>` line). Files are limited to 1 GB, `network.db` to 1 GB uncompressed, and text plans to 512 MB, 2,000 tables and 2,000 columns per table. For a text plan, the Overview shows the `Network` table's `Version` as the plan format.

## Views

| Tab | Contents |
|-----|----------|
| **Overview** | Summary cards, plan file details, network properties and options, AS list, top utilised interfaces. |
| **Utilisation** | Share of interfaces in each utilisation band, per capacity range, as stacked 100% bars, plus a summary table. Click a bar segment to list its interfaces. |
| **Topology** | Optional stacked 3D views: **3D Flex-Algo** and **3D L1/L3** (L3 over the L1 topology). Nodes are placed at their schematic X/Y from `<PlotLayoutElements>` when the plan has them, otherwise by a D3 [force-directed graph](https://observablehq.com/@d3/force-directed-graph/2), or geographically when every node has latitude/longitude. A **Layout** menu switches between the available layouts. Links are coloured by utilisation and sized by capacity, and parallel circuits between two nodes fan out as curves so each is visible. Hovering a node turns its circuits cyan, draws them above the other links, outlines its neighbours and fades everything else. Hover a link for details, drag a node to pull the graph around it, scroll to zoom, double-click a node to open it in **Nodes**. |
| **Nodes** | Addresses, prefix SID, flex-algo SIDs, SRv6 locators, capabilities, vendor info and tags. |
| **Circuits** | Both endpoints with per-direction traffic, utilisation, IGP/TE metrics, delay and SRLGs. |
| **Interfaces** | Remote end, IP addresses, capacity, traffic, utilisation, metrics and adjacency SIDs. |
| **SR Policies / LSPs** | Headend, endpoint, color, and every candidate path with its preference, metric type, constraints and segment list hops (★ = active path). |
| **Demands** | A hierarchical edge bundling chart of demand flows, followed by each demand's source and destination (prefix destinations decoded), traffic and share of total. |
| **SR & Flex-Algo** | Flex-algorithm definitions with participating nodes, node SIDs, SRv6 locators and adjacency SIDs. |
| **SRLGs** | Each SRLG with its member circuits and nodes. |
| **All Tables** | Browse any of the plan's raw SQLite tables. |

### Utilisation by capacity range

Interfaces are grouped into capacity ranges with fixed boundaries at 10G, 40G, 100G and 400G. Lower bounds are inclusive and upper bounds exclusive. Only the ranges the plan reaches are shown, and the top range ends at the largest interface capacity in the plan (*max*), including it:

| Largest capacity in plan | Ranges shown |
|--------------------------|--------------|
| 5G | Up to 5G (max) |
| 30G | Below 10G · 10G to 30G (max) |
| 400G | Below 10G · 10G to <40G · 40G to <100G · 100G to <400G · 400G (max) |
| 500G | Below 10G · 10G to <40G · 40G to <100G · 100G to <400G · 400G to 500G (max) |

Interfaces are grouped into the utilisation bands `<1%`, `1–<40%`, `40–<70%`, `70–<90%`, `≥90%` and `No data` (no measured traffic). Interfaces without an interface or circuit capacity are shown in an *Unknown capacity* row.

### Topology layouts

- **Plot layout** (default when available): each node with `X` and `Y` in a `<PlotLayoutElements>` row for that layout (`ElementNode` set, no `ElementInterface`), or whose site has one, is pinned there. One scale is used for both axes so the shape matches WAE Design, and `Y` grows downward. Nodes without coordinates are placed by the force layout around the pinned ones. Each layout in `<PlotLayouts>` with at least two distinct locations appears in the menu, with `Default` first.
- **Geographic:** offered when every node can be placed by its own or its site's longitude/latitude, and at least two locations differ.
- **OpenFreeMap:** a checkbox shown in the geographic layout, off by default. When ticked, an [OpenFreeMap](https://openfreemap.org) vector map (the *Dark* style, drawn with [MapLibre GL JS](https://maplibre.org)) is drawn under the topology, and nodes are placed on a Web Mercator projection with one scale for both axes so they line up with the map. The map follows pan and zoom smoothly at any zoom level. Map data (style, vector tiles, fonts and icons) is fetched from `tiles.openfreemap.org`, so this is the only feature that needs network access, and OpenFreeMap sees which area you view. It needs WebGL; without it, or when the map data cannot be loaded, a note appears next to the attribution. Unticking it removes the map and restores the stretched longitude/latitude layout.
- **Sites:** in plot and geographic layouts, a node that belongs to a site is drawn at the site's location instead of its own: the site's `X`/`Y` row in `<PlotLayoutElements>`, or the site's longitude/latitude in `<Sites>`. If the site has no location, the nearest parent site (`ParentSite`) with one is used. Nodes sharing a site are arranged in a ring around that point inside a labelled halo, for example *EU (3)*. Nodes with no located site use their own coordinates.
- **Show Groups:** a checkbox next to **Layout**, shown when the layout has located sites. When ticked, each site is drawn as a single node at its location, labelled with its node count. Circuits inside a site are hidden. All circuits between two groups (sites or ungrouped nodes) are merged into one link, coloured by the highest utilisation among them and sized by their total capacity; hover it to see the circuit count and which circuit is busiest. Double-click a site to list its nodes in **Nodes**.
- **Force-directed:** always offered. When the plan has a plot layout, the force layout starts from plot layout `Default` (or the first plot layout with coordinates), including nodes placed at their site, and gently pulls each node back towards that position. The result keeps the plot's overall shape while the forces separate overlapping nodes. Nodes are not pinned, and Re-layout scatters them and lets them settle back towards the same shape.

- **3D Flex-Algo:** a checkbox shown when the plan has `<FlexAlgorithms>`. It replaces the graph with an Algo 0 baseline pane at the bottom and one tilted pane per Flex-Algorithm above it, in ascending algorithm order, each drawn over the selected layout's node positions. Each pane is labelled with its metric type, constraints, member count and usable links, for example *Algo 129 · igp · excl 10 · 7 nodes · 8/17 links*.
  - **Nodes** listed for the algorithm in `<NodeFlexAlgorithms>` are drawn in the pane's colour; other nodes are faded ghosts. Dashed vertical pillars join a node's copies across the panes it takes part in.
  - **Links** are judged per direction, using the attributes of the interface the direction leaves from. The direction is usable when the interface's IGP process defines the algorithm, both nodes take part in it in that process, its affinities pass the definition's `ExcludeAll` (any listed bit excludes), `IncludeAny` (at least one bit) and `IncludeAll` (every bit), and the interface is in no SRLG excluded for the algorithm (`<FlexAlgoExcludeSRLGs>`, `<FlexAlgoSRLGInterfaces>`). Affinities come from `FlexAlgoAffinities`, or from `Affinities` when that is blank. Each link is split at its midpoint: the half next to a node shows the direction leaving it, solid when usable and dashed when pruned.
  - **Hover** a link to see each direction's status, the reason it was pruned (for example *R3 TenGigE0/0/0/2 has FA affinities 10 (exclude 10)*) or the metric the algorithm uses (`igp` = IGP metric, `te` = TE metric, `delay` = delay). Hover a node to see the algorithms and Flex-Algo SIDs it has and to highlight its copies, pillars and links on every pane.
  - **Starting view:** the panes start from the current 2D plot, with the selected layout and any nodes you have dragged, unrotated so left/right and up/down match the 2D view and tilted 60° so the stack is visible. Each time you tick the box the rotation resets to this view.
  - **Controls:** drag to rotate, Shift+drag to pan, scroll to zoom, **Fit** to frame all panes. **Pane spacing** sets the gap between panes, and the **Panes** chips show or hide algorithms. **Algo 0 (baseline)**, on by default, is the bottom pane: every node and link of the IGP topology, so each Flex-Algo pane above can be compared with it; untick it to hide it. Node size applies; Show Groups, Directional, Curvature and the map background are hidden. An algorithm defined in several IGP processes shares one pane, and each link is judged against its own process's definition. For very large scenes (nodes × panes above 1,500), ghost nodes and pruned links are left out.

- **3D L1/L3:** a checkbox shown when the plan has `<L1Nodes>`. It uses the same stacked 3D view as 3D Flex-Algo, with the L1 (optical) topology as the bottom pane and the L3 (IP) topology on a pane above it, over the selected layout's node positions. The two 3D views are exclusive; ticking one unticks the other.
  - **L1 nodes** are drawn as squares at their site's location, or their own, in the layout: the `ElementL1Node` X/Y in a plot layout, or longitude/latitude in the geographic layout. Otherwise they sit under the L3 nodes attached to them, and an L1 node with neither is placed by a force layout along its L1 links. Dashed pillars join each L3 node to its L1 node (`<L3L1Links>`).
  - **L1 links** are thicker the more L3 circuits they carry, dotted when they carry none and red-dashed when down. In the geographic layout they follow their `<L1LinkWaypoints>`.
  - **L3 circuits** are coloured by utilisation as in 2D, each half by direction when **Directional** is ticked. Each circuit is matched to its L1 circuit by its `L1CircuitNodeA/PortA/NodeB/PortB`, in either order.
  - **L1 routes:** an L1 circuit follows `<ActualL1CircuitPathHops>`. Otherwise it follows the `<L1CircuitPathHops>` of its active path option, with any gaps between hops filled by the lowest-metric path, so loose hops work. Failing both, the viewer estimates the lowest L1 `Metric` path over the L1 links that are up. It does not model spectrum or `MaxL1CircuitPaths` limits.
  - **Hover** an L3 circuit to see its L1 route in cyan, an L3 node to see its circuits and their routes, or an L1 link or node to see every L3 circuit that rides it (what a fibre cut or ROADM failure would hit). Tooltips give the L1 circuit, route, metric and where the route came from. L3 circuits without L1 ports are reported as L3 only.
  - **Controls** are as in 3D Flex-Algo: drag to rotate, Shift+drag to pan, scroll to zoom, **Pane spacing**, **Node size** and **Fit**.

- **Directional:** a checkbox, on by default. Each link is split at its midpoint, and each half is coloured by the utilisation of the traffic leaving the node at that end: A→B next to A, B→A next to B. This shows which direction is busy, using the same colour ranges (the layout's `UtilizationColors`, or the built-in bands). Merged group links use the highest utilisation in each direction. Untick it to colour each link by the higher of its two directions. Toggling keeps node positions and zoom.

In plot and geographic layouts, circuits are curved to stay readable: parallel circuits fan out with spacing scaled to the link's length, links that would run over another node bow around it, and nearly overlapping links (for example two links leaving the same site towards the same place) are pushed apart. Curves are recomputed after you drag a node. The **Curvature** slider (0 = straight lines, up to 3×, default 1×) scales every curve in all layouts, moving links further apart or closer together without moving nodes. The **Node size** slider, just left of it (0.3× to 3×, default 1×), scales node circles, their labels and Show Groups site nodes, and grows site halos and their labels to match, also without moving nodes; Fit accounts for the larger halos. Pinned nodes stay where you drop them. Re-layout puts them back at their coordinates and re-settles the rest.

### Utilisation colours

Topology links use the plot layout's own **UtilizationColors** from `<PlotLayoutOptions>`, for example `#075E39,30,#45991F,50,#028E99,80,#CC8604,90,#F26722,100,#CC2D37`. That value is colours separated by ascending thresholds, so here: <30%, 30–<50%, … ≥100%. The row matching the layout's `PlotLayoutType` in `<PlotLayouts>` is used (Design by default), and the legend shows that layout's ranges.

- **Plot layouts** each use their own colours.
- **Force-directed and Geographic** use the colours of their base layout (`Default`).
- **Fallback:** layouts without a valid value (hex colours and ascending numeric thresholds) use the built-in bands. Links without traffic data are always grey.

### Demand flows

The Demands tab opens with a [hierarchical edge bundling](https://observablehq.com/@d3/hierarchical-edge-bundling/2) chart. Each demand endpoint is placed on a ring, and demands between the same source and destination are merged into one flow whose line width scales with traffic. Hover over or focus a node to highlight its outgoing flows (amber) and incoming flows (teal). The chart shows only the demands that match the row filter. The **Demands graphed** slider limits the chart to the N largest of those demands by traffic, up to 1,000. By default it graphs all of them, or the largest 1,000 when there are more.

- **Endpoints:** a prefix destination such as `ipv6pfx{fcbb:bb03:101::/48}` is mapped to the node that owns that SRv6 locator, loopback (`NodePrefixLoopbacks`) or node address. Endpoints that cannot be mapped are shown under *External*.
- **Hierarchy:** flows are bundled through each node's site and its parent sites (`Sites.ParentSite`). Without sites, nodes are grouped by AS when the plan has more than one; otherwise flows bundle through the centre.
- The layout and bundling (bundle strength 0.85) are ported from D3's `cluster` and `curveBundle`, so the viewer has no extra dependency.

Bandwidth values in a plan are in Mbps and are shown with automatic units. Utilisation is measured traffic (`InterfaceTraffic.TraffMeas`) divided by interface capacity, falling back to circuit capacity. When a plan has multiple traffic levels, a selector appears in the toolbar. Internal `mate::pln::id::*` columns are hidden unless **Show internal IDs** is ticked.

## Security model

Plan files are treated as untrusted input.

| Control | Detail |
|---------|--------|
| Content Security Policy | `default-src 'none'`. Only the four embedded scripts (sql.js, D3, MapLibre, the app) and the stylesheet run, allowed by SHA-256 hash. `img-src 'none'`. `connect-src` allows only `https://tiles.openfreemap.org` (and `blob:`), used by the opt-in OpenFreeMap background; nothing is requested unless that box is ticked. `worker-src blob: data:` lets MapLibre start its embedded worker; when the page is opened from `file://`, MapLibre re-reads the worker from its `blob:` URL into a `data:` URL. |
| Trusted Types | `require-trusted-types-for 'script'`. The only HTML sink is `setHtml()`, which accepts only output of the auto-escaping `` h`...` `` template. A `default` policy accepts exactly two script URLs, the `blob:` and `data:` URLs of MapLibre's embedded worker, and rejects every other script or worker URL and all HTML. |
| Output encoding | Every value from the plan is HTML-escaped. No inline event handlers or inline styles. |
| SQLite | Opened with `PRAGMA query_only = ON` and `trusted_schema = OFF`. Table and column names from the file are quoted as identifiers; all queries are read-only. |
| Zip handling | Only `network.db` and `version.dat` are extracted. Entry count, offsets, declared vs. actual size (zip-bomb guard), encryption flag and CRC-32 are all checked. |
| Text plans | Must be valid UTF-8. They are parsed as plain data and loaded with parameterised inserts into a fresh in-memory database, which is then made read-only. Table and column names are quoted as identifiers. Rows outside a `<Table>` section are rejected. |
| CSV export | Cells starting with `=`, `+`, `-`, `@`, tab or CR are prefixed with `'` to prevent spreadsheet formula injection. |
| Supply chain | sql.js, D3 and MapLibre GL JS are pinned to exact versions, verified by the npm lockfile integrity hash, and the build fails if the installed versions differ. D3's prebuilt bundle (`d3.min.js`) is inlined as is; the app never calls its HTML or CSV-parsing functions, and the CSP and Trusted Types would block their `innerHTML` and `new Function` uses. MapLibre's ES module is inlined as a module script with its export list replaced by a `maplibregl` global holding only `Map` and `setWorkerUrl`; its worker is embedded as base64 and its stylesheet is inlined. The app creates no popups or controls, so MapLibre's only `innerHTML` use (`Popup.setHTML`) is never reached. The build records the WASM SHA-256 in the page's `<meta name="generator">`. |

A `<meta>` CSP cannot set `frame-ancestors`. If you host the viewer on a web server, also send `Content-Security-Policy: frame-ancestors 'none'` and `X-Content-Type-Options: nosniff` as HTTP headers.

## Development

`plan-viewer.html` is a build artifact. Edit the sources in `src/` and rebuild.

| Path | Purpose |
|------|---------|
| `src/template.html` | Page skeleton with build placeholders |
| `src/styles.css` | Styles |
| `src/app.js` | Application logic |
| `build.mjs` | Inlines sources, sql.js, D3 and MapLibre into `plan-viewer.html` and computes CSP hashes |
| `test/smoke.mjs` | Headless Chrome functional and security tests |

```bash
npm ci            # install exact locked dependencies
npm run build     # produce plan-viewer.html
npm run lint      # ESLint (incl. no-unsanitized) + html-validate
npm test          # smoke and security tests
npm run check     # lint, build, audit and test in one go
```

`npm test` uses Google Chrome at its default macOS location; set `CHROME_PATH` to override. It loads the sample plan from this folder or its parent, or the file in `PLAN_FILE`. It also loads the matching `.txt` export (or `PLAN_TEXT_FILE`) and checks that it renders the same as the `.pln`. Generated hostile fixtures cover XSS payloads, hostile table names, zip bombs, bad CRCs and malformed text plans.
