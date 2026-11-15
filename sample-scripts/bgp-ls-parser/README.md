# BGP-LS Parser — Getting Started Tutorial

> **Document type:** Tutorial (Diátaxis: learning-oriented)
> **Audience:** Network engineers familiar with BGP and Segment Routing, new to this tool.

---

## 1. Introduction

The **BGP-LS Parser** is a standalone, browser-based tool that parses raw output from the IOS-XR commands `show bgp link-state link-state`, `show lslib server topology-db` and `show lslib server topology-db detail`, and presents it in a structured, sortable, and filterable dashboard.

It extracts and decodes all BGP-LS Network Layer Reachability Information (NLRI) types:

- **Node NLRIs** (`[V]`) — with `detail`: hostname, TE router-IDs, SR-ALG, SRGB/SRLB, SRv6-Capabilities, MSD (MPLS and SRv6 types 41/42/44/45), Flex-Algo Definitions.
- **Link NLRIs** (`[E]`) — IPv4 and IPv6 (MT 0x0002); with `detail`: IGP/TE metric, bandwidths, delay, admin groups, SRLG, MPLS ADJ-SIDs, **SRv6 End.X SIDs** (behavior, algo, B/S/P flags, SID structure) and ASLA (flex-algo application attributes).
- **Prefix NLRIs** (`[T]`) — IPv4 and IPv6; with `detail`: metric, prefix-SIDs per algo, IGP flags and the **SRv6-Locator TLV** (algo, metric, D-flag for leaked locators).
- **SRv6 SID NLRIs** (`[S]`) — SID, endpoint behavior (e.g. 48 = uN), algo, SID structure.
- **SR Policy NLRIs** (`[SP]`) — candidate paths; with `detail`: policy/CP name, state, MPLS or **SRv6 BSID**, constraints, segment lists including **SRv6 segments and their endpoint behavior**.

When `detail` output is parsed, an **SRv6 Health** tab cross-checks the topology (see section 8a), and the **Topology** tab draws the IGP graph (see section 8b).

By the end of this tutorial you will be able to drop a CLI capture onto the parser, understand every column in the resulting tables, use filters and sorting to find the data you need, and explore the topology.

---

## 2. Requirements

- Platform: None (browser-only). Input is `show bgp link-state link-state` output from IOS-XR, for example SR-PCE.
- A modern web browser (Chrome, Firefox, Safari, or Edge).
- Access to the `bgp-ls-parser.html` file.
- Optionally, raw output from `show bgp link-state link-state` collected from an IOS-XR router (e.g., an SR-PCE).

No installation, server, or internet connection is required — the tool runs entirely in the browser.

---

## 3. Opening the Tool

Open `bgp-ls-parser.html` by double-clicking the file or dragging it into a browser window. You will see:

- A **title bar** ("🔍 BGP-LS Parser").
- A **drop zone**. Drop a `.txt` / `.log` CLI capture anywhere on the page, or click the drop zone to browse for one.
- An **Or paste CLI output** panel with a text area plus *Parse* and *Load Sample* buttons.

After a file is loaded, the drop zone shrinks to an **Open another capture** bar, and the page shows:

- a **toolbar** with the file summary, filter controls, *Export JSON* and *Close*;
- **tabs** with entry counts: **Overview**, **Topology**, **Nodes**, **Links**, **Prefixes**, **SRv4 SIDs**, **SRv6 Locators**, **SR Policies** and **SRv6 Health**.

---

## 4. Your First Parse: Loading Sample Data

Open **Or paste CLI output** and click **Load Sample**. The text area fills with a small synthetic `show lslib server topology-db detail` capture, and the parser runs automatically.

You should now see:

1. The **Overview** tab, with stat cards for every NLRI type plus SRv6 and SR Policy summaries.
2. Tabs for each NLRI table. Clicking a stat card opens the matching tab.
3. A **Topology** tab with two routers and one adjacency.

This confirms the tool is working. The rest of this tutorial explains what you are looking at.

---

## 5. Understanding the Dashboard

After parsing, a row of **stat cards** summarises the data at a glance:

| Card | Meaning |
|------|---------|
| **Total Entries** | Combined count of prefix + SR Policy NLRIs parsed. |
| **Prefixes** | Number of IS-IS Prefix (`[T]`) entries. |
| **SR Policies** | Number of SR Policy (`[SP]`) entries. |
| **Unique Colors** | Distinct SR Policy color values found (decimal). |
| **Unique Headends** | Distinct headend router addresses (`te` field). |
| **Unique Endpoints** | Distinct endpoint addresses (`e` field). |
| **IPv4 Endpoint** | SR Policies with an IPv4 endpoint (flags bit 7 = 0). |
| **IPv6 Endpoint** | SR Policies with an IPv6 endpoint (flags bit 7 = 1). |
| **\<Origin\> Origin** | Count of SR Policies per protocol origin (PCEP, BGP, Config). |

---

## 6. Reading the IS-IS Prefix Table

The **IS-IS Prefix NLRIs** section appears when Topology (`[T]`) entries are found. Each row represents one prefix advertisement.

| Column | Field Code | Description |
|--------|-----------|-------------|
| **#** | — | Row number (display only). |
| **Status** | Path marker | BGP path status. Tags indicate: **best** (best path `>`), **valid** (`*`), **iBGP** (learned via iBGP `i`), or **local** (locally originated). |
| **Level** | `[L1]` / `[L2]` | IS-IS level (Level 1 or Level 2). |
| **Instance** | `[I...]` | IS-IS instance ID shown as decimal and hex. |
| **ASN (c)** | `[c...]` | Autonomous System Number from the node descriptor. |
| **BGP Router (b)** | `[b...]` | BGP Router ID from the node descriptor. |
| **System ID (s)** | `[s...]` | IS-IS System ID (e.g., `0000.0000.6666.00`). |
| **Prefix (p)** | `[p...]` | The advertised IP prefix (e.g., `66.66.66.66/32`). |
| **Next-Hop** | — | BGP next-hop from the continuation line. |
| **Local Pref** | — | BGP Local Preference attribute. |
| **MED** | — | Multi-Exit Discriminator attribute. |
| **Origin** | — | BGP origin code: IGP (`i`), EGP (`e`), or Incomplete (`?`). |
| **NLRI Size** | `/nnn` | The NLRI byte length shown at the end of the raw line. |

---

## 7. Reading the SR Policy Table

The **SR Policy NLRIs** section appears when SR Policy (`[SP]`) entries are found. Each row represents one SR Policy candidate path.

### Node Descriptor Fields (N block)

| Column | Field Code | Description |
|--------|-----------|-------------|
| **Instance (I)** | `[I...]` | Instance ID (decimal + hex). |
| **Node ASN (N.c)** | `[c...]` | ASN from the node descriptor `[N]` block. |
| **BGP Router (N.b)** | `[b...]` | BGP Router ID from the node descriptor. |
| **Orig Node (N.q)** | `[q...]` | Originator Node — the router that originated the NLRI. |
| **Headend (N.te)** | `[te...]` | Headend address of the SR Policy. |

### Candidate Path Descriptor Fields (C block)

| Column | Field Code | Description |
|--------|-----------|-------------|
| **Origin (C.po)** | `[po...]` | Protocol Origin — how the policy was created. Decoded values: `0x1` = **PCEP**, `0x2` = **BGP**, `0x3` = **Config**. |
| **Flags (C.f)** | `[f...]` | Flags field. Bit 7 (`0x80`) indicates an IPv6 endpoint. |
| **AF** | — | Address Family derived from the flags: **IPv4** or **IPv6**. |
| **Endpoint (C.e)** | `[e...]` | Endpoint address of the SR Policy (IPv4 or IPv6). |
| **Color (C.cl)** | `[cl...]` | SR Policy color shown as decimal and hex (e.g., `2000 (0x7d0)`). |
| **AS (C.as)** | `[as...]` | AS Number from the candidate path descriptor. |
| **Orig Addr (C.oa)** | `[oa...]` | Originator Address of the candidate path. |
| **Disc (C.di)** | `[di...]` | Discriminator — distinguishes multiple candidate paths for the same color/endpoint. |

### BGP Attribute Fields

| Column | Description |
|--------|-------------|
| **Next-Hop** | BGP next-hop from the continuation line. |
| **Local Pref** | BGP Local Preference attribute. |
| **MED** | Multi-Exit Discriminator. |
| **Origin** | BGP origin code (IGP / EGP / Incomplete). |
| **NLRI Size** | NLRI byte length. |

### Segment list hops

With `detail`, the **Segment list hops** column resolves every segment to the router, or router and interface, it steers through. Like plan-viewer, each segment list is drawn as a chain: the headend, then one chip per hop showing the hop type, the target and the SID. When a candidate path has several segment lists, each chain is prefixed with its weight (`w1`). Hover a chip to see how it was resolved. The **Headend** and **Endpoint** columns show the router name above the address when the address is a TE router ID or a host route in the dump.

| Hop type | Meaning |
|----------|---------|
| **Node** | A prefix SID, or an SRv6 uN or End SID, of that router. |
| **Interface** | An adjacency SID, or an SRv6 End.X or uA SID: the local router, its interface address, and the neighbor (`R1 10.0.12.1 → R2`). |
| **Anycast** | A prefix SID advertised by more than one router. |
| **Function** | Another local SRv6 function on the router, such as uDT. |
| **Unresolved** | Shown in red. The tooltip explains what was missing. |

The hops are walked from the headend, and each segment is looked up where it takes effect:

- **SR-MPLS label:** the label is first matched against prefix SIDs, using the SRGB of the router reached so far. Copies of a prefix SID re-advertised by another router (R-flag) are ignored. If no prefix SID matches, the label is looked up in the adjacency SIDs of **the router reached so far only**. Adjacency SIDs are local, and the same label often means a different link on each router. After an anycast SID or an unresolved hop, the walk no longer knows the current router, so a following adjacency SID is reported as unresolved rather than guessed.
- **SRv6 SID:** an exact End.X or SRv6 SID NLRI match resolves directly. Otherwise, if the covering locator's owner advertises micro-SID (NEXT-CSID) behaviors, the SID is treated as a uSID container. Each 16-bit uSID after the block is then either a node locator (`block:uSID::/48`, the uN of that router) or a local uSID of the router reached so far: its uA (End.X, giving an Interface hop) or another local function. Classic SIDs resolve to the locator's owner and, when advertised, its function. Where a locator is advertised by several routers, the originator wins: own (no D-flag) over leaked, then the lowest metric, which handles redistribution between IGP instances.
- **Segments without a SID** (node address types): matched by address.

When the walk ends somewhere other than the endpoint, the chain is tagged **ends at …**. Search also matches hop router names and interface addresses.

---

## 8. Parsing Your Own Data

1. SSH into your IOS-XR router (e.g., an SR-PCE) and run:

   ```
   show bgp link-state link-state
   ```

   Optionally pipe through a filter for a specific address:

   ```
   show bgp link-state link-state | include 66.66.66.66
   ```

2. Save the terminal session to a file, then **drag the file onto the page** (or click the drop zone to browse). Parsing starts immediately. Extract `.tar.gz` bundles first; binary files are rejected.

3. Alternatively, copy the output, open **Or paste CLI output**, paste it (`Ctrl+V` / `Cmd+V`) and click **Parse** (or press `Ctrl+Enter` / `Cmd+Enter`).

The tabs and counts update to reflect your data. **Close** returns to the empty drop zone.

### SRv4 SIDs tab

Lists every SR-MPLS **prefix SID** (from Prefix NLRIs) and **adjacency SID** (from Link NLRIs) in one table. It needs `detail` output.

| Column | Meaning |
|---|---|
| **Type** | *Prefix-SID* or *Adj-SID*. Type `prefix-sid` or `adj-sid` in the filter to show one kind. |
| **Node** | Advertising router: the prefix originator, or the local end of the adjacency. |
| **Prefix / Adjacency** | The prefix, or the remote router with the interface → neighbor addresses. |
| **Algo** | Prefix-SID algorithm (0 = SPF, 128+ = Flex-Algo). Adjacency SIDs carry no algorithm. |
| **Index / Label** | The advertised value: an SRGB index, or an absolute label when the V-flag is set. |
| **MPLS Label** | The resolved label. An index is resolved against the advertising node's SRGB ranges (in order) in the same IGP domain. |
| **Flags** | Hex value and decoded RFC 8667 flags. Prefix SIDs: R, N, P, E, V, L. Adjacency SIDs: F (IPv6), B (backup), V, L, S, P. |
| **Notes** | *node SID* (N-flag), *backup* (B-flag), **SID conflict** (the same algo and index used for a different prefix in the same domain; anycast prefixes advertised by several nodes are not conflicts), and issues such as an index outside the SRGB or a missing Node NLRI. |

The tab count turns red when any row has a conflict or an issue.

### SRv6 Locators tab

Lists the SRv6 SID NLRIs (`[S]`). The **Locator Prefix** column shows the SRv6-Locator prefix (with its algo and D-flag) of the same node and IGP instance that covers each SID. A node's own locators are preferred over leaked ones, then the most specific. When no advertised locator covers a SID, the cell shows the SID itself marked *no locator* (see the SRv6 Health checks). The SID address stays searchable in the filter, and clicking a row shows the full SID NLRI.

---

## 8a. SRv6 Health and Detail Views

Paste `show lslib server topology-db detail` output to enable the SRv6 attributes. The summary listing printed before the detail section is merged automatically (the **Duplicates Merged** card shows how many).

- **Click any row** to expand the parsed attributes (JSON) and the raw attribute text.
- **Export JSON** downloads the full parsed model and the SRv6 analysis.
- Node, link, prefix and SID tables show hostnames resolved from the Node NLRIs.

The **SRv6 Health** tab lists findings by severity. The Overview tab repeats the high and medium ones, and the tab count turns red when any finding is high:

| Check | Severity | Meaning |
|---|---|---|
| SRv6 nodes without SRv6 MSD | High | No MSD types 41/42/44/45; consumers such as CNC fall back to the MPLS MSD for SRv6 SID depth |
| SRv6-capable nodes with no SRv6-Locator TLV | High | SRv6-Capabilities present but no locator from the node in any domain |
| Locators not leaked between IGP instances | High | Other locators of the same algo are leaked into the instance, but these are not; shows whether the SR-MPLS prefix-SID for the same node/algo is leaked |
| Locator prefixes without SRv6-Locator TLV | Medium | SIDs fall in a plain IPv6 prefix with no locator TLV |
| SRv6 SIDs not covered / owned by several nodes | Medium | SID outside every prefix of its node, or several nodes own it with non-leaked locators |
| IPv6 links of SRv6 nodes without End.X | Medium | No SRv6 adjacency SID on the link |
| Hostname with several ISO system IDs | Medium | E.g. a mistyped NET in one IS-IS instance |
| SR policy CPs with no segments | Medium | Empty segment lists (no path / invalid) |
| Links without reservable bandwidth | Low/Medium | No max-reservable / unreserved BW |
| IPv4/IPv6 TE metric mismatch, unidirectional links, locator/SID only in another domain | Low | Consistency checks |
| FAD Excl-Min-BW / Excl-Max-Delay, opaque SRv6 behavior 65535 | Info | Constraints not modelled by CNC; segment behavior to be resolved from the SID |

Node identity is the full node descriptor (instance, ASN, BGP-LS identifier, ISO ID), so feeds that export the same IGP via several BGP-LS producers are analysed per domain. SIDs carried inside leaked locators (re-advertised by ABRs) are counted as leaked, not flagged.

Tick **SRv6 only** in the toolbar to show only SRv6-capable nodes, links with End.X, locators, SRv6 SIDs and SRv6 policies.

### Running the tests

The parser and analysis run without a browser. From this directory:

```
node test/run-tests.mjs [dump1.txt dump2.txt ...]
```

The script runs header unit tests, the built-in sample, a synthetic multi-instance topology and topology edge-merging cases. For each dump given, it cross-checks every parsed count (NLRIs per type, End.X, locators, SIDs, MSDs, FADs, prefix/adj SIDs, ASLA, policy segments) against independent raw-text counts, checks that every link NLRI lands in exactly one topology edge, and prints the SRv6 findings. No dependencies are required (Node.js 18+).

---

## 8b. Topology View

The **Topology** tab draws a force-directed graph, using the same renderer as the plan-viewer tool (d3 v7 is embedded, so it still works offline).

- **Nodes** are routers, one per IS-IS system ID. A router present in several IGP instances is drawn once. Labels are hostnames from the Node NLRIs. A dashed node has links but no Node NLRI of its own.
- **Edges** are adjacencies. Both directions and IPv4/IPv6 of one adjacency merge into one edge: link NLRIs are matched on address pairs, or on IS-IS link IDs when `detail` is present. When several IGP instances (or BGP-LS producers) export the same adjacency, it is drawn as parallel curved edges.
- **Width** follows the maximum link bandwidth. **Dashed** edges are advertised in only one direction.
- **Hover** a router to highlight its adjacencies and see TE router-IDs, SR-ALG, MSD, locators and SID counts. Hover an edge to see A→B / B→A IGP and TE metrics, delay, admin group, bandwidth and End.X counts per algo.
- **Drag** nodes, **scroll** to zoom and drag the background to pan. **Double-click** a router to filter the tables to it and jump to the Nodes tab.
- The **text filter** and **SRv6 only** dim routers that don't match.

Controls:

| Control | Effect |
|---|---|
| **Colour → IGP instance** | Edges and node outlines use one colour per instance. An outer ring marks a router in a second instance. |
| **Colour → SRv6 End.X / MSD** | Edges are green with End.X in both directions, amber with End.X in one direction, red with none between two SRv6 nodes, and grey when the link is not SRv6. Node outlines are green with an SRv6 MSD, red for SRv6 without one, and grey when not SRv6. A dotted ring means the router owns a locator. |
| **Colour → Flex-Algo participation** | Pick an algo. Edges are solid when usable both ways, dashed when usable one way, and faded when pruned (see the pruning rules below). Routers outside the algo fade out. Hover an edge to see the FAD and the verdict for each direction. |
| **3D Flex-Algo** | Stack one tilted pane per Flex-Algorithm over the 2D layout (see below). |
| **Show** | Draw one IGP instance only (shown when the capture has several). |
| **Labels / Node size / Curvature** | Display options. In 3D, *Pane spacing* replaces *Curvature*. |
| **Re-layout / Reset view / Fit** | Recompute the layout from scratch, restore the 3D camera, or zoom to fit. |

### 3D Flex-Algo view

Tick **3D Flex-Algo** to stack one pane per algorithm found in SR-ALG above an **Algo 0 (IGP)** baseline pane, the same way the plan-viewer tool does.
- **Layout:** every pane reuses the 2D router positions. Dotted pillars join the copies of the same router across panes.
- **Pane labels** show the FAD metric type, the member routers and how many adjacencies are usable.
- **Pane chips** above the view show or hide individual panes.
- **Link halves:** each half of a link shows the direction leaving its nearer router. It is solid in the pane colour when that direction is usable, and grey and dashed when it is pruned. Routers that don't advertise the algo are drawn as faded ghosts.
- **Navigation:** drag on a pane to rotate and tilt the stack. Drag outside the panes, or Shift-drag, to pan, and scroll to zoom.
- **Hover:** hovering a router highlights its copies, pillars and links on every pane, and the tooltip lists its algos and FADs for each instance (marking the winning FAD). Hovering a link shows the winning FAD with its constraints, the verdict and metric for each direction, and that direction's SRv6 End.X SIDs for the algo.

Pruning rules (RFC 9350), applied per IGP instance and per link direction using the link NLRI advertised by the router the link leaves:

1. **Winning FAD:** the highest priority wins, with the highest system ID as tie-break. Without a FAD, the algo is unusable in that instance.
2. **Participation:** both ends must advertise the algo in SR-ALG in that instance.
3. **Affinities:** Excl-Any, Incl-Any and Incl-All are checked against the admin group (or extended admin group) from the ASLA with the Flex-Algo bit. When the link has no Flex-Algo ASLA, the legacy TE attributes are used instead, and the tooltip says which source was used.
4. **Other constraints:** Excl-Min-BW (against max link bandwidth), Excl-Max-Delay (against min delay) and Excl-SRLG.
5. **Metric:** the FAD metric type (IGP, min delay or TE) must be advertised on the link. Otherwise that direction is pruned.

Links in summary-only captures are marked usable but unchecked, because they carry no attributes.

---

## 9. Filtering Results

Each tab shows one NLRI type. The toolbar filters apply to the active tab and stay set when you switch tabs:

| Control | Location | Usage |
|---------|----------|-------|
| **Text filter** | Text input field | Type any string (IP address, color value, headend, etc.). Matches against all visible fields in a row. Case-insensitive. Also filters SRv6 Health findings and dims routers in the Topology tab. |
| **SRv6 only** | Checkbox | Show only SRv6-related entries. |
| **Origin dropdown** | "All Origins" select (SR Policies tab only) | Restrict to **PCEP**, **BGP**, or **Config** originated SR Policies. |

Filters apply **instantly** as you type or change a dropdown — no need to click Parse again. The table badge shows `visible / total`.

### Example: Find all Config-originated policies for endpoint 66.66.66.66

1. Open the **SR Policies** tab.
2. Type `66.66.66.66` in the text filter.
3. Select **Config** from the Origin dropdown.

Only matching rows remain visible.

---

## 10. Sorting Columns

Click any **column header** to sort by that column (an arrow marks the sorted column):

- **First click** → ascending order (A→Z, 0→9).
- **Second click** → descending order (Z→A, 9→0).

Numeric columns (Instance, Color, ASN, Discriminator, Local Pref, MED, NLRI Size) sort numerically. All other columns sort alphabetically.

Sorting and filtering work together — the current filter is re-applied after sorting.

---

## 11. Understanding NLRI Field Codes

The raw BGP-LS NLRI lines use bracket-encoded fields. Here is a quick reference of every code the parser recognises:

| Code | Full Name | Context |
|------|-----------|---------|
| `[T]` | Topology | Identifies an IS-IS Prefix NLRI. |
| `[V]` / `[E]` | Node / Link | Node and Link NLRIs. |
| `[S]` | SRv6 SID | Identifies an SRv6 SID NLRI; the `S` block contains `t` (multi-topology) and `sd` (SID). |
| `[SP]` | SR Policy | Identifies an SR Policy NLRI. |
| `[SR]` | Segment Routing | Marks the policy as SR-based. |
| `[L1]` / `[L2]` | IS-IS Level 1 / Level 2 | IS-IS level for prefix NLRIs. |
| `[I...]` | Instance ID | IS-IS instance identifier (hex). |
| `[N[...]]` | Node Descriptor | Contains `c` (ASN), `b` (BGP Router ID), `s` (System ID), `te` (headend), `q` (originator node). |
| `[P[...]]` | Prefix Descriptor | Contains `p` (prefix). |
| `[C[...]]` | Candidate Path Descriptor | Contains `po` (protocol origin), `f` (flags), `e` (endpoint), `cl` (color), `as` (AS), `oa` (originator address), `di` (discriminator). |

### Protocol Origin Codes

| Hex | Decoded |
|-----|---------|
| `0x1` | PCEP |
| `0x2` | BGP |
| `0x3` | Config |

### Endpoint Flags

| Flag | Meaning |
|------|---------|
| `0x0` | IPv4 endpoint |
| `0x80` | IPv6 endpoint |

---

## 12. Keyboard Shortcut

| Shortcut | Action |
|----------|--------|
| `Ctrl+Enter` (Windows/Linux) or `Cmd+Enter` (macOS) | Parse the content of the paste text area. |

---

## 13. Summary & Next Steps

In this tutorial you learned how to:

- ✅ Open the BGP-LS Parser in a browser.
- ✅ Drag and drop or paste your own `show lslib` / `show bgp link-state link-state` output.
- ✅ Read the Overview tab for a quick summary.
- ✅ Interpret every column in the per-NLRI tabs.
- ✅ Explore the topology, coloured by IGP instance, SRv6 End.X / MSD or Flex-Algo.
- ✅ Use the text, SRv6 and origin filters to narrow results.
- ✅ Sort columns to find specific entries.
- ✅ Decode raw NLRI bracket field codes.

### Tips for Daily Use

- **Bookmark the file** in your browser for quick access.
- **Pipe filtered output** from the router (e.g., `| include <endpoint>`) to reduce noise before pasting.
- **Combine filters** — use the text box for IP addresses while restricting by Origin to quickly isolate PCEP- vs. Config-originated policies.
- The tool runs **entirely offline** — no data leaves your browser.
