# get_topo.py

Retrieves L3 and L2 topology operational data (networks, nodes, termination points, and links) from Cisco Crosswork Network Controller using the `ietf-network-state:networks` API.

It calls:

```text
GET /crosswork/nbi/topology/v3/restconf/data/ietf-network-state:networks
```

API reference: [Retrieve topology networks operational data](https://developer.cisco.com/docs/crosswork/network-controller/retrieve-topology-networks-operational-data/)

## Requirements

- Python 3.10 or later

No `pip install` is needed. The script is self-contained: its dependencies, pinned in `requirements.txt`, are bundled as pure-Python packages in `vendor/`, which `get_topo.py` adds to `sys.path` at startup. Copy the whole `get_topo/` directory to run it on another host.

```text
get_topo/
├── get_topo.py
├── requirements.txt
└── vendor/          # requests, urllib3, certifi, idna, charset-normalizer
```

The vendored packages take precedence over any versions installed in the Python environment. TLS verification uses the CA bundle from the vendored `certifi`, so a CNC certificate signed by a private CA needs `REQUESTS_CA_BUNDLE=/path/to/ca.pem` (or `-k` in a lab).

To update the bundled dependencies, edit the versions in `requirements.txt` and rebuild `vendor/`:

```bash
rm -rf vendor
python3 -m pip install -r requirements.txt --target vendor --no-deps \
  --only-binary=:all: --platform any --implementation py --python-version 3.10 --no-compile
rm -rf vendor/bin
```

The `--platform any --implementation py` flags select pure-Python wheels only, so the bundle works on any OS and CPU architecture.

## Usage

```bash
python get_topo.py --ip <IP_ADDRESS> [OPTIONS]
```

### Required Arguments

| Argument | Description |
|----------|-------------|
| `--ip` | Crosswork controller IP address or hostname |

### Authentication

When `--username`, `--password`, and `--jwt` are omitted, a non-empty `~/.crosswork/<ip>.jwt` (created by `cw_get_jwt.py`) is used. If that file is missing or empty, or if `--username` or `--password` is set, credentials come from the CLI flags, then `CW_USERNAME` / `CW_PASSWORD`, then an interactive prompt.

| Argument | Description |
|----------|-------------|
| `-u`, `--username` | Authentication username |
| `-p`, `--password` | Authentication password |
| `-j`, `--jwt` | Path to a JWT file |

### Optional Arguments

| Argument | Description |
|----------|-------------|
| `--port` | Crosswork HTTPS port (default: `30603`) |
| `-k`, `--insecure` | Disable TLS certificate verification (lab CNCs with self-signed certificates only) |
| `--offset` | RESTCONF `offset`: number of list entries to skip (>= 0) |
| `--limit` | RESTCONF `limit`: maximum number of list entries to return (>= 1) |
| `-o`, `--output` | Save JSON output to file |
| `-s`, `--short` | Display short tabular output |
| `--json` | Output raw JSON instead of formatted display |

`--offset` and `--limit` are passed to CNC unchanged. CNC applies them to the nested node and link lists as well as the network list, so `--limit 1` returns a reduced sample of the network rather than one complete network.

## Examples

**Formatted display using the saved JWT:**
```bash
python get_topo.py --ip 192.168.1.100 -k
```

**Short tabular output:**
```bash
python get_topo.py --ip 192.168.1.100 -k --short
```

**Supply a JWT explicitly and output raw JSON:**
```bash
python get_topo.py --ip 10.0.0.1 --jwt ~/.crosswork/10.0.0.1.jwt -k --json
```

**Authenticate with environment variables and save to file:**
```bash
export CW_USERNAME=admin
read -rs CW_PASSWORD && export CW_PASSWORD
python get_topo.py --ip 10.0.0.1 -k --output topology.json
```

## Output Formats

### Default (Formatted Display)
For each network:
- Network ID, topology type (for example `L3 (isis, sr-mpls, srv6)` or `L2`), topology name, and ISIS/OSPF area
- Node and link counts, with links split into L3 and L2
- Node table: name, router ID, algorithm 0 prefix SID, other SR algorithms with prefix SIDs (Strict SPF and Flex-Algo), termination point count
- Link table: source node and interface, destination node and interface, layer (L3/L2), IGP metric, TE metric, max bandwidth, and link type

Nodes and links are sorted by name using natural ordering (`node-2` before `node-10`).

The link type (`ISIS_IPV4_L2`, `ISIS_IPV6_L2`, `ETHERNET`, `LAG`, ...) is not a separate YANG leaf; it is taken from the last field of the `link-id`, for example `node-9 : Bundle-Ether29 : node-2 : Bundle-Ether29 : LAG`. Links whose ID does not follow this format show `N/A`.

Each physical link usually appears more than once: as an L2 `ETHERNET` or `LAG` link, and as L3 `ISIS_IPV4_L2` / `ISIS_IPV6_L2` adjacencies.

### Short (`--short`)
- Network summary: network ID, type, node count, link count
- Node table (same columns as the default display)

### JSON (`--json` or `--output`)
Raw API response in JSON format.

## API Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `/crosswork/sso/v1/tickets` | Obtain ticket-granting ticket |
| `/crosswork/sso/v2/tickets/jwt` | Exchange ticket for JWT token |
| `/crosswork/nbi/topology/v3/restconf/data/ietf-network-state:networks` | Retrieve topology data |

## Notes

- TLS certificate verification is enabled by default; use `-k` only for CNCs with self-signed certificates
- Default port: 30603
- Timeout: 20s for authentication, 120s for topology retrieval
- Max bandwidth is reported by CNC in kbps and displayed as Gbps/Mbps
