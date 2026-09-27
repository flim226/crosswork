# Beginner-friendly code-generation prompt

Copy this prompt into a code-generation model and attach `circuitid_mappings.csv`.

---

## Prompt

Create a complete, easy-to-follow Python script named `append_circuit_id.py` for Cisco Crosswork Planning. Include helpful comments and clear error messages. Return only the script in one code block.

Use the Crosswork OPM library and external-script instructions in these Cisco references:

- https://developer.cisco.com/docs/crosswork/planning/libraries-crosswork-planning-design-rpc-python-library/
- https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/design-guide/cisco-crosswork-planning-design-user-guide-7-2/view-jobs/run-external-scripts.html

Requirements:

- The Collector provides the source and destination paths as `sys.argv[1]` and `sys.argv[2]`; `sys.argv[6]` may contain the user-uploaded files directory. Show usage and exit 1 if either required path is missing.
- Read `circuitid_mappings.csv` at runtime with Python’s `csv` module. Its required, case-sensitive columns are `Circuit ID`, `NodeA`, `InterfaceA`, `NodeB`, and `InterfaceB`. Search beside the script, beside the source plan, in the current directory, then in the optional upload directory. Trim values; report missing/empty files, missing headers, and incomplete rows (with line numbers). Require at least one usable row.
- Match both node/interface endpoints to plan circuits, allowing reversed order. Use OPM circuit objects first, then look up interfaces by node and name if needed. Support keyed or iterable OPM collections. Update existing endpoints where possible; report missing interfaces and fully unmatched rows. Warn on conflicting IDs for a circuit or endpoint and let the last CSV row win.
- Set each applicable description to its existing text plus `(Circuit ID: <id>)`. Replace any old Circuit ID suffix, preserve other text, and avoid duplicate suffixes on reruns. Report unchanged descriptions.
- Validate the source plan (exists, readable, non-empty) and report its size. Open it with `Network(source)` and save with `network.write(destination)`. If import or writing fails, report the error and use `mate_convert` with temporary `.pln` files as a fallback; explain both import errors if both import attempts fail. Clean up temporary files and never intentionally remove the source.
- Print the chosen CSV, loaded mapping/endpoint counts, per-interface changes, missing interfaces, matched circuits, changed-description total, and unmatched-row count when nonzero. Use only the standard library, documented OPM library, and `mate_convert`. Keep code compatible with the Python version bundled with Crosswork and put execution in a `__main__` block.

The target Crosswork environment is authoritative; its OPM library may not be available in normal Python.

---

