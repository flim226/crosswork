"""Append Circuit IDs from a CSV mapping into interface descriptions.

Used as a Crosswork Planning external script. The Collector framework
passes positional arguments: base planfile path, output planfile path,
and possibly extra args (ignored).

The mapping CSV is read from circuitid_mappings.csv, looked up next to this
script, beside the source plan file, in the current working directory, then
in the Collector user-uploaded external files directory (argv[6]).

CSV columns (header required):
    Circuit ID, NodeA, InterfaceA, NodeB, InterfaceB

For each mapping row, both circuit endpoints are updated:

    existing description  ->  existing description (Circuit ID: <id>)

Matching is orientation-independent: NodeA/InterfaceA vs NodeB/InterfaceB
in the plan may be swapped relative to the CSV.

For reference please see:
https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/setup-guide/cisco-crosswork-planning-7-2-collection-setup-and-administration/m-collectors-in-cp.html#run-external-scripts
"""

import csv
import os
import re
import subprocess
import sys

from com.cisco.wae.opm.network import Network

DEFAULT_CSV_NAME = "circuitid_mappings.csv"
CIRCUIT_ID_PATTERN = re.compile(r"\s*\(Circuit ID:\s*.*?\)\s*")


def _norm(value):
    if value is None:
        return ""
    return str(value).strip()


def _endpoint_key(node, interface):
    return (_norm(node), _norm(interface))


def _circuit_key(node_a, iface_a, node_b, iface_b):
    """Order-independent identity of a two-ended circuit."""
    left = _endpoint_key(node_a, iface_a)
    right = _endpoint_key(node_b, iface_b)
    if left <= right:
        return (left, right)
    return (right, left)


def with_circuit_id(description, circuit_id):
    """Return description with a single (Circuit ID: ...) suffix."""
    text = _norm(description)
    text = CIRCUIT_ID_PATTERN.sub(" ", text).strip()
    suffix = "(Circuit ID: {})".format(circuit_id)
    if not text:
        return suffix
    return "{} {}".format(text, suffix)


def find_mapping_file(src, user_upload_dir=None):
    """Locate the circuit-ID CSV next to the script, plan, cwd, then uploads."""
    search_dirs = []
    for path in (
        os.path.dirname(os.path.abspath(__file__)),
        os.path.dirname(os.path.abspath(src)),
        os.getcwd(),
        user_upload_dir,
    ):
        if path and os.path.isdir(path) and path not in search_dirs:
            search_dirs.append(path)

    for directory in search_dirs:
        full = os.path.join(directory, DEFAULT_CSV_NAME)
        if os.path.isfile(full):
            return full

    searched = ", ".join(search_dirs) or "(no search directories)"
    raise IOError(
        "Circuit ID mapping file not found. Looked for {} under {}".format(
            DEFAULT_CSV_NAME, searched
        )
    )


def load_circuit_mappings(csv_path):
    """Load CSV rows into endpoint and circuit lookups.

    Returns:
        endpoint_to_cid: (node, interface) -> circuit_id (both ends)
        circuit_to_cid: ordered-independent circuit key -> circuit_id
        rows: list of parsed row dicts
    """
    endpoint_to_cid = {}
    circuit_to_cid = {}
    rows = []

    with open(csv_path, "r") as handle:
        reader = csv.DictReader(handle)
        required = ("Circuit ID", "NodeA", "InterfaceA", "NodeB", "InterfaceB")
        if reader.fieldnames is None:
            raise ValueError("CSV file is empty: {}".format(csv_path))
        missing = [col for col in required if col not in reader.fieldnames]
        if missing:
            raise ValueError(
                "CSV {} is missing required columns: {}".format(csv_path, ", ".join(missing))
            )

        for lineno, raw in enumerate(reader, start=2):
            circuit_id = _norm(raw.get("Circuit ID"))
            node_a = _norm(raw.get("NodeA"))
            iface_a = _norm(raw.get("InterfaceA"))
            node_b = _norm(raw.get("NodeB"))
            iface_b = _norm(raw.get("InterfaceB"))
            if not circuit_id or not node_a or not iface_a or not node_b or not iface_b:
                print("Skipping incomplete CSV row {}: {}".format(lineno, raw))
                continue

            row = {
                "circuit_id": circuit_id,
                "node_a": node_a,
                "iface_a": iface_a,
                "node_b": node_b,
                "iface_b": iface_b,
                "line": lineno,
            }
            rows.append(row)

            ckey = _circuit_key(node_a, iface_a, node_b, iface_b)
            if ckey in circuit_to_cid and circuit_to_cid[ckey] != circuit_id:
                print(
                    "WARNING: circuit {} already mapped to {}; CSV line {} uses {}".format(
                        ckey, circuit_to_cid[ckey], lineno, circuit_id
                    )
                )
            circuit_to_cid[ckey] = circuit_id

            for endpoint in (_endpoint_key(node_a, iface_a), _endpoint_key(node_b, iface_b)):
                existing = endpoint_to_cid.get(endpoint)
                if existing and existing != circuit_id:
                    print(
                        "WARNING: endpoint {} already mapped to {}; CSV line {} uses {}".format(
                            endpoint, existing, lineno, circuit_id
                        )
                    )
                endpoint_to_cid[endpoint] = circuit_id

    print("Loaded {} circuit mappings ({} endpoints) from {}".format(
        len(rows), len(endpoint_to_cid), csv_path
    ))
    return endpoint_to_cid, circuit_to_cid, rows


def _set_description(interface, circuit_id, node_name, iface_name):
    current = getattr(interface, "description", None)
    updated = with_circuit_id(current, circuit_id)
    if _norm(current) == updated:
        print("Unchanged {} {}: {}".format(node_name, iface_name, updated))
        return False
    interface.description = updated
    print("Updated {} {}: '{}' -> '{}'".format(node_name, iface_name, current or "", updated))
    return True


def _lookup_interface(network, node_name, iface_name):
    """Resolve an interface by node name and interface name."""
    try:
        node = network.model.nodes[node_name]
    except (KeyError, TypeError, AttributeError):
        node = None
        for candidate in network.model.nodes:
            if _norm(candidate.name) == node_name:
                node = candidate
                break
    if node is None:
        return None
    try:
        return node.interfaces[iface_name]
    except (KeyError, TypeError, AttributeError):
        for interface in node.interfaces:
            if _norm(interface.name) == iface_name:
                return interface
    return None


def check_source_plan(src):
    """Fail early with a readable message when the source plan is unusable."""
    if not os.path.exists(src):
        raise IOError("Source plan file does not exist: {}".format(src))
    if not os.path.isfile(src):
        raise IOError("Source plan path is not a file: {}".format(src))

    size = os.path.getsize(src)
    print("Source plan: {} ({} bytes)".format(src, size))
    if size == 0:
        raise IOError(
            "Source plan file is empty: {}. The upstream collector produced no "
            "plan; check that the source step completed successfully.".format(src)
        )
    if not os.access(src, os.R_OK):
        raise IOError("Source plan file is not readable: {}".format(src))


def open_source_network(src):
    """Open the source plan, converting .db to .pln if direct import fails."""
    try:
        return Network(src), None
    except Exception as first_error:
        print("Direct plan import failed: {}".format(first_error))
        converted = "{}.converted.pln".format(os.path.splitext(os.path.basename(src))[0])
        print("Retrying via mate_convert: {} -> {}".format(src, converted))
        try:
            subprocess.check_call(
                ["mate_convert", "-plan-file", src, "-out-file", converted]
            )
        except (OSError, subprocess.CalledProcessError) as convert_error:
            raise RuntimeError(
                "Could not import source plan '{}'. Direct import error: {}. "
                "mate_convert fallback error: {}".format(src, first_error, convert_error)
            )
        return Network(converted), converted


def write_network(network, dest):
    """Write the model to dest, falling back to mate_convert for the format."""
    try:
        network.write(dest)
        return
    except Exception as first_error:
        print("Direct plan write failed: {}".format(first_error))
        staged = "{}.staged.pln".format(os.path.splitext(os.path.basename(dest))[0])
        print("Retrying via mate_convert: {} -> {}".format(staged, dest))
        network.write(staged)
        try:
            subprocess.check_call(
                ["mate_convert", "-plan-file", staged, "-out-file", dest]
            )
        finally:
            try:
                os.remove(staged)
            except OSError:
                pass


def apply_circuit_ids(src, dest, user_upload_dir=None):
    """Read a planfile, append Circuit IDs to matching interfaces, write dest."""
    csv_path = find_mapping_file(src, user_upload_dir=user_upload_dir)
    print("Using Circuit ID mapping file: {}".format(csv_path))
    _endpoint_to_cid, circuit_to_cid, rows = load_circuit_mappings(csv_path)
    check_source_plan(src)
    network, converted = open_source_network(src)
    update_count = 0
    matched_circuits = set()
    unmatched_rows = []

    # Prefer circuit objects so both sides of a discovered circuit are updated
    # together, regardless of A/B orientation in the plan vs the CSV.
    for circuit in network.model.circuits:
        iface_a = circuit.interface_a
        iface_b = circuit.interface_b
        node_a = iface_a.node.name
        node_b = iface_b.node.name
        name_a = iface_a.name
        name_b = iface_b.name
        ckey = _circuit_key(node_a, name_a, node_b, name_b)
        circuit_id = circuit_to_cid.get(ckey)
        if not circuit_id:
            continue
        matched_circuits.add(ckey)
        if _set_description(iface_a, circuit_id, node_a, name_a):
            update_count += 1
        if _set_description(iface_b, circuit_id, node_b, name_b):
            update_count += 1

    # Fallback: CSV endpoints that did not match a plan circuit (or whose
    # circuit object was missing) are still applied directly on interfaces.
    for row in rows:
        ckey = _circuit_key(row["node_a"], row["iface_a"], row["node_b"], row["iface_b"])
        if ckey in matched_circuits:
            continue
        sid = row["circuit_id"]
        updated_any = False
        for node_name, iface_name in (
            (row["node_a"], row["iface_a"]),
            (row["node_b"], row["iface_b"]),
        ):
            interface = _lookup_interface(network, node_name, iface_name)
            if interface is None:
                print(
                    "No interface found for {} {} (Circuit ID: {})".format(
                        node_name, iface_name, sid
                    )
                )
                continue
            if _set_description(interface, sid, node_name, iface_name):
                update_count += 1
            updated_any = True
        if not updated_any:
            unmatched_rows.append(row)

    for row in unmatched_rows:
        print(
            "Unmatched CSV mapping line {}: {} {} <-> {} {} (Circuit ID: {})".format(
                row["line"],
                row["node_a"],
                row["iface_a"],
                row["node_b"],
                row["iface_b"],
                row["circuit_id"],
            )
        )

    write_network(network, dest)
    if converted:
        try:
            os.remove(converted)
        except OSError:
            pass

    print("Matched {} of {} CSV circuits against the plan".format(
        len(matched_circuits), len(circuit_to_cid)
    ))
    print("Total interface descriptions updated: {}".format(update_count))
    if unmatched_rows:
        print("Unmatched CSV mappings: {}".format(len(unmatched_rows)))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: {} <base_planfile> <output_planfile> [extra args...]".format(sys.argv[0]))
        sys.exit(1)

    base_planfile = sys.argv[1]
    output_planfile = sys.argv[2]
    # Collector argv[6] is the user-uploaded external files directory, used as
    # the last place to look for circuitid_mappings.csv. Other extra args ignored.
    user_upload_dir = sys.argv[6] if len(sys.argv) > 6 else None

    apply_circuit_ids(base_planfile, output_planfile, user_upload_dir=user_upload_dir)
