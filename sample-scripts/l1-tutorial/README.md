# L1 Topology Collection with External Scripts: Tutorial Files

These are the files for the xrdocs tutorial
[Using External Scripts for L1 Topology Collection in Crosswork Planning](https://xrdocs.io/automation/tutorials/tutorial-external-scripts-l1-topology).

The tutorial shows how to add an optical (Layer 1) network, computed with
[GNPy](https://github.com/Telecominfraproject/oopt-gnpy), to a network model collected by the
Crosswork Planning Collector. An external script attaches the optical layer to every collection run.
Follow the tutorial for the full walkthrough; this README lists the files and the quick-start commands.

## Contents

```
l1-tutorial/
├── l1_gnpy_collect.py        the external script run by the Collector
├── gnpy2cp.py                converts GNPy files into L1 plan tables (imported by the script)
├── gnpy_oms_noise.py         exports the length, amplifiers and noise of each OMS (needs GNPy)
├── check_cp_l1.py            checks a plan file's L1 tables for consistency
└── example/
    ├── us_backbone_l3.txt    the collected IP network (12 sites, 33 routers, 50 IP links)
    ├── gnpy_topology.json    the optical network (75 ROADMs, CORONET CONUS)
    ├── gnpy_services.json    17 wavelength services
    ├── eqpt_config.json      GNPy equipment library
    ├── l1_site_map.csv       which ROADM serves each IP site
    └── l1_circuit_map.csv    which wavelength service carries each IP link
```

`l1_gnpy_collect.py`, `gnpy2cp.py` and `check_cp_l1.py` need only the Python 3 standard library.
`gnpy_oms_noise.py` and `gnpy-path-request` need GNPy, which supports Python 3.12 or earlier.

## Quick Start

Run these commands from this directory. They cover Parts 1 to 4 of the tutorial: compute the
optical model, run the script with the Collector's argument order, and check the result.

```bash
python3.12 -m venv .venv && . .venv/bin/activate && pip install gnpy

cd example
python ../gnpy_oms_noise.py gnpy_topology.json -e eqpt_config.json -o gnpy_oms_noise.json
gnpy-path-request gnpy_topology.json gnpy_services.json -e eqpt_config.json -o gnpy_responses.json
cd ..

mkdir -p lab/home lab/upload/gnpy
cp example/l1_site_map.csv example/l1_circuit_map.csv lab/home/
cp example/gnpy_*.json example/eqpt_config.json lab/upload/gnpy/
python3 l1_gnpy_collect.py example/us_backbone_l3.txt lab/us_backbone_l1.txt \
    /dev/null /dev/null lab/home lab/upload /tmp
python3 check_cp_l1.py lab/us_backbone_l1.txt
```

The last command should report:

```
lab/us_backbone_l1.txt: 75 L1 nodes, 99 L1 links, 34 L1 ports, 17 L1 circuits, 17 paths, 0 hops, 50 L3 circuits (17 mapped to L1)
  OK
```

To deploy the script in the Collector, package it with the mapping files and follow Part 5 of the tutorial:

```bash
zip -j lab/l1_gnpy_collect.zip l1_gnpy_collect.py gnpy2cp.py \
    example/l1_site_map.csv example/l1_circuit_map.csv
```

## Using `gnpy2cp.py` on Its Own

`gnpy2cp.py` can also write a standalone plan file with an L1 layer, plus an optional IP overlay and
a validation report that compares GNPy's results with Crosswork Planning's L1 model:

```bash
python3 gnpy2cp.py --topology example/gnpy_topology.json \
  --oms-noise example/gnpy_oms_noise.json --services example/gnpy_services.json \
  --responses example/gnpy_responses.json --equipment example/eqpt_config.json \
  --with-l3 -o conus_l1.txt --report conus_report.md
```

Run `python3 gnpy2cp.py --help` for all options.

## Notes

- `example/gnpy_topology.json` is `CORONET_CONUS_Topology.json` from the
  [GNPy example data](https://github.com/Telecominfraproject/oopt-gnpy/tree/master/gnpy/example-data),
  with two changes so that current GNPy releases accept it: the top-level `metadata` key is removed,
  and `→` in element names is replaced with `->`.
- `example/eqpt_config.json` is the unmodified GNPy example equipment library.
- `example/us_backbone_l3.txt` is a sample Crosswork Planning plan file with its L1 tables removed,
  standing in for an IGP database or SR-PCE collection.
- The extended output (`l1_options.json` with `{"extended": true}`, or `gnpy2cp.py --extended`)
  writes L1 columns whose plan file names are not yet confirmed against a Crosswork Planning
  export. See Appendix B of the tutorial before relying on it.
