# AGENTS.md

Guidance for agents editing the Crosswork Planning external scripts in this
directory. `append_circuit_id.py` is the reference for style and structure.

## Runtime context

- Scripts run inside the Crosswork Planning Collector as external scripts, not
  in a normal Python environment. `com.cisco.wae.opm.network` is only
  available there; do not expect local imports to succeed.
- Positional args: `sys.argv[1]` is the source plan, `sys.argv[2]` is the
  destination plan, `sys.argv[6]` (optional) is the user-uploaded external
  files directory. Ignore other extra args.
- Use only the standard library, the OPM library (`Network`), and the
  `mate_convert` CLI. No third-party dependencies.
- Keep code compatible with the Python bundled with the target Crosswork
  release: use `"{}".format(...)`, not f-strings; no type hints, walrus,
  dataclasses, or `pathlib`.

## File layout

Order sections like `append_circuit_id.py`:

1. Module docstring: one-line summary, how the Collector invokes the script,
   input file names and lookup order, input format (e.g. CSV columns), the
   transformation (`before  ->  after`), and a "For reference please see:"
   list of Cisco doc URLs.
2. Imports: stdlib alphabetically, blank line, then `from com.cisco.wae.opm.network import Network`.
3. Module constants in `UPPER_SNAKE_CASE` (file names, compiled regexes).
4. Small private helpers prefixed with `_` (`_norm`, `_endpoint_key`).
5. Public step functions (`find_mapping_file`, `load_circuit_mappings`,
   `check_source_plan`, `open_source_network`, `write_network`).
6. One orchestration function (`apply_circuit_ids(src, dest, ...)`).
7. `if __name__ == "__main__":` block that checks `len(sys.argv)`, prints
   `Usage: {} <base_planfile> <output_planfile> [extra args...]`, exits 1, and
   otherwise calls the orchestration function.

## Code style

- 4-space indents, `snake_case` names, two blank lines between top-level defs.
- Docstrings: one-line imperative summary in triple double quotes. Add a
  `Returns:` block only when returning multiple values.
- Comments are sparse and explain *why* (e.g. why circuits are matched before
  the per-interface fallback), never restate the code.
- Normalize all external strings with a `_norm()` helper (`None` -> `""`,
  `str(...).strip()`) before comparing.
- Build order-independent keys with sorted tuples instead of checking both
  orientations inline.
- Look up OPM collections by key first, then fall back to iterating and
  matching `.name`, catching `(KeyError, TypeError, AttributeError)`.
- Make edits idempotent: strip any previous suffix (regex) before appending,
  and skip writing when the value is unchanged.

## Errors and logging

- Validate inputs early and raise `IOError`, `ValueError`, or `RuntimeError`
  with a readable message naming the path, file, or columns involved.
- When something is not found, list where you looked.
- On plan import/write failure, print the original error, retry through
  `mate_convert -plan-file <in> -out-file <out>` via `subprocess.check_call`
  with a list argv (never `shell=True`), and include both errors if the
  fallback also fails.
- Remove temporary `.converted.pln` / `.staged.pln` files in `finally` or after
  success, ignoring `OSError`. Never modify or delete the source plan.
- Log with plain `print()`; Collector captures stdout. Print the input file
  used, counts loaded, each change as `Updated <node> <iface>: '<old>' -> '<new>'`,
  unchanged items, warnings prefixed `WARNING:`, and a final summary of
  matched and updated counts. Include CSV line numbers for skipped, conflicting,
  or unmatched rows.

## Security

- Never hardcode credentials, tokens, hostnames with credentials, or customer
  data in scripts. Runtime data (e.g. `circuitid_mappings.csv`) is read at
  runtime, never embedded.
- Do not print secrets or sensitive values in logs.

## Docs

- Keep `README.md` and `generation_prompt.md` / `minimalist_generation_prompt.md`
  in sync when behavior, arguments, file lookup order, or input format changes.
