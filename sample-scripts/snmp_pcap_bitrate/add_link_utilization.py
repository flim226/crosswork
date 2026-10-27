#!/usr/bin/env python3
"""Add link bandwidth and percent utilization columns to an interface-counter CSV.

Bandwidth is taken from the "Links" table of a Crosswork topology dump (get_topo.txt) 
from https://github.com/flim226/crosswork/tree/main/sample-scripts/get_topo
for links of the requested Link Type (default ISIS_IPV4_L2). Rows are matched on 
(hostname, interface) against the link's Source Node / Source TP only.

Example:
    python3 add_link_utilization.py get_topo.txt crosswork1.rest-bb1-01.csv
    python3 add_link_utilization.py get_topo.txt crosswork1.rest-bb1-01.csv \
        -o out.csv --matched-only
"""

import argparse
import csv
import re
import sys
from pathlib import Path

UNIT_MULTIPLIER = {
    "bps": 1,
    "kbps": 1e3,
    "mbps": 1e6,
    "gbps": 1e9,
    "tbps": 1e12,
}

LINK_ROW_RE = re.compile(
    r"^\s+(?P<src_node>\S+)\s+(?P<src_tp>\S+)\s+"
    r"(?P<dst_node>\S+)\s+(?P<dst_tp>\S+)\s+"
    r"(?P<layer>L\d)\s+(?P<igp>\S+)\s+(?P<te>\S+)\s+"
    r"(?P<bandwidth>-|[\d.]+\s*[KMGT]?bps)\s+(?P<link_type>\S+)\s*$",
    re.IGNORECASE,
)
NODE_ROW_RE = re.compile(r"^\s{4}(?P<name>\S+)\s+(?:\d{1,3}(?:\.\d{1,3}){3}|N/A)\s")


def parse_bandwidth(text):
    """Convert e.g. '1600.0 Gbps' to bits per second; return None for '-'."""
    m = re.fullmatch(r"([\d.]+)\s*([KMGT]?bps)", text.strip(), re.IGNORECASE)
    if not m:
        return None
    return float(m.group(1)) * UNIT_MULTIPLIER[m.group(2).lower()]


def parse_topology(path, link_type):
    """Return {(source node, source tp): bandwidth_bps} from the topo dump."""
    node_names = []
    links = []
    section = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped.startswith("Nodes ("):
                section = "nodes"
                continue
            if stripped.startswith("Links ("):
                section = "links"
                continue
            if stripped.startswith("Network ") or stripped.startswith("===="):
                section = None
                continue

            if section == "nodes":
                m = NODE_ROW_RE.match(line)
                if m:
                    node_names.append(m.group("name"))
            elif section == "links":
                m = LINK_ROW_RE.match(line)
                if m and m.group("link_type").upper() == link_type.upper():
                    links.append(m.groupdict())

    resolve = make_node_resolver(node_names)
    bandwidth = {}
    for link in links:
        bw = parse_bandwidth(link["bandwidth"])
        if bw is None:
            continue
        bandwidth.setdefault((resolve(link["src_node"]), link["src_tp"]), bw)
    return bandwidth


def make_node_resolver(node_names):
    """Map possibly-truncated node names in the Links table to full node names."""
    full = set(node_names)

    def resolve(name):
        if name in full:
            return name
        candidates = [n for n in node_names if n.startswith(name)]
        return candidates[0] if len(candidates) == 1 else name

    return resolve


def lookup_bandwidth(bandwidth, hostname, interface):
    """Return the Source-side bandwidth (bps) for a hostname/interface, or None."""
    bw = bandwidth.get((hostname, interface))
    if bw is not None:
        return bw
    # Fall back to short hostname, e.g. "rest-b6" vs "rest-b6.ip.example.net".
    short = hostname.split(".")[0]
    for (node, tp), value in bandwidth.items():
        if tp == interface and node.split(".")[0] == short:
            return value
    return None


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Add bandwidth and percent utilization columns to a counter CSV "
        "using link bandwidth from a Crosswork topology file."
    )
    parser.add_argument("topo_file", help="Topology dump, e.g. get_topo.txt")
    parser.add_argument("csv_file", help="Counter CSV, e.g. crosswork1.rest-bb1-01.csv")
    parser.add_argument(
        "-o", "--output",
        help="Output CSV path (default: <csv_file stem>_util.csv)",
    )
    parser.add_argument(
        "--link-type", default="ISIS_IPV4_L2",
        help="Link Type to read bandwidth from (default: %(default)s)",
    )
    parser.add_argument(
        "--rate-column", default="bitrate_bps",
        help="CSV column holding the measured rate in bps (default: %(default)s)",
    )
    parser.add_argument(
        "--matched-only", action="store_true",
        help="Only write rows whose interface matched a link in the topology",
    )
    return parser.parse_args()


def annotate_csv(bandwidth, csv_path, out_path, rate_column, matched_only):
    """Write csv_path to out_path with bandwidth_bps and util_pct columns added.

    Returns (rows_written, rows_matched, {matched (hostname, interface): bps}).
    """
    rows_written = rows_matched = 0
    matched = {}
    with open(csv_path, newline="", encoding="utf-8") as fin, \
            open(out_path, "w", newline="", encoding="utf-8") as fout:
        reader = csv.DictReader(fin)
        if rate_column not in (reader.fieldnames or []):
            sys.exit(f"Column '{rate_column}' not found in {csv_path}")
        writer = csv.DictWriter(
            fout, fieldnames=list(reader.fieldnames) + ["bandwidth_bps", "util_pct"]
        )
        writer.writeheader()

        for row in reader:
            bw = lookup_bandwidth(bandwidth, row["hostname"], row["interface"])
            if bw is None:
                if matched_only:
                    continue
                row["bandwidth_bps"] = ""
                row["util_pct"] = ""
            else:
                rows_matched += 1
                matched[(row["hostname"], row["interface"])] = bw
                row["bandwidth_bps"] = f"{bw:.0f}"
                rate = row.get(rate_column, "").strip()
                row["util_pct"] = f"{float(rate) / bw * 100:.3f}" if rate else ""
            writer.writerow(row)
            rows_written += 1
    return rows_written, rows_matched, matched


def main():
    """Entry point."""
    args = parse_args()
    csv_path = Path(args.csv_file)
    out_path = Path(args.output) if args.output else csv_path.with_name(
        f"{csv_path.stem}_util{csv_path.suffix}"
    )

    bandwidth = parse_topology(args.topo_file, args.link_type)
    if not bandwidth:
        sys.exit(f"No {args.link_type} links with bandwidth found in {args.topo_file}")

    rows_written, rows_matched, matched = annotate_csv(
        bandwidth, csv_path, out_path, args.rate_column, args.matched_only
    )

    print(f"{args.link_type} interfaces in topology: {len(bandwidth)}")
    print(f"Matched interfaces in CSV: {len(matched)}")
    for (host, intf), bw in sorted(matched.items()):
        print(f"  {host} {intf}: {bw / 1e9:g} Gbps")
    print(f"Rows written: {rows_written} (with utilization: {rows_matched}) -> {out_path}")


if __name__ == "__main__":
    main()
