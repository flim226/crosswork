#!/usr/bin/env python3
"""
Crosswork Network Controller - Topology Retrieval Script
Retrieves L3/L2 topology operational data using the ietf-network-state:networks API
"""

import argparse
import getpass
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import requests  # noqa: E402
import urllib3  # noqa: E402

BASE_PORT = 30603
CONNECT_TIMEOUT = 20
TOPOLOGY_TIMEOUT = 120
TOPOLOGY_PATH = "/crosswork/nbi/topology/v3/restconf/data/ietf-network-state:networks"
ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"


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
    """Read a JWT from an explicit file path. An empty file is an error."""
    with open(path, "r", encoding="utf-8") as jwt_file:
        token = jwt_file.read().strip()
    if not token:
        raise CrossworkAuthError(f"JWT file is empty: {path}")
    return token


def read_stored_jwt(path: str):
    """Return the token at *path* when the file exists and is non-empty."""
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as jwt_file:
        token = jwt_file.read().strip()
    return token or None


def default_jwt_path(ip: str) -> str:
    """Return the default JWT path created by cw_get_jwt.py for *ip*."""
    return os.path.join(os.path.expanduser("~/.crosswork"), f"{ip}.jwt")


def get_topology(ip_address: str, token: str, port: int = BASE_PORT, verify_ssl: bool = True,
                 offset: int = None, limit: int = None) -> dict:
    """Retrieve topology networks operational data from Crosswork Network Controller."""
    base_url = f"https://{ip_address}:{port}"
    url = f"{base_url}{TOPOLOGY_PATH}"
    headers = {
        "Accept": "application/yang-data+json, application/json",
        "Authorization": f"Bearer {token}",
    }
    params = {}
    if offset is not None:
        params["offset"] = offset
    if limit is not None:
        params["limit"] = limit

    session = _create_session(verify_ssl=verify_ssl)
    response = session.get(url, headers=headers, params=params, timeout=TOPOLOGY_TIMEOUT)
    check_response(response, "get_topology")
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def _get(obj, name, default=None):
    """Look up a YANG leaf by local name, with or without its module prefix."""
    if not isinstance(obj, dict):
        return default
    if name in obj:
        return obj[name]
    suffix = f":{name}"
    for key, value in obj.items():
        if key.endswith(suffix):
            return value
    return default


def _networks(data: dict) -> list:
    return _get(_get(data, "networks", {}), "network", []) or []


def _network_types(network: dict) -> str:
    """Return a readable list of topology types, e.g. 'L3 (isis, sr-mpls)'."""
    types = _get(network, "network-types", {}) or {}
    labels = []
    l3 = _get(types, "l3-unicast-topology")
    if l3 is not None:
        subtypes = sorted(key.split(":")[-1] for key in (l3 or {}))
        labels.append(f"L3 ({', '.join(subtypes)})" if subtypes else "L3")
    if _get(types, "l2-topology") is not None:
        labels.append("L2")
    return ", ".join(labels) or "N/A"


def _node_attrs(node: dict) -> dict:
    return _get(node, "l3-node-attributes") or _get(node, "l2-node-attributes") or {}


def _node_name(node: dict) -> str:
    return str(_get(_node_attrs(node), "name") or _get(node, "node-id", "N/A"))


def _router_ids(node: dict) -> str:
    rids = _get(_get(node, "l3-node-attributes", {}), "router-id", []) or []
    return ", ".join(str(r) for r in rids) or "N/A"


def _natural_key(text: str) -> list:
    """Sort key that orders 'node-2' before 'node-10'."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text.lower())]


def _sr_entries(node: dict) -> list:
    """Return (algorithm, sid, value-type) tuples for the node's SR-MPLS prefix SIDs."""
    entries = []
    for prefix in _get(_get(node, "l3-node-attributes", {}), "prefix", []) or []:
        for sr in _get(prefix, "sr-mpls", []) or []:
            sid = _get(sr, "start-sid")
            if sid is None:
                continue
            algo = _get(sr, "algorithm-value", 0)
            entries.append((int(algo), sid, _get(sr, "value-type", "index")))
    return sorted(set(entries), key=lambda e: (e[0], str(e[1])))


def _prefix_sid(node: dict) -> str:
    """Return the algorithm 0 (SPF) prefix SID, e.g. 'idx 1' or 'label 16001'."""
    sids = [f"{'label' if vtype == 'label' else 'idx'} {sid}"
            for algo, sid, vtype in _sr_entries(node) if algo == 0]
    return ", ".join(sids) or "N/A"


def _sid_algos(node: dict) -> str:
    """Return the non-zero algorithms with prefix SIDs, e.g. '1,128,129'."""
    algos = sorted({algo for algo, _, _ in _sr_entries(node) if algo != 0})
    return ",".join(str(a) for a in algos) or "-"


def _format_kbps(kbps) -> str:
    if kbps is None:
        return "N/A"
    kbps = int(kbps)
    if kbps >= 1_000_000:
        return f"{kbps / 1_000_000:.1f} Gbps"
    if kbps >= 1_000:
        return f"{kbps / 1_000:.1f} Mbps"
    return f"{kbps} kbps"


def _links(network: dict) -> list:
    return _get(network, "link", []) or []


def _link_endpoints(link: dict, names: dict) -> tuple:
    src = _get(link, "source", {}) or {}
    dst = _get(link, "destination", {}) or {}
    src_node = _get(src, "source-node", "N/A")
    dst_node = _get(dst, "dest-node", "N/A")
    return (
        names.get(src_node, str(src_node)),
        str(_get(src, "source-tp", "N/A")),
        names.get(dst_node, str(dst_node)),
        str(_get(dst, "dest-tp", "N/A")),
    )


def _link_type(link: dict) -> str:
    """Return the link type (e.g. ISIS_IPV4_L2, ETHERNET, LAG) encoded as the last link-id field."""
    parts = str(_get(link, "link-id", "")).split(" : ")
    return parts[-1].strip() if len(parts) >= 5 else "N/A"


def _link_metrics(link: dict) -> tuple:
    """Return (layer, IGP metric, TE metric, bandwidth) for a link."""
    l3 = _get(link, "l3-link-attributes")
    if l3:
        te = _get(l3, "l3-link-attributes", {}) or {}
        return (
            "L3",
            _get(l3, "metric1", "-"),
            _get(l3, "metric2", "-"),
            _format_kbps(_get(te, "max-bandwidth-kbps")),
        )
    l2 = _get(link, "l2-link-attributes")
    if l2 is not None:
        rate = _get(l2, "rate")
        return "L2", "-", "-", str(rate) if rate is not None else "-"
    return "N/A", "-", "-", "-"


def _display_nodes(nodes: list, indent: str = ""):
    print(f"{indent}{'Name':<28} {'Router ID':<18} {'Prefix SID':<14} {'SID Algos':<24} {'TPs':>5}")
    print(f"{indent}{'-'*28} {'-'*18} {'-'*14} {'-'*24} {'-'*5}")
    for node in nodes:
        name = _node_name(node)[:28]
        rid = _router_ids(node)[:18]
        sid = _prefix_sid(node)[:14]
        algos = _sid_algos(node)[:24]
        tps = len(_get(node, "termination-point", []) or [])
        print(f"{indent}{name:<28} {rid:<18} {sid:<14} {algos:<24} {tps:>5}")


def _sorted_nodes(network: dict) -> list:
    return sorted(_get(network, "node", []) or [], key=lambda n: _natural_key(_node_name(n)))


def display_short(data: dict):
    """Display topology in short tabular format."""
    networks = _networks(data)
    if not networks:
        print("No topology data found.")
        return

    print(f"{'Network ID':<30} {'Type':<35} {'Nodes':>8} {'Links':>8}")
    print(f"{'-'*30} {'-'*35} {'-'*8} {'-'*8}")
    for network in networks:
        net_id = str(_get(network, "network-id", "N/A"))[:30]
        net_type = _network_types(network)[:35]
        nodes = _get(network, "node", []) or []
        print(f"{net_id:<30} {net_type:<35} {len(nodes):>8} {len(_links(network)):>8}")

    for network in networks:
        nodes = _sorted_nodes(network)
        if not nodes:
            continue
        print(f"\nNetwork: {_get(network, 'network-id', 'N/A')}")
        _display_nodes(nodes)
        print(f"Total: {len(nodes)} nodes, {len(_links(network))} links")


def display_topology(data: dict):
    """Display topology data in a user-friendly format."""
    networks = _networks(data)
    if not networks:
        print("No topology data found.")
        return

    print("=" * 100)
    print(f"{'CROSSWORK NETWORK CONTROLLER - TOPOLOGY':^100}")
    print("=" * 100)
    print(f"\nTotal Networks: {len(networks)}\n")

    for idx, network in enumerate(networks, 1):
        nodes = _sorted_nodes(network)
        links = _links(network)
        names = {_get(n, "node-id"): _node_name(n) for n in nodes}
        topo_attrs = (_get(network, "l3-topology-attributes")
                      or _get(network, "l2-topology-attributes") or {})
        isis_area = _get(_get(topo_attrs, "isis-topology-attributes", {}), "area")
        ospf_area = _get(_get(topo_attrs, "ospf-topology-attributes", {}), "area-id")
        layers = [_link_metrics(link)[0] for link in links]

        print("-" * 100)
        print(f"Network {idx}: {_get(network, 'network-id', 'N/A')}")
        print("-" * 100)
        print(f"  {'Type:':<20} {_network_types(network)}")
        print(f"  {'Topology Name:':<20} {_get(topo_attrs, 'name', 'N/A')}")
        if isis_area is not None:
            print(f"  {'ISIS Area:':<20} {isis_area}")
        if ospf_area is not None:
            print(f"  {'OSPF Area:':<20} {ospf_area}")
        print(f"  {'Nodes:':<20} {len(nodes)}")
        print(f"  {'Links:':<20} {len(links)} (L3: {layers.count('L3')}, L2: {layers.count('L2')})")

        if nodes:
            print(f"\n  Nodes ({len(nodes)}):")
            _display_nodes(nodes, indent="    ")

        if links:
            rows = sorted(
                ((_link_endpoints(link, names), _link_metrics(link), _link_type(link)) for link in links),
                key=lambda r: (_natural_key(r[0][0]), r[0][1], _natural_key(r[0][2]), r[0][3], r[1][0], r[2]),
            )
            print(f"\n  Links ({len(links)}):")
            print(f"    {'Source Node':<18} {'Source TP':<26} {'Dest Node':<18} {'Dest TP':<26} "
                  f"{'Layer':<5} {'IGP':>6} {'TE':>6} {'Bandwidth':>11} {'Link Type':<14}")
            print(f"    {'-'*18} {'-'*26} {'-'*18} {'-'*26} {'-'*5} {'-'*6} {'-'*6} {'-'*11} {'-'*14}")
            for (src, src_tp, dst, dst_tp), (layer, igp, te, bw), link_type in rows:
                print(f"    {src[:18]:<18} {src_tp[:26]:<26} {dst[:18]:<18} {dst_tp[:26]:<26} "
                      f"{layer:<5} {str(igp):>6} {str(te):>6} {str(bw)[:11]:>11} {link_type}")
        print()

    print("=" * 100)
    print("Topology retrieval complete.")
    print("=" * 100)


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def main():
    parser = argparse.ArgumentParser(
        description="Retrieve topology networks operational data from Crosswork Network Controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Example:
  python get_topo.py --ip 192.168.1.100 -u admin
  python get_topo.py --ip 10.0.0.1 --jwt ~/.crosswork/10.0.0.1.jwt --json
  python get_topo.py --ip 10.0.0.1 -u admin --output topology.json
  python get_topo.py --ip 10.0.0.1 --offset 0 --limit 1 --short

When --username, --password, and --jwt are omitted, a non-empty
~/.crosswork/<ip>.jwt is used. If that file is missing or empty, or if
--username or --password is set, credentials are resolved in order:
CLI flags > environment variables ({ENV_USERNAME}, {ENV_PASSWORD}) > interactive prompt.
        """,
    )

    parser.add_argument("--ip", required=True, help="Crosswork controller IP address")
    parser.add_argument("--port", type=int, default=BASE_PORT,
                        help=f"Crosswork HTTPS port (default: {BASE_PORT})")
    parser.add_argument("--username", "-u", default=None,
                        help=f"Username (or set {ENV_USERNAME})")
    parser.add_argument("--password", "-p", default=None,
                        help=f"Password (or set {ENV_PASSWORD}; will prompt if omitted)")
    parser.add_argument("--jwt", "-j", help="Path to JWT file (skips username/password auth)")
    parser.add_argument("-k", "--insecure", action="store_true",
                        help="Disable SSL certificate verification (not recommended)")
    parser.add_argument("--offset", type=_non_negative_int, default=None,
                        help="Number of list entries to skip (RESTCONF offset)")
    parser.add_argument("--limit", type=_positive_int, default=None,
                        help="Maximum number of list entries to return (RESTCONF limit)")
    parser.add_argument("--output", "-o", help="Output filename (saves JSON to file)")
    parser.add_argument("--short", "-s", action="store_true", help="Short tabular output")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of formatted display",
    )

    args = parser.parse_args()

    verify_ssl = not args.insecure
    if args.insecure:
        print("WARNING: SSL verification disabled", file=sys.stderr)

    stored_jwt_path = default_jwt_path(args.ip)
    print(f"Connecting to Crosswork at {args.ip}...")
    try:
        token = None
        if args.jwt:
            token = load_token_from_file(args.jwt)
            print(f"Using JWT from {args.jwt}")
        elif not args.username and not args.password:
            token = read_stored_jwt(stored_jwt_path)
            if token:
                print(f"Using JWT from {stored_jwt_path}")
        if not token:
            print("Authenticating...")
            username, password = _resolve_credentials(args.username, args.password)
            token = get_jwt(args.ip, username, password, verify_ssl=verify_ssl, port=args.port)

        topology_data = get_topology(args.ip, token, port=args.port, verify_ssl=verify_ssl,
                                     offset=args.offset, limit=args.limit)
    except (CrossworkAuthError, requests.RequestException, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(topology_data, f, indent=2)
        print(f"Topology saved to: {args.output}")
    elif args.json:
        print(json.dumps(topology_data, indent=2))
    elif args.short:
        display_short(topology_data)
    else:
        display_topology(topology_data)


if __name__ == "__main__":
    main()
