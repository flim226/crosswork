"""Add an L1 (optical) topology from GNPy exports to a Crosswork Planning network model.

Used as a Crosswork Planning external script. The Collector framework passes positional
arguments: argv[1] source plan file, argv[2] output plan file, argv[5] home directory (files
bundled with the script), argv[6] user-upload directory (files sent through the file gateway).
Other arguments are ignored.

Input files, looked up first in the user-upload directory and then in the home directory:
  gnpy_topology.json    GNPy network topology (elements + connections)            required
  gnpy_oms_noise.json   per-OMS line noise from gnpy_oms_noise.py                 optional
  gnpy_services.json    GNPy path requests                                        optional
  gnpy_responses.json   gnpy-path-request results                                 optional
  eqpt_config.json      GNPy equipment library                                    optional
  l1_site_map.csv       L3Site,Roadm             which ROADM serves each L3 site  required
  l1_circuit_map.csv    NodeA,InterfaceA,NodeB,InterfaceB,ServiceId               required
  l1_options.json       {"extended": false, "feasibility_margin_db": 0.3}         optional
"""

import csv
import json
import os
import re
import sys

import gnpy2cp

GNPY_FILES = {
    '--topology': 'gnpy_topology.json',
    '--oms-noise': 'gnpy_oms_noise.json',
    '--services': 'gnpy_services.json',
    '--responses': 'gnpy_responses.json',
    '--equipment': 'eqpt_config.json',
}
L1_COLUMNS = ['L1CircuitNodeA', 'L1CircuitPortA', 'L1CircuitNodeB', 'L1CircuitPortB']


# ---------------------------------------------------------------- plan file I/O

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
                f.write('\t'.join(gnpy2cp.fmt(row.get(c)) for c in cols) + '\n')
            f.write('\n')


def read_plan(path):
    if not path.endswith('.txt'):
        # The OPM library converts between plan formats by file extension.
        from com.cisco.wae.opm.network import Network
        text_path = '/tmp/l1_gnpy_source.txt'
        Network(path).write(text_path)
        path = text_path
    with open(path) as f:
        return parse_plan_text(f.read())


def write_plan(tables, path):
    if path.endswith('.txt'):
        write_plan_text(tables, path)
        return
    from com.cisco.wae.opm.network import Network
    text_path = '/tmp/l1_gnpy_output.txt'
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
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


def service_key(service_id):
    return re.sub(r'\W+', '_', str(service_id)).strip('_')


# ---------------------------------------------------------------- main

def add_l1_topology(src, dest, search_dirs):
    files = {name: find_file(name, search_dirs) for name in
             list(GNPY_FILES.values()) + ['l1_site_map.csv', 'l1_circuit_map.csv', 'l1_options.json']}
    for name, path in files.items():
        print('Input {}: {}'.format(name, path or 'not found'))
    for required in ('gnpy_topology.json', 'l1_site_map.csv', 'l1_circuit_map.csv'):
        if not files[required]:
            sys.exit('Required input file {} not found in {}'.format(required, search_dirs))
    options = {}
    if files['l1_options.json']:
        with open(files['l1_options.json']) as f:
            options = json.load(f)

    # 1. Build the L1 tables from the GNPy exports.
    cli = ['-o', '/tmp/l1_gnpy_unused.txt',
           '--feasibility-margin-db', str(options.get('feasibility_margin_db', 0.3))]
    for flag, name in GNPY_FILES.items():
        if files[name]:
            cli += [flag, files[name]]
    if options.get('extended'):
        cli.append('--extended')
    l1_plan, links, services, report, _, hops, _ = gnpy2cp.build(gnpy2cp.make_parser().parse_args(cli))
    l1_tables = {name: [name, cols, rows] for name, cols, rows in l1_plan.tables}

    # 2. Replace any existing L1 objects, as File > Import > Layer 1 does.
    tables = read_plan(src)
    tables = [t for t in tables if not (t[0].startswith('L1') or t[0] == 'L3L1Links')]
    new_l1_nodes = {r['Name'] for r in l1_tables['L1Nodes'][2]}
    layout = table(tables, 'PlotLayoutElements')
    if layout and 'ElementL1Node' in layout[1]:
        layout[2] = [r for r in layout[2] if not r.get('ElementL1Node') or r['ElementL1Node'] in new_l1_nodes]

    # 3. Put each L1 node in its L3 site; ROADMs without routers get the GNPy city as site.
    site_map = {r['L3Site']: gnpy2cp.short(r['Roadm']) for r in read_csv(files['l1_site_map.csv'])}
    l1_site = {l1: site for site, l1 in site_map.items()}
    sites = table(tables, 'Sites')
    if sites is None:
        sites = ['Sites', ['Name', 'Longitude', 'Latitude'], []]
        tables.insert(1, sites)
    known_sites = {r['Name'] for r in sites[2]}
    gnpy_sites = {r['Name']: r for r in l1_tables['Sites'][2]}
    added_sites = []
    for row in l1_tables['L1Nodes'][2]:
        gnpy_site = row['Site']
        row['Site'] = l1_site.get(row['Name'], gnpy_site)
        if row['Site'] not in known_sites:
            site_row = dict(gnpy_sites[gnpy_site], Name=row['Site'])
            sites[2].append(site_row)
            known_sites.add(row['Site'])
            if row['Site'] in site_map:
                added_sites.append(row['Site'])
    for col in ('Longitude', 'Latitude'):
        if col not in sites[1]:
            sites[1].append(col)

    for name in ('L1Nodes', 'L1Links', 'L1LinkWaypoints', 'L1Ports', 'L1Circuits', 'L1CircuitPaths'):
        tables.append(l1_tables[name])
    hop_table = gnpy2cp.DEFAULT_SCHEMA['L1CircuitPathHops.table']
    if hop_table in l1_tables:
        tables.append(l1_tables[hop_table])

    # 4. Connect each L3 router to the L1 node at its site.
    nodes = table(tables, 'Nodes')[2]
    node_site = {n['Name']: n.get('Site') for n in nodes}
    l3l1 = [{'Node': n, 'L1Node': site_map[s]} for n, s in node_site.items() if s in site_map]
    no_site = sorted(n for n, s in node_site.items() if not s)
    tables.append(['L3L1Links', ['Node', 'L1Node'], l3l1])

    # 5. Map L3 circuits onto L1 circuits by service ID.
    l1_by_service = {svc['id']: row for svc, row in zip(services, l1_tables['L1Circuits'][2])}
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

    mapped, unmatched, unsited = 0, [], []
    for m in read_csv(files['l1_circuit_map.csv']):
        key = (m['NodeA'], m['InterfaceA'], m['NodeB'], m['InterfaceB'])
        circuit = by_key.get(key)
        l1 = l1_by_service.get(service_key(m['ServiceId']))
        if circuit is None or l1 is None:
            unmatched.append('{} ({})'.format(m['ServiceId'], 'no L3 circuit' if circuit is None
                                              else 'no GNPy service'))
            continue
        a_side = site_map.get(node_site.get(circuit['NodeA']))
        b_side = site_map.get(node_site.get(circuit['NodeB']))
        if a_side is None and b_side is None:
            unsited.append(m['ServiceId'])
        same = a_side == l1['L1NodeA'] if a_side is not None else b_side != l1['L1NodeA']
        circuit['L1CircuitNodeA'] = l1['L1NodeA'] if same else l1['L1NodeB']
        circuit['L1CircuitPortA'] = l1['L1PortA'] if same else l1['L1PortB']
        circuit['L1CircuitNodeB'] = l1['L1NodeB'] if same else l1['L1NodeA']
        circuit['L1CircuitPortB'] = l1['L1PortB'] if same else l1['L1PortA']
        mapped += 1

    write_plan(tables, dest)

    print('L1 nodes: {}, L1 links: {}, L1 circuits: {}, hop rows: {}'.format(
        len(l1_tables['L1Nodes'][2]), len(links), len(l1_tables['L1Circuits'][2]), len(hops)))
    print('L3L1 links: {}, L3 circuits mapped to L1: {}'.format(len(l3l1), mapped))
    if added_sites:
        print('WARNING sites in l1_site_map.csv missing from the source plan, added from GNPy: {}'
              .format(', '.join(sorted(added_sites))))
    if no_site:
        print('WARNING {} L3 nodes have no Site and are not connected to an L1 node: {}'
              .format(len(no_site), ', '.join(no_site[:10]) + (' ...' if len(no_site) > 10 else '')))
    for u in unmatched:
        print('WARNING circuit map row not applied: {}'.format(u))
    if unsited:
        print('WARNING {} L3 circuits mapped with neither end in a mapped site; their A and B ends '
              'follow the L1 circuit and may be reversed: {}'.format(len(unsited), ', '.join(unsited)))
    for r in report:
        if r['gnpy_status'] != 'OK':
            print('WARNING GNPy reports {} as {}'.format(r['circuit'], r['gnpy_status']))


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('Usage: {} <source_plan> <output_plan> [auth] [net_access] [home] [upload_dir] [archive]'
              .format(sys.argv[0]))
        sys.exit(1)
    upload_dir = sys.argv[6] if len(sys.argv) > 6 else None
    home_dir = sys.argv[5] if len(sys.argv) > 5 else os.path.dirname(os.path.abspath(__file__))
    add_l1_topology(sys.argv[1], sys.argv[2], [d for d in (upload_dir, home_dir) if d and os.path.isdir(d)])
