"""Check an L1 plan in Crosswork Planning: run as a Design batch job (USER_SCRIPT).

Loads the plan with the OPM library, runs a route simulation, and prints for every L1 circuit path
its simulated route, central frequency ID, feasibility metric and limit, plus the controller route stored
as actual hops and the router circuits mapped to each L1 circuit. Optionally tests TCP reachability
and simulates the failure of L1 nodes.
OPM 8.0 omits the route options when it asks for L1 route records, so this script calls the RPC
simulation directly.

Usage in the job options: verify_l1_plan.py <plan> [host:port ...] [fail=<L1 node> ...]
"""
import socket
import sys

from com.cisco.wae.opm.network import Network
from com.cisco.wae.opm.network.simulation.route import RouteSimulation


def main():
    fail = [a[5:] for a in sys.argv[2:] if a.startswith('fail=')]
    for target in [a for a in sys.argv[2:] if not a.startswith('fail=')]:
        host, port = target.rsplit(':', 1)
        try:
            socket.create_connection((host, int(port)), timeout=10).close()
            print('REACH {} OK'.format(target))
        except OSError as e:
            print('REACH {} FAILED {}'.format(target, e))

    net = Network(sys.argv[1])
    model = net.model
    print('L1 nodes {}, L1 links {}, L1 circuits {}, L3-L1 links {}'.format(
        len(list(model.l1_nodes)), len(list(model.l1_links)), len(list(model.l1_circuits)),
        len(list(model.l3_l1_links))))
    actual = {}
    for a in model.actual_l1_circuit_paths:
        actual[str(a).replace('act_', '')] = [h.hop.name for h in a.hops]
    failures = [n for n in model.l1_nodes if n.name in fail]
    if fail:
        print('Failed L1 nodes: {}'.format([n.name for n in failures] or 'none found'))
    sim = RouteSimulation(model, failures=failures)
    unrouted = set()
    for p in model.l1_circuit_paths:
        r = sim.rpc_simulation.getL1CircuitPathRouteRecord(p._rpc_object, sim._rpc_route_options)
        route = [h.hopLink.name for h in r.hopRecs]
        if not route:
            unrouted.add(p.l1_circuit.name)
        limit = p.feasibility_limit
        metric = r.feasibilityMetricSim
        feasible = 'NA' if limit is None or not route else str(metric <= limit)
        print('L1 circuit {}\n  route      {}\n  ctrl route {}\n  frequency ID {}  width ID {}  spectrum blocking {}\n'
              '  feasibility metric {}  limit {}  feasible {}'.format(
                  p.l1_circuit.name, route or 'NOT ROUTED', actual.get(str(p), []), r.centralFrequencyIDSim,
                  p.spectral_width_id, r.spectrumBlocking, metric, limit, feasible))
    for c in model.circuits:
        if getattr(c, 'l1_circuit', None) is not None:
            print('Router circuit {} rides {}{}'.format(
                c, c.l1_circuit.name, ' (L1 circuit not routed)' if c.l1_circuit.name in unrouted else ''))


if __name__ == '__main__':
    main()
