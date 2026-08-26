import argparse
import base64
import logging
import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

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
BASE_PORT = 30603
CONNECT_TIMEOUT = 20
ENV_USERNAME = "CW_USERNAME"
ENV_PASSWORD = "CW_PASSWORD"

TRIM_FILES = {
    "trim_include.txt": "include node table",
    "trim_exclude.txt": "exclude node table",
    "trim_include_regex.txt": "include nodes regex",
    "trim_exclude_regex.txt": "exclude nodes regex",
}

logger = logging.getLogger(__name__)


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


def _resolve_credentials(username=None, password=None) -> Tuple[str, str]:
    """Resolve credentials from args, environment, or script defaults."""
    username = username or os.environ.get(ENV_USERNAME) or CROSSWORK_USERNAME
    password = password or os.environ.get(ENV_PASSWORD) or CROSSWORK_PASSWORD
    return username, password


def load_token_from_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as jwt_file:
        return jwt_file.read().strip()


def get_plan(session: requests.Session, base_url: str, token: str, plan_format: str, version: str) -> bytes:
    """Retrieve the plan file from Crosswork."""
    plan_url = f"{base_url}/crosswork/nbi/optima/v2/restconf/operations/cisco-crosswork-optimization-engine-operations:get-plan"
    headers = {
        "Authorization": f"Bearer {token}",
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
    check_response(response, "get_plan")

    data = response.json()

    # Extract plan file content from response
    output = data.get("cisco-crosswork-optimization-engine-operations:output", data.get("output", {}))
    if "planfile-content" in output:
        return base64.b64decode(output["planfile-content"])
    elif "plan-file-content" in output:
        return base64.b64decode(output["plan-file-content"])
    else:
        return response.content


def get_dlm_node_coordinates(session: requests.Session, base_url: str, token: str) -> Dict[str, Tuple[object, object]]:
    """Return DLM Inventory coordinates keyed by case-insensitive hostname."""
    inventory_url = f"{base_url}/crosswork/inventory/v1/nodes/query"
    response = session.post(
        inventory_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"limit": 0, "filter": {}},
    )
    check_response(response, "get_dlm_node_coordinates")

    try:
        nodes = response.json().get("data", [])
    except ValueError as exc:
        raise CrossworkAuthError("get_dlm_node_coordinates returned invalid JSON") from exc
    if not isinstance(nodes, list):
        raise CrossworkAuthError("get_dlm_node_coordinates returned an unexpected response format")

    coordinates: Dict[str, Tuple[object, object]] = {}
    for node in nodes:
        hostname = node.get("host_name")
        geo = node.get("geo_info", {}).get("coordinates", {})
        latitude = geo.get("latitude", {}).get("value")
        longitude = geo.get("longitude", {}).get("value")
        if isinstance(hostname, str) and latitude is not None and longitude is not None:
            coordinates[hostname.casefold()] = (longitude, latitude)
    return coordinates


def update_plan_node_coordinates(
    plan_content: bytes, coordinates: Dict[str, Tuple[object, object]]
) -> Tuple[bytes, int, int]:
    """Update Longitude and Latitude columns in a text plan's ``<Nodes>`` table."""
    try:
        plan_text = plan_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("--geoloc requires a UTF-8 text plan (.txt), not a binary plan") from exc

    lines = plan_text.splitlines(keepends=True)
    in_nodes_table = False
    header_indexes: Optional[Dict[str, int]] = None
    updated = 0
    plan_node_count = 0

    for index, line in enumerate(lines):
        line_body = line.rstrip("\r\n")
        if line_body.strip() == "<Nodes>":
            in_nodes_table = True
            header_indexes = None
            continue
        if in_nodes_table and line_body.startswith("<"):
            break
        if not in_nodes_table:
            continue

        columns = line_body.split("\t")
        if header_indexes is None:
            if "Name" in columns and "Longitude" in columns and "Latitude" in columns:
                header_indexes = {
                    "name": columns.index("Name"),
                    "longitude": columns.index("Longitude"),
                    "latitude": columns.index("Latitude"),
                }
            continue
        if not line_body:
            continue

        plan_node_count += 1
        name_index = header_indexes["name"]
        if len(columns) <= name_index:
            continue
        node_coordinates = coordinates.get(columns[name_index].casefold())
        if node_coordinates is None:
            continue

        required_columns = max(header_indexes.values()) + 1
        columns.extend([""] * (required_columns - len(columns)))
        longitude, latitude = node_coordinates
        columns[header_indexes["longitude"]] = str(longitude)
        columns[header_indexes["latitude"]] = str(latitude)
        line_ending = line[len(line_body):]
        lines[index] = "\t".join(columns) + line_ending
        updated += 1

    if not in_nodes_table or header_indexes is None:
        raise ValueError("The text plan does not contain a <Nodes> table with Longitude and Latitude columns")
    return "".join(lines).encode("utf-8"), updated, plan_node_count


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
    trim_output = base + ".trim" + os.path.splitext(tmpfile)[1]
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
    parser.add_argument("--username", "-u", default=CROSSWORK_USERNAME,
                        help=f"Username (default: {CROSSWORK_USERNAME}, or set {ENV_USERNAME})")
    parser.add_argument("--password", "-p", default=CROSSWORK_PASSWORD,
                        help=f"Password (default: {CROSSWORK_PASSWORD}, or set {ENV_PASSWORD})")
    parser.add_argument("--jwt", "-j", help="Path to JWT file (skips username/password auth)")
    parser.add_argument("-k", "--insecure", action="store_true",
                        help="Disable SSL certificate verification (not recommended)")
    parser.add_argument("--version", "-v", default=PLAN_VERSION, help="Planfile version (default: empty)")
    parser.add_argument("--geoloc", action="store_true",
                        help="Populate <Nodes> Longitude and Latitude from DLM Inventory (uses a text temporary plan)")

    args = parser.parse_args()

    # DLM coordinates are written into the text plan before it is converted to .db.
    if args.geoloc and args.tmpfile == TMP_PLANFILE:
        args.tmpfile = os.path.splitext(TMP_PLANFILE)[0] + ".txt"

    # Deduce format from temporary planfile extension
    ext = os.path.splitext(args.tmpfile)[1].lower()
    if ext == ".txt":
        file_format = "txt"
    elif ext == ".pln":
        file_format = "pln"
    else:
        logger.error("Temporary planfile must have .txt or .pln, got '%s'", ext)
        sys.exit(1)
    if args.geoloc and file_format != "txt":
        logger.error("--geoloc requires a .txt temporary plan file; use --tmpfile planfile.txt")
        sys.exit(1)

    temp_files: List[str] = []
    try:
        logger.info("Startup script get_plan_cp_cw.py is initializing...")
        verify_ssl = not args.insecure
        if args.insecure:
            logger.warning("SSL verification disabled")
        session = _create_session(verify_ssl=verify_ssl)
        base_url = f"https://{args.ip}:{BASE_PORT}"

        if args.jwt:
            logger.info("Using JWT from %s", args.jwt)
            token = load_token_from_file(args.jwt)
        else:
            logger.info("Authenticating to Crosswork at %s...", args.ip)
            username, password = _resolve_credentials(args.username, args.password)
            ticket = get_ticket(session, base_url, username, password)
            token = get_token(session, base_url, ticket)

        logger.info("Retrieving plan: %s...", args.tmpfile)
        plan_content = get_plan(session, base_url, token, file_format, args.version)
        logger.info("  Retrieved %d bytes", len(plan_content))

        if args.geoloc:
            logger.info("Retrieving node coordinates from DLM Inventory...")
            coordinates = get_dlm_node_coordinates(session, base_url, token)
            plan_content, updated_nodes, plan_nodes = update_plan_node_coordinates(plan_content, coordinates)
            logger.info("Updated coordinates for %d of %d plan nodes.", updated_nodes, plan_nodes)

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

    except (CrossworkAuthError, requests.RequestException, OSError) as e:
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
