"""Add the L1 (optical) topology from Cisco Optical Network Controller to a Crosswork Planning plan.

Used as a Crosswork Planning external script. The Collector framework passes positional
arguments: argv[1] source plan file, argv[2] output plan file, argv[5] home directory (files
bundled with the script), argv[6] user-upload directory (files sent through the file gateway).
Other arguments are ignored.

ONC data comes from the ONC REST API when onc_access.json is found, otherwise from snapshot
files saved earlier with --save-snapshot. Input files are looked up first in the user-upload
directory and then in the home directory:

  onc_access.json         {"url": "https://onc:8443", "username": "...", "password": "...",
                           "verify_tls": true, "ca_file": "onc-ca.pem"}             optional
  onc_topology.json       GET /api/topology                                  snapshot mode
  onc_ots_links.json      GET /onc-topology-service/v1/ots-links             snapshot mode
  onc_oms_links.json      GET /onc-topology-service/v1/oms-links/{uuid}, as a list
  onc_circuits.json       GET /onc-circuit-service/v1/circuits               snapshot mode
  l1_node_map.csv         Node,L1Node    the ROADMs each router connects to, one row each   required
  l1_circuit_map.csv      NodeA,InterfaceA,NodeB,InterfaceB,OncChannel                  required
  l1_link_overrides.csv   L1Link,Distance,NoiseAtoB,NoiseSigmaAtoB,NoiseBtoA,NoiseSigmaBtoA
                                                                                       optional
  l1_transceivers.csv     InterfaceType,MinLineOSNR      minimum line OSNR in dB (0.1 nm) optional
  l1_options.json         {"spectrum": "auto", "anchor_thz": 191.3, "granularity_ghz": 25,
                           "actual_hops": true}                                         optional

spectrum "auto" lets Crosswork Planning assign central frequency IDs; "onc" converts each ONC
channel frequency and width to IDs on the grid given by anchor_thz and granularity_ghz.

To save a snapshot on a host that can reach ONC (credentials from ONC_USERNAME/ONC_PASSWORD):
  python3 onc_l1_collect.py --save-snapshot <dir> https://<onc-host>:8443 [--insecure]
"""

import base64
import csv
import json
import math
import os
import re
import ssl
import sys
import tempfile
import urllib.request

ONC_FILES = {
    'topology': ('onc_topology.json', '/api/topology'),
    'ots_links': ('onc_ots_links.json', '/onc-topology-service/v1/ots-links'),
    'circuits': ('onc_circuits.json', '/onc-circuit-service/v1/circuits'),
}
OMS_FILE, OMS_PATH = 'onc_oms_links.json', '/onc-topology-service/v1/oms-links/'
CSV_FILES = ['l1_node_map.csv', 'l1_circuit_map.csv', 'l1_link_overrides.csv', 'l1_transceivers.csv']
L1_COLUMNS = ['L1CircuitNodeA', 'L1CircuitPortA', 'L1CircuitNodeB', 'L1CircuitPortB']
DEFAULT_OPTIONS = {'spectrum': 'auto', 'anchor_thz': 191.3, 'granularity_ghz': 25, 'actual_hops': True}
ENDPOINT_RE = re.compile(r'endpoint-port-([0-9a-f-]{36})-(.+?)-RX-port-')


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
    with tempfile.TemporaryDirectory(prefix='onc_l1_') as tmp:
        text_path = os.path.join(tmp, 'source.txt')
        Network(path).write(text_path)
        with open(text_path) as f:
            return parse_plan_text(f.read())


def write_plan(tables, path):
    if path.endswith('.txt'):
        write_plan_text(tables, path)
        return
    from com.cisco.wae.opm.network import Network
    with tempfile.TemporaryDirectory(prefix='onc_l1_') as tmp:
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
        return [{k.strip(): (v or '').strip() for k, v in row.items() if k} for row in csv.DictReader(f)]


def read_json(path):
    with open(path) as f:
        return json.load(f)


def number(value):
    return float(value) if value not in (None, '') else None


# ---------------------------------------------------------------- ONC access

class Onc:
    def __init__(self, url, username, password, verify_tls=True, ca_file=None):
        self.url = url.rstrip('/')
        token = base64.b64encode('{}:{}'.format(username, password).encode()).decode()
        self.headers = {'Authorization': 'Basic ' + token, 'Accept': 'application/json'}
        self.ctx = ssl.create_default_context(cafile=ca_file or None)
        if not verify_tls:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def get(self, path):
        req = urllib.request.Request(self.url + path, headers=self.headers)
        with urllib.request.urlopen(req, context=self.ctx, timeout=120) as resp:
            return json.load(resp)


def fetch_onc(onc):
    data = {key: onc.get(path) for key, (_, path) in ONC_FILES.items()}
    data['oms_links'] = [onc.get(OMS_PATH + f['properties']['uuid'])
                         for f in data['topology']['networkData']['features']
                         if f['properties'].get('type') == 'OMS']
    return data


def load_onc(files, search_dirs):
    """Return (ONC data, source description), reading the API or the snapshot files."""
    if files['onc_access.json']:
        cfg = read_json(files['onc_access.json'])
        ca_file = cfg.get('ca_file')
        if ca_file and not os.path.isabs(ca_file):
            ca_file = find_file(ca_file, search_dirs)
        onc = Onc(cfg['url'], cfg['username'], cfg['password'], cfg.get('verify_tls', True), ca_file)
        try:
            return fetch_onc(onc), 'ONC API ' + onc.url
        except OSError as e:
            print('WARNING ONC API {} not reachable ({}); trying snapshot files'.format(onc.url, e))
    names = [name for name, _ in ONC_FILES.values()] + [OMS_FILE]
    paths = {name: find_file(name, search_dirs) for name in names}
    missing = [name for name, path in paths.items() if not path]
    if missing:
        sys.exit('No ONC data: onc_access.json failed or is missing, and snapshot files are missing: {}'
                 .format(', '.join(missing)))
    data = {key: read_json(paths[name]) for key, (name, _) in ONC_FILES.items()}
    data['oms_links'] = read_json(paths[OMS_FILE])
    return data, 'ONC snapshot ' + os.path.dirname(paths[OMS_FILE])


def save_snapshot(outdir, url, insecure):
    user, password = os.environ.get('ONC_USERNAME'), os.environ.get('ONC_PASSWORD')
    if not user or not password:
        sys.exit('Set ONC_USERNAME and ONC_PASSWORD')
    data = fetch_onc(Onc(url, user, password, verify_tls=not insecure))
    os.makedirs(outdir, exist_ok=True)
    for key, (name, _) in list(ONC_FILES.items()) + [('oms_links', (OMS_FILE, None))]:
        with open(os.path.join(outdir, name), 'w') as f:
            json.dump(data[key], f)
        print('Saved {}'.format(os.path.join(outdir, name)))


# ---------------------------------------------------------------- ONC to L1 model

def l1_model(onc, overrides, transceivers, options):
    """Build the L1 tables from ONC data. Returns (tables dict, channels by label, warnings)."""
    warnings = []
    features = onc['topology']['networkData']['features']
    points = [f for f in features if f['geometry']['type'] == 'Point']
    node_by_dev = {f['properties']['deviceID']: f for f in points}
    ots_by_uid = {o['UID']: o for o in onc['ots_links']}
    oms_detail = {o.get('uuid') or o.get('UID') or o.get('uid'): o for o in onc['oms_links']}

    def label(dev):
        return node_by_dev[dev]['properties']['label']

    roadms = [f for f in points if f['properties'].get('siteType') == 'roadm']
    l1nodes = []
    for f in roadms:
        p = f['properties']
        lon, lat = f['geometry']['coordinates'][:2]
        l1nodes.append({'Name': p['label'], 'Site': p.get('siteName') or p['label'],
                        'IPAddress': p.get('ipAddress'), 'Vendor': 'Cisco',
                        'Description': 'ONC device {}'.format(p['deviceID']),
                        'Longitude': lon, 'Latitude': lat})
        if p.get('connectivityState') != 'connected':
            warnings.append('ONC reports ROADM {} as {}; its data may be stale'
                            .format(p['label'], p.get('connectivityState')))

    links, link_by_oms, waypoints = [], {}, []
    names = set()
    for f in features:
        p = f['properties']
        if p.get('type') != 'OMS':
            continue
        a_dev = next(d for d, n in node_by_dev.items() if n['properties']['label'] == p['srcLabel'])
        spans = [o for o in onc['ots_links'] if p['uuid'] in (o.get('omsLink') or [])]
        # Walk the OTS spans from the OMS A end to put the in-line amplifiers in order.
        chain, at, todo = [], a_dev, list(spans)
        while todo:
            span = next((s for s in todo if at in (s['srcDeviceUID'], s['dstDeviceUID'])), None)
            if span is None:
                break
            todo.remove(span)
            forward = span['srcDeviceUID'] == at
            at = span['dstDeviceUID'] if forward else span['srcDeviceUID']
            chain.append((span, forward, at))
        if todo or label(at) != p['dstLabel']:
            warnings.append('OMS {} spans do not chain from {} to {}'.format(p['uuid'], p['srcLabel'], p['dstLabel']))

        base = '{}_{}'.format(p['srcLabel'], p['dstLabel'])
        name, i = base, 2
        while name in names:
            name, i = '{}_{}'.format(base, i), i + 1
        names.add(name)

        detail = oms_detail.get(p['uuid'], {})
        fwd_p = (detail.get('forward') or {}).get('opticalParams') or {}
        rev_p = (detail.get('reverse') or {}).get('opticalParams') or {}
        distance = sum(s['length'] or 0 for s, _, _ in chain)
        loss = sum((s['forward'] if fw else s['reverse']).get('loss') or 0 for s, fw, _ in chain)
        row = {'Name': name, 'Description': 'ONC OMS {}'.format(p['uuid']),
               'L1NodeA': p['srcLabel'], 'L1NodeB': p['dstLabel'],
               'Distance': distance, 'Loss': round(loss, 3),
               'NoiseAtoB': fwd_p.get('noise'), 'NoiseSigmaAtoB': fwd_p.get('noiseSigma'),
               'NoiseBtoA': rev_p.get('noise'), 'NoiseSigmaBtoA': rev_p.get('noiseSigma')}
        for direction, key, other in (('AtoB', 'forward', 'reverse'), ('BtoA', 'reverse', 'forward')):
            no_noise = [s['label'] for s, fw, _ in chain if (s[key if fw else other] or {}).get('noise') is None]
            if no_noise and row['Noise' + direction] is not None:
                warnings.append('L1 link {} Noise{} covers only part of the OMS: ONC has no noise for {}'
                                .format(name, direction, ', '.join(no_noise)))
        o = overrides.pop(name, None)
        if o:
            for col in ('Distance', 'NoiseAtoB', 'NoiseSigmaAtoB', 'NoiseBtoA', 'NoiseSigmaBtoA'):
                if o.get(col):
                    row[col] = number(o[col])
        if not row['Distance']:
            warnings.append('L1 link {} has distance 0 in ONC; add it to l1_link_overrides.csv'.format(name))
        if row['NoiseAtoB'] is None or row['NoiseBtoA'] is None:
            warnings.append('L1 link {} has no noise in one or both directions; feasibility checks treat '
                            'it as noise-free'.format(name))
        row['Metric'] = max(1, int(round(row['Distance'] or 0)))
        links.append(row)
        link_by_oms[p['uuid']] = row
        for order, (_, _, dev) in enumerate(chain[:-1], start=1):
            lon, lat = node_by_dev[dev]['geometry']['coordinates'][:2]
            waypoints.append({'L1NodeA': row['L1NodeA'], 'L1NodeB': row['L1NodeB'], 'L1LinkName': name,
                              'Order': order, 'Longitude': lon, 'Latitude': lat,
                              'Description': label(dev)})
    for name in overrides:
        warnings.append('l1_link_overrides.csv row {} matches no L1 link'.format(name))

    circuits = onc['circuits']['networks'][0]['links']['networkLinks']['logical']['circuits']
    channels = [c for c in circuits.get('connectionCircuits', []) if c.get('protocolLayer') == 'OCH-CHANNEL']
    ports, l1circuits, paths, hops, by_label = [], [], [], [], {}
    trx = {t['InterfaceType']: t for t in transceivers}
    for c in channels:
        props = c.get('ochChannelProps') or {}
        ends = ENDPOINT_RE.findall(c.get('hierarchicalUID', ''))
        if len(ends) != 2 or any(d not in node_by_dev for d, _ in ends):
            warnings.append('ONC channel {} skipped: cannot read its add/drop ports'.format(c.get('label')))
            continue
        oms_seq = [step[0]['omsUID'] for step in props.get('omsPath') or []]
        first = ots_by_uid.get((props.get('path') or [None])[0])
        if first and ends[0][0] not in (first['srcDeviceUID'], first['dstDeviceUID']):
            ends.reverse()
        (dev_a, port_a), (dev_b, port_b) = ends
        na, nb = label(dev_a), label(dev_b)
        ports += [{'L1Node': na, 'L1Port': port_a}, {'L1Node': nb, 'L1Port': port_b}]
        signal = (c.get('srcEndPoint') or {}).get('ochEndPointProps', {}).get('opticalSignal', {})
        rate = re.match(r'R(\d+)G', signal.get('portRate') or '')
        name = c['label']
        spectrum = props.get('channelSpectrum') or {}
        tags = 'ONC_{}THz_{}GHz'.format(spectrum.get('frequency'), spectrum.get('width')) if spectrum else ''
        l1circuits.append({'Name': name, 'L1NodeA': na, 'L1PortA': port_a, 'L1NodeB': nb, 'L1PortB': port_b,
                           'BW': int(rate.group(1)) * 1000 if rate else None, 'Tags': tags})
        path = {'L1Circuit': name, 'L1NodeA': na, 'L1PortA': port_a, 'L1NodeB': nb, 'L1PortB': port_b,
                'PathOption': 1, 'Standby': False, 'Active': True, 'AutoSpectrumAssignment': True}
        cfid = None
        gran = options['granularity_ghz']
        if spectrum.get('width'):
            path['SpectralWidthID'] = int(math.ceil(spectrum['width'] / (2 * gran) - 1e-6))
        if options['spectrum'] == 'onc' and spectrum.get('frequency'):
            steps = (spectrum['frequency'] - options['anchor_thz']) * 1000 / gran
            cfid = int(math.floor(steps + 0.5))
            if abs(steps - cfid) * gran > 0.5:
                warnings.append('Channel {}: {} THz is {:.2f} GHz off the {} GHz grid; using ID {}'.format(
                    name, spectrum['frequency'], (steps - cfid) * gran, gran, cfid))
            path.update(CentralFrequencyID=cfid, AutoSpectrumAssignment=False)
        t = trx.get(signal.get('interfaceType'))
        if t and t.get('MinLineOSNR'):
            # CP compares the limit with the sum of 10^(noise/10) over the path's L1 links, plus
            # threshold x 10^(sigma/10); a threshold of 0 makes the limit a linear noise-to-signal ratio.
            path['FeasibilityLimit'] = 10 ** (-number(t['MinLineOSNR']) / 10)
            path['FeasibilityAcceptanceThreshold'] = 0
        elif transceivers:
            warnings.append('Channel {}: interface type {} not in l1_transceivers.csv; no feasibility limit'
                            .format(name, signal.get('interfaceType') or '(none)'))
        paths.append(path)
        if options['actual_hops']:
            for step, oms in enumerate(oms_seq, start=1):
                link = link_by_oms.get(oms)
                if link is None:
                    warnings.append('Channel {} uses unknown OMS {}; actual hops not written'.format(name, oms))
                    hops = [h for h in hops if h['L1Circuit'] != name]
                    break
                hops.append({'L1Circuit': name, 'L1NodeA': na, 'L1PortA': port_a, 'L1NodeB': nb,
                             'L1PortB': port_b, 'PathOption': 1, 'Step': step, 'HopL1NodeA': link['L1NodeA'],
                             'HopL1NodeB': link['L1NodeB'], 'HopL1Link': link['Name'], 'CentralFrequencyID': cfid})
        if c.get('lifeCycleState') != 'INSTALLED':
            warnings.append('Channel {} is {} in ONC'.format(name, c.get('lifeCycleState')))
        by_label[name] = l1circuits[-1]

    tables = {
        'L1Nodes': ['Name', 'Site', 'Description', 'IPAddress', 'Vendor', 'Longitude', 'Latitude'],
        'L1Links': ['Name', 'Description', 'L1NodeA', 'L1NodeB', 'Metric', 'Distance', 'Loss',
                    'NoiseAtoB', 'NoiseSigmaAtoB', 'NoiseBtoA', 'NoiseSigmaBtoA'],
        'L1LinkWaypoints': ['L1NodeA', 'L1NodeB', 'L1LinkName', 'Order', 'Longitude', 'Latitude', 'Description'],
        'L1Ports': ['L1Node', 'L1Port'],
        'L1Circuits': ['Name', 'L1NodeA', 'L1PortA', 'L1NodeB', 'L1PortB', 'BW', 'Tags'],
        'L1CircuitPaths': ['L1Circuit', 'L1NodeA', 'L1PortA', 'L1NodeB', 'L1PortB', 'PathOption', 'Standby',
                           'Active', 'CentralFrequencyID', 'SpectralWidthID', 'AutoSpectrumAssignment',
                           'FeasibilityLimit', 'FeasibilityAcceptanceThreshold'],
        'ActualL1CircuitPathHops': ['L1Circuit', 'L1NodeA', 'L1PortA', 'L1NodeB', 'L1PortB', 'PathOption',
                                    'Step', 'HopL1NodeA', 'HopL1NodeB', 'HopL1Link', 'CentralFrequencyID'],
    }
    rows = {'L1Nodes': l1nodes, 'L1Links': links, 'L1LinkWaypoints': waypoints, 'L1Ports': ports,
            'L1Circuits': l1circuits, 'L1CircuitPaths': paths, 'ActualL1CircuitPathHops': hops}
    return {name: [name, cols, rows[name]] for name, cols in tables.items()}, by_label, warnings


# ---------------------------------------------------------------- main

def add_l1_topology(src, dest, search_dirs):
    names = ['onc_access.json', 'l1_options.json'] + CSV_FILES
    files = {name: find_file(name, search_dirs) for name in names}
    for name in names:
        print('Input {}: {}'.format(name, files[name] or 'not found'))
    for required in ('l1_node_map.csv', 'l1_circuit_map.csv'):
        if not files[required]:
            sys.exit('Required input file {} not found in {}'.format(required, search_dirs))
    options = dict(DEFAULT_OPTIONS)
    if files['l1_options.json']:
        options.update(read_json(files['l1_options.json']))

    onc, source = load_onc(files, search_dirs)
    print('ONC data from {}'.format(source))
    overrides = {r['L1Link']: r for r in read_csv(files['l1_link_overrides.csv'])}
    l1, channels, warnings = l1_model(onc, overrides, read_csv(files['l1_transceivers.csv']), options)

    # Replace any existing L1 objects, as File > Import > Layer 1 does.
    tables = [t for t in read_plan(src)
              if not (t[0].startswith('L1') or t[0] in ('L3L1Links', 'ActualL1CircuitPathHops'))]
    new_l1_nodes = {r['Name'] for r in l1['L1Nodes'][2]}
    layout = table(tables, 'PlotLayoutElements')
    if layout and 'ElementL1Node' in layout[1]:
        layout[2] = [r for r in layout[2] if not r.get('ElementL1Node') or r['ElementL1Node'] in new_l1_nodes]

    # Connect each router to its ROADMs. A router with no site in the source plan goes in the site of
    # its first ROADM; a router that has a site puts its ROADMs in that site.
    node_map = {}
    for r in read_csv(files['l1_node_map.csv']):
        if r['L1Node'] not in node_map.setdefault(r['Node'], []):
            node_map[r['Node']].append(r['L1Node'])
    nodes = table(tables, 'Nodes')
    node_rows = {n['Name']: n for n in nodes[2]}
    source_site = {name: n.get('Site') for name, n in node_rows.items()}
    l1_by_name = {r['Name']: r for r in l1['L1Nodes'][2]}
    if 'Site' not in nodes[1]:
        nodes[1].append('Site')
    sites = table(tables, 'Sites')
    if sites is None:
        sites = ['Sites', ['Name', 'Longitude', 'Latitude'], []]
        tables.insert(1, sites)
    site_rows = {r['Name']: r for r in sites[2]}
    l3l1, bad_map = [], []
    for node, l1nodes in node_map.items():
        for l1node in l1nodes:
            if node not in node_rows or l1node not in l1_by_name:
                bad_map.append('{} -> {} ({})'.format(node, l1node, 'router not in plan' if node not in node_rows
                                                      else 'ROADM not in ONC'))
                continue
            if source_site.get(node):
                l1_by_name[l1node]['Site'] = source_site[node]
            elif not node_rows[node].get('Site'):
                node_rows[node]['Site'] = l1_by_name[l1node]['Site']
            l3l1.append({'Node': node, 'L1Node': l1node})
    for row in l1['L1Nodes'][2]:
        if row['Site'] not in site_rows:
            site_rows[row['Site']] = {'Name': row['Site'], 'Longitude': row['Longitude'],
                                      'Latitude': row['Latitude']}
            sites[2].append(site_rows[row['Site']])

    for name in ('L1Nodes', 'L1Links', 'L1LinkWaypoints', 'L1Ports', 'L1Circuits', 'L1CircuitPaths',
                 'ActualL1CircuitPathHops'):
        tables.append(l1[name])
    tables.append(['L3L1Links', ['Node', 'L1Node'], l3l1])

    # Map router circuits onto ONC channels.
    circuits = table(tables, 'Circuits')
    for col in L1_COLUMNS:
        if col not in circuits[1]:
            circuits[1].append(col)
    by_key = {}
    for c in circuits[2]:
        for col in L1_COLUMNS:
            c[col] = ''
        by_key[(c['NodeA'], c['InterfaceA'], c['NodeB'], c['InterfaceB'])] = c
        by_key[(c['NodeB'], c['InterfaceB'], c['NodeA'], c['InterfaceA'])] = c
    mapped, unmatched = [], []
    for m in read_csv(files['l1_circuit_map.csv']):
        circuit = by_key.get((m['NodeA'], m['InterfaceA'], m['NodeB'], m['InterfaceB']))
        ch = channels.get(m['OncChannel'])
        if circuit is None or ch is None:
            unmatched.append('{} ({})'.format(m['OncChannel'], 'no router circuit {} {} - {} {}'.format(
                m['NodeA'], m['InterfaceA'], m['NodeB'], m['InterfaceB']) if circuit is None
                else 'no ONC channel with this label'))
            continue
        same = ch['L1NodeA'] in node_map.get(circuit['NodeA'], []) or ch['L1NodeB'] in node_map.get(circuit['NodeB'], [])
        circuit['L1CircuitNodeA'] = ch['L1NodeA'] if same else ch['L1NodeB']
        circuit['L1CircuitPortA'] = ch['L1PortA'] if same else ch['L1PortB']
        circuit['L1CircuitNodeB'] = ch['L1NodeB'] if same else ch['L1NodeA']
        circuit['L1CircuitPortB'] = ch['L1PortB'] if same else ch['L1PortA']
        mapped.append(m['OncChannel'])

    write_plan(tables, dest)

    print('L1 nodes: {}, L1 links: {}, waypoints: {}, L1 circuits: {}, actual hop rows: {}, spectrum: {}'.format(
        *(len(l1[t][2]) for t in ('L1Nodes', 'L1Links', 'L1LinkWaypoints', 'L1Circuits', 'ActualL1CircuitPathHops')),
        options['spectrum']))
    print('L3L1 links: {}, router circuits mapped to L1 circuits: {}'.format(len(l3l1), len(mapped)))
    for ch in sorted(set(channels) - set(mapped)):
        print('INFO ONC channel {} carries no router circuit in the plan'.format(ch))
    for w in warnings:
        print('WARNING ' + w)
    for b in bad_map:
        print('WARNING node map row not applied: ' + b)
    for u in unmatched:
        print('WARNING circuit map row not applied: ' + u)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--save-snapshot':
        if len(sys.argv) < 4:
            sys.exit('Usage: {} --save-snapshot <dir> https://<onc-host>:8443 [--insecure]'.format(sys.argv[0]))
        save_snapshot(sys.argv[2], sys.argv[3], '--insecure' in sys.argv[4:])
        sys.exit(0)
    if len(sys.argv) < 3:
        print('Usage: {} <source_plan> <output_plan> [auth] [net_access] [home] [upload_dir] [archive]'
              .format(sys.argv[0]))
        sys.exit(1)
    upload_dir = sys.argv[6] if len(sys.argv) > 6 else None
    home_dir = sys.argv[5] if len(sys.argv) > 5 else os.path.dirname(os.path.abspath(__file__))
    add_l1_topology(sys.argv[1], sys.argv[2], [d for d in (upload_dir, home_dir) if d and os.path.isdir(d)])
