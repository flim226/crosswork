#!/usr/bin/env python3
"""Convert GNPy topology/service/result files into a Crosswork Planning plan file (.txt)
containing L1 objects, optionally with an L3 overlay mapped onto the L1 circuits.

Inputs (GNPy formats, see https://gnpy.readthedocs.io):
  --topology     GNPy topology JSON (elements + connections)                       required
  --oms-noise    output of gnpy_oms_noise.py (per-OMS length, noise, waypoints)    optional
  --services     GNPy service JSON (path-request + synchronization)                optional
  --responses    gnpy-path-request output JSON (routes, N/M, GSNR)                 optional
  --equipment    GNPy eqpt_config.json (transceiver OSNR, SI margins)              optional

Mapping (G.807 layering):
  GNPy Roadm                         -> L1Nodes (one per ROADM), Sites (city, lat/long)
  ROADM->...->ROADM chain (OMS)      -> L1Links (pair of directions), ILAs -> L1LinkWaypoints
  path-request / response            -> L1Ports (2), L1Circuits, L1CircuitPaths
  response route vs shortest Metric  -> L1CircuitPathHops (loose node hops), extended only
  transceiver site                   -> L3 node pe.<City> + L3L1Links, L3 circuit mapped to L1

Output columns are split into:
  core      columns proven by the reference plan us_wan_L1.txt (always written)
  extended  columns documented for WAE/CP L1 objects but whose .txt spellings are not yet
            verified against a CP export (written with --extended; override with --schema)

Only the Python standard library is needed.
"""
import argparse
import csv
import heapq
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

FIBER_DELAY_MS_PER_KM = 0.0049
LINE_TYPES = {'Fiber', 'RamanFiber', 'Edfa', 'Fused', 'Multiband_amplifier'}

DEFAULT_SCHEMA = {
    'L1Links.Distance': 'Distance',
    'L1Links.NoiseAtoB': 'NoiseAtoB',
    'L1Links.NoiseBtoA': 'NoiseBtoA',
    'L1Links.NoiseSigmaAtoB': 'NoiseSigmaAtoB',
    'L1Links.NoiseSigmaBtoA': 'NoiseSigmaBtoA',
    'L1CircuitPaths.FeasibilityLimit': 'FeasibilityLimit',
    'L1CircuitPaths.CentralFrequencyID': 'CentralFrequencyID',
    'L1CircuitPaths.SpectralWidthID': 'SpectralWidthID',
    'L1CircuitPathHops.table': 'L1CircuitPathHops',
    'L1CircuitPathHops.columns': ['L1Circuit', 'L1NodeA', 'L1PortA', 'L1NodeB', 'L1PortB',
                                  'PathOption', 'Step', 'L1Node', 'Type'],
    'L1CircuitPathHops.loose': 'Loose',
}


def short(uid):
    """'roadm Lannion_CAS' -> 'Lannion_CAS'; also strip characters CP dislikes in names."""
    name = re.sub(r'^(roadm|trx)\s+', '', uid)
    return re.sub(r'\s+', '_', name)


def fmt(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'T' if v else 'F'
    if isinstance(v, float):
        return f'{v:.6g}' if abs(v) < 1e6 else f'{v:.1f}'
    return str(v)


class Plan:
    def __init__(self):
        self.tables = []

    def add(self, name, columns, rows):
        self.tables.append((name, columns, rows))

    def write(self, path):
        with open(path, 'w', newline='\n') as f:
            for name, cols, rows in self.tables:
                f.write(f'<{name}>\n')
                f.write('\t'.join(cols) + '\n')
                for r in rows:
                    f.write('\t'.join(fmt(r.get(c)) for c in cols) + '\n')
                f.write('\n')


# ---------------------------------------------------------------- GNPy topology

def load_topology(path):
    d = json.loads(Path(path).read_text())
    els = {e['uid']: e for e in d['elements']}
    succ = defaultdict(list)
    for c in d['connections']:
        succ[c['from_node']].append(c['to_node'])
    return els, succ


def fiber_km(el):
    p = el.get('params', {})
    length = float(p.get('length', 0))
    return length / 1000 if p.get('length_units', 'km') == 'm' else length


def loc(el):
    l = (el.get('metadata') or {}).get('location') or {}
    if 'latitude' in l and 'longitude' in l:
        return float(l['latitude']), float(l['longitude'])
    return None


def walk_oms(els, succ):
    """Return directional OMS: (roadm_a, roadm_b, [element uids])."""
    oms = []
    for r, el in els.items():
        if el['type'] != 'Roadm':
            continue
        for first in succ[r]:
            if els[first]['type'] not in LINE_TYPES:
                continue
            chain, node = [first], first
            while True:
                nxt = succ[node]
                if len(nxt) != 1:
                    raise SystemExit(f'OMS from {r} branches at {node}')
                node = nxt[0]
                if els[node]['type'] == 'Roadm':
                    oms.append((r, node, chain))
                    break
                if els[node]['type'] not in LINE_TYPES:
                    chain = None
                    break
                chain.append(node)
    return oms


# ---------------------------------------------------------------- helpers

def dijkstra(adj, src, dst):
    dist, prev, pq = {src: 0.0}, {}, [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == dst:
            break
        if d > dist.get(u, math.inf):
            continue
        for v, w, link in adj[u]:
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v], prev[v] = nd, (u, link)
                heapq.heappush(pq, (nd, v))
    if dst not in dist:
        return None
    path, n = [dst], dst
    while n != src:
        n = prev[n][0]
        path.append(n)
    return path[::-1]


def trx_requirements(eqpt, trx_type, trx_mode):
    """Return (required OSNR @0.1nm dB, tx OSNR dB, baud rate GBaud) from eqpt_config."""
    if not eqpt:
        return None
    for t in eqpt.get('Transceiver', []):
        if t.get('type_variety') != trx_type:
            continue
        for m in t.get('mode', []):
            if m.get('format') == trx_mode:
                return m.get('OSNR'), m.get('tx_osnr'), m.get('baud_rate', 0) / 1e9
    return None


def snr_from_metrics(metrics, name):
    for m in metrics:
        if m.get('metric-type') == name:
            try:
                return float(m['accumulative-value'])
            except (TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------- services

def services_from_responses(responses, requests_by_id, els):
    svcs = []
    for r in responses['gnpy-path-computation:responses']['response']:
        rid = str(r['response-id'])
        blocked = None
        props = r.get('path-properties')
        if 'no-path' in r:
            blocked = r['no-path'].get('no-path')
            props = r['no-path'].get('path-properties')
            if not props:
                print(f'warning: request {rid} has no route in GNPy results ({blocked}), skipped',
                      file=sys.stderr)
                continue
        route, nm, trx = [], None, (None, None)
        for o in props['path-route-objects']:
            pro = o['path-route-object']
            if 'num-unnum-hop' in pro:
                uid = pro['num-unnum-hop']['node-id']
                if els.get(uid, {}).get('type') == 'Roadm':
                    route.append(uid)
            elif 'label-hop' in pro and nm is None:
                lh = pro['label-hop'][0] if isinstance(pro['label-hop'], list) else pro['label-hop']
                nm = (lh.get('N'), lh.get('M'))
            elif 'transponder' in pro:
                trx = (pro['transponder'].get('transponder-type'), pro['transponder'].get('transponder-mode'))
        first_id = rid.split('|')[0].strip()
        req = requests_by_id.get(first_id, {})
        bw = snr_from_metrics(props['path-metric'], 'path_bandwidth')
        if bw is None:
            bw = req.get('path-constraints', {}).get('te-bandwidth', {}).get('path_bandwidth', 100e9)
        svcs.append({
            'id': re.sub(r'\W+', '_', rid).strip('_'),
            'src_roadm': route[0], 'dst_roadm': route[-1], 'route': route,
            'N': nm[0] if nm else None, 'M': nm[1] if nm else None,
            'trx_type': trx[0], 'trx_mode': trx[1],
            'gsnr_01nm': snr_from_metrics(props['path-metric'], 'SNR-0.1nm'),
            'bandwidth_gbps': bw / 1e9, 'blocked': blocked,
        })
    return svcs


def roadm_of_trx(trx_uid, els, succ):
    for n in succ[trx_uid]:
        if els[n]['type'] == 'Roadm':
            return n
    raise SystemExit(f'transceiver {trx_uid} is not attached to a ROADM')


def services_from_requests(requests, els, succ):
    svcs = []
    for req in requests:
        te = req.get('path-constraints', {}).get('te-bandwidth', {})
        svcs.append({
            'id': str(req['request-id']),
            'src_roadm': roadm_of_trx(req['source'], els, succ),
            'dst_roadm': roadm_of_trx(req['destination'], els, succ),
            'route': None, 'N': None, 'M': None,
            'trx_type': te.get('trx_type'), 'trx_mode': te.get('trx_mode'),
            'gsnr_01nm': None, 'bandwidth_gbps': (te.get('path_bandwidth') or 100e9) / 1e9,
        })
    return svcs


def services_adjacent(links):
    return [{'id': str(i + 1), 'src_roadm': l['uid_a'], 'dst_roadm': l['uid_b'], 'route': None,
             'N': None, 'M': None, 'trx_type': None, 'trx_mode': None, 'gsnr_01nm': None,
             'bandwidth_gbps': 100.0} for i, l in enumerate(links)]


# ---------------------------------------------------------------- main build

def build(args):
    schema = dict(DEFAULT_SCHEMA)
    if args.schema:
        schema.update(json.loads(Path(args.schema).read_text()))
    els, succ = load_topology(args.topology)
    eqpt = json.loads(Path(args.equipment).read_text()) if args.equipment else None
    noise = json.loads(Path(args.oms_noise).read_text()) if args.oms_noise else None
    noise_by_dir = {}
    if noise:
        ref_baud = noise['reference_channel']['baud_rate_gbaud']
        bw_to_01nm = 10 * math.log10(ref_baud / 12.5)
        for o in noise['oms']:
            noise_by_dir.setdefault((o['from_roadm'], o['to_roadm']), []).append(o)

    roadms = sorted(u for u, e in els.items() if e['type'] == 'Roadm')
    l1name = {u: short(u) for u in roadms}
    site_of = {}
    sites = {}
    for u in roadms:
        city = (els[u].get('metadata') or {}).get('location', {}).get('city') or short(u)
        city = short(city)
        site_of[u] = city
        ll = loc(els[u])
        sites[city] = {'Name': city, 'Latitude': ll[0] if ll else None, 'Longitude': ll[1] if ll else None}

    # ---- L1 links: pair the two directions of each OMS
    dirs = defaultdict(list)
    for a, b, chain in walk_oms(els, succ):
        dirs[(a, b)].append(chain)
    links, done = [], set()
    for (a, b), chains in sorted(dirs.items()):
        if (a, b) in done:
            continue
        rev = dirs.get((b, a), [])
        done.update({(a, b), (b, a)})
        if l1name[a] > l1name[b]:
            a, b, chains, rev = b, a, rev, chains
        for i, chain in enumerate(chains):
            rchain = rev[i] if i < len(rev) else None
            base = f'{l1name[a]}-{l1name[b]}' + (f'-{i + 1}' if len(chains) > 1 else '')
            km = sum(fiber_km(els[e]) for e in chain if els[e]['type'] in ('Fiber', 'RamanFiber'))
            nab = noise_by_dir.get((a, b), [None] * (i + 1))[i] if noise else None
            nba = noise_by_dir.get((b, a), [None] * (i + 1))[i] if noise else None
            if nab:
                km = nab['length_km']
                wps = [(w['latitude'], w['longitude']) for w in nab['waypoints']]
            else:
                wps = [loc(els[e]) for e in chain if els[e]['type'] in ('Edfa', 'Fused') and loc(els[e])]
            endpoints = {loc(els[a]), loc(els[b])}
            wps = [w for j, w in enumerate(wps) if w not in endpoints and w not in wps[:j]]
            links.append({
                'Name': base, 'L1NodeA': l1name[a], 'L1NodeB': l1name[b],
                'uid_a': a, 'uid_b': b,
                'km': km, 'Metric': round(km, 1), 'Delay': round(km * FIBER_DELAY_MS_PER_KM, 3),
                'NoiseAtoB': round(nab['line_nsr_db_mean'] - bw_to_01nm, 2) if nab else None,
                'NoiseBtoA': round(nba['line_nsr_db_mean'] - bw_to_01nm, 2) if nba else None,
                'NoiseSigmaAtoB': nab['line_nsr_db_sigma'] if nab else None,
                'NoiseSigmaBtoA': nba['line_nsr_db_sigma'] if nba else None,
                'waypoints': wps, 'bidirectional': rchain is not None,
            })
    max_paths = args.max_l1_circuit_paths
    if max_paths is None and eqpt:
        si = eqpt['SI'][0]
        max_paths = int((si['f_max'] - si['f_min']) / si['spacing']) + 1
    max_paths = max_paths or 96

    adj = defaultdict(list)
    link_by_pair = {}
    for l in links:
        adj[l['uid_a']].append((l['uid_b'], l['km'], l))
        adj[l['uid_b']].append((l['uid_a'], l['km'], l))
        link_by_pair.setdefault(frozenset((l['uid_a'], l['uid_b'])), l)

    # ---- services -> L1 circuits
    requests = json.loads(Path(args.services).read_text())['path-request'] if args.services else []
    req_by_id = {str(r['request-id']): r for r in requests}
    if args.responses:
        svcs = services_from_responses(json.loads(Path(args.responses).read_text()), req_by_id, els)
    elif requests:
        svcs = services_from_requests(requests, els, succ)
    else:
        svcs = services_adjacent(links)

    roadm_add_drop = math.inf
    if eqpt:
        default_roadm = next((r for r in eqpt.get('Roadm', [])
                              if r.get('type_variety', 'default') == 'default'), {})
        roadm_add_drop = default_roadm.get('add_drop_osnr', math.inf)

    l1ports, l1circuits, l1paths, hops, spectrum, report = [], [], [], [], [], []
    l3nodes, l3l1, l3circuits, l3ifaces = {}, set(), [], []
    for s in svcs:
        a, b = s['src_roadm'], s['dst_roadm']
        na, nb = l1name[a], l1name[b]
        cname = f'{na}-{nb}-{s["id"]}'
        pa, pb = f'{na}-{nb}-{s["id"]}', f'{nb}-{na}-{s["id"]}'
        l1ports += [{'L1Node': na, 'L1Port': pa}, {'L1Node': nb, 'L1Port': pb}]
        l1circuits.append({'Name': cname, 'L1NodeA': na, 'L1PortA': pa, 'L1NodeB': nb, 'L1PortB': pb})

        shortest = dijkstra(adj, a, b)
        route = s['route'] or shortest
        needs_hops = s['route'] is not None and shortest != s['route']
        req_osnr = trx_requirements(eqpt, s['trx_type'], s['trx_mode']) if s['trx_mode'] else None
        margin = eqpt['SI'][0].get('sys_margins', 0) if eqpt else 0
        feas_limit = None
        if req_osnr and req_osnr[0] is not None:
            # line noise budget = required OSNR (+ system and non-additivity margins) minus the
            # transmitter and ROADM add/drop contributions, which CP link noise does not carry
            need = 10 ** (-(req_osnr[0] + margin + args.feasibility_margin_db) / 10)
            fixed = (10 ** (-req_osnr[1] / 10) if req_osnr[1] else 0) + 2 * 10 ** (-roadm_add_drop / 10)
            feas_limit = round(10 * math.log10(need - fixed), 2) if need > fixed else None

        path_row = {'L1Circuit': cname, 'L1NodeA': na, 'L1PortA': pa, 'L1NodeB': nb, 'L1PortB': pb,
                    'PathOption': 1, 'Standby': True, 'Active': True, 'AutoLambda': False,
                    'FeasibilityLimit': feas_limit, 'CentralFrequencyID': s['N'], 'SpectralWidthID': s['M']}
        l1paths.append(path_row)
        if needs_hops:
            for step, uid in enumerate(route[1:-1], start=1):
                hops.append({'L1Circuit': cname, 'L1NodeA': na, 'L1PortA': pa, 'L1NodeB': nb,
                             'L1PortB': pb, 'PathOption': 1, 'Step': step, 'L1Node': l1name[uid],
                             'Type': schema['L1CircuitPathHops.loose']})

        # CP-style feasibility estimate along the route (linear sum of per-link line noise)
        line_nsr, route_km = 0.0, 0.0
        for u, v in zip(route, route[1:]):
            l = link_by_pair[frozenset((u, v))]
            route_km += l['km']
            n = l['NoiseAtoB'] if u == l['uid_a'] else l['NoiseBtoA']
            line_nsr = line_nsr + 10 ** (n / 10) if (n is not None and line_nsr is not None) else None
        cp_line_gsnr = round(-10 * math.log10(line_nsr), 2) if line_nsr else None
        if s['N'] is not None:
            spectrum.append({'L1Circuit': cname, 'N': s['N'], 'M': s['M'],
                             'center_THz': round(193.1 + s['N'] * 0.00625, 5),
                             'width_GHz': s['M'] * 12.5})
        report.append({'circuit': cname, 'gnpy_route': [l1name[x] for x in route],
                       'cp_shortest': [l1name[x] for x in shortest] if shortest else None,
                       'needs_hops': needs_hops, 'km': round(route_km, 1),
                       'trx': f'{s["trx_type"]} / {s["trx_mode"]}' if s['trx_type'] else '',
                       'gnpy_gsnr_01nm': s['gsnr_01nm'], 'cp_line_gsnr_01nm': cp_line_gsnr,
                       'req_osnr': (req_osnr[0] + margin) if req_osnr and req_osnr[0] is not None else None,
                       'feas_limit': feas_limit, 'gnpy_status': s.get('blocked') or 'OK',
                       'cp_feasible': (line_nsr is not None and feas_limit is not None
                                       and 10 * math.log10(line_nsr) < feas_limit)})

        if args.with_l3:
            ra, rb = f'pe.{na}', f'pe.{nb}'
            for r, rsite, l1 in ((ra, site_of[a], na), (rb, site_of[b], nb)):
                l3nodes[r] = {'Name': r, 'Site': rsite, 'Function': 'core'}
                l3l1.add((r, l1))
            ia, ib = f'to_{rb}.{s["id"]}', f'to_{ra}.{s["id"]}'
            igp = max(1, round(route_km / 10))
            l3ifaces += [{'Node': ra, 'Interface': ia, 'IGPMetric': igp},
                         {'Node': rb, 'Interface': ib, 'IGPMetric': igp}]
            l3circuits.append({'Name': f'{ra}-{rb}.{s["id"]}', 'NodeA': ra, 'InterfaceA': ia,
                               'NodeB': rb, 'InterfaceB': ib,
                               'Capacity': round(s['bandwidth_gbps'] * 1000, 1),
                               'L1CircuitNodeA': na, 'L1CircuitPortA': pa,
                               'L1CircuitNodeB': nb, 'L1CircuitPortB': pb})

    # ---- assemble plan
    ext = args.extended
    plan = Plan()
    plan.add('Network', ['Property', 'Value'],
             [{'Property': 'Title', 'Value': args.title}, {'Property': 'Version', 'Value': '7.0'}])
    plan.add('Sites', ['Name', 'Longitude', 'Latitude'], sorted(sites.values(), key=lambda r: r['Name']))
    if args.with_l3:
        plan.add('Nodes', ['Name', 'Site', 'Function'], sorted(l3nodes.values(), key=lambda r: r['Name']))
        plan.add('L3L1Links', ['Node', 'L1Node'], [{'Node': n, 'L1Node': l} for n, l in sorted(l3l1)])
        plan.add('Circuits', ['Name', 'NodeA', 'InterfaceA', 'NodeB', 'InterfaceB', 'Capacity',
                              'L1CircuitNodeA', 'L1CircuitPortA', 'L1CircuitNodeB', 'L1CircuitPortB'],
                 l3circuits)
        plan.add('Interfaces', ['Node', 'Interface', 'IGPMetric'], l3ifaces)

    link_cols = ['Name', 'L1NodeA', 'L1NodeB', 'Metric', 'Delay', 'MaxL1CircuitPaths']
    ext_link = ['L1Links.Distance', 'L1Links.NoiseAtoB', 'L1Links.NoiseBtoA',
                'L1Links.NoiseSigmaAtoB', 'L1Links.NoiseSigmaBtoA']
    link_rows = []
    for l in links:
        row = {k: l[k] for k in link_cols if k in l}
        row['MaxL1CircuitPaths'] = max_paths
        if ext:
            row[schema['L1Links.Distance']] = round(l['km'], 3)
            for key in ext_link[1:]:
                row[schema[key]] = l[key.split('.')[1]]
        link_rows.append(row)
    if ext:
        link_cols = link_cols + [schema[k] for k in ext_link if noise or k == 'L1Links.Distance']
    plan.add('L1Links', link_cols, link_rows)

    wp_rows = []
    for l in links:
        for order, (lat, lon) in enumerate(l['waypoints'], start=1):
            wp_rows.append({'L1NodeA': l['L1NodeA'], 'L1NodeB': l['L1NodeB'], 'L1LinkName': l['Name'],
                            'Order': order, 'Longitude': lon, 'Latitude': lat})
    plan.add('L1LinkWaypoints', ['L1NodeA', 'L1NodeB', 'L1LinkName', 'Order', 'Longitude', 'Latitude'],
             wp_rows)
    plan.add('L1Nodes', ['Name', 'Site'], [{'Name': l1name[u], 'Site': site_of[u]} for u in roadms])
    plan.add('L1Ports', ['L1Node', 'L1Port'], l1ports)
    plan.add('L1Circuits', ['Name', 'L1NodeA', 'L1PortA', 'L1NodeB', 'L1PortB'], l1circuits)

    path_cols = ['L1Circuit', 'L1NodeA', 'L1PortA', 'L1NodeB', 'L1PortB', 'PathOption', 'Standby',
                 'Active', 'AutoLambda']
    if ext:
        extra = ['FeasibilityLimit'] + (['CentralFrequencyID', 'SpectralWidthID'] if args.spectrum else [])
        for r in l1paths:
            for k in extra:
                r[schema[f'L1CircuitPaths.{k}']] = r.pop(k)
        path_cols += [schema[f'L1CircuitPaths.{k}'] for k in extra]
    plan.add('L1CircuitPaths', path_cols, l1paths)
    if ext and hops:
        cols = schema['L1CircuitPathHops.columns']
        plan.add(schema['L1CircuitPathHops.table'], cols, hops)

    if args.with_l3 and args.demands_mbps:
        pes = sorted(l3nodes)
        dem = [{'Name': f'{s}_{d}', 'Source': s, 'Destination': d, 'ServiceClass': 'Default',
                'TrafficLevel': 'Default', 'Traffic': args.demands_mbps}
               for s in pes for d in pes if s != d]
        plan.add('ServiceClasses', ['Name'], [{'Name': 'Default'}])
        plan.add('Demands', ['Name', 'Source', 'Destination', 'ServiceClass'], dem)
        plan.add('TrafficLevels', ['Name'], [{'Name': 'Default'}])
        plan.add('DemandTraffic', ['Name', 'Source', 'Destination', 'ServiceClass', 'TrafficLevel',
                                   'Traffic'], dem)
    plan.add('PlotLayouts', ['Name', 'PlotLayoutType'], [{'Name': 'Geo', 'PlotLayoutType': 'Design'}])
    plan.add('PlotLayoutOptions', ['PlotLayoutName', 'PlotLayoutType', 'Property', 'Value'],
             [{'PlotLayoutName': 'Geo', 'PlotLayoutType': 'Common', 'Property': p, 'Value': v}
              for p, v in (('CanvasType', 'Geographic'), ('ShowL1inL3', 'T'), ('ShowL3inL1', 'T'))])
    return plan, links, svcs, report, spectrum, hops, max_paths


def write_report(path, args, links, report, spectrum, hops, max_paths):
    out = [f'# GNPy to Crosswork Planning conversion report', '',
           f'- Topology: `{args.topology}`', f'- Output plan: `{args.output}`'
           + (' (extended columns)' if args.extended else ' (core columns only)'),
           f'- L1 links: {len(links)}, L1 circuits: {len(report)}, MaxL1CircuitPaths: {max_paths}', '']
    out += ['## L1 links', '', '| L1 link | km | Metric | Delay ms | NoiseAtoB dB | NoiseBtoA dB | waypoints |',
            '|---|---:|---:|---:|---:|---:|---:|']
    for l in links:
        out.append(f'| {l["Name"]} | {l["km"]:.1f} | {l["Metric"]} | {l["Delay"]} | {fmt(l["NoiseAtoB"])} '
                   f'| {fmt(l["NoiseBtoA"])} | {len(l["waypoints"])} |')
    out += ['', '## L1 circuits: GNPy versus CP model', '',
            '| L1 circuit | GNPy route | CP shortest by Metric | hops needed | km | transceiver | GNPy GSNR@0.1nm '
            '| CP line-only GSNR@0.1nm | gap dB | required OSNR+sys margin | FeasibilityLimit | GNPy status '
            '| CP model feasible |',
            '|---|---|---|---|---:|---|---:|---:|---:|---:|---:|---|---|']
    agree = 0
    for r in report:
        gap = (round(r['cp_line_gsnr_01nm'] - r['gnpy_gsnr_01nm'], 2)
               if r['cp_line_gsnr_01nm'] is not None and r['gnpy_gsnr_01nm'] is not None else None)
        agree += (r['gnpy_status'] == 'OK') == r['cp_feasible']
        out.append(f'| {r["circuit"]} | {" > ".join(r["gnpy_route"])} | '
                   f'{" > ".join(r["cp_shortest"]) if r["cp_shortest"] else "unreachable"} | '
                   f'{"yes" if r["needs_hops"] else "no"} | {r["km"]} | {r["trx"]} | {fmt(r["gnpy_gsnr_01nm"])} | '
                   f'{fmt(r["cp_line_gsnr_01nm"])} | {fmt(gap)} | {fmt(r["req_osnr"])} | {fmt(r["feas_limit"])} | '
                   f'{r["gnpy_status"]} | {"yes" if r["cp_feasible"] else "no"} |')
    out += ['', f'Feasibility verdict agreement (GNPy versus CP additive model, '
                f'non-additivity margin {args.feasibility_margin_db} dB): {agree} of {len(report)} circuits.']
    if hops:
        out += ['', '## L1 circuit path hops to enter (GUI fallback)', '',
                'If the extended hop table is not accepted, add these loose node hops in the L1 circuit '
                'path Edit page, Hops tab (Starts from = A):', '']
        by = defaultdict(list)
        for h in hops:
            by[h['L1Circuit']].append(h['L1Node'])
        for c, ns in by.items():
            out.append(f'- `{c}` path option 1: ' + ', '.join(f'{n} (Loose)' for n in ns))
    if spectrum:
        out += ['', '## GNPy spectrum assignment', '',
                'N and M are ITU-T G.694.1 flexible-grid values (centre = 193.1 THz + N x 6.25 GHz, '
                'width = M x 12.5 GHz). With a CP spectrum grid anchored at 193.1 THz and 6.25 GHz '
                'granularity they equal CentralFrequencyID and SpectralWidthID.', '',
                '| L1 circuit | N | M | centre THz | width GHz |', '|---|---:|---:|---:|---:|']
        for s in spectrum:
            out.append(f'| {s["L1Circuit"]} | {s["N"]} | {s["M"]} | {s["center_THz"]} | {s["width_GHz"]} |')
    Path(path).write_text('\n'.join(out) + '\n')


def make_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--topology', required=True)
    ap.add_argument('--oms-noise')
    ap.add_argument('--services')
    ap.add_argument('--responses')
    ap.add_argument('--equipment')
    ap.add_argument('-o', '--output', required=True)
    ap.add_argument('--report', help='markdown validation report')
    ap.add_argument('--spectrum-csv', help='write GNPy N/M spectrum assignment per L1 circuit')
    ap.add_argument('--title', default='GNPy import')
    ap.add_argument('--extended', action='store_true',
                    help='add Distance, Noise*, FeasibilityLimit columns and the hop table')
    ap.add_argument('--spectrum', action='store_true',
                    help='with --extended, also write CentralFrequencyID/SpectralWidthID from GNPy N/M')
    ap.add_argument('--schema', help='JSON overriding extended column/table names')
    ap.add_argument('--with-l3', action='store_true', help='add PE routers and L3 circuits mapped to L1')
    ap.add_argument('--demands-mbps', type=float, default=0,
                    help='with --with-l3, add a full mesh of demands of this size')
    ap.add_argument('--max-l1-circuit-paths', type=int)
    ap.add_argument('--feasibility-margin-db', type=float, default=0.3,
                    help='extra OSNR margin for noise that does not add linearly per OMS '
                         '(ROADM power equalisation of accumulated noise); default 0.3 dB')
    return ap


def main():
    args = make_parser().parse_args()
    plan, links, svcs, report, spectrum, hops, max_paths = build(args)
    plan.write(args.output)
    if args.report:
        write_report(args.report, args, links, report, spectrum, hops, max_paths)
    if args.spectrum_csv and spectrum:
        with open(args.spectrum_csv, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(spectrum[0]))
            w.writeheader()
            w.writerows(spectrum)
    print(f'wrote {args.output}: {len(links)} L1 links, {len(svcs)} L1 circuits, {len(hops)} hop rows',
          file=sys.stderr)


if __name__ == '__main__':
    main()
