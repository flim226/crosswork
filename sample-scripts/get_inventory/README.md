# get_inventory.py

Retrieves physical node inventory from Cisco Crosswork Network Controller using the `resource-physical:node` API.

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
python get_inventory.py --ip <IP_ADDRESS> -u <USERNAME> -p <PASSWORD> [OPTIONS]
```

### Required Arguments

| Argument | Description |
|----------|-------------|
| `--ip` | Crosswork controller IP address |
| `-u`, `--username` | Authentication username |
| `-p`, `--password` | Authentication password |

### Optional Arguments

| Argument | Description |
|----------|-------------|
| `-o`, `--output` | Save JSON output to file |
| `-s`, `--short` | Display short tabular output |
| `--json` | Output raw JSON instead of formatted display |

## Examples

**Basic inventory retrieval (formatted display):**
```bash
python get_inventory.py --ip 192.168.1.100 -u admin -p mypassword
```

**Short tabular output:**
```bash
python get_inventory.py --ip 192.168.1.100 -u admin -p mypassword --short
```

**Output raw JSON:**
```bash
python get_inventory.py --ip 10.0.0.1 -u admin -p secret --json
```

**Save to file:**
```bash
python get_inventory.py --ip 10.0.0.1 -u admin -p secret --output inventory.json
```

## Output Formats

### Default (Formatted Display)
Displays detailed inventory with:
- Node basic information (hostname, UUID, management IP, product type, software version)
- Equipment list grouped by type (chassis, modules, power supplies, fans)
- Network interfaces with admin/operational status

### Short (`--short`)
Compact table showing:
- Name, Management IP, Product Type, Version, UUID

### JSON (`--json` or `--output`)
Raw API response in JSON format.

## API Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `/crosswork/sso/v1/tickets` | Obtain authentication ticket |
| `/crosswork/sso/v1/tickets/{ticket}` | Exchange ticket for JWT token |
| `/crosswork/inventory/restconf/data/v2/resource-physical:node` | Retrieve inventory data |

## Notes

- SSL certificate verification is disabled (for self-signed certificates)
- Default port: 30603
- Timeout: 30s for authentication, 60s for inventory retrieval
