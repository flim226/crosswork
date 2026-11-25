"""Add the L1 (optical) topology from Crosswork Hierarchical Controller to a Crosswork Planning plan.

Used as a Crosswork Planning external script. The Collector framework passes positional
arguments: argv[1] source plan file, argv[2] output plan file, argv[5] home directory (files
bundled with the script), argv[6] user-upload directory (files sent through the file gateway).
Other arguments are ignored.

HCO is the only source of L1 data: ROADMs, amplifiers and sites, OMS and OTS links, media channels
(NMC) and their routes, the cross links between router optics and ROADM ports, and the IP links that
HCO has stitched over the media channels. HCO data comes from the HCO API (SHQL queries and the
manual cross links) when hco_access.json is found, otherwise from a snapshot saved earlier with
--save-snapshot. Input files are looked up first in the user-upload directory and then in the home
directory:

  hco_access.json         {"url": "https://hco:8443", "username": "...", "password": "...",
                           "verify_tls": true, "ca_file": "hco-ca.pem"}             optional
  hco_l1_snapshot.json    the same data, saved with --save-snapshot              snapshot mode
  l1_link_overrides.csv   L1Link,Distance,Loss,NoiseAtoB,NoiseSigmaAtoB,NoiseBtoA,NoiseSigmaBtoA
                                                                                       optional
  l1_options.json         {"spectrum": "auto", "anchor_thz": 191.3, "granularity_ghz": 25,
                           "channel_width_ghz": 75, "route_factor": 1.0,
                           "exclude_channels": "^ASE_", "actual_hops": true}        optional

HCO doesn't hold every span length. A span without one gets the great-circle distance between the
sites at its ends, multiplied by route_factor, and the script reports each estimate. HCO holds no
span loss or noise; add them in l1_link_overrides.csv if you need feasibility checks.

spectrum "auto" lets Crosswork Planning assign central frequency IDs; "hco" converts the center
frequency HCO reports for each media channel to an ID on the grid given by anchor_thz and
granularity_ghz. HCO doesn't report channel widths, so every channel gets channel_width_ghz.

To save a snapshot on a host that can reach HCO (credentials from HCO_USERNAME/HCO_PASSWORD):
  python3 hco_l1_collect.py --save-snapshot <dir> https://<hco-host>:8443 [--insecure]
"""

import base64
import concurrent.futures
import csv
import json
import math
import os
import re
import ssl
import sys
import tempfile
import urllib.request

SNAPSHOT_FILE = 'hco_l1_snapshot.json'
CSV_FILES = ['l1_link_overrides.csv']
L1_COLUMNS = ['L1CircuitNodeA', 'L1CircuitPortA', 'L1CircuitNodeB', 'L1CircuitPortB']
DEFAULT_OPTIONS = {'spectrum': 'auto', 'anchor_thz': 191.3, 'granularity_ghz': 25, 'channel_width_ghz': 75,
                   'route_factor': 1.0, 'exclude_channels': '^ASE_', 'actual_hops': True}
CROSS_LINK = 'MANUAL_CROSS_LINK'
QUERIES = {
    'devices': 'inventory_item[.type in ("ONE", "ROUTER")] | view("guid": .guid, "name": .name, '
               '"type": .type, "provider": .provider, "site": .site.guid, "ip": .managementIp, '
               '"uuid": .extra.uuid, "devices": .extra.devices)',
    'sites': 'site | view("guid": .guid, "name": .name, "latitude": .latitude, "longitude": .longitude)',
    'links': 'link[.layer in ("OTS", "OMS", "NMC") and .provider != "{}"] | view("guid": .guid, '
             '"name": .name, "desc": .desc, "layer": .layer, "provider": .provider, "portA": .portA.guid, '
             '"portB": .portB.guid, "distanceMeters": .distanceMeters, "operStatus": .operStatus)'.format(CROSS_LINK),
    'eth': 'link[.layer="ETH" and .provider="optical-persistor"] | view("guid": .guid, "name": .name, '
           '"portA": .portA.guid, "portB": .portB.guid, "operStatus": .operStatus)',
    'spectrum': 'port[.type="NMC" and .selectedSpectrum.centerFrequencyMhz > 0] | view("guid": .guid, '
                '"centerFrequencyMhz": .selectedSpectrum.centerFrequencyMhz)',
}
# Links one layer below each link, through the SHQL downward function.
BELOW = {'OMS': 'OTS', 'NMC': 'OMS', 'ETH': 'NMC'}
SPEEDS = {'TenGigE': 10, 'TwentyFiveGigE': 25, 'FortyGigE': 40, 'HundredGigE': 100, 'TwoHundredGigE': 200,
          'FourHundredGigE': 400, 'EightHundredGigE': 800}


# ---------------------------------------------------------------- plan file I/O

def fmt(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'T' if v else 'F'
    if isinstance(v, float):
        return '{:.6g}'.format(v)
    return str(v)


def parse_plan_text(text):
    """Return the plan as an ordered list of [table, columns, rows], each row a dict."""
    tables, current = [], None
    for line in text.splitlines():
        if line.startswith('<') and line.endswith('>'):
            current = [line[1:-1], None, []]
            tables.append(current)
        elif not line.strip():
            current = None
        elif current is not None and current[1] is None:
            current[1] = line.split('\t')
        elif current is not None:
            current[2].append(dict(zip(current[1], line.split('\t'))))
    return tables


def write_plan_text(tables, path):
    with open(path, 'w') as f:
        for name, cols, rows in tables:
            f.write('<{}>\n{}\n'.format(name, '\t'.join(cols)))
            for row in rows:
                f.write('\t'.join(fmt(row.get(c)) for c in cols) + '\n')
            f.write('\n')


def read_plan(path):
    if path.endswith('.txt'):
        with open(path) as f:
            return parse_plan_text(f.read())
    # The OPM library converts between plan formats by file extension. On the Collector,
    # only /tmp is writable, so the text copy goes in a per-run directory there.
    from com.cisco.wae.opm.network import Network
    with tempfile.TemporaryDirectory(prefix='hco_l1_') as tmp:
        text_path = os.path.join(tmp, 'source.txt')
        Network(path).write(text_path)
        with open(text_path) as f:
            return parse_plan_text(f.read())


def write_plan(tables, path):
    if path.endswith('.txt'):
        write_plan_text(tables, path)
        return
    from com.cisco.wae.opm.network import Network
    with tempfile.TemporaryDirectory(prefix='hco_l1_') as tmp:
        text_path = os.path.join(tmp, 'output.txt')
        write_plan_text(tables, text_path)
        Network(text_path).write(path)


def table(tables, name):
    return next((t for t in tables if t[0] == name), None)


# ---------------------------------------------------------------- input files

def find_file(name, search_dirs):
    for top in search_dirs:
        for root, _, files in os.walk(top):
            if name in files:
                return os.path.join(root, name)
    return None


def read_csv(path):
    if not path:
        return []
    with open(path, newline='') as f:
        return [{k.strip(): (v or '').strip() for k, v in row.items()} for row in csv.DictReader(f)]


def read_json(path):
    with open(path) as f:
        return json.load(f)


def number(value):
    return float(value) if value not in (None, '') else None


def host(router):
    """'Router-1.example.com' -> 'Router-1'"""
    return re.sub(r'(\.[A-Za-z][A-Za-z0-9-]*){2,}$', '', router or '')


# ---------------------------------------------------------------- HCO access

class Hco:
    def __init__(self, url, username, password, verify_tls=True, ca_file=None):
        self.url = url.rstrip('/')
        token = base64.b64encode('{}:{}'.format(username, password).encode()).decode()
        self.headers = {'Authorization': 'Basic ' + token, 'Accept': 'application/json'}
        self.ctx = ssl.create_default_context(cafile=ca_file or None)
        if not verify_tls:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _call(self, path, body=None):
        headers = dict(self.headers)
        if body is not None:
            headers['Content-Type'] = 'text/plain'
        req = urllib.request.Request(self.url + path, data=body, headers=headers,
                                     method='POST' if body is not None else 'GET')
        with urllib.request.urlopen(req, context=self.ctx, timeout=300) as resp:
            return json.load(resp)

    def shql(self, query):
        """Run a read-only SHQL query (POST /api/v2/shql with the query as a text/plain body)."""
        return self._call('/api/v2/shql', query.encode())

    def get(self, path):
        return self._call(path)


def fetch_hco(hco):
    data = {key: hco.shql(query) for key, query in QUERIES.items()}
    for rows in data.values():
        for r in rows:
            r.pop('__view__', None)
    # For each OMS, NMC and stitched ETH link, the links one layer below it.
    query = 'link[.guid="{}"] | downward | link[.layer="{}" and .provider != "{}"] | view("guid": .guid)'
    upper = [x for x in data['links'] + data['eth'] if x.get('layer', 'ETH') in BELOW]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        rows = pool.map(lambda x: hco.shql(query.format(x['guid'], BELOW[x.get('layer', 'ETH')], CROSS_LINK)), upper)
        data['below'] = {x['guid']: [r['guid'] for r in found] for x, found in zip(upper, rows)}
    # Cross links between router optics and optical ports, without the validation results.
    data['cross_links'] = [{'technology': x.get('technology'),
                            'router': x['interfaceA'].get('deviceName'), 'routerPort': x['interfaceA'].get('interfaceName'),
                            'device': x['interfaceB'].get('deviceName'), 'devicePort': x['interfaceB'].get('interfaceName'),
                            'guid': x['interfaceB'].get('guid')}
                           for x in hco.get('/api/v2/crosslinks/manual')]
    return data


def load_hco(files, search_dirs):
    """Return (HCO data, source description), reading the API or the snapshot file."""
    if files['hco_access.json']:
        cfg = read_json(files['hco_access.json'])
        ca_file = cfg.get('ca_file')
        if ca_file and not os.path.isabs(ca_file):
            ca_file = find_file(ca_file, search_dirs)
        hco = Hco(cfg['url'], cfg['username'], cfg['password'], cfg.get('verify_tls', True), ca_file)
        try:
            return fetch_hco(hco), 'HCO API ' + hco.url
        except OSError as e:
            print('WARNING HCO API {} not reachable ({}); trying the snapshot file'.format(hco.url, e))
    path = find_file(SNAPSHOT_FILE, search_dirs)
    if not path:
        sys.exit('No HCO data: hco_access.json failed or is missing, and {} is missing'.format(SNAPSHOT_FILE))
    return read_json(path), 'HCO snapshot ' + path


def save_snapshot(outdir, url, insecure):
    user, password = os.environ.get('HCO_USERNAME'), os.environ.get('HCO_PASSWORD')
    if not user or not password:
        sys.exit('Set HCO_USERNAME and HCO_PASSWORD')
    data = fetch_hco(Hco(url, user, password, verify_tls=not insecure))
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, SNAPSHOT_FILE)
    with open(path, 'w') as f:
        json.dump(data, f, indent=1)
    print('Saved {} ({} devices, {} links, {} stitched IP links, {} cross links)'.format(
        path, len(data['devices']), len(data['links']), len(data['eth']), len(data['cross_links'])))


# ---------------------------------------------------------------- HCO to L1 model

def great_circle_km(a, b):
    (lat1, lon1), (lat2, lon2) = [(math.radians(p[0]), math.radians(p[1])) for p in (a, b)]
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


class Resolver:
    """Find the device (and port name) behind an HCO port guid.

    ONC ports carry the ONC device or ROADM uuid, EPNM ports the device name, and the optical ports of
    a router (for example a pluggable line system) the router name. A router's optical ports become one
    L1 node named <router>-ols."""

    def __init__(self, hco):
        self.sites = {s['guid']: s for s in hco['sites']}
        self.by_uuid, self.by_name, self.routers = {}, {}, {}
        for d in hco['devices']:
            if d['type'] == 'ROUTER':
                self.routers[host(d['name'])] = d
                continue
            self.by_name[d['name']] = d
            devices = d.get('devices') or []
            if isinstance(devices, str):
                devices = json.loads(devices)
            for u in [d.get('uuid')] + list(devices):
                if u:
                    self.by_uuid[u] = d

    def device(self, port):
        """Return (L1 node name, device record or None, port name) for a port guid, or None."""
        port = port or ''
        m = re.match(r'PO/[\w-]+/[^/]+/([0-9a-f]{8}-[0-9a-f-]{27})/(.+)$', port)
        if m and m.group(1) in self.by_uuid:
            d = self.by_uuid[m.group(1)]
            return d['name'], d, m.group(2)
        m = re.match(r'PO/[\w-]+/([^/]+)/(?:[A-Z]+:)?(.+)$', port)
        if m and m.group(1) in self.by_name:
            d = self.by_name[m.group(1)]
            return d['name'], d, m.group(2)
        m = re.match(r'PO/cisco-cnc-adpt/[A-Z]+-(.+?):(.+)$', port) or re.match(r'PO/nmc/([^/]+)/(.+)$', port)
        if m:
            router = host(m.group(1))
            return router + '-ols', self.routers.get(router), re.sub(r'^.*/(?=Optics)', '', m.group(2))
        return None

    def coords(self, device):
        site = self.sites.get((device or {}).get('site') or '')
        if site and site.get('latitude') is not None and site.get('longitude') is not None:
            return site['latitude'], site['longitude']
        return None

    def site_name(self, device):
        site = self.sites.get((device or {}).get('site') or '')
        return site['name'] if site else None


def chain(links, start, ends):
    """Order links (each with end nodes in ends[guid]) into a path from start. Returns (path, last node)."""
    path, at, todo = [], start, list(links)
    while todo:
        link = next((x for x in todo if at in ends[x['guid']]), None)
        if link is None:
            break
        todo.remove(link)
        a, b = ends[link['guid']]
        forward = a == at
        at = b if forward else a
        path.append((link, forward, at))
    return path, at, todo


def l1_model(hco, overrides, options):
    """Build the L1 tables from HCO data.

    Returns (tables dict, L1 circuits by NMC guid, resolver, link ends by guid, warnings, estimates)."""
    warnings, estimates = [], []
    res = Resolver(hco)
    links = {x['guid']: x for x in hco['links']}
    below = hco.get('below', {})
    exclude = re.compile(options['exclude_channels']) if options.get('exclude_channels') else None

    ends, unresolved = {}, []
    for x in hco['links']:
        a, b = res.device(x['portA']), res.device(x['portB'])
        if a and b:
            ends[x['guid']] = (a[0], b[0])
        elif x['layer'] == 'OMS' or (x['layer'] == 'NMC' and not (exclude and exclude.search(x['name']))):
            unresolved.append('{} {}'.format(x['layer'], x['name']))
    for u in unresolved:
        warnings.append('HCO {} skipped: cannot find the devices at its ends'.format(u))

    devices = {}
    for x in hco['links']:
        if x['guid'] in ends:
            for p in ('portA', 'portB'):
                name, d, _ = res.device(x[p])
                devices[name] = d

    # One L1 link per OMS; its OTS spans give the length and the in-line amplifiers.
    l1links, link_by_oms, waypoints, names, l1nodes = [], {}, [], set(), set()
    for oms in (x for x in hco['links'] if x['layer'] == 'OMS' and x['guid'] in ends):
        na, nb = ends[oms['guid']]
        # HCO splits an ONC node into ROADM and transponder ONEs that share one management IP;
        # OMS links between them stay inside the chassis.
        ip_a, ip_b = (devices.get(na) or {}).get('ip'), (devices.get(nb) or {}).get('ip')
        if na == nb or (ip_a and ip_a == ip_b):
            continue
        spans = [links[g] for g in below.get(oms['guid'], []) if g in links and g in ends]
        path, last, left = chain(spans, na, ends)
        if left or (spans and last != nb):
            warnings.append('OMS {} spans do not chain from {} to {}'.format(oms['name'], na, nb))
        if not path:
            path = [(oms, True, nb)]
        distance, estimated = 0.0, []
        prev = na
        for span, _, at in path:
            if span.get('distanceMeters'):
                distance += span['distanceMeters'] / 1000.0
            else:
                a, b = res.coords(devices.get(prev)), res.coords(devices.get(at))
                km = great_circle_km(a, b) * options['route_factor'] if a and b else None
                estimated.append((prev, at, km))
                distance += km or 0
            prev = at
        base = '{}_{}'.format(na, nb)
        name, i = base, 2
        while name in names:
            name, i = '{}_{}'.format(base, i), i + 1
        names.add(name)
        row = {'Name': name, 'Description': 'HCO {} OMS {}'.format(oms['provider'], oms['name']),
               'L1NodeA': na, 'L1NodeB': nb, 'Distance': round(distance, 1), 'Loss': None,
               'NoiseAtoB': None, 'NoiseSigmaAtoB': None, 'NoiseBtoA': None, 'NoiseSigmaBtoA': None}
        o = overrides.pop(name, None)
        if o:
            for col in ('Distance', 'Loss', 'NoiseAtoB', 'NoiseSigmaAtoB', 'NoiseBtoA', 'NoiseSigmaBtoA'):
                if o.get(col):
                    row[col] = number(o[col])
        if estimated and not (o and o.get('Distance')):
            missing = [(a, b) for a, b, km in estimated if km is None]
            if missing:
                warnings.append('L1 link {}: no length in HCO and no site coordinates for {}; add it to '
                                'l1_link_overrides.csv'.format(name, ', '.join('{}-{}'.format(a, b) for a, b in missing)))
            else:
                estimates.append('L1 link {} distance {:.1f} km: {} of {} spans estimated from site coordinates'
                                 .format(name, row['Distance'], len(estimated), len(path)))
                row['Description'] += '; distance estimated'
        if not row['Distance']:
            warnings.append('L1 link {} has distance 0; add it to l1_link_overrides.csv'.format(name))
        row['Metric'] = max(1, int(round(row['Distance'] or 0)))
        l1links.append(row)
        link_by_oms[oms['guid']] = row
        l1nodes.update((na, nb))
        for order, (_, _, dev) in enumerate(path[:-1], start=1):
            c = res.coords(devices.get(dev))
            if c:
                waypoints.append({'L1NodeA': na, 'L1NodeB': nb, 'L1LinkName': name, 'Order': order,
                                  'Latitude': c[0], 'Longitude': c[1], 'Description': dev})
    for name in overrides:
        warnings.append('l1_link_overrides.csv row {} matches no L1 link'.format(name))

    nodes = []
    for name in sorted(l1nodes):
        d = devices.get(name) or {}
        c = res.coords(d)
        if not c:
            warnings.append('L1 node {} has no site coordinates in HCO'.format(name))
        nodes.append({'Name': name, 'Site': res.site_name(d), 'IPAddress': d.get('ip'), 'Vendor': 'Cisco',
                      'Description': 'HCO {}'.format(d.get('guid') or 'line system of router ' + name[:-4]),
                      'Latitude': c[0] if c else None, 'Longitude': c[1] if c else None})

    # One L1 circuit per media channel (NMC), routed over the OMS links HCO reports under it.
    freq = {p['guid']: p['centerFrequencyMhz'] for p in hco['spectrum']}
    gran = options['granularity_ghz']
    ports, circuits, paths, hops, channels, cnames = [], [], [], [], {}, set()
    for x in (x for x in hco['links'] if x['layer'] == 'NMC' and x['guid'] in ends):
        if exclude and exclude.search(x['name']):
            continue
        (na, _, pa), (nb, _, pb) = res.device(x['portA']), res.device(x['portB'])
        m = re.match(r'^\w+: (\S+?)/(\S+) to (\S+?)/(\S+)$', x.get('desc') or '')
        if m:
            pa, pb = m.group(2), m.group(4)
        if na not in l1nodes or nb not in l1nodes:
            warnings.append('HCO channel {} skipped: {} has no OMS link'.format(
                x['name'], na if na not in l1nodes else nb))
            continue
        oms = [links[g] for g in below.get(x['guid'], []) if g in link_by_oms]
        route, last, left = chain(oms, na, ends)
        if left or last != nb:
            warnings.append('HCO channel {} route does not chain from {} to {}; actual hops not written'.format(x['name'], na, nb))
            route = []
        name = re.sub(r'_otsimc$', '', x['name'])
        if not re.fullmatch(r'[\w .-]+', name):
            name = '{}_{}_NMC'.format(na, nb)
        base, i = name, 2
        while name in cnames:
            name, i = '{}_{}'.format(base, i), i + 1
        cnames.add(name)
        mhz = freq.get(x['portA']) or freq.get(x['portB'])
        rate = re.search(r'(\d+)G', x['name'])
        ports += [{'L1Node': na, 'L1Port': pa}, {'L1Node': nb, 'L1Port': pb}]
        circuit = {'Name': name, 'L1NodeA': na, 'L1PortA': pa, 'L1NodeB': nb, 'L1PortB': pb,
                   'BW': int(rate.group(1)) * 1000 if rate else None,
                   'Tags': 'HCO_{}THz'.format(mhz / 1e6) if mhz else ''}
        path = {'L1Circuit': name, 'L1NodeA': na, 'L1PortA': pa, 'L1NodeB': nb, 'L1PortB': pb,
                'PathOption': 1, 'Standby': False, 'Active': True, 'AutoSpectrumAssignment': True,
                'SpectralWidthID': int(math.ceil(options['channel_width_ghz'] / (2 * gran) - 1e-6))}
        cfid = None
        if options['spectrum'] == 'hco' and mhz:
            steps = (mhz / 1e6 - options['anchor_thz']) * 1000 / gran
            cfid = int(math.floor(steps + 0.5))
            if abs(steps - cfid) * gran > 0.5:
                warnings.append('Channel {}: {} THz is {:.2f} GHz off the {} GHz grid; using ID {}'.format(
                    name, mhz / 1e6, (steps - cfid) * gran, gran, cfid))
            path.update(CentralFrequencyID=cfid, AutoSpectrumAssignment=False)
        if x.get('operStatus') not in (None, 'UP'):
            warnings.append('HCO channel {} is {}'.format(name, x['operStatus']))
        circuits.append(circuit)
        paths.append(path)
        if options['actual_hops']:
            for step, (o, _, _) in enumerate(route, start=1):
                link = link_by_oms[o['guid']]
                hops.append({'L1Circuit': name, 'L1NodeA': na, 'L1PortA': pa, 'L1NodeB': nb, 'L1PortB': pb,
                             'PathOption': 1, 'Step': step, 'HopL1NodeA': link['L1NodeA'],
                             'HopL1NodeB': link['L1NodeB'], 'HopL1Link': link['Name'], 'CentralFrequencyID': cfid})
        channels[x['guid']] = circuit

    tables = {
        'L1Nodes': ['Name', 'Site', 'Description', 'IPAddress', 'Vendor', 'Longitude', 'Latitude'],
        'L1Links': ['Name', 'Description', 'L1NodeA', 'L1NodeB', 'Metric', 'Distance', 'Loss',
                    'NoiseAtoB', 'NoiseSigmaAtoB', 'NoiseBtoA', 'NoiseSigmaBtoA'],
        'L1LinkWaypoints': ['L1NodeA', 'L1NodeB', 'L1LinkName', 'Order', 'Longitude', 'Latitude', 'Description'],
        'L1Ports': ['L1Node', 'L1Port'],
        'L1Circuits': ['Name', 'L1NodeA', 'L1PortA', 'L1NodeB', 'L1PortB', 'BW', 'Tags'],
        'L1CircuitPaths': ['L1Circuit', 'L1NodeA', 'L1PortA', 'L1NodeB', 'L1PortB', 'PathOption', 'Standby',
                           'Active', 'CentralFrequencyID', 'SpectralWidthID', 'AutoSpectrumAssignment'],
        'ActualL1CircuitPathHops': ['L1Circuit', 'L1NodeA', 'L1PortA', 'L1NodeB', 'L1PortB', 'PathOption',
                                    'Step', 'HopL1NodeA', 'HopL1NodeB', 'HopL1Link', 'CentralFrequencyID'],
    }
    rows = {'L1Nodes': nodes, 'L1Links': l1links, 'L1LinkWaypoints': waypoints, 'L1Ports': ports,
            'L1Circuits': circuits, 'L1CircuitPaths': paths, 'ActualL1CircuitPathHops': hops}
    return ({name: [name, cols, rows[name]] for name, cols in tables.items()}, channels, res, ends, warnings,
            estimates)


def router_links(hco, res, l1nodes):
    """Return ({(router, L1 node)}, warnings) from the HCO cross links between router optics and optical ports."""
    pairs, warnings = set(), []
    for x in hco['cross_links']:
        if x.get('technology') != 'NMC':
            continue
        router = host(x['router'])
        found = res.device(x.get('guid'))
        if found is None:
            warnings.append('cross link {} {} -> {} {} is not resolved in HCO; router not connected to it'.format(
                router, x['routerPort'], x['device'], x['devicePort']))
        elif found[0] in l1nodes:
            pairs.add((router, found[0]))
    return pairs, warnings


# ---------------------------------------------------------------- main

def add_l1_topology(src, dest, search_dirs):
    names = ['hco_access.json', 'l1_options.json'] + CSV_FILES
    files = {name: find_file(name, search_dirs) for name in names}
    for name in names:
        print('Input {}: {}'.format(name, files[name] or 'not found'))
    options = dict(DEFAULT_OPTIONS)
    if files['l1_options.json']:
        options.update(read_json(files['l1_options.json']))

    hco, source = load_hco(files, search_dirs)
    print('HCO data from {}'.format(source))
    overrides = {r['L1Link']: r for r in read_csv(files['l1_link_overrides.csv'])}
    l1, channels, res, ends, warnings, estimates = l1_model(hco, overrides, options)
    l1nodes = {r['Name']: r for r in l1['L1Nodes'][2]}

    # Replace any existing L1 objects, as File > Import > Layer 1 does.
    tables = [t for t in read_plan(src)
              if not (t[0].startswith('L1') or t[0] in ('L3L1Links', 'ActualL1CircuitPathHops'))]
    layout = table(tables, 'PlotLayoutElements')
    if layout and 'ElementL1Node' in layout[1]:
        layout[2] = [r for r in layout[2] if not r.get('ElementL1Node') or r['ElementL1Node'] in l1nodes]

    # Plan routers are matched without their DNS domain; each one without a site gets its HCO site.
    nodes = table(tables, 'Nodes')
    plan_node = {host(n['Name']): n for n in nodes[2]}
    if 'Site' not in nodes[1]:
        nodes[1].append('Site')
    sites = table(tables, 'Sites')
    if sites is None:
        sites = ['Sites', ['Name', 'Longitude', 'Latitude'], []]
        tables.insert(1, sites)
    site_rows = {r['Name']: r for r in sites[2]}

    def add_site(name, lat, lon):
        if name and name not in site_rows:
            site_rows[name] = {'Name': name, 'Latitude': lat, 'Longitude': lon}
            sites[2].append(site_rows[name])

    for row in l1['L1Nodes'][2]:
        add_site(row['Site'], row['Latitude'], row['Longitude'])
    sited = 0
    for router, n in plan_node.items():
        d = res.routers.get(router)
        if d and not n.get('Site') and res.site_name(d):
            c = res.coords(d)
            add_site(res.site_name(d), c[0] if c else None, c[1] if c else None)
            n['Site'] = res.site_name(d)
            sited += 1
    for name, row in l1nodes.items():
        if name.endswith('-ols') and not row['Site'] and name[:-4] in plan_node:
            row['Site'] = plan_node[name[:-4]].get('Site')

    pairs, cross_warnings = router_links(hco, res, l1nodes)
    l3l1, not_in_plan = [], []
    for router, l1node in sorted(pairs):
        if router in plan_node:
            l3l1.append({'Node': plan_node[router]['Name'], 'L1Node': l1node})
        else:
            not_in_plan.append('{} -> {}'.format(router, l1node))

    for name in ('L1Nodes', 'L1Links', 'L1LinkWaypoints', 'L1Ports', 'L1Circuits', 'L1CircuitPaths',
                 'ActualL1CircuitPathHops'):
        tables.append(l1[name])
    tables.append(['L3L1Links', ['Node', 'L1Node'], l3l1])

    # Map each IP link that HCO has stitched over a media channel onto that L1 circuit.
    circuits = table(tables, 'Circuits')
    for col in L1_COLUMNS:
        if col not in circuits[1]:
            circuits[1].append(col)
    by_key = {}
    for c in circuits[2]:
        for col in L1_COLUMNS:
            c[col] = ''
        a, b = (host(c['NodeA']), c['InterfaceA']), (host(c['NodeB']), c['InterfaceB'])
        by_key[(a, b)] = by_key[(b, a)] = c
    eth_re = re.compile(r'PO/cisco-cnc-adpt/ETH-(.+?):(.+)$')
    mapped, info = [], []
    connected = {(host(r['Node']), r['L1Node']) for r in l3l1}
    for e in hco['eth']:
        ma, mb = eth_re.match(e.get('portA') or ''), eth_re.match(e.get('portB') or '')
        if not (ma and mb):
            end = ma or mb
            info.append('HCO ETH half link {} {} ({}); not mapped'.format(
                host(end.group(1)) if end else '?', end.group(2) if end else e['name'], e.get('operStatus')))
            continue
        a, b = (host(ma.group(1)), ma.group(2)), (host(mb.group(1)), mb.group(2))
        circuit = by_key.get((a, b))
        ch = next((channels[g] for g in hco['below'].get(e['guid'], []) if g in channels), None)
        if circuit is None or ch is None:
            info.append('HCO ETH link {} {} - {} {}: {}'.format(*a, *b, 'no IP link in the plan' if circuit is None
                                                              else 'its media channel is not in the L1 model'))
            continue
        same = (host(circuit['NodeA']), ch['L1NodeA']) in connected or (host(circuit['NodeB']), ch['L1NodeB']) in connected
        circuit['L1CircuitNodeA'] = ch['L1NodeA'] if same else ch['L1NodeB']
        circuit['L1CircuitPortA'] = ch['L1PortA'] if same else ch['L1PortB']
        circuit['L1CircuitNodeB'] = ch['L1NodeB'] if same else ch['L1NodeA']
        circuit['L1CircuitPortB'] = ch['L1PortB'] if same else ch['L1PortA']
        speed = next((v for k, v in SPEEDS.items() if circuit['InterfaceA'].startswith(k)), None)
        if speed and not ch['BW']:
            ch['BW'] = speed * 1000
        mapped.append(ch['Name'])

    write_plan(tables, dest)

    print('L1 nodes: {}, L1 links: {}, waypoints: {}, L1 circuits: {}, actual hop rows: {}, spectrum: {}'.format(
        *(len(l1[t][2]) for t in ('L1Nodes', 'L1Links', 'L1LinkWaypoints', 'L1Circuits', 'ActualL1CircuitPathHops')),
        options['spectrum']))
    print('L3L1 links: {}, IP links mapped to L1 circuits: {}, routers given an HCO site: {}'.format(
        len(l3l1), len(mapped), sited))
    for e in estimates:
        print('ESTIMATE ' + e)
    for name in sorted({c['Name'] for c in l1['L1Circuits'][2]} - set(mapped)):
        print('INFO L1 circuit {} carries no IP link in the plan'.format(name))
    for i in info:
        print('INFO ' + i)
    for w in warnings + cross_warnings:
        print('WARNING ' + w)
    for n in not_in_plan:
        print('WARNING cross link router not in the plan: ' + n)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--save-snapshot':
        if len(sys.argv) < 4:
            sys.exit('Usage: {} --save-snapshot <dir> https://<hco-host>:8443 [--insecure]'.format(sys.argv[0]))
        save_snapshot(sys.argv[2], sys.argv[3], '--insecure' in sys.argv[4:])
        sys.exit(0)
    if len(sys.argv) < 3:
        print('Usage: {} <source_plan> <output_plan> [auth] [net_access] [home] [upload_dir] [archive]'
              .format(sys.argv[0]))
        sys.exit(1)
    upload_dir = sys.argv[6] if len(sys.argv) > 6 else None
    home_dir = sys.argv[5] if len(sys.argv) > 5 else os.path.dirname(os.path.abspath(__file__))
    add_l1_topology(sys.argv[1], sys.argv[2], [d for d in (upload_dir, home_dir) if d and os.path.isdir(d)])
