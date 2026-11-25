#!/usr/bin/env python3
"""Referential-integrity check for the L1 tables of a Crosswork Planning .txt plan file.

Checks that L1 links, ports, circuits, paths, hops and waypoints reference existing L1 nodes,
that every L1 port is used by at most one L1 circuit, that every L1 circuit's endpoints are
connected in the L1 link graph, and that L3 circuits mapped to L1 reference existing L1 ports.

Usage: check_cp_l1.py PLAN.txt [PLAN.txt ...]
"""
import re
import sys
from collections import defaultdict


def read_plan(path):
    tables, name, cols = defaultdict(list), None, None
    for line in open(path):
        line = line.rstrip('\n')
        m = re.match(r'^<(\w+)>$', line)
        if m:
            name, cols = m.group(1), None
            continue
        if name is None or not line.strip():
            continue
        parts = line.split('\t')
        if cols is None:
            cols = parts
        else:
            tables[name].append(dict(zip(cols, parts)))
    return tables


def check(path):
    t = read_plan(path)
    errors = []
    sites = {r['Name'] for r in t['Sites']}
    l1nodes = {r['Name'] for r in t['L1Nodes']}
    for r in t['L1Nodes']:
        if r.get('Site') and r['Site'] not in sites:
            errors.append(f'L1 node {r["Name"]}: unknown site {r["Site"]}')
    adj = defaultdict(set)
    for r in t['L1Links']:
        for k in ('L1NodeA', 'L1NodeB'):
            if r[k] not in l1nodes:
                errors.append(f'L1 link {r["Name"]}: unknown {k} {r[k]}')
        adj[r['L1NodeA']].add(r['L1NodeB'])
        adj[r['L1NodeB']].add(r['L1NodeA'])
    links = {(r['L1NodeA'], r['L1NodeB'], r['Name']) for r in t['L1Links']}
    for r in t['L1LinkWaypoints']:
        if (r['L1NodeA'], r['L1NodeB'], r['L1LinkName']) not in links:
            errors.append(f'waypoint on unknown L1 link {r["L1LinkName"]}')
    ports = set()
    for r in t['L1Ports']:
        if r['L1Node'] not in l1nodes:
            errors.append(f'L1 port {r["L1Port"]}: unknown L1 node {r["L1Node"]}')
        ports.add((r['L1Node'], r['L1Port']))
    used = defaultdict(list)

    def reachable(a, b):
        seen, todo = {a}, [a]
        while todo:
            n = todo.pop()
            for m in adj[n] - seen:
                seen.add(m)
                todo.append(m)
        return b in seen

    circuits = set()
    for r in t['L1Circuits']:
        circuits.add(r['Name'])
        for n, p in (('L1NodeA', 'L1PortA'), ('L1NodeB', 'L1PortB')):
            if (r[n], r[p]) not in ports:
                errors.append(f'L1 circuit {r["Name"]}: unknown port {r[n]}/{r[p]}')
            used[(r[n], r[p])].append(r['Name'])
        if not reachable(r['L1NodeA'], r['L1NodeB']):
            errors.append(f'L1 circuit {r["Name"]}: endpoints not connected by L1 links')
    for p, cs in used.items():
        if len(cs) > 1:
            errors.append(f'L1 port {p} used by several L1 circuits: {cs}')
    for r in t['L1CircuitPaths']:
        if r['L1Circuit'] not in circuits:
            errors.append(f'L1 circuit path for unknown circuit {r["L1Circuit"]}')
    for r in t['L1CircuitPathHops']:
        if r.get('L1Node') and r['L1Node'] not in l1nodes:
            errors.append(f'hop on unknown L1 node {r["L1Node"]}')
    l3nodes = {r['Name'] for r in t['Nodes']}
    for r in t['L3L1Links']:
        if r['Node'] not in l3nodes and l3nodes:
            errors.append(f'L3-L1 link: unknown L3 node {r["Node"]}')
        if r['L1Node'] not in l1nodes:
            errors.append(f'L3-L1 link: unknown L1 node {r["L1Node"]}')
    mapped = 0
    for r in t['Circuits']:
        if r.get('L1CircuitNodeA'):
            mapped += 1
            for n, p in (('L1CircuitNodeA', 'L1CircuitPortA'), ('L1CircuitNodeB', 'L1CircuitPortB')):
                if (r[n], r[p]) not in ports:
                    errors.append(f'L3 circuit {r.get("Name")}: unknown L1 port {r[n]}/{r[p]}')
    summary = (f'{path}: {len(l1nodes)} L1 nodes, {len(t["L1Links"])} L1 links, {len(ports)} L1 ports, '
               f'{len(circuits)} L1 circuits, {len(t["L1CircuitPaths"])} paths, '
               f'{len(t["L1CircuitPathHops"])} hops, {len(t["Circuits"])} L3 circuits ({mapped} mapped to L1)')
    return summary, errors


def main():
    rc = 0
    for path in sys.argv[1:]:
        summary, errors = check(path)
        print(summary)
        for e in errors:
            print(f'  ERROR {e}')
        print('  OK' if not errors else f'  {len(errors)} errors')
        rc |= bool(errors)
    sys.exit(rc)


if __name__ == '__main__':
    main()
