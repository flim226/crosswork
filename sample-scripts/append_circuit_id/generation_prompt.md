# One-shot generation prompt: `append_circuit_id.py`

Copy the prompt below into the code-generation model and attach `circuitid_mappings.csv` as the input resource. The CSV is runtime data; do not hard-code its current rows into the script.

---

## Prompt

Generate a single, complete Python script named `append_circuit_id.py` for Cisco Crosswork Planning. Return only the contents of the Python file in one code block. Do not return a README, tests, explanation, pseudocode, or additional files.

### Reference material and supported API

Use these Cisco references as the API/runtime authority:

- Crosswork Planning Design RPC / OPM Python library: https://developer.cisco.com/docs/crosswork/planning/libraries-crosswork-planning-design-rpc-python-library/
- Crosswork Planning Design 7.2 guide, “Run external scripts”: https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/design-guide/cisco-crosswork-planning-design-user-guide-7-2/view-jobs/run-external-scripts.html

The script runs inside the Crosswork Planning Python environment, where `from com.cisco.wae.opm.network import Network` is available. Follow the documented external-script contract: `sys.argv[1]` is the source plan path, `sys.argv[2]` is the destination plan path, and `Network(source_path)` opens the model and `network.write(destination_path)` writes it. Do not substitute a generic plan-file parser or introduce third-party dependencies. Additional Collector arguments may be present; use `sys.argv[6]` as an optional user-uploaded external-files directory, and ignore other extra arguments.

### Purpose and data format

Read a file named exactly `circuitid_mappings.csv`. It has a header row with these required, case-sensitive columns:

```csv
Circuit ID,NodeA,InterfaceA,NodeB,InterfaceB
```

Each non-empty data row maps one circuit ID to two node/interface endpoints. Read the CSV at runtime using Python’s standard `csv` module. Strip leading and trailing whitespace from all field values. Skip and log rows where any required value is empty. A missing file, empty CSV, or missing required header must produce a useful error.

Locate the CSV by checking these directories in order, using the first matching file and avoiding duplicate directories:

1. Directory containing this script.
2. Directory containing the source plan.
3. Current working directory.
4. The optional `sys.argv[6]` directory, if supplied and valid.

If the CSV cannot be found, raise a readable error that names the expected filename and directories searched.

### Matching and updates

Use the OPM network model. Prefer matching `network.model.circuits`: each circuit has `interface_a` and `interface_b`; each interface has `name`, `description`, and a parent `node.name`. Match a CSV circuit using both `(node name, interface name)` endpoint pairs, independent of endpoint order. Strip whitespace before comparing names. For each matched circuit, update the descriptions on both endpoint interfaces.

If a CSV row does not match a circuit object, fall back to resolving its two named interfaces through `network.model.nodes` and each node’s `interfaces` collection. Support both keyed collection lookup and iteration by object `.name` so the code works with either mapping-like or iterable OPM collections. Update whichever endpoint interfaces exist; log a missing interface. Log a CSV mapping as unmatched only if neither endpoint interface could be found.

Append this exact suffix to each applicable interface description:

`(Circuit ID: <Circuit ID>)`

Preserve any existing description text. Before appending, remove any existing suffix matching `(Circuit ID: ...)`, then trim surrounding whitespace. This makes reruns idempotent and replaces an old ID instead of accumulating duplicate IDs. If the description is empty after cleanup, set it to the suffix alone. Do not change an interface if its resulting description is already identical; log it as unchanged.

If duplicate CSV rows map the same order-independent circuit to different IDs, or the same endpoint to different IDs, print a warning and let the last row win. Keep enough row information to report the CSV line number for incomplete, conflicting, and unmatched rows.

### Plan-file handling and error recovery

Before importing the plan, check that the source path exists, is a regular file, is readable, and is non-empty; report failures clearly. Print the source path and byte size.

Try `Network(source_path)` first. If direct import fails, log the original error and try the Crosswork `mate_convert` CLI with `-plan-file <source> -out-file <temporary .pln>`, then open the converted file with `Network`. If both methods fail, raise a useful error containing both failures.

Try `network.write(destination_path)` first. If direct write fails, log the error, write a temporary `.pln` file with `network.write`, then convert it to the requested destination with `mate_convert -plan-file <temporary .pln> -out-file <destination>`. Remove the temporary staged file even if conversion fails. Remove the temporary converted input file after successfully writing the destination. Never delete or overwrite the source plan intentionally.

### Logging and command-line behavior

Print useful progress and audit messages: mapping file selected, number of valid mappings/endpoints loaded, plan validation, each updated or unchanged interface, missing interfaces, unmatched mappings, number of matched plan circuits, total descriptions changed, and unmatched mapping count when nonzero. Do not print sensitive data.

If fewer than two positional arguments are supplied, print concise usage showing `<source_plan> <destination_plan> [extra args...]` and exit with status 1. Otherwise run the operation with arguments 1 and 2 and optional upload directory at argument 6. Put executable behavior behind `if __name__ == "__main__":` and organize nontrivial behavior into small, documented helper functions.

Keep the implementation compatible with the Python runtime bundled with the target Crosswork Planning release. Use only the standard library plus the documented Crosswork OPM library and `mate_convert` CLI. Ensure the script is syntactically complete and handles filesystem, CSV, OPM collection, and subprocess errors with actionable messages.

---
