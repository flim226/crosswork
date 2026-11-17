#!/usr/bin/env python3
"""Design a GNPy topology and compute per-OMS line noise for Crosswork Planning L1 links.

For every directional OMS (ROADM egress degree -> next ROADM ingress), propagate the GNPy
reference channel and report the line-only noise-to-signal ratio (ASE + NLI added between
the two ROADMs, relative to signal). Expressed in dB this is negative (for example -25 dB
means a line GSNR of 25 dB) and is additive in linear units across OMSs, which is how it is
used as a Crosswork Planning L1 link NoiseAtoB / NoiseBtoA value.

Requires GNPy (Python 3.11-3.12): pip install gnpy  (or pip install -e <oopt-gnpy clone>)

Usage:
  gnpy_oms_noise.py TOPOLOGY.json -e eqpt_config.json -o oms_noise.json \
      [--designed-topology designed.json]
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

from gnpy.core import elements
from gnpy.core.info import create_input_spectral_information
from gnpy.tools.json_io import load_equipment, load_network, save_network
from gnpy.tools.worker_utils import designed_network
from gnpy.topology.request import filter_si
from gnpy.core.parameters import SimParams

LINE_TYPES = (elements.Fiber, elements.RamanFiber, elements.Edfa, elements.Fused)
if hasattr(elements, 'Multiband_amplifier'):
    LINE_TYPES = LINE_TYPES + (elements.Multiband_amplifier,)


def lin2db(x):
    return 10 * math.log10(x)


def walk_oms(network, roadm, first):
    """Follow line elements from a ROADM egress element until the next ROADM."""
    chain = [first]
    node = first
    while True:
        nxt = [n for n in network.successors(node)]
        if len(nxt) != 1:
            raise ValueError(f'OMS from {roadm.uid} is not a simple chain at {node.uid}')
        node = nxt[0]
        if isinstance(node, elements.Roadm):
            return chain, node
        if not isinstance(node, LINE_TYPES):
            return None, None
        chain.append(node)


def location(el):
    loc = getattr(el, 'lat', None), getattr(el, 'lng', None)
    if loc[0] is None:
        m = getattr(el, 'metadata', {}) or {}
        l = m.get('location')
        if l is None:
            return None
        loc = (l.latitude, l.longitude)
    return {'latitude': float(loc[0]), 'longitude': float(loc[1])}


def local_trx(network, roadm):
    for n in list(network.predecessors(roadm)) + list(network.successors(roadm)):
        if isinstance(n, elements.Transceiver):
            return n
    return None


def oms_line_nsr(network, equipment, req, roadm_a, chain, roadm_b):
    """Propagate the reference channel over trxA -> roadmA -> chain -> roadmB and return
    the per-channel line NSR (linear) added between roadmA output and roadmB input."""
    trx_a = local_trx(network, roadm_a)
    trx_b = local_trx(network, roadm_b)
    path = [trx_a, roadm_a, *chain, roadm_b, trx_b] if trx_a and trx_b else [roadm_a, *chain, roadm_b]
    si = create_input_spectral_information(
        f_min=req.f_min, f_max=req.f_max, roll_off=req.roll_off, baud_rate=req.baud_rate,
        spacing=req.spacing, tx_osnr=req.tx_osnr, tx_power=req.tx_power,
        required_osnr_db_01nm=req.required_osnr_db_01nm, penalties=req.penalties,
        rx_channel_power_min_dbm=req.rx_channel_power_min_dbm,
        rx_channel_power_max_dbm=req.rx_channel_power_max_dbm,
        detailed_rx=req.detailed_rx, delta_pdb=req.offset_db)
    si = filter_si(path, equipment, si)
    if trx_a:
        si = trx_a(si, role=elements.TransceiverRole.EMITTER)
        si = roadm_a(si, degree=chain[0].uid, from_degree=trx_a.uid)
    nsr0 = (si.ase + si.nli) / si.signal
    for el in chain:
        si = el(si)
    nsr1 = (si.ase + si.nli) / si.signal
    return nsr1 - nsr0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('topology', type=Path)
    ap.add_argument('-e', '--equipment', type=Path, required=True)
    ap.add_argument('-o', '--output', type=Path, required=True)
    ap.add_argument('--designed-topology', type=Path,
                    help='save the GNPy-designed topology (with auto-inserted amplifiers)')
    args = ap.parse_args()

    SimParams.set_params({})
    equipment = load_equipment(args.equipment)
    network = load_network(args.topology, equipment)
    network, req, _ = designed_network(equipment, network)
    if args.designed_topology:
        save_network(network, args.designed_topology)

    roadms = [n for n in network.nodes() if isinstance(n, elements.Roadm)]
    result = []
    for ra in sorted(roadms, key=lambda r: r.uid):
        for first in network.successors(ra):
            if not isinstance(first, LINE_TYPES):
                continue
            chain, rb = walk_oms(network, ra, first)
            if chain is None:
                continue
            nsr = oms_line_nsr(network, equipment, req, ra, chain, rb)
            nsr_mean = float(np.mean(nsr))
            nsr_db = np.array([lin2db(x) for x in nsr])
            fibers = [e for e in chain if isinstance(e, (elements.Fiber, elements.RamanFiber))]
            amps = [e for e in chain if not isinstance(e, (elements.Fiber, elements.RamanFiber, elements.Fused))]
            waypoints = []
            for e in chain:
                if isinstance(e, (elements.Fiber, elements.RamanFiber)):
                    continue
                loc = location(e)
                if loc and loc not in waypoints:
                    waypoints.append(loc)
            result.append({
                'from_roadm': ra.uid,
                'to_roadm': rb.uid,
                'elements': [e.uid for e in chain],
                'length_km': round(sum(f.params.length for f in fibers) / 1000, 3),
                'fiber_count': len(fibers),
                'amplifier_count': len(amps),
                'line_nsr_db_mean': round(lin2db(nsr_mean), 3),
                'line_nsr_db_sigma': round(float(np.std(nsr_db)), 3),
                'line_gsnr_db_mean': round(-lin2db(nsr_mean), 3),
                'waypoints': waypoints,
            })

    out = {
        'source_topology': str(args.topology),
        'equipment': str(args.equipment),
        'reference_channel': {
            'baud_rate_gbaud': req.baud_rate / 1e9,
            'spacing_ghz': req.spacing / 1e9,
            'power_dbm': round(10 * math.log10(req.power * 1e3), 2),
            'f_min_thz': req.f_min / 1e12,
            'f_max_thz': req.f_max / 1e12,
        },
        'roadm_locations': {r.uid: location(r) for r in roadms},
        'oms': result,
    }
    args.output.write_text(json.dumps(out, indent=1))
    print(f'{len(result)} directional OMS written to {args.output}', file=sys.stderr)


if __name__ == '__main__':
    main()
