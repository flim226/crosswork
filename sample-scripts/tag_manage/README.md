# tag_manage.py

Manage tags on Crosswork Network Controller nodes. List, add, and remove tags from individual hosts or all hosts at once.

## Requirements

- Python 3.x
- `requests` library
- `urllib3` library

Install dependencies:
```bash
pip install requests urllib3
```

## Usage

```bash
python3 tag_manage.py [HOST] --ip <IP> --username <USER> --password <PASS> <ACTION> [OPTIONS]
```

### Positional Arguments

| Argument | Description |
|----------|-------------|
| `HOST` | Target hostname. If omitted, the action applies to all hosts. |

### Required Arguments

| Argument | Description |
|----------|-------------|
| `--ip` | Crosswork controller IP address |
| `--username` | Authentication username |
| `--password` | Authentication password |

### Actions (one required)

| Argument | Description |
|----------|-------------|
| `--get`, `--list` | List tags for the target host(s) |
| `--add` | Add a tag to the target host(s) |
| `--remove`, `--rm`, `--del`, `--delete` | Remove a tag from the target host(s) |

### Optional Arguments

| Argument | Description |
|----------|-------------|
| `--tag TAG` | Tag name (required with `--add` or `--remove`) |
| `--raw` | Output raw JSON instead of table (use with `--get`) |

## Examples

**List tags for all hosts:**
```bash
python3 tag_manage.py --ip 198.18.134.219 --username admin --password 'PASSWORD' --get
```

**List tags for a specific host:**
```bash
python3 tag_manage.py node-2 --ip 198.18.134.219 --username admin --password 'PASSWORD' --get
```

**Get raw JSON output:**
```bash
python3 tag_manage.py --ip 198.18.134.219 --username admin --password 'PASSWORD' --get --raw
```

**Add a tag to all hosts:**
```bash
python3 tag_manage.py --ip 198.18.134.219 --username admin --password 'PASSWORD' --add --tag mytag
```

**Add a tag to a specific host:**
```bash
python3 tag_manage.py node-2 --ip 198.18.134.219 --username admin --password 'PASSWORD' --add --tag mytag
```

**Remove a tag from all hosts:**
```bash
python3 tag_manage.py --ip 198.18.134.219 --username admin --password 'PASSWORD' --remove --tag mytag
```

**Remove a tag from a specific host:**
```bash
python3 tag_manage.py node-2 --ip 198.18.134.219 --username admin --password 'PASSWORD' --rm --tag mytag
```

## Output Formats

### Default (Table)
Sorted tabular output showing hostname and associated tags:
```
Host                 Tags
-------------------  -------------------------------------------------------
cpe-21.nso-topology  cli, snmp, reach-check, clock-drift-check
node-1               ios-xr, cli, gnmi, snmp, reach-check, te-tunnel-id
node-2               ios-xr, cli, gnmi, snmp, reach-check, te-tunnel-id
```

### Raw JSON (`--raw`)
Full API response in JSON format, useful for scripting or debugging.

## API Endpoints Used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/crosswork/sso/v1/tickets` | POST | Obtain TGT authentication ticket |
| `/crosswork/sso/v1/tickets/{tgt}` | POST | Exchange TGT for JWT token |
| `/crosswork/inventory/v1/nodes/query` | POST | Retrieve nodes and their tags |
| `/crosswork/inventory/v1/tags` | POST | Create a new tag in the system |
| `/crosswork/inventory/v1/nodes` | PUT | Update nodes (used for adding tags) |
| `/crosswork/inventory/v1/nodes/unassigntag` | PUT | Remove tags from nodes |

## Behavior Notes

- **Adding tags:** The script first creates the tag in the system (idempotent — skips if it already exists), then appends it to each target node. Nodes that already have the tag are skipped.
- **Removing tags:** Only nodes that currently have the specified tag are updated. Nodes without the tag are skipped.
- **Authentication:** Uses JWT-based authentication via Crosswork SSO (port 30603).
- **SSL:** Certificate verification is disabled to support self-signed certificates.
