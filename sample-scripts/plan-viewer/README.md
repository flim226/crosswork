# Plan Viewer

A single-file, fully offline, browser-based viewer for Cisco Crosswork Planning / WAE plan files (`.pln`).

## Usage

1. Open `plan-viewer.html` in Chrome, Edge, Firefox or Safari. No internet connection, server or install is needed.
2. Drop a `.pln` file onto the page, or click the drop zone to browse.
3. Use the tabs to explore the plan. The filter box applies to every table on the current tab; click a column header to sort; **⬇ CSV** exports a table.

The plan is parsed entirely in the browser and never leaves your machine. The SQLite engine ([sql.js](https://sql.js.org) 1.12.0, WebAssembly) and [D3](https://d3js.org) 7.9.2 are embedded in the HTML file.

## Supported files

| Format | Description |
|--------|-------------|
| Zipped plan (CP 8.x) | Zip archive containing `network.db` (SQLite) and `version.dat`. |
| Raw SQLite plan | Older plan files that are a SQLite database directly. |

Accepted extensions are `.pln`, `.db`, `.sqlite` and `.sqlite3`; the content is also checked by its file signature. Files are limited to 1 GB, and `network.db` to 1 GB uncompressed.

## Views

| Tab | Contents |
|-----|----------|
| **Overview** | Summary cards, plan file details, network properties and options, AS list, top utilised interfaces. |
| **Utilisation** | Share of interfaces in each utilisation band, per capacity range, as stacked 100% bars, plus a summary table. Click a bar segment to list its interfaces. |
| **Topology** | Nodes are placed at their schematic X/Y from `<PlotLayoutElements>` when the plan has them, otherwise by a D3 [force-directed graph](https://observablehq.com/@d3/force-directed-graph/2), or geographically when every node has latitude/longitude. A **Layout** menu switches between the available layouts. Links are coloured by utilisation and sized by capacity, and parallel circuits between two nodes fan out as curves so each is visible. Hovering a node turns its circuits cyan, draws them above the other links, outlines its neighbours and fades everything else. Hover a link for details, drag a node to pull the graph around it, scroll to zoom, double-click a node to open it in **Nodes**. |
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
- **Sites:** in plot and geographic layouts, a node that belongs to a site is drawn at the site's location instead of its own: the site's `X`/`Y` row in `<PlotLayoutElements>`, or the site's longitude/latitude in `<Sites>`. If the site has no location, the nearest parent site (`ParentSite`) with one is used. Nodes sharing a site are arranged in a ring around that point inside a labelled halo, for example *EU (3)*. Nodes with no located site use their own coordinates.
- **Show Groups:** a checkbox next to **Layout**, shown when the layout has located sites. When ticked, each site is drawn as a single node at its location, labelled with its node count. Circuits inside a site are hidden. All circuits between two groups (sites or ungrouped nodes) are merged into one link, coloured by the highest utilisation among them and sized by their total capacity; hover it to see the circuit count and which circuit is busiest. Double-click a site to list its nodes in **Nodes**.
- **Force-directed:** always offered. When the plan has a plot layout, the force layout starts from plot layout `Default` (or the first plot layout with coordinates), including nodes placed at their site, and gently pulls each node back towards that position. The result keeps the plot's overall shape while the forces separate overlapping nodes. Nodes are not pinned, and Re-layout scatters them and lets them settle back towards the same shape.

In plot and geographic layouts, circuits are curved to stay readable: parallel circuits fan out with spacing scaled to the link's length, links that would run over another node bow around it, and nearly overlapping links (for example two links leaving the same site towards the same place) are pushed apart. Curves are recomputed after you drag a node. The **Curvature** slider (0 = straight lines, up to 3×, default 1×) scales every curve in all layouts, moving links further apart or closer together without moving nodes. At 0 the parallel circuits lie on top of each other, so a link carrying more than one circuit is drawn dotted; it turns solid again once the curvature is above 0. Pinned nodes stay where you drop them. Re-layout puts them back at their coordinates and re-settles the rest.

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
| Content Security Policy | `default-src 'none'`. Only the three embedded scripts (sql.js, D3, the app) and the stylesheet run, allowed by SHA-256 hash. `connect-src 'none'` means the page cannot send data anywhere. |
| Trusted Types | `require-trusted-types-for 'script'`. The only HTML sink is `setHtml()`, which accepts only output of the auto-escaping `` h`...` `` template. |
| Output encoding | Every value from the plan is HTML-escaped. No inline event handlers or inline styles. |
| SQLite | Opened with `PRAGMA query_only = ON` and `trusted_schema = OFF`. Table and column names from the file are quoted as identifiers; all queries are read-only. |
| Zip handling | Only `network.db` and `version.dat` are extracted. Entry count, offsets, declared vs. actual size (zip-bomb guard), encryption flag and CRC-32 are all checked. |
| CSV export | Cells starting with `=`, `+`, `-`, `@`, tab or CR are prefixed with `'` to prevent spreadsheet formula injection. |
| Supply chain | sql.js and D3 are pinned to exact versions, verified by the npm lockfile integrity hash, and the build fails if the installed versions differ. D3's prebuilt bundle (`d3.min.js`) is inlined as is; the app never calls its HTML or CSV-parsing functions, and the CSP and Trusted Types would block their `innerHTML` and `new Function` uses. The build records the WASM SHA-256 in the page's `<meta name="generator">`. |

A `<meta>` CSP cannot set `frame-ancestors`. If you host the viewer on a web server, also send `Content-Security-Policy: frame-ancestors 'none'` and `X-Content-Type-Options: nosniff` as HTTP headers.

## Development

`plan-viewer.html` is a build artifact. Edit the sources in `src/` and rebuild.

| Path | Purpose |
|------|---------|
| `src/template.html` | Page skeleton with build placeholders |
| `src/styles.css` | Styles |
| `src/app.js` | Application logic |
| `build.mjs` | Inlines sources, sql.js and D3 into `plan-viewer.html` and computes CSP hashes |
| `test/smoke.mjs` | Headless Chrome functional and security tests |

```bash
npm ci            # install exact locked dependencies
npm run build     # produce plan-viewer.html
npm run lint      # ESLint (incl. no-unsanitized) + html-validate
npm test          # smoke and security tests
npm run check     # lint, build, audit and test in one go
```

`npm test` uses Google Chrome at its default macOS location; set `CHROME_PATH` to override. It loads the sample plan from the parent folder, or the file in `PLAN_FILE`, plus generated hostile fixtures (XSS payloads, hostile table names, zip bomb, bad CRC).
