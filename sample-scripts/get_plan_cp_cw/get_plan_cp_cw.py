import argparse
import base64
import logging
import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import requests
import urllib3

# Crosswork Planning Startup Script to Retrieve Plan from CNC
# To be used in Crosswork Planning 7.2 Collector > Startup script > Script
# Connection settings - modify these values as needed

CROSSWORK_IP = "198.18.134.219"
CROSSWORK_USERNAME = "admin"
CROSSWORK_PASSWORD = "PASSWORD"
PLAN_VERSION = ""
TMP_PLANFILE = "planfile.pln"
CONNECT_TIMEOUT = 20

TRIM_FILES = {
    "trim_include.txt": "include node table",
    "trim_exclude.txt": "exclude node table",
    "trim_include_regex.txt": "include nodes regex",
    "trim_exclude_regex.txt": "exclude nodes regex",
}

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class _TimeoutAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that applies a default timeout to all requests."""
    def send(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = CONNECT_TIMEOUT
        return super().send(*args, **kwargs)


def _create_session() -> requests.Session:
    """Create an HTTP session with SSL verification disabled and default timeout."""
    session = requests.Session()
    session.verify = False
    adapter = _TimeoutAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_auth_ticket(session: requests.Session, ip: str, username: str, password: str) -> str:
    """Authenticate and get a ticket from Crosswork."""
    # Step 1: Get TGT ticket
    auth_url = f"https://{ip}:30603/crosswork/sso/v1/tickets"
    payload = f"username={quote_plus(username)}&password={quote_plus(password)}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    response = session.post(auth_url, data=payload, headers=headers)
    response.raise_for_status()
    tgt = response.text.strip()

    # Step 2: Get JWT token using TGT
    jwt_url = f"https://{ip}:30603/crosswork/sso/v1/tickets/{tgt}"
    jwt_payload = f"service=https://{ip}:30603/app-dashboard"

    response = session.post(jwt_url, data=jwt_payload, headers=headers)
    response.raise_for_status()
    return response.text.strip()


def get_plan(session: requests.Session, ip: str, ticket: str, plan_format: str, version: str) -> bytes:
    """Retrieve the plan file from Crosswork."""
    plan_url = f"https://{ip}:30603/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-operations:get-plan"
    headers = {
        "Authorization": f"Bearer {ticket}",
        "Accept": "application/yang-data+json",
        "Content-Type": "application/yang-data+json",
    }
    payload = {
        "input": {
            "version": version,
            "format": plan_format,
        }
    }

    response = session.post(plan_url, headers=headers, json=payload)
    response.raise_for_status()

    data = response.json()

    # Extract plan file content from response
    output = data.get("cisco-crosswork-optimization-engine-operations:output", data.get("output", {}))
    if "planfile-content" in output:
        return base64.b64decode(output["planfile-content"])
    elif "plan-file-content" in output:
        return base64.b64decode(output["plan-file-content"])
    else:
        return response.content


def run_command(cmd: List[str], description: str) -> None:
    """Run a subprocess command, log output, and raise on failure."""
    logger.info("%s: %s", description, " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        logger.info(result.stdout)
    if result.stderr:
        logger.warning(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"{description} failed with return code {result.returncode}")


def find_trim_file(
    filename: str, search_dirs: List[str], dir_labels: Dict[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    """Search for a trim file in the given directories, return (path, label) or (None, None)."""
    for d in search_dirs:
        path = os.path.join(d, filename)
        if os.path.isfile(path):
            return path, dir_labels[d]
    return None, None


def find_and_apply_trim(
    tmpfile: str, search_dirs: List[str], dir_labels: Dict[str, str]
) -> str:
    """Discover trim config files, run trim_nodes if needed, and return the input path for conversion."""
    logger.info("Checking for trim config files in %s...", list(dir_labels.values()))
    found_trim_files: Dict[str, str] = {}
    for fname, desc in TRIM_FILES.items():
        path, label = find_trim_file(fname, search_dirs, dir_labels)
        if path:
            logger.info("  Found %s in %s (%s)", fname, label, desc)
            found_trim_files[fname] = path
        else:
            logger.info("  Not found: %s (%s) - skipped", fname, desc)

    trim_args: List[str] = []
    if "trim_include.txt" in found_trim_files:
        trim_args.extend(["-node-table", found_trim_files["trim_include.txt"], "-exclude-node-table", "false"])
    if "trim_exclude.txt" in found_trim_files:
        trim_args.extend(["-node-table", found_trim_files["trim_exclude.txt"], "-exclude-node-table", "true"])
    for fname, flag in [("trim_include_regex.txt", "-include-nodes-regex"),
                        ("trim_exclude_regex.txt", "-exclude-nodes-regex")]:
        if fname in found_trim_files:
            with open(found_trim_files[fname], "r") as rf:
                regex = rf.read().strip()
            trim_args.extend([flag, regex])

    if not trim_args:
        return tmpfile

    base, _ = os.path.splitext(tmpfile)
    trim_output = base + ".trim.pln"
    trim_cmd = ["trim_nodes", "-plan-file", tmpfile, "-out-file", trim_output] + trim_args
    run_command(trim_cmd, "Trimming nodes: ")
    return trim_output


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(
        description="Retrieve a plan file from Crosswork Network Controller"
    )
    parser.add_argument("dummy", help="First parameter, base planfile (ignored)")
    parser.add_argument("planfile", help="Output plan file name")
    parser.add_argument("device_auth_file", help="Device access authentication file")
    parser.add_argument("network_access_config", help="Global network access configuration file")
    parser.add_argument("home_dir", help="Home directory")
    parser.add_argument("user_upload_dir", help="Path where user uploaded external files are available")
    parser.add_argument("archive_root_dir", help="Path to access archive root directory")
    parser.add_argument("--tmpfile", default=TMP_PLANFILE)
    parser.add_argument("--ip", default=CROSSWORK_IP, help=f"Crosswork controller IP address (default: {CROSSWORK_IP})")
    parser.add_argument("--username", "-u", default=CROSSWORK_USERNAME, help=f"Username (default: {CROSSWORK_USERNAME})")
    parser.add_argument("--password", "-p", default=CROSSWORK_PASSWORD, help="Password")
    parser.add_argument("--version", "-v", default=PLAN_VERSION, help="Planfile version (default: empty)")

    args = parser.parse_args()

    # Deduce format from temporary planfile extension
    ext = os.path.splitext(args.tmpfile)[1].lower()
    if ext == ".txt":
        file_format = "txt"
    elif ext == ".pln":
        file_format = "pln"
    else:
        logger.error("Temporary planfile must have .txt or .pln, got '%s'", ext)
        sys.exit(1)

    temp_files: List[str] = []
    try:
        logger.info("Startup script get_plan_cp_cw.py is initializing...")
        session = _create_session()

        logger.info("Authenticating to Crosswork at %s...", args.ip)
        ticket = get_auth_ticket(session, args.ip, args.username, args.password)

        logger.info("Retrieving plan: %s...", args.tmpfile)
        plan_content = get_plan(session, args.ip, ticket, file_format, args.version)
        logger.info("  Retrieved %d bytes", len(plan_content))

        with open(args.tmpfile, "wb") as f:
            f.write(plan_content)
        temp_files.append(args.tmpfile)

        logger.info("Plan saved to: %s", args.tmpfile)

        search_dirs = [args.home_dir, args.user_upload_dir]
        dir_labels = {args.home_dir: "home_dir", args.user_upload_dir: "user_upload_dir"}

        convert_input = find_and_apply_trim(args.tmpfile, search_dirs, dir_labels)
        if convert_input != args.tmpfile:
            temp_files.append(convert_input)

        # Convert .pln to .db format as required by Crosswork Planning
        run_command(
            ["mate_convert", "-plan-file", convert_input, "-out-file", args.planfile],
            f"Converting plan: ",
        )

    except requests.exceptions.HTTPError as e:
        logger.error("HTTP Error: %s", e)
        logger.error("Response: %s", e.response.text if e.response else "No response")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        logger.error("Connection Error: Could not connect to %s", args.ip)
        sys.exit(1)
    except Exception as e:
        logger.error("Error: %s", e)
        sys.exit(1)
    finally:
        for tmp in temp_files:
            try:
                os.remove(tmp)
                logger.info("Cleaned up temporary file: %s", tmp)
            except OSError:
                pass


if __name__ == "__main__":
    main()
