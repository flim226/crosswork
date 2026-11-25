"""Use Crosswork Hierarchical Controller (HCO) as the source of truth for the IP-to-optical mapping.

  hco_l1.py maps <hco-url> <outdir> [--insecure]
      Write l1_node_map.csv and l1_circuit_map.csv for onc_l1_collect.py from the HCO model.
  hco_l1.py validate <plan.txt> <hco-url> [--insecure]
      Compare the L1 and L3-L1 model in a plan file written by onc_l1_collect.py with HCO.

HCO stitches routers to the optical network with cross links (router optics port to ROADM add/drop
port) and builds ETH links over ONC media channels (NMC). This script reads them with SHQL queries
(POST /api/v2/shql, query in a text/plain body) and GET /api/v2/crosslinks/manual. It never changes
the HCO model. Credentials come from HCO_USERNAME and HCO_PASSWORD.

Router names are used without their DNS domain (Router-1, not Router-1.example.com), both in the
files written and when comparing with a plan.
"""
import base64
import csv
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

ONC = 'cisco-onc-adpt'


class Hco:
    def __init__(self, url, insecure=False):
        user, password = os.environ.get('HCO_USERNAME'), os.environ.get('HCO_PASSWORD')
        if not user or not password:
            sys.exit('Set HCO_USERNAME and HCO_PASSWORD')
        self.url = url.rstrip('/')
        self.auth = 'Basic ' + base64.b64encode('{}:{}'.format(user, password).encode()).decode()
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _call(self, path, body=None):
        headers = {'Authorization': self.auth, 'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = 'text/plain'
        req = urllib.request.Request(self.url + path, data=body, headers=headers,
                                     method='POST' if body is not None else 'GET')
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=300) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            sys.exit('HCO {} failed: HTTP {} {}'.format(path, e.code, e.read().decode(errors='replace')[:300]))

    def shql(self, query):
        return self._call('/api/v2/shql', query.encode())

    def get(self, path):
        return self._call(path)


def device_uuid(port_guid):
    m = re.search(r'/(?:oms|nmc)/([0-9a-f-]{36})/', port_guid or '')
    return m.group(1) if m else None


def host(router):
    """'Router-1.example.com' -> 'Router-1'"""
    return re.sub(r'(\.[A-Za-z][A-Za-z0-9-]*){2,}$', '', router)


def eth_end(port_guid):
    """'PO/cisco-cnc-adpt/ETH-Battipaglia-54:FourHundredGigE0/0/0/6' -> ('Battipaglia-54', 'FourHundredGigE0/0/0/6')"""
    m = re.search(r'/ETH-(.+?):(.+)$', port_guid or '')
    return (host(m.group(1)), m.group(2)) if m else None


def base_name(nmc_name):
    return re.sub(r'_otsimc$', '', nmc_name or '')


def router_roadm_links(hco):
    """Return [(router, ROADM label, router port, ROADM port, resolved)] from the manual cross links.

    The ROADM label is the ONC node name at the start of the ROADM port, as in 'Roma-75/4/CH-6'.
    Cross links that HCO could not resolve to an ONC port are returned with resolved=False."""
    links, other = [], []
    for x in hco.get('/api/v2/crosslinks/manual'):
        if x.get('technology') != 'NMC':
            continue
        router, opt = x['interfaceA'], x['interfaceB']
        row = (host(router['deviceName']), opt['interfaceName'].split('/')[0], router['interfaceName'], opt['interfaceName'])
        if ONC in (opt.get('guid') or ''):
            links.append(row + (True,))
        elif not opt.get('guid'):
            links.append(row + (False,))
        else:
            other.append('{} {} -> {} {}'.format(row[0], row[2], opt['deviceName'], row[3]))
    return links, other


def ip_over_nmc(hco):
    """Return HCO ETH links between routers with the ONC NMC under each one."""
    result = []
    for x in hco.shql('link[.layer="ETH" and .provider="optical-persistor"]'):
        a, b = eth_end((x.get('portA') or {}).get('guid')), eth_end((x.get('portB') or {}).get('guid'))
        under = hco.shql('link[.guid="{}"] | downward | link[.layer="NMC"]'.format(x['guid'])) if a and b else []
        result.append({'name': x['name'], 'status': x.get('operStatus'), 'a': a, 'b': b,
                       'nmc': [base_name(u['name']) for u in under if u.get('provider') == ONC],
                       'other': [u['name'] for u in under if u.get('provider') not in (ONC, 'MANUAL_CROSS_LINK')]})
    return result


def write_maps(hco, outdir):
    links, other = router_roadm_links(hco)
    os.makedirs(outdir, exist_ok=True)
    rows = sorted({(r, l1) for r, l1, _, _, _ in links})
    with open(os.path.join(outdir, 'l1_node_map.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['Node', 'L1Node'])
        w.writerows(rows)
    for r, l1, rp, op, resolved in links:
        if not resolved:
            print('WARNING cross link {} {} -> {} is not resolved in HCO; check the row {},{}'.format(r, rp, op, r, l1))
    for line in other:
        print('INFO cross link {} goes to optics not managed by ONC; not written'.format(line))
    circuits = []
    for e in ip_over_nmc(hco):
        if not (e['a'] and e['b']):
            print('INFO ETH link {} is a half link in HCO ({}); not written'.format(e['name'], e['status']))
        elif not e['nmc']:
            print('INFO ETH link {} {} - {} {} rides {}, not an ONC channel; not written'.format(
                *e['a'], *e['b'], e['other'] or 'no media channel'))
        else:
            (na, ia), (nb, ib) = sorted((e['a'], e['b']))
            circuits.append((na, ia, nb, ib, e['nmc'][0]))
    with open(os.path.join(outdir, 'l1_circuit_map.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['NodeA', 'InterfaceA', 'NodeB', 'InterfaceB', 'OncChannel'])
        w.writerows(sorted(circuits, key=lambda c: c[4]))
    print('Wrote {} rows to {}/l1_node_map.csv and {} rows to {}/l1_circuit_map.csv'.format(
        len(rows), outdir, len(circuits), outdir))


def validate(hco, plan_path):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from onc_l1_collect import parse_plan_text
    plan = {n: r for n, c, r in parse_plan_text(open(plan_path).read())}
    failed = []

    def check(name, ok, detail):
        print('{} {:<14} {}'.format('PASS' if ok else 'FAIL', name, detail))
        if not ok:
            failed.append(name)

    # ONC device ID -> plan L1 node, from the Description that onc_l1_collect.py writes.
    by_device = {re.sub(r'^ONC device ', '', r.get('Description', '')): r['Name'] for r in plan.get('L1Nodes', [])}
    nodes = hco.shql('inventory_item[.provider="{}" and .type="ONE" and .deviceType="roadm"]'.format(ONC))
    label = {}
    for n in nodes:
        found = [by_device[d] for d in (n.get('extra') or {}).get('devices') or [] if d in by_device]
        if found:
            label[n['extra']['uuid']] = found[0]
    hco_l1 = set(label.values())
    for name in sorted(set(by_device.values()) | hco_l1):
        check('L1 node', name in hco_l1 and name in by_device.values(), '{}: plan {} / HCO {}'.format(
            name, name in by_device.values(), name in hco_l1))

    def ends(link):
        return ' - '.join(sorted(label.get(device_uuid(link[p]['guid']), '?') for p in ('portA', 'portB')))

    links = hco.shql('link[.provider="{}" and .layer in ("OMS", "NMC")]'.format(ONC))
    hco_oms = {}
    for x in links:
        if x['layer'] == 'OMS':
            a, b = (label.get(device_uuid(x[p]['guid'])) for p in ('portA', 'portB'))
            if a and b and a != b:
                hco_oms.setdefault(ends(x), []).append(x['name'])
    plan_links = {}
    for r in plan.get('L1Links', []):
        plan_links.setdefault(' - '.join(sorted((r['L1NodeA'], r['L1NodeB']))), []).append(r['Name'])
    for key in sorted(set(plan_links) | set(hco_oms)):
        check('L1 link', len(plan_links.get(key, [])) == len(hco_oms.get(key, [])), '{}: plan {} / HCO {}'.format(
            key, plan_links.get(key) or '-', hco_oms.get(key) or '-'))

    plan_circ = {r['Name']: r for r in plan.get('L1Circuits', [])}
    hops = {}
    for r in sorted(plan.get('ActualL1CircuitPathHops', []), key=lambda r: int(r['Step'])):
        hops.setdefault(r['L1Circuit'], []).append(' - '.join(sorted((r['HopL1NodeA'], r['HopL1NodeB']))))
    hco_nmc = [x for x in links if x['layer'] == 'NMC']
    for x in hco_nmc:
        name, c = base_name(x['name']), plan_circ.get(base_name(x['name']))
        if c is None:
            check('L1 circuit', False, '{}: in HCO, not in the plan'.format(name))
            continue
        m = re.match(r'NMC:\s*(\S+?)/(\S+)\s+to\s+(\S+?)/(\S+)$', x.get('desc') or '')
        hco_ends = {(m.group(1), m.group(2)), (m.group(3), m.group(4))} if m else set()
        plan_ends = {(c['L1NodeA'], c['L1PortA']), (c['L1NodeB'], c['L1PortB'])}
        check('L1 circuit', hco_ends == plan_ends, '{}: ports {}'.format(name, sorted(plan_ends) if hco_ends == plan_ends
                                                                         else 'plan {} / HCO {}'.format(sorted(plan_ends), sorted(hco_ends))))
        route = sorted(ends(r) for r in hco.shql('link[.guid="{}"] | downward | link[.layer="OMS"]'.format(x['guid'])))
        check('L1 route', sorted(hops.get(name, [])) == route, '{}: plan {} / HCO {}'.format(
            name, hops.get(name) or '-', route))
    for name in sorted(set(plan_circ) - {base_name(x['name']) for x in hco_nmc}):
        check('L1 circuit', False, '{}: in the plan, not in HCO'.format(name))

    cross, other = router_roadm_links(hco)
    hco_pairs = {(r, l1) for r, l1, _, _, _ in cross}
    for r, l1, rp, op, resolved in cross:
        if not resolved:
            print('WARN Router-ROADM   {} {} -> {}: cross link not resolved in HCO; compared by port name'.format(r, rp, op))
    for line in other:
        print('INFO Router-ROADM   {}: optics not managed by ONC, not compared'.format(line))
    plan_pairs = {(host(r['Node']), r['L1Node']) for r in plan.get('L3L1Links', [])}
    plan_routers = {host(r['Name']) for r in plan.get('Nodes', [])}
    for pair in sorted(plan_pairs | hco_pairs):
        note = '' if pair[0] in plan_routers else ' (router not in the plan)'
        check('Router-ROADM', pair in plan_pairs and pair in hco_pairs, '{} -> {}: plan {} / HCO {}{}'.format(
            pair[0], pair[1], pair in plan_pairs, pair in hco_pairs, note))

    by_ports = {}
    for name, c in plan_circ.items():
        by_ports[(c['L1NodeA'], c['L1PortA'], c['L1NodeB'], c['L1PortB'])] = name
        by_ports[(c['L1NodeB'], c['L1PortB'], c['L1NodeA'], c['L1PortA'])] = name
    plan_ip, all_ip = {}, set()
    for c in plan.get('Circuits', []):
        key = frozenset(((host(c['NodeA']), c['InterfaceA']), (host(c['NodeB']), c['InterfaceB'])))
        all_ip.add(key)
        if c.get('L1CircuitNodeA'):
            plan_ip[key] = by_ports.get((c['L1CircuitNodeA'], c['L1CircuitPortA'], c['L1CircuitNodeB'],
                                         c['L1CircuitPortB']), '?')
    hco_ip = {}
    for e in ip_over_nmc(hco):
        if not (e['a'] and e['b']):
            print('INFO IP over L1     {}: half link in HCO ({}), not compared'.format(e['name'], e['status']))
        elif not e['nmc']:
            print('INFO IP over L1     {} {} - {} {}: rides {}, not an ONC channel'.format(
                *e['a'], *e['b'], e['other'] or 'no media channel'))
        else:
            hco_ip[frozenset((e['a'], e['b']))] = e['nmc'][0]
    for key in sorted(set(plan_ip) | set(hco_ip), key=sorted):
        p, q = plan_ip.get(key), hco_ip.get(key)
        link = ' - '.join('{} {}'.format(*e) for e in sorted(key))
        if key not in all_ip:
            print('WARN IP over L1     {}: HCO has an ETH link over {}, but the plan has no IP link here'.format(link, q))
            continue
        check('IP over L1', p == q, '{}: plan {} / HCO {}'.format(link, p or '-', q or '-'))

    print('{} check(s) failed'.format(len(failed)) if failed else 'All checks passed')
    return 1 if failed else 0


def main():
    args = [a for a in sys.argv[1:] if a != '--insecure']
    insecure = '--insecure' in sys.argv
    if len(args) == 3 and args[0] == 'maps':
        write_maps(Hco(args[1], insecure), args[2])
    elif len(args) == 3 and args[0] == 'validate':
        sys.exit(validate(Hco(args[2], insecure), args[1]))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
