#!/usr/bin/env python3
"""
Script to retrieve a plan file from Crosswork Network Controller.
"""

import argparse
import os
import requests
import base64
import urllib3
from datetime import datetime

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def get_auth_ticket(ip: str, username: str, password: str) -> str:
    """Authenticate and get a ticket from Crosswork."""
    # Step 1: Get TGT ticket
    auth_url = f"https://{ip}:30603/crosswork/sso/v1/tickets"
    payload = f"username={username}&password={password}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    
    response = requests.post(auth_url, data=payload, headers=headers, verify=False)
    response.raise_for_status()
    tgt = response.text.strip()
    
    # Step 2: Get JWT token using TGT
    jwt_url = f"https://{ip}:30603/crosswork/sso/v1/tickets/{tgt}"
    jwt_payload = f"service=https://{ip}:30603/app-dashboard"
    
    response = requests.post(jwt_url, data=jwt_payload, headers=headers, verify=False)
    response.raise_for_status()
    return response.text.strip()


def get_plan(ip: str, ticket: str, plan_name: str, format: str, version: str) -> bytes:
    """Retrieve the plan file from Crosswork."""
    plan_url = f"https://{ip}:30603/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-operations:get-plan"
    headers = {
        "Authorization": f"Bearer {ticket}",
        "Accept": "application/yang-data+json",
        "Content-Type": "application/yang-data+json"
    }
    payload = {
        "input": {
            "version": version,
            "format": format
        }
    }
    
    response = requests.post(plan_url, headers=headers, json=payload, verify=False)
    response.raise_for_status()
    
    data = response.json()
    
    # Extract plan file content from response
    output = data.get("cisco-crosswork-optimization-engine-operations:output", data.get("output", {}))
    if "planfile-content" in output:
        return base64.b64decode(output["planfile-content"])
    elif "plan-file-content" in output:
        return base64.b64decode(output["plan-file-content"])
    else:
        # Return raw response if no encoded content found
        return response.content


def main():
    parser = argparse.ArgumentParser(
        description="Retrieve a plan file from Crosswork Network Controller"
    )
    parser.add_argument("--ip", required=True, help="Crosswork controller IP address")
    parser.add_argument("--username", "-u", required=True, help="Username")
    parser.add_argument("--password", "-p", required=True, help="Password")
    parser.add_argument("--planfile", "-f", help="Plan file name (must end with .txt or .pln). Default: <ip>.<timestamp>.<format>")
    parser.add_argument("--format", choices=["txt", "pln"], default="txt", help="Plan file format (default: txt). Used for default filename when --planfile is not provided.")
    parser.add_argument("--version", "-v", default="", help="Planfile schema version (default: empty string)")

    
    args = parser.parse_args()
    
    # Generate default filename if not provided, using --format for extension
    if not args.planfile:
        now = datetime.now().astimezone()
        timestamp = now.strftime("%Y%m%d-%H%M%S")
        utc_offset = now.strftime("%z")
        args.planfile = f"{args.ip}.{timestamp}{utc_offset}.{args.format}"
        file_format = args.format
    else:
        # Deduce format from planfile extension
        ext = os.path.splitext(args.planfile)[1].lower()
        if ext == ".txt":
            file_format = "txt"
        elif ext == ".pln":
            file_format = "pln"
        else:
            print(f"Error: --planfile must have .txt or .pln extension, got '{ext}'")
            exit(1)
    
    try:
        print(f"Authenticating to {args.ip}...")
        ticket = get_auth_ticket(args.ip, args.username, args.password)
        
        print(f"Retrieving plan: {args.planfile}...")
        plan_content = get_plan(args.ip, ticket, args.planfile, file_format, args.version)
        
        with open(args.planfile, "wb") as f:
            f.write(plan_content)
        
        print(f"Plan saved to: {args.planfile}")
        
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {e}")
        print(f"Response: {e.response.text if e.response else 'No response'}")
        exit(1)
    except requests.exceptions.ConnectionError as e:
        print(f"Connection Error: Could not connect to {args.ip}")
        exit(1)
    except Exception as e:
        print(f"Error: {e}")
        exit(1)


if __name__ == "__main__":
    main()
