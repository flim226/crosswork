# BGP-LS Parser — Getting Started Tutorial

> **Document type:** Tutorial (Diátaxis: learning-oriented)
> **Audience:** Network engineers familiar with BGP and Segment Routing, new to this tool.

---

## 1. Introduction

The **BGP-LS Parser** is a standalone, browser-based tool that parses raw output from the IOS-XR command `show bgp link-state link-state` and presents it in a structured, sortable, and filterable dashboard.

It extracts and decodes two types of Network Layer Reachability Information (NLRI):

- **IS-IS Prefix NLRIs** — topology prefix advertisements (`[T]` entries).
- **SR Policy NLRIs** — Segment Routing policy candidate paths (`[SP]` entries).

By the end of this tutorial you will be able to paste router output into the parser, understand every column in the resulting tables, and use filters and sorting to find the data you need.

---

## 2. Prerequisites

- A modern web browser (Chrome, Firefox, Safari, or Edge).
- Access to the `bgp-ls-parser.html` file.
- Optionally, raw output from `show bgp link-state link-state` collected from an IOS-XR router (e.g., an SR-PCE).

No installation, server, or internet connection is required — the tool runs entirely in the browser.

---

## 3. Opening the Tool

Open `bgp-ls-parser.html` by double-clicking the file or dragging it into a browser window. You will see:

- A **title bar** ("🔍 BGP-LS Parser").
- A **text area** for pasting BGP-LS output.
- A **toolbar** with *Parse*, *Load Sample*, and *Clear* buttons, plus filter controls.
- An **empty-state prompt** asking you to paste data.

---

## 4. Your First Parse: Loading Sample Data

Click the **Load Sample** button in the toolbar. The text area populates with example `show bgp link-state link-state` output filtered for endpoint `66.66.66.66`, and the parser runs automatically.

You should now see:

1. **Stat cards** across the top of the page.
2. An **IS-IS Prefix NLRIs** table (if prefix entries exist in the sample).
3. An **SR Policy NLRIs** table listing parsed candidate paths.

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

2. Copy the output from the terminal.

3. In the BGP-LS Parser, **click inside the text area** and paste (`Ctrl+V` / `Cmd+V`).

4. Click **Parse** (or press `Ctrl+Enter` / `Cmd+Enter`).

The stat cards and tables update to reflect your data.

---

## 9. Filtering Results

The toolbar provides three filter controls that work in combination:

| Control | Location | Usage |
|---------|----------|-------|
| **Text filter** | Text input field | Type any string (IP address, color value, headend, etc.). Matches against all visible fields in a row. Case-insensitive. |
| **Type dropdown** | "All Types" select | Restrict to **Prefix** or **SR Policy** entries only. |
| **Origin dropdown** | "All Origins" select | Restrict to **PCEP**, **BGP**, or **Config** originated SR Policies. |

Filters apply **instantly** as you type or change a dropdown — no need to click Parse again.

### Example: Find all Config-originated policies for endpoint 66.66.66.66

1. Type `66.66.66.66` in the text filter.
2. Select **SR Policy** from the Type dropdown.
3. Select **Config** from the Origin dropdown.

Only matching rows remain visible.

---

## 10. Sorting Columns

Click any **column header** in either table to sort by that column:

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
| `Ctrl+Enter` (Windows/Linux) or `Cmd+Enter` (macOS) | Parse the current text area content. |

---

## 13. Summary & Next Steps

In this tutorial you learned how to:

- ✅ Open the BGP-LS Parser in a browser.
- ✅ Load sample data and parse your own `show bgp link-state link-state` output.
- ✅ Read the stat card dashboard for a quick summary.
- ✅ Interpret every column in the IS-IS Prefix and SR Policy tables.
- ✅ Use text filters, type filters, and origin filters to narrow results.
- ✅ Sort columns to find specific entries.
- ✅ Decode raw NLRI bracket field codes.

### Tips for Daily Use

- **Bookmark the file** in your browser for quick access.
- **Pipe filtered output** from the router (e.g., `| include <endpoint>`) to reduce noise before pasting.
- **Combine filters** — use the text box for IP addresses while restricting by Origin to quickly isolate PCEP- vs. Config-originated policies.
- The tool runs **entirely offline** — no data leaves your browser.
