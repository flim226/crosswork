# L1 Topology Collection with External Scripts: Tutorial Files

These are the files for the xrdocs tutorial
[Using External Scripts for L1 Topology Collection in Crosswork Planning](https://xrdocs.io/automation/tutorials/using-external-scripts-for-l1-collection-in-crosswork-planning).

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
    ├── us_backbone_l1l3.txt  the finished plan from Part 4, for comparison (IP and L1 layers)
    ├── gnpy_topology.json    the optical network (75 ROADMs, CORONET CONUS)
    ├── gnpy_services.json    17 wavelength services
    ├── eqpt_config.json      GNPy equipment library
    ├── l1_site_map.csv       which ROADM serves each IP site
    ├── l1_circuit_map.csv    which wavelength service carries each IP link
    └── LICENSE-GNPy          license for the files taken from GNPy
```

`l1_gnpy_collect.py`, `gnpy2cp.py` and `check_cp_l1.py` need only the Python 3 standard library.
`gnpy_oms_noise.py` and `gnpy-path-request` need GNPy. GNPy 3.0.1 supports Python 3.11 and 3.12 only.

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
python3 l1_gnpy_collect.py example/us_backbone_l3.txt lab/us_backbone_l1l3.txt \
    /dev/null /dev/null lab/home lab/upload /tmp
python3 check_cp_l1.py lab/us_backbone_l1l3.txt
```

The last command should report:

```
lab/us_backbone_l1l3.txt: 75 L1 nodes, 99 L1 links, 34 L1 ports, 17 L1 circuits, 17 paths, 0 hops, 50 L3 circuits (17 mapped to L1)
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

## Attribution

Two example files come from the
[GNPy example data](https://github.com/Telecominfraproject/oopt-gnpy/tree/master/gnpy/example-data)
in the [Telecom Infra Project oopt-gnpy repository](https://github.com/Telecominfraproject/oopt-gnpy),
taken at commit `3b0cf09`:

| File | Original GNPy file | Changes |
| --- | --- | --- |
| `example/gnpy_topology.json` | [`gnpy/example-data/CORONET_CONUS_Topology.json`](https://github.com/Telecominfraproject/oopt-gnpy/blob/master/gnpy/example-data/CORONET_CONUS_Topology.json) | Top-level `metadata` key removed, and `→` in element names replaced with `->`, so that current GNPy releases accept the file |
| `example/eqpt_config.json` | [`gnpy/example-data/eqpt_config.json`](https://github.com/Telecominfraproject/oopt-gnpy/blob/master/gnpy/example-data/eqpt_config.json) | None |

GNPy is Copyright (c) 2018, Telecom Infra Project, and is distributed under the BSD 3-Clause
License. A copy of the license is in [`example/LICENSE-GNPy`](example/LICENSE-GNPy).

The other files were written for this tutorial:

- `example/gnpy_services.json` uses the GNPy service format, but its 17 requests were generated
  from the L1 circuits of the sample Crosswork Planning plan.
- `example/us_backbone_l3.txt` is a sample Crosswork Planning plan file with its L1 tables removed,
  standing in for an IGP database or SR-PCE collection.
- The Python scripts are original. `gnpy_oms_noise.py` imports the GNPy library, which you install
  separately with `pip install gnpy`.

## Notes

- The extended output (`l1_options.json` with `{"extended": true}`, or `gnpy2cp.py --extended`)
  writes L1 columns whose plan file names are not yet confirmed against a Crosswork Planning
  export. See Appendix B of the tutorial before relying on it.
