#!/usr/bin/env python3
"""
Crosswork Network Controller - Deep Inventory Retrieval Script
Retrieves physical node inventory using the resource-physical:node API
"""

import argparse
import json
import sys
import urllib3
import requests

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def get_ticket(ip_address: str, username: str, password: str, port: int = 30603) -> str:
    """Obtain JWT ticket from Crosswork SSO."""
    url = f"https://{ip_address}:{port}/crosswork/sso/v1/tickets"
    
    # Try JSON body format
    try:
        response = requests.post(
            url,
            json={"username": username, "password": password},
            headers={"Content-Type": "application/json"},
            verify=False,
            timeout=30
        )
        response.raise_for_status()
        return response.text.strip()
    except requests.exceptions.HTTPError:
        pass
    
    # Try form-urlencoded format
    try:
        response = requests.post(
            url,
            data={"username": username, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            verify=False,
            timeout=30
        )
        response.raise_for_status()
        return response.text.strip()
    except requests.exceptions.HTTPError as e:
        print(f"Authentication Error: {e}")
        print(f"Response: {response.text}")
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        print(f"Connection Error: Unable to connect to {ip_address}:{port}")
        sys.exit(1)


def get_jwt_token(ip_address: str, ticket: str, port: int = 30603) -> str:
    """Exchange ticket for JWT token."""
    url = f"https://{ip_address}:{port}/crosswork/sso/v1/tickets/{ticket}"
    
    try:
        response = requests.post(
            url,
            data={"service": f"https://{ip_address}:{port}/app-dashboard"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            verify=False,
            timeout=30
        )
        response.raise_for_status()
        return response.text.strip()
    except requests.exceptions.HTTPError as e:
        print(f"Token Error: {e}")
        sys.exit(1)


def get_inventory(ip_address: str, username: str, password: str, port: int = 30603) -> dict:
    """Retrieve deep inventory from Crosswork Network Controller."""
    # First get authentication token
    print("Authenticating...")
    ticket = get_ticket(ip_address, username, password, port)
    token = get_jwt_token(ip_address, ticket, port)
    
    url = f"https://{ip_address}:{port}/crosswork/inventory/restconf/data/v2/resource-physical:node"
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    
    try:
        response = requests.get(
            url,
            headers=headers,
            verify=False,
            timeout=60
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {e}")
        print(f"Response: {response.text}")
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        print(f"Connection Error: Unable to connect to {ip_address}")
        print(f"Details: {e}")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print(f"Timeout: Request to {ip_address} timed out")
        sys.exit(1)
    except json.JSONDecodeError:
        print("Error: Invalid JSON response from server")
        sys.exit(1)


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
        epilog="""
Example:
  python get_inventory.py --ip 192.168.1.100 -u admin -p mypassword
  python get_inventory.py --ip 10.0.0.1 -u admin -p secret --json
  python get_inventory.py --ip 10.0.0.1 -u admin -p secret --output inventory.json
        """
    )
    
    parser.add_argument("--ip", required=True, help="Crosswork controller IP address")
    parser.add_argument("--username", "-u", required=True, help="Username")
    parser.add_argument("--password", "-p", required=True, help="Password")
    parser.add_argument("--output", "-o", help="Output filename (saves JSON to file)")
    parser.add_argument("--short", "-s", action="store_true", help="Short tabular output")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of formatted display"
    )
    
    args = parser.parse_args()
    
    print(f"Connecting to Crosswork at {args.ip}...")
    inventory_data = get_inventory(args.ip, args.username, args.password)
    
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
