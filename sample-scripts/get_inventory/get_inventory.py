#!/usr/bin/env python3
"""
Crosswork Network Controller - Deep Inventory Retrieval Script
Retrieves physical node inventory using the resource-physical:node API
"""

import argparse
import getpass
import json
import os
import sys

import requests
import urllib3

BASE_PORT = 30603
CONNECT_TIMEOUT = 20
INVENTORY_TIMEOUT = 60
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
    with open(path, "r", encoding="utf-8") as jwt_file:
        return jwt_file.read().strip()


def get_inventory(ip_address: str, token: str, port: int = BASE_PORT, verify_ssl: bool = True) -> dict:
    """Retrieve deep inventory from Crosswork Network Controller."""
    base_url = f"https://{ip_address}:{port}"
    url = f"{base_url}/crosswork/inventory/restconf/data/v2/resource-physical:node"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    session = _create_session(verify_ssl=verify_ssl)
    response = session.get(url, headers=headers, timeout=INVENTORY_TIMEOUT)
    check_response(response, "get_inventory")
    return response.json()


def display_short(data: dict):
    """Display inventory in short tabular format."""
    response = data.get("com.response-message", {})
    nodes = response.get("com.data", {}).get("nd.node", [])
    
    if not nodes:
        print("No inventory data found.")
        return
    
    # Sort by hostname
    nodes = sorted(nodes, key=lambda n: str(n.get("nd.name", "")).lower())
    
    print(f"{'Name':<25} {'Management IP':<18} {'Product Type':<35} {'Version':<15} {'UUID':<40}")
    print(f"{'-'*25} {'-'*18} {'-'*35} {'-'*15} {'-'*40}")
    
    for node in nodes:
        name = str(node.get("nd.name", "N/A"))[:25]
        mgmt_ip = str(node.get("nd.management-address", "N/A"))[:18]
        product = str(node.get("nd.product-type", "N/A"))[:35]
        version = str(node.get("nd.software-version", "N/A"))[:15]
        uuid = str(node.get("nd.uuid", "N/A"))[:40]
        print(f"{name:<25} {mgmt_ip:<18} {product:<35} {version:<15} {uuid:<40}")
    
    print(f"\nTotal: {len(nodes)} nodes")


def display_inventory(data: dict):
    """Display inventory data in a user-friendly format."""
    response = data.get("com.response-message", {})
    header = response.get("com.header", {})
    nodes = response.get("com.data", {}).get("nd.node", [])
    
    if not nodes:
        print("No inventory data found.")
        return
    
    print("=" * 90)
    print(f"{'CROSSWORK NETWORK CONTROLLER - DEEP INVENTORY':^90}")
    print("=" * 90)
    print(f"\nTotal Nodes: {len(nodes)} (Index {header.get('com.firstIndex', 0)} - {header.get('com.lastIndex', 0)})\n")
    
    for idx, node in enumerate(nodes, 1):
        print("-" * 90)
        print(f"Node {idx}: {node.get('nd.name', 'N/A')}")
        print("-" * 90)
        
        # Basic Information
        print(f"  {'Host Name:':<25} {node.get('nd.name', 'N/A')}")
        print(f"  {'UUID:':<25} {node.get('nd.uuid', 'N/A')}")
        print(f"  {'Management IP:':<25} {node.get('nd.management-address', 'N/A')}")
        print(f"  {'Product Type:':<25} {node.get('nd.product-type', 'N/A')}")
        print(f"  {'Software Version:':<25} {node.get('nd.software-version', 'N/A')}")
        print(f"  {'Description:':<25} {node.get('nd.description', 'N/A')[:60]}")
        print(f"  {'Communication State:':<25} {node.get('nd.communication-state', 'N/A')}")
        print(f"  {'Collection Status:':<25} {node.get('nd.collection-status', 'N/A')[:50]}")
        print(f"  {'Collection Time:':<25} {node.get('nd.collection-time', 'N/A')}")
        print(f"  {'Creation Time:':<25} {node.get('nd.creation-time', 'N/A')}")
        
        # Equipment List
        equipment_list = node.get("nd.equipment-list", {}).get("eq.equipment", [])
        if equipment_list:
            print(f"\n  Equipment ({len(equipment_list)} items):")
            print(f"    {'Name':<40} {'Type':<15} {'Product ID':<25} {'Serial Number':<20} {'Status':<10}")
            print(f"    {'-'*40} {'-'*15} {'-'*25} {'-'*20} {'-'*10}")
            
            # Group by equipment type
            chassis = []
            modules = []
            power = []
            fans = []
            others = []
            
            for eq in equipment_list:
                eq_type = eq.get("eq.equipment-type", "OTHER")
                if eq_type == "CHASSIS":
                    chassis.append(eq)
                elif eq_type == "MODULE":
                    modules.append(eq)
                elif eq_type == "POWERSUPPLY":
                    power.append(eq)
                elif eq_type == "FAN":
                    fans.append(eq)
                else:
                    others.append(eq)
            
            # Display chassis first
            for eq in chassis:
                name = str(eq.get("fdtn.name", "N/A"))[:40]
                eq_type = str(eq.get("eq.equipment-type", "N/A"))[:15]
                product = str(eq.get("eq.product-id", "N/A"))[:25]
                serial = str(eq.get("eq.serial-number", "N/A"))[:20]
                status = str(eq.get("eq.operational-state-code", "N/A"))[:10]
                print(f"    {name:<40} {eq_type:<15} {product:<25} {serial:<20} {status:<10}")
            
            # Display modules (limit to 10)
            if modules:
                print(f"\n    Modules ({len(modules)}):")
                for eq in modules[:10]:
                    name = str(eq.get("fdtn.name", "N/A"))[:40]
                    product = str(eq.get("eq.product-id", "N/A"))[:25]
                    serial = str(eq.get("eq.serial-number", "N/A"))[:20]
                    status = str(eq.get("eq.operational-state-code", "N/A"))[:10]
                    print(f"      {name:<40} {product:<25} {serial:<20} {status:<10}")
                if len(modules) > 10:
                    print(f"      ... and {len(modules) - 10} more modules")
            
            # Display power supplies
            if power:
                print(f"\n    Power Supplies ({len(power)}):")
                for eq in power:
                    name = str(eq.get("fdtn.name", "N/A"))[:40]
                    product = str(eq.get("eq.product-id", "N/A"))[:25]
                    serial = str(eq.get("eq.serial-number", "N/A"))[:20]
                    status = str(eq.get("eq.operational-state-code", "N/A"))[:10]
                    print(f"      {name:<40} {product:<25} {serial:<20} {status:<10}")
            
            # Display fans (summary)
            if fans:
                up_fans = sum(1 for f in fans if f.get("eq.operational-state-code") == "Up")
                print(f"\n    Fans: {len(fans)} total ({up_fans} operational)")
        
        # Interfaces
        interfaces = node.get("nd.interface-list", {}).get("intf.interface", [])
        if interfaces:
            print(f"\n  Interfaces ({len(interfaces)}):")
            print(f"    {'Name':<35} {'Admin':<10} {'Oper':<10} {'Speed':<15} {'Type':<20}")
            print(f"    {'-'*35} {'-'*10} {'-'*10} {'-'*15} {'-'*20}")
            for intf in interfaces[:20]:
                name = str(intf.get("fdtn.name", "N/A"))[:35]
                admin = str(intf.get("intf.admin-status", "N/A"))[:10]
                oper = str(intf.get("intf.oper-status", "N/A"))[:10]
                speed = str(intf.get("intf.speed", "N/A"))[:15]
                intf_type = str(intf.get("intf.type", "N/A"))[:20]
                print(f"    {name:<35} {admin:<10} {oper:<10} {speed:<15} {intf_type:<20}")
            if len(interfaces) > 20:
                print(f"    ... and {len(interfaces) - 20} more interfaces")
        
        print()
    
    print("=" * 90)
    print("Inventory retrieval complete.")
    print("=" * 90)


def main():
    parser = argparse.ArgumentParser(
        description="Retrieve deep inventory from Crosswork Network Controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Example:
  python get_inventory.py --ip 192.168.1.100 -u admin
  python get_inventory.py --ip 10.0.0.1 --jwt ~/.crosswork/10.0.0.1.jwt --json
  python get_inventory.py --ip 10.0.0.1 -u admin --output inventory.json

Credentials are resolved in order: CLI flags > environment variables
({ENV_USERNAME}, {ENV_PASSWORD}) > interactive prompt.
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

    print(f"Connecting to Crosswork at {args.ip}...")
    try:
        if args.jwt:
            token = load_token_from_file(args.jwt)
            print(f"Using JWT from {args.jwt}")
        else:
            print("Authenticating...")
            username, password = _resolve_credentials(args.username, args.password)
            token = get_jwt(args.ip, username, password, verify_ssl=verify_ssl, port=args.port)

        inventory_data = get_inventory(args.ip, token, port=args.port, verify_ssl=verify_ssl)
    except (CrossworkAuthError, requests.RequestException, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)
    
    if args.output:
        with open(args.output, "w") as f:
            json.dump(inventory_data, f, indent=2)
        print(f"Inventory saved to: {args.output}")
    elif args.json:
        print(json.dumps(inventory_data, indent=2))
    elif args.short:
        display_short(inventory_data)
    else:
        display_inventory(inventory_data)


if __name__ == "__main__":
    main()
