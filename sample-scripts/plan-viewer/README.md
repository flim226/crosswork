# Plan Viewer

A single-file, browser-based viewer for Cisco Crosswork Planning / WAE plan files (`.pln`).

## Usage

1. Open `plan-viewer.html` in Chrome, Edge, Firefox or Safari.
2. Drop a `.pln` file onto the page, or click the drop zone to browse.
3. Use the tabs to explore the plan. The filter box applies to every table on the current tab; click a column header to sort; **⬇ CSV** exports a table.

The plan is parsed entirely in the browser — no data is uploaded anywhere. The SQLite engine ([sql.js](https://sql.js.org)) is loaded from cdnjs, so the first load needs internet access.

## Supported files

| Format | Description |
|--------|-------------|
| Zipped plan (CP 8.x) | Zip archive containing `network.db` (SQLite) and `version.dat`. |
| Raw SQLite plan | Older plan files that are a SQLite database directly. |

## Views

| Tab | Contents |
|-----|----------|
| **Overview** | Summary cards, plan file details, network properties and options, AS list, top utilised interfaces. |
| **Topology** | Interactive graph (force-directed, or geographic when every node has latitude/longitude). Links are coloured by utilisation; hover for details, drag to rearrange, scroll to zoom, double-click a node to open it in **Nodes**. |
| **Nodes** | Addresses, prefix SID, flex-algo SIDs, SRv6 locators, capabilities, vendor info and tags. |
| **Circuits** | Both endpoints with per-direction traffic, utilisation, IGP/TE metrics, delay and SRLGs. |
| **Interfaces** | Remote end, IP addresses, capacity, traffic, utilisation, metrics and adjacency SIDs. |
| **SR Policies / LSPs** | Headend, endpoint, color, and every candidate path with its preference, metric type, constraints and segment list hops (★ = active path). |
| **Demands** | Source and destination (prefix destinations decoded), traffic and share of total. |
| **SR & Flex-Algo** | Flex-algorithm definitions with participating nodes, node SIDs, SRv6 locators and adjacency SIDs. |
| **SRLGs** | Each SRLG with its member circuits and nodes. |
| **All Tables** | Browse any of the plan's raw SQLite tables. |

## Notes

- Bandwidth values in a plan are in Mbps and are shown with automatic units (Kbps/Mbps/Gbps/Tbps).
- Utilisation is measured traffic (`InterfaceTraffic.TraffMeas`) divided by interface capacity, falling back to circuit capacity. When a plan has multiple traffic levels, a selector appears in the toolbar.
- Internal `mate::pln::id::*` columns are hidden by default; tick **Show internal IDs** to display them.
