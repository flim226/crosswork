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

DEFAULT_PORT = 30603
DEFAULT_TIMEOUT = 30
VERIFY_SSL = False

# Suppress SSL warnings when verification is disabled
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class CrossworkAuthError(RuntimeError):
    """Raised when authentication or API calls fail."""


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


def get_ticket(
    base_url: str,
    username: str,
    password: str,
    *,
    verify_ssl: bool,
    timeout: int,
) -> str:
    """POST credentials to Crosswork SSO and return a ticket-granting ticket."""
    url = f"{base_url}/crosswork/sso/v1/tickets"
    resp = requests.post(
        url,
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        verify=verify_ssl,
        timeout=timeout,
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


def get_token(
    base_url: str,
    ticket: str,
    *,
    verify_ssl: bool,
    timeout: int,
) -> str:
    """Exchange a Crosswork SSO ticket for a JWT bearer token via SSO v2."""
    url = f"{base_url}/crosswork/sso/v2/tickets/jwt"
    resp = requests.post(
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"tgt": ticket, "service": f"{base_url}/app-dashboard"},
        verify=verify_ssl,
        timeout=timeout,
    )
    check_response(resp, "get_token")

    try:
        token = resp.json().get("token", "")
    except ValueError:
        token = resp.text.strip()

    if not token:
        raise CrossworkAuthError(f"Could not extract token from response: {resp.text[:300]}")
    return token


def get_plan(base_url: str, token: str, plan_name: str, format: str, version: str,
             *, verify_ssl: bool, timeout: int) -> bytes:
    """Retrieve the plan file from Crosswork."""
    plan_url = f"{base_url}/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-operations:get-plan"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/yang-data+json",
        "Content-Type": "application/yang-data+json"
    }
    payload = {
        "input": {
            "version": version,
            "format": format
        }
    }
    
    response = requests.post(plan_url, headers=headers, json=payload,
                             verify=verify_ssl, timeout=timeout)
    check_response(response, "get_plan")
    
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
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Crosswork HTTPS port (default: {DEFAULT_PORT})")
    parser.add_argument("--username", "-u", required=True, help="Username")
    parser.add_argument("--password", "-p", required=True, help="Password")
    parser.add_argument("--planfile", "-f", help="Plan file name (must end with .txt or .pln). Default: <ip>.<timestamp>.<format>")
    parser.add_argument("--format", choices=["txt", "pln"], default="txt", help="Plan file format (default: txt). Used for default filename when --planfile is not provided.")
    parser.add_argument("--version", "-v", default="", help="Planfile schema version (default: empty string)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT})")

    
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
    
    base_url = f"https://{args.ip}:{args.port}"

    try:
        print(f"Authenticating to {args.ip}...")
        ticket = get_ticket(base_url, args.username, args.password,
                            verify_ssl=VERIFY_SSL, timeout=args.timeout)
        token = get_token(base_url, ticket,
                          verify_ssl=VERIFY_SSL, timeout=args.timeout)
        
        print(f"Retrieving plan: {args.planfile}...")
        plan_content = get_plan(base_url, token, args.planfile, file_format, args.version,
                                verify_ssl=VERIFY_SSL, timeout=args.timeout)
        
        with open(args.planfile, "wb") as f:
            f.write(plan_content)
        
        print(f"Plan saved to: {args.planfile}")
        
    except (CrossworkAuthError, requests.RequestException, OSError) as e:
        print(f"Error: {e}")
        exit(1)


if __name__ == "__main__":
    main()
