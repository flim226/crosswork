#!/usr/bin/env python3
"""
Crosswork Path Analytics - ECMP Path Retrieval Script
Retrieves ECMP paths using the Path Analytics gRPC API via grpcurl.

Requires: grpcurl (https://github.com/fullstorydev/grpcurl)
          pa.protoset (Path Analytics protobuf descriptor set)
"""

import argparse
import base64
import getpass
import ipaddress
import json
import os
import struct
import subprocess
import sys

import graphviz
import requests
import urllib3

BASE_PORT = 30603
CONNECT_TIMEOUT = 20
ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"
PROTOSET_FILE = "pa.protoset"


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or API calls fail."""


class _TimeoutAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that applies a default timeout to all requests."""

    def send(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = CONNECT_TIMEOUT
        return super().send(*args, **kwargs)


def _create_session(verify_ssl: bool = True) -> requests.Session:
    """Create an HTTP session with default timeout and configurable SSL verification."""
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.verify = verify_ssl
    adapter = _TimeoutAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def check_response(resp: requests.Response, context: str) -> None:
    """Raise CrossworkAuthError with useful context when an HTTP response fails."""
    if resp.ok:
        return
    body = (resp.text or "").strip()
    if len(body) > 500:
        body = body[:500] + "..."
    if not body:
        body = "<empty response body>"
    reason = getattr(resp, "reason", "")
    status = f"HTTP {resp.status_code}"
    if reason:
        status = f"{status} {reason}"
    raise CrossworkAuthError(f"{context} returned {status}: {body}")


def get_ticket(session: requests.Session, base_url: str, username: str, password: str) -> str:
    """POST credentials to Crosswork SSO and return a ticket-granting ticket."""
    url = f"{base_url}/crosswork/sso/v1/tickets"
    resp = session.post(
        url,
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    check_response(resp, "get_ticket")

    location = resp.headers.get("Location", "")
    if location:
        ticket = location.rstrip("/").split("/")[-1]
    else:
        try:
            ticket = resp.json().get("ticket", "")
        except ValueError:
            ticket = resp.text.strip()

    if not ticket:
        raise CrossworkAuthError(f"Could not extract ticket from response: {resp.text[:300]}")
    return ticket


def get_token(session: requests.Session, base_url: str, ticket: str) -> str:
    """Exchange a Crosswork SSO ticket for a JWT bearer token via SSO v2."""
    url = f"{base_url}/crosswork/sso/v2/tickets/jwt"
    resp = session.post(
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"tgt": ticket, "service": f"{base_url}/app-dashboard"},
    )
    check_response(resp, "get_token")

    try:
        token = resp.json().get("token", "")
    except ValueError:
        token = resp.text.strip()

    if not token:
        raise CrossworkAuthError(f"Could not extract token from response: {resp.text[:300]}")
    return token


def get_jwt(ip: str, username: str, password: str, verify_ssl: bool = True, port: int = BASE_PORT) -> str:
    """Authenticate to Crosswork and return the JWT token string."""
    session = _create_session(verify_ssl=verify_ssl)
    base_url = f"https://{ip}:{port}"
    ticket = get_ticket(session, base_url, username, password)
    return get_token(session, base_url, ticket)


def _resolve_credentials(username=None, password=None) -> tuple:
    """Resolve username and password from args, environment, or interactive prompt."""
    username = username or os.environ.get(ENV_USERNAME)
    if not username:
        username = input("Username: ")

    password = password or os.environ.get(ENV_PASSWORD)
    if not password:
        password = getpass.getpass("Password: ")

    return username, password


def load_token_from_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as jwt_file:
        return jwt_file.read().strip()


def default_jwt_path(ip: str) -> str:
    """Return the default JWT path created by cw_get_jwt.py for *ip*."""
    return os.path.join(os.path.expanduser("~/.crosswork"), f"{ip}.jwt")


# ---------------------------------------------------------------------------
# IP address encoding / decoding helpers
# ---------------------------------------------------------------------------

def encode_ip(ip_str: str) -> dict:
    """Encode an IP address for the Path Analytics protobuf message.

    IPv4: encoded as an unsigned 32-bit integer (network byte order).
    IPv6: encoded as base64-encoded 16-byte address.
    """
    addr = ipaddress.ip_address(ip_str)
    if isinstance(addr, ipaddress.IPv4Address):
        return {"v4": struct.unpack("!I", addr.packed)[0]}
    else:
        return {"v6": base64.b64encode(addr.packed).decode("ascii")}


def decode_ip_field(field: dict) -> str:
    """Decode a protobuf IP address oneof field ({v4: int} or {v6: b64})."""
    if "v4" in field:
        return str(ipaddress.IPv4Address(struct.pack("!I", field["v4"])))
    if "v6" in field:
        return str(ipaddress.IPv6Address(base64.b64decode(field["v6"])))
    return str(field)


def decode_b64_ip(b64_str: str) -> str:
    """Decode a base64-encoded IP address, auto-detecting v4 (4B) / v6 (16B)."""
    try:
        raw = base64.b64decode(b64_str)
        if len(raw) == 4:
            return str(ipaddress.IPv4Address(raw))
        if len(raw) == 16:
            return str(ipaddress.IPv6Address(raw))
    except Exception:
        pass
    return b64_str


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_bandwidth(bw_bytes_per_sec: float) -> str:
    """Format bandwidth from bytes/sec to human-readable bits/sec."""
    bps = bw_bytes_per_sec * 8
    if bps >= 1e12:
        return f"{bps / 1e12:.2f} Tbps"
    if bps >= 1e9:
        return f"{bps / 1e9:.2f} Gbps"
    if bps >= 1e6:
        return f"{bps / 1e6:.2f} Mbps"
    if bps >= 1e3:
        return f"{bps / 1e3:.2f} Kbps"
    return f"{bps:.0f} bps"


def format_delay(delay_us) -> str:
    """Format a delay value given in microseconds."""
    if delay_us is None:
        return "N/A"
    if delay_us >= 1_000_000:
        return f"{delay_us / 1_000_000:.2f} s"
    if delay_us >= 1000:
        return f"{delay_us / 1000:.2f} ms"
    return f"{delay_us} µs"


def _link_address(link: dict, kind: str) -> str:
    """Extract and decode an interface or neighbor address from a link."""
    desc = link.get("linkNlri", {}).get("linkDescriptor", {})
    for prefix in ("ipv6", "ipv4"):
        key = f"{prefix}{kind}"
        if key in desc:
            return decode_b64_ip(desc[key])
    return "N/A"


# ---------------------------------------------------------------------------
# Tabular display
# ---------------------------------------------------------------------------

def _print_table(headers: list[str], rows: list[tuple[str, ...]], indent: int = 2):
    """Print a simple fixed-width table with dynamic column widths."""
    widths = [
        max(len(h), *(len(r[i]) for r in rows))
        for i, h in enumerate(headers)
    ]
    pad = " " * indent
    hdr = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    sep = "  ".join("─" * w for w in widths)
    print(f"{pad}{hdr}")
    print(f"{pad}{sep}")
    for row in rows:
        print(f"{pad}{'  '.join(v.ljust(w) for v, w in zip(row, widths))}")


def display_paths(data: dict, source: str, destination: str, color: int):
    """Render path entries as a polished tabular report."""
    entries = data.get("pathEntries", [])

    if not entries:
        print("No paths found.")
        return

    print(f"\n  ECMP Paths: {source} → {destination} │ Color: {color}")
    print(f"  {'═' * 96}")

    for idx, entry in enumerate(entries, 1):
        info = entry.get("info", {})
        path_key = entry.get("pathKey", {})

        src_name = info.get("sourceName", "N/A")
        dst_name = info.get("destinationName", "N/A")
        src_ip = decode_ip_field(path_key.get("source", {}))
        dst_ip = decode_ip_field(path_key.get("destination", {}))
        metric = info.get("metricValue", "N/A")
        num_paths = info.get("numPaths", "N/A")
        timestamp = entry.get("timestamp", "N/A")
        links = info.get("pathLinks", [])

        print(
            f"\n  Path {idx} of {len(entries)} │ "
            f"{src_name} ({src_ip}) → {dst_name} ({dst_ip})"
        )
        print(
            f"  Metric: {metric} │ Hops: {len(links)} │ "
            f"ECMP Width: {num_paths} │ Timestamp: {timestamp}"
        )
        print()

        if links:
            headers = [
                "S/N", "From", "To",
                "Interface Address", "Neighbor Address",
                "Metric", "Delay", "Bandwidth",
            ]
            rows = []
            for hop_num, link in enumerate(links, 1):
                delay = link.get("minUnidirectionalDelay")
                bw = link.get("maxLinkBandwidth")
                rows.append((
                    str(hop_num),
                    link.get("localNodeName", "N/A"),
                    link.get("remoteNodeName", "N/A"),
                    _link_address(link, "InterfaceAddress"),
                    _link_address(link, "NeighborAddress"),
                    str(link.get("igpMetric", "N/A")),
                    format_delay(delay),
                    format_bandwidth(bw) if bw is not None else "N/A",
                ))
            _print_table(headers, rows, indent=4)

        # Path-level summary
        min_d = info.get("minPathPropagationDelay")
        avg_d = info.get("avgPathPropagationDelay")
        max_d = info.get("maxPathPropagationDelay")
        min_c = info.get("minPathCapacity")
        est_c = info.get("estPathCapacity")

        print()
        if min_d is not None:
            print(
                f"    Path Delay: {format_delay(min_d)} (min) / "
                f"{format_delay(avg_d)} (avg) / {format_delay(max_d)} (max)"
            )
        if min_c is not None:
            print(
                f"    Path Capacity: {format_bandwidth(min_c)} (min) / "
                f"{format_bandwidth(est_c)} (est)"
            )

    print()


# ---------------------------------------------------------------------------
# Graphviz topology graph
# ---------------------------------------------------------------------------

def generate_graph(data: dict, source: str, destination: str, color: int, filename: str):
    """Generate a Graphviz topology graph of the ECMP paths.

    Nodes represent routers; edges represent links labelled with metric,
    delay, and bandwidth.  Multiple ECMP paths are overlaid on a single
    graph so shared links appear only once.
    """
    entries = data.get("pathEntries", [])
    if not entries:
        print("No paths to graph.", file=sys.stderr)
        return

    dot = graphviz.Digraph(
        name="ecmp_paths",
        format=os.path.splitext(filename)[1].lstrip(".") or "png",
        graph_attr={
            "rankdir": "LR",
            "label": f"ECMP Paths: {source} → {destination}  (color {color})",
            "labelloc": "t",
            "fontsize": "16",
            "fontname": "Helvetica",
            "bgcolor": "white",
            "pad": "0.5",
        },
        node_attr={
            "shape": "box",
            "style": "rounded,filled",
            "fillcolor": "#E8F0FE",
            "fontname": "Helvetica",
            "fontsize": "11",
        },
        edge_attr={
            "fontname": "Helvetica",
            "fontsize": "9",
            "color": "#4285F4",
            "penwidth": "1.5",
        },
    )

    # Collect unique nodes and edges across all path entries
    nodes: dict[str, str] = {}   # node_name -> label
    edges: dict[tuple[str, str], str] = {}  # (from, to) -> edge label

    for entry in entries:
        info = entry.get("info", {})
        src_name = info.get("sourceName", source)
        dst_name = info.get("destinationName", destination)
        src_ip = decode_ip_field(entry.get("pathKey", {}).get("source", {}))
        dst_ip = decode_ip_field(entry.get("pathKey", {}).get("destination", {}))

        nodes.setdefault(src_name, f"{src_name}\n{src_ip}")
        nodes.setdefault(dst_name, f"{dst_name}\n{dst_ip}")

        for link in info.get("pathLinks", []):
            local = link.get("localNodeName", "?")
            remote = link.get("remoteNodeName", "?")

            nodes.setdefault(local, local)
            nodes.setdefault(remote, remote)

            edge_key = (local, remote)
            if edge_key not in edges:
                intf_addr = _link_address(link, "InterfaceAddress")
                nbr_addr = _link_address(link, "NeighborAddress")
                metric = link.get("igpMetric", "N/A")
                delay = link.get("minUnidirectionalDelay")
                bw = link.get("maxLinkBandwidth")

                parts = [f"metric {metric}"]
                if delay is not None:
                    parts.append(format_delay(delay))
                if bw is not None:
                    parts.append(format_bandwidth(bw))
                parts.append(f"{intf_addr} ↔ {nbr_addr}")

                edges[edge_key] = "\n".join(parts)

    # Highlight source and destination nodes
    for name, label in nodes.items():
        attrs: dict[str, str] = {}
        if name in (
            entries[0].get("info", {}).get("sourceName"),
            source,
        ):
            attrs = {"fillcolor": "#C8E6C9", "penwidth": "2"}
        elif name in (
            entries[0].get("info", {}).get("destinationName"),
            destination,
        ):
            attrs = {"fillcolor": "#FFCDD2", "penwidth": "2"}
        dot.node(name, label=label, **attrs)

    for (src, dst), label in edges.items():
        dot.edge(src, dst, label=label)

    # Render – graphviz appends the format extension automatically
    base, ext = os.path.splitext(filename)
    if not ext:
        ext = ".png"
        base = filename
    dot.format = ext.lstrip(".")
    outpath = dot.render(filename=base, cleanup=True)
    print(f"Graph saved to {outpath}", file=sys.stderr)


# ---------------------------------------------------------------------------
# gRPC call via grpcurl
# ---------------------------------------------------------------------------

def run_grpcurl(
    ip: str,
    token: str,
    source: str,
    destination: str,
    color: int,
    protoset: str,
    port: int,
    raw: bool = False,
    graph: str | None = None,
) -> int:
    """Invoke grpcurl for PathAnalytics/GetPaths.

    In raw mode the full verbose grpcurl output is printed as-is.
    Otherwise the JSON response is parsed and displayed as a table.
    If *graph* is set, a Graphviz topology diagram is rendered to that file.
    """
    request_data = {
        "paths": [
            {
                "source": encode_ip(source),
                "destination": encode_ip(destination),
                "color": color,
            }
        ]
    }

    cmd = [
        "grpcurl",
        "-protoset", protoset,
        "-d", json.dumps(request_data),
        "-insecure",
        "-H", f"Authorization: Bearer {token}",
        f"{ip}:{port}",
        "rca.analytics.PathAnalytics/GetPaths",
    ]
    if raw:
        cmd.insert(1, "-v")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if raw:
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.stdout:
            print(result.stdout, end="")
        return result.returncode

    # Non-raw: parse and display formatted output
    if result.returncode != 0:
        print("grpcurl error:", file=sys.stderr)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        return result.returncode

    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        display_paths(data, source, destination, color)
        if graph:
            generate_graph(data, source, destination, color, graph)
    except json.JSONDecodeError as exc:
        print(f"Error parsing grpcurl response: {exc}", file=sys.stderr)
        if result.stdout:
            print(result.stdout)
        return 1

    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Retrieve ECMP paths from Crosswork Path Analytics API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python get_ecmp_paths.py -s 199.20.53.72 -d 199.20.53.71 -c 131
  python get_ecmp_paths.py -s 199.20.53.72 -d 199.20.53.71 -c 131 --ip 10.56.112.158
  python get_ecmp_paths.py -s 2001:db8::1 -d 2001:db8::2 -c 100
  python get_ecmp_paths.py -s 2001:db8::1 -d 2001:db8::2 -c 100 --raw
  python get_ecmp_paths.py -s 199.20.53.72 -d 199.20.53.71 -c 131 --graph topology.png
  python get_ecmp_paths.py -s 199.20.53.72 -d 199.20.53.71 -c 131 --graph topology.svg
""",
    )

    # Query parameters
    parser.add_argument(
        "--source", "-s", required=True, help="Source IP address (IPv4 or IPv6)"
    )
    parser.add_argument(
        "--destination",
        "-d",
        required=True,
        help="Destination IP address (IPv4 or IPv6)",
    )
    parser.add_argument(
        "--color", "-c", required=True, type=int, help="SR-TE color value"
    )

    # Output mode
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print raw verbose grpcurl output instead of tabular format",
    )
    parser.add_argument(
        "--graph",
        metavar="FILENAME",
        help="Generate a Graphviz topology graph (format from extension, e.g. .png .svg .pdf)",
    )

    # Connection settings
    parser.add_argument(
        "--ip",
        required=True,
        help="Crosswork controller IP address",
    )
    parser.add_argument(
        "--username",
        "-u",
        default=None,
        help=f"Username (or set {ENV_USERNAME})",
    )
    parser.add_argument(
        "--password",
        "-p",
        default=None,
        help=f"Password (or set {ENV_PASSWORD}; will prompt if omitted)",
    )
    parser.add_argument("--jwt", "-j", help="Path to JWT file (skips username/password auth)")
    parser.add_argument("-k", "--insecure", action="store_true",
                        help="Disable SSL certificate verification (not recommended)")
    parser.add_argument(
        "--port",
        type=int,
        default=BASE_PORT,
        help=f"gRPC port (default: {BASE_PORT})",
    )
    parser.add_argument(
        "--protoset",
        default=PROTOSET_FILE,
        help=f"Path to protoset file (default: {PROTOSET_FILE})",
    )

    args = parser.parse_args()

    # Validate IP addresses
    for label, value in [("source", args.source), ("destination", args.destination)]:
        try:
            ipaddress.ip_address(value)
        except ValueError:
            print(f"Error: Invalid {label} IP address: {value}")
            sys.exit(1)

    try:
        verify_ssl = not args.insecure
        if args.insecure:
            print("WARNING: SSL verification disabled", file=sys.stderr)
        stored_jwt_path = default_jwt_path(args.ip)

        if args.jwt:
            token = load_token_from_file(args.jwt)
            print(f"Using JWT from {args.jwt}", file=sys.stderr)
        elif not args.username and not args.password and os.path.isfile(stored_jwt_path):
            token = load_token_from_file(stored_jwt_path)
            print(f"Using JWT from {stored_jwt_path}", file=sys.stderr)
        else:
            print(f"Authenticating to {args.ip}...", file=sys.stderr)
            username, password = _resolve_credentials(args.username, args.password)
            token = get_jwt(args.ip, username, password, verify_ssl=verify_ssl, port=args.port)

        print(
            f"Querying ECMP paths: {args.source} -> {args.destination} (color: {args.color})",
            file=sys.stderr,
        )
        rc = run_grpcurl(
            args.ip,
            token,
            args.source,
            args.destination,
            args.color,
            args.protoset,
            args.port,
            raw=args.raw,
            graph=args.graph,
        )
        sys.exit(rc)

    except (CrossworkAuthError, requests.RequestException) as e:
        print(f"Error: {e}")
        sys.exit(1)
    except FileNotFoundError:
        print(
            "Error: grpcurl not found. Please install grpcurl: "
            "https://github.com/fullstorydev/grpcurl"
        )
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
