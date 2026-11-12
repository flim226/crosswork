# Plan Viewer

A single-file, fully offline, browser-based viewer for Cisco Crosswork Planning / WAE plan files (`.pln`).

## Usage

1. Open `plan-viewer.html` in Chrome, Edge, Firefox or Safari. No internet connection, server or install is needed.
2. Drop a `.pln` file onto the page, or click the drop zone to browse.
3. Use the tabs to explore the plan. The filter box applies to every table on the current tab; click a column header to sort; **⬇ CSV** exports a table.

The plan is parsed entirely in the browser and never leaves your machine. The SQLite engine ([sql.js](https://sql.js.org) 1.12.0, WebAssembly) is embedded in the HTML file.

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
| **Topology** | Interactive graph (force-directed, or geographic when every node has latitude/longitude). Links are coloured by utilisation; hover for details, drag to rearrange, scroll to zoom, double-click a node to open it in **Nodes**. |
| **Nodes** | Addresses, prefix SID, flex-algo SIDs, SRv6 locators, capabilities, vendor info and tags. |
| **Circuits** | Both endpoints with per-direction traffic, utilisation, IGP/TE metrics, delay and SRLGs. |
| **Interfaces** | Remote end, IP addresses, capacity, traffic, utilisation, metrics and adjacency SIDs. |
| **SR Policies / LSPs** | Headend, endpoint, color, and every candidate path with its preference, metric type, constraints and segment list hops (★ = active path). |
| **Demands** | Source and destination (prefix destinations decoded), traffic and share of total. |
| **SR & Flex-Algo** | Flex-algorithm definitions with participating nodes, node SIDs, SRv6 locators and adjacency SIDs. |
| **SRLGs** | Each SRLG with its member circuits and nodes. |
| **All Tables** | Browse any of the plan's raw SQLite tables. |

### Utilisation by capacity range

The capacity ranges scale with the largest interface capacity in the plan (*max*). Lower bounds are inclusive and upper bounds exclusive:

| Range | Boundaries | With a 400G max | With a 30G max |
|-------|-----------|-----------------|----------------|
| 1 | below max/40 | below 10G | below 750M |
| 2 | max/40 to below max/10 | 10G to <40G | 750M to <3G |
| 3 | max/10 to below max/4 | 40G to <100G | 3G to <7.5G |
| 4 | max/4 to below max | 100G to <400G | 7.5G to <30G |
| 5 | max | 400G | 30G |

Interfaces are grouped into the utilisation bands `<1%`, `1–<40%`, `40–<70%`, `70–<90%`, `≥90%` and `No data` (no measured traffic). Interfaces without an interface or circuit capacity are shown in an *Unknown capacity* row.

Bandwidth values in a plan are in Mbps and are shown with automatic units. Utilisation is measured traffic (`InterfaceTraffic.TraffMeas`) divided by interface capacity, falling back to circuit capacity. When a plan has multiple traffic levels, a selector appears in the toolbar. Internal `mate::pln::id::*` columns are hidden unless **Show internal IDs** is ticked.

## Security model

Plan files are treated as untrusted input.

| Control | Detail |
|---------|--------|
| Content Security Policy | `default-src 'none'`. Only the two embedded scripts and the stylesheet run, allowed by SHA-256 hash. `connect-src 'none'` means the page cannot send data anywhere. |
| Trusted Types | `require-trusted-types-for 'script'`. The only HTML sink is `setHtml()`, which accepts only output of the auto-escaping `` h`...` `` template. |
| Output encoding | Every value from the plan is HTML-escaped. No inline event handlers or inline styles. |
| SQLite | Opened with `PRAGMA query_only = ON` and `trusted_schema = OFF`. Table and column names from the file are quoted as identifiers; all queries are read-only. |
| Zip handling | Only `network.db` and `version.dat` are extracted. Entry count, offsets, declared vs. actual size (zip-bomb guard), encryption flag and CRC-32 are all checked. |
| CSV export | Cells starting with `=`, `+`, `-`, `@`, tab or CR are prefixed with `'` to prevent spreadsheet formula injection. |
| Supply chain | sql.js is pinned to an exact version and verified by the npm lockfile integrity hash. The build records the WASM SHA-256 in the page's `<meta name="generator">`. |

A `<meta>` CSP cannot set `frame-ancestors`. If you host the viewer on a web server, also send `Content-Security-Policy: frame-ancestors 'none'` and `X-Content-Type-Options: nosniff` as HTTP headers.

## Development

`plan-viewer.html` is a build artifact. Edit the sources in `src/` and rebuild.

| Path | Purpose |
|------|---------|
| `src/template.html` | Page skeleton with build placeholders |
| `src/styles.css` | Styles |
| `src/app.js` | Application logic |
| `build.mjs` | Inlines sources and sql.js into `plan-viewer.html` and computes CSP hashes |
| `test/smoke.mjs` | Headless Chrome functional and security tests |

```bash
npm ci            # install exact locked dependencies
npm run build     # produce plan-viewer.html
npm run lint      # ESLint (incl. no-unsanitized) + html-validate
npm test          # smoke and security tests
npm run check     # lint, build, audit and test in one go
```

`npm test` uses Google Chrome at its default macOS location; set `CHROME_PATH` to override. It loads the sample plan from the parent folder, or the file in `PLAN_FILE`, plus generated hostile fixtures (XSS payloads, hostile table names, zip bomb, bad CRC).
