#!/usr/bin/env python3
"""
Mock CNC (Crosswork Network Controller) server for testing get_plan.py.

Usage:
    python fake_cnc_server.py [--port PORT] [--plan-file PLAN_FILE]

Then run get_plan.py against it:
    python get_plan.py --ip 127.0.0.1 -u admin -p admin -f plan.txt
"""

import argparse
import base64
import json
import os
import ssl
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Default plan file path (same directory as this script)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PLAN_FILE = os.path.join(SCRIPT_DIR, "sample_plan.txt")

# Plan content will be loaded from file or use fallback
PLAN_CONTENT_TXT = None
PLAN_CONTENT_PLN = None

# Store active tickets
active_tgts = {}
active_jwts = {}


class MockCNCHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[CNC] {args[0]}")

    def send_json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_text_response(self, text, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode() if content_length else ""
        path = urlparse(self.path).path

        # Step 1: Initial authentication - get TGT
        if path == "/crosswork/sso/v1/tickets":
            # Parse form data
            params = dict(p.split("=") for p in body.split("&") if "=" in p)
            username = params.get("username", "")
            password = params.get("password", "")
            
            # Accept any credentials for testing
            if username and password:
                tgt = f"TGT-{uuid.uuid4().hex[:16]}"
                active_tgts[tgt] = username
                print(f"[CNC] Issued TGT for user: {username}")
                self.send_text_response(tgt)
            else:
                self.send_text_response("Invalid credentials", 401)
            return

        # Step 2: Exchange TGT for JWT
        if path.startswith("/crosswork/sso/v1/tickets/TGT-"):
            tgt = path.split("/")[-1]
            if tgt in active_tgts:
                jwt = f"eyJhbGciOiJIUzI1NiJ9.{uuid.uuid4().hex}.mock-signature"
                active_jwts[jwt] = active_tgts[tgt]
                print(f"[CNC] Issued JWT for TGT: {tgt[:20]}...")
                self.send_text_response(jwt)
            else:
                self.send_text_response("Invalid TGT", 401)
            return

        # Step 3: Get plan
        if path == "/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-operations:get-plan":
            auth_header = self.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                self.send_json_response({"error": "Unauthorized"}, 401)
                return

            token = auth_header.replace("Bearer ", "")
            if token not in active_jwts:
                self.send_json_response({"error": "Invalid token"}, 401)
                return

            # Parse request body
            try:
                request_data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                request_data = {}

            input_data = request_data.get("input", {})
            file_format = input_data.get("format", "txt")
            version = input_data.get("version", "7.10")

            print(f"[CNC] Plan request - format: {file_format}, version: {version}")

            # Return appropriate plan content
            if file_format == "pln":
                plan_content = PLAN_CONTENT_PLN
            else:
                plan_content = PLAN_CONTENT_TXT

            encoded_content = base64.b64encode(plan_content).decode()

            response = {
                "cisco-crosswork-optimization-engine-operations:output": {
                    "planfile-content": encoded_content
                }
            }
            self.send_json_response(response)
            return

        # Unknown endpoint
        self.send_text_response("Not Found", 404)


def load_plan_content(plan_file):
    """Load plan content from file or use fallback."""
    global PLAN_CONTENT_TXT, PLAN_CONTENT_PLN
    
    if plan_file and os.path.exists(plan_file):
        print(f"[CNC] Loading plan from: {plan_file}")
        with open(plan_file, "rb") as f:
            PLAN_CONTENT_TXT = f.read()
        # For PLN format, use the same content (or could load separate file)
        PLAN_CONTENT_PLN = PLAN_CONTENT_TXT
    else:
        print("[CNC] Using fallback sample plan data")
        PLAN_CONTENT_TXT = FALLBACK_PLAN.encode()
        PLAN_CONTENT_PLN = PLAN_CONTENT_TXT


# Fallback minimal plan if no file provided
FALLBACK_PLAN = """<Network>
Property	Value
Title	Fake Network
Version	7.10.-1

<NetworkOptions>
Option	Value
IGP_Protocol	ISIS

<Nodes>
Name	Site	Function	Protected	Active	Type	AS	IPAddress
node-1		core	F	T	physical	65000	198.19.1.1
node-2		core	F	T	physical	65000	198.19.1.2
"""


def create_self_signed_cert():
    """Create a self-signed certificate for HTTPS."""
    from subprocess import run, PIPE
    import tempfile
    import os

    cert_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    cert_file.close()
    key_file.close()

    # Generate self-signed cert using openssl
    run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", key_file.name, "-out", cert_file.name,
        "-days", "1", "-nodes",
        "-subj", "/CN=localhost"
    ], check=True, stdout=PIPE, stderr=PIPE)

    return cert_file.name, key_file.name


def main():
    parser = argparse.ArgumentParser(description="Mock CNC Server for testing")
    parser.add_argument("--port", type=int, default=30603, help="Port to listen on (default: 30603)")
    parser.add_argument("--no-ssl", action="store_true", help="Disable SSL (use HTTP instead of HTTPS)")
    parser.add_argument("--plan-file", default=DEFAULT_PLAN_FILE, 
                        help=f"Path to plan file to serve (default: {DEFAULT_PLAN_FILE})")
    args = parser.parse_args()

    # Load plan content
    load_plan_content(args.plan_file)

    server_address = ("0.0.0.0", args.port)
    httpd = HTTPServer(server_address, MockCNCHandler)

    if not args.no_ssl:
        print("[CNC] Generating self-signed certificate...")
        cert_file, key_file = create_self_signed_cert()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        protocol = "HTTPS"
    else:
        protocol = "HTTP"

    print(f"[CNC] Mock CNC Server started on {protocol}://0.0.0.0:{args.port}")
    print(f"[CNC] Test with: python get_plan.py --ip 127.0.0.1 -u admin -p admin -f plan.txt")
    print("[CNC] Press Ctrl+C to stop")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[CNC] Server stopped")


if __name__ == "__main__":
    main()
