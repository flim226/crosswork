# append_circuit_id.py

Appends Circuit IDs from a CSV mapping file to interface descriptions in a Crosswork Planning plan file, using the **Crosswork Planning OPM Python Library**.

## Overview

Inventory or OSS systems often store a Circuit ID for each physical circuit, but that identifier is not present on interface descriptions in the collected network model. Downstream planning, reporting, and troubleshooting workflows that rely on the plan file then cannot correlate an interface with the circuit that owns it.

This script reads a mapping CSV (`circuitid_mappings.csv`) and, for each mapped circuit, appends a `(Circuit ID: <id>)` suffix to both endpoint interface descriptions. Matching is orientation-independent: `NodeA`/`InterfaceA` versus `NodeB`/`InterfaceB` in the plan may be swapped relative to the CSV. If a Circuit ID suffix is already present, it is replaced so the description never accumulates duplicate Circuit ID tags.

The script is designed to run as a **Crosswork Planning external script** within the Collector framework. It can be attached to any collection chain to post-process the network model automatically on every scheduled collection run.

## Requirements

- Python 3.10+
- Crosswork Planning
- A Circuit ID mapping CSV named `circuitid_mappings.csv` (see [Mapping File](#mapping-file))

## Mapping File

The mapping file must be named `circuitid_mappings.csv` and include a header row with these columns:

| Column | Description |
|--------|-------------|
| `Circuit ID` | Identifier to append to both endpoint descriptions |
| `NodeA` | Name of the first endpoint node |
| `InterfaceA` | Interface name on `NodeA` |
| `NodeB` | Name of the second endpoint node |
| `InterfaceB` | Interface name on `NodeB` |

Example:

```
Circuit ID,NodeA,InterfaceA,NodeB,InterfaceB
SR/IPIG/483921/CNC,node-4,GigabitEthernet0/0/0/8,node-8,GigabitEthernet0/0/0/4
```

Node and interface names are matched after stripping leading and trailing whitespace. Incomplete rows (any required field empty) are skipped and logged.

The script searches for the CSV in this order and uses the first match:

1. The directory that contains `append_circuit_id.py`
2. The directory that contains the source plan file
3. The current working directory
4. The Collector user-uploaded external files directory (`argv[6]`), if provided

If the file is not found, the script exits with an error listing the directories it searched.

When deploying as an external script, package `append_circuit_id.py` and `circuitid_mappings.csv` in a `.zip` archive so the mapping file is extracted next to the script. You can also place `circuitid_mappings.csv` among the user-uploaded external files; that location is used only if the CSV is not found in the earlier search paths.

## How It Works

1. **Locate Mapping File** — Finds `circuitid_mappings.csv` as described above.
2. **Load Mappings** — Parses each CSV row into endpoint and circuit lookups. Circuit identity is order-independent, so A/B orientation in the CSV does not have to match the plan.
3. **Validate Source Plan** — Confirms the source plan exists, is a readable non-empty file.
4. **Open Source Plan** — Uses `com.cisco.wae.opm.network.Network(src)` to load the source plan. If direct import fails, retries via `mate_convert` to a temporary `.pln` file.
5. **Match Circuits** — Walks `network.model.circuits` and, for each circuit whose two endpoints match a CSV row (in either orientation), updates both interface descriptions.
6. **Fallback Interface Lookup** — CSV rows that did not match a plan circuit object are applied directly on the named interfaces, if those interfaces exist.
7. **Rewrite Descriptions** — For each matched interface, any existing `(Circuit ID: ...)` suffix is removed and replaced with the mapped Circuit ID. An empty description becomes just the Circuit ID suffix.
8. **Write Output** — Saves the modified network model to the destination plan file. If a direct write fails, the script stages a `.pln` file and converts it with `mate_convert`.

Each change is logged to stdout for auditability:

```
Updated node-4 GigabitEthernet0/0/0/8: 'to node-8' -> 'to node-8 (Circuit ID: SR/IPIG/483921/CNC)'
Updated node-8 GigabitEthernet0/0/0/4: '' -> '(Circuit ID: SR/IPIG/483921/CNC)'
Unchanged node-1 GigabitEthernet0/0/0/6: to node-6 (Circuit ID: SR/IPIG/716304/CNC)
```

Unmatched CSV rows and missing interfaces are also logged:

```
No interface found for node-9 GigabitEthernet0/0/0/1 (Circuit ID: SR/IPIG/000000/CNC)
Unmatched CSV mapping line 12: node-9 GigabitEthernet0/0/0/1 <-> node-10 GigabitEthernet0/0/0/2 (Circuit ID: SR/IPIG/000000/CNC)
```

## Deploying as a Crosswork Planning External Script

This script is intended to be deployed as an external script within a Crosswork Planning collection chain. The Collector framework automatically provides the source plan file and output plan file as command-line arguments (`argv[1]` and `argv[2]`). Additional arguments (`argv[3]` through `argv[7]`) are ignored except `argv[6]`, which is searched last for `circuitid_mappings.csv`.

For full details on the external script framework, see [Run an external script against a network model](https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/setup-guide/cisco-crosswork-planning-7-2-collection-setup-and-administration/m-collectors-in-cp.html#run-external-scripts) in the *Cisco Crosswork Planning 7.2 Collection Setup and Administration Guide*.

### Collector Framework Arguments

When invoked by the Collector framework, the script receives these positional arguments:

| Argument | Description |
|----------|-------------|
| `argv[1]` | Source plan file |
| `argv[2]` | Output plan file |
| `argv[3]` | Device access authentication file *(ignored)* |
| `argv[4]` | Global network access configuration file *(ignored)* |
| `argv[5]` | Home directory *(ignored)* |
| `argv[6]` | Path to user-uploaded external files *(last-choice search directory for `circuitid_mappings.csv`)* |
| `argv[7]` | Path to archive root directory *(ignored)* |

### Step-by-Step Deployment

1. **Create or edit a collection** — In the Crosswork Planning UI, navigate to your collection configuration.

2. **Add an external script** — On the Configure page, click **+ Add external script** under the **Basic topology**, **Advanced modeling**, or **Traffic and Demands** section (depending on where in the chain you want this script to run).

3. **Configure the script parameters:**

   | Option | Value |
   |--------|-------|
   | **Collector name** | A descriptive name, e.g. `Append Circuit IDs` |
   | **Is source a plan file?** | Leave unchecked (the source comes from an upstream collector) |
   | **Source** | Select the upstream collector whose output should be processed (e.g., the SR-PCE collector or an aggregator) |
   | **Input file** | Upload a `.zip` archive containing `append_circuit_id.py` and `circuitid_mappings.csv` |
   | **Executable script** | `append_circuit_id.py` |
   | **Script language** | Python |
   | **Timeout** | 30 minutes (default) or adjust as needed |

4. **Preview and create** — Click **Next**, review the configuration, and click **Create**.

5. **Schedule the collection** — Configure the collection schedule to run immediately or at specific intervals.

### Typical Collection Chain Placement

This script is most useful when placed **after topology collection** (or after aggregation), so that nodes, interfaces, and circuits are already present in the model. A typical chain might look like:

```
SR-PCE Collector → PCEP LSP Collector → Append Circuit IDs (this script) → DARE Aggregation
```

Or if using the IGP database collector:

```
IGP Database Collector → LSP Collector → Append Circuit IDs (this script) → DARE Aggregation
```

## API Reference

This script uses the following components from the Crosswork Planning OPM Python Library (see [API documentation](https://developer.cisco.com/docs/crosswork/planning/)):

| OPM Class / Method | Purpose |
|---------------------|---------|
| `com.cisco.wae.opm.network.Network(plan_file)` | Opens and loads a plan file into an OPM Network object |
| `network.model.circuits` | Iterable collection of circuit objects in the network model |
| `circuit.interface_a` / `circuit.interface_b` | Endpoint interfaces of a circuit |
| `interface.node` | Node that owns the interface |
| `interface.name` | Interface name used for CSV matching |
| `interface.description` | Read/write property for the interface description |
| `network.model.nodes` | Iterable (and name-indexable) collection of nodes |
| `node.interfaces` | Iterable (and name-indexable) collection of interfaces on a node |
| `network.write(dest_file)` | Writes the modified network model to a plan file |

Plan import and export fall back to the `mate_convert` CLI (`-plan-file`, `-out-file`) when the OPM `Network` constructor or `write` call cannot handle the file format directly.

## Notes

- Descriptions are rewritten to contain a single `(Circuit ID: <id>)` suffix. Re-running the script with an updated CSV replaces the previous Circuit ID rather than appending another one.
- Circuit matching ignores A/B orientation. A CSV row for `node-4`/`GigabitEthernet0/0/0/8` ↔ `node-8`/`GigabitEthernet0/0/0/4` matches the same circuit even if the plan stores the endpoints in the opposite order.
- If the same endpoint or circuit appears in the CSV with conflicting Circuit IDs, the last row wins and a warning is printed.
- CSV rows that match neither a plan circuit nor either named interface are reported as unmatched; they do not fail the script.
- When running inside the Collector framework, stdout output (update, skip, and unmatched messages) is captured in the collection logs accessible via the Crosswork Planning UI under **Administration > Show Tech**.
- If migrating this script from Cisco WAE, verify that file path references are compatible with the Crosswork Planning architecture, as noted in the [external scripts documentation](https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/setup-guide/cisco-crosswork-planning-7-2-collection-setup-and-administration/m-collectors-in-cp.html#run-external-scripts).
