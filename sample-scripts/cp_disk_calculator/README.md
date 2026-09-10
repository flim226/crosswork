# Crosswork Planning Disk Calculator

A self-contained, client-side disk sizing calculator for Cisco Crosswork Planning.

## Overview

When sizing a Crosswork Planning VM, the installer accepts four OVF storage parameters—but the resulting partition layout on `/dev/sdb` and `/dev/sdc` is computed with integer arithmetic and a fixed extrafs backup offset. Manual mental math is error-prone and easy to get wrong.

This calculator estimates the initial disk partition layout from those four storage-related OVF parameters and displays partition sizes, a data-disk visualization, and formula details. It runs entirely in a web browser with no server component, build step, external JavaScript library, or network dependency.

## Scope

- Initial provisioning disk layout for `/dev/sdb` (`logfs`) and `/dev/sdc` (`corefs`, `ddatafs`, `backupfs`)
- Four installer parameters: `logfs`, `ddatafs`, `corefs`, `bckup_min_percent`
- Fixed 50 GB extrafs backup offset (`extra_sz`) from the reference deployment
- Whole-GB integer arithmetic matching the installer's shell implementation
- Input validation and invalid-layout detection

### Calculation

The extrafs backup offset is fixed at **50 GB**. This represents the reference deployment's backup-marked `dregfs` partition on `/dev/sdd` and corresponds to `extra_sz` in the source formula. It is not part of the `/dev/sdc` partition table.

The final sizes are calculated as follows:

```text
extra_sz   = 50 GB
datafs_sz  = floor((ddatafs - extra_sz) × (100 - bckup_min_percent) / 100)
backupfs   = (ddatafs - corefs) - datafs_sz
```

The `floor` operation represents the truncation performed by the shell implementation's integer arithmetic.

The resulting layout is:

| Device partition | Filesystem | Size |
|---|---|---:|
| `/dev/sdb1` | `logfs` | `logsfs` |
| `/dev/sdc1` | `corefs` | `corefs` |
| `/dev/sdc2` | `ddatafs` | calculated `datafs_sz` |
| `/dev/sdc3` | `backupfs` | calculated remainder |

### Default result

With the Cisco defaults and the fixed 50 GB extrafs offset:

```text
logsfs              = 20 GB
ddatafs             = 485 GB
corefs              = 18 GB
bckup_min_percent   = 35%

ddatafs partition   = floor((485 - 50) × 65 / 100) = 282 GB
backupfs partition  = 485 - 18 - 282 = 185 GB
```

## Limitations

- Covers initial provisioning only; does not model the separate post-deployment resize formula, which uses a fixed 65/35 split after the `corefs` partition
- The 50 GB `extra_sz` value is based on the reference deployment—update `EXTRA_BACKUP_GB` in the HTML file if another deployment has a different total of backup-marked extrafs partitions
- Defaults are referenced from the [Cisco Crosswork Planning 7.2.x Installation Guide](https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/install-guide/cisco-crosswork-planning-7-2-installation-guide/install-crosswork-planning/installation-parameters.html) and should be verified against the current installation guide for your release

## Usage

Typical usage includes:

- Opening [`cp_disk_calculator.html`](./cp_disk_calculator.html) in a modern web browser
- Adjusting inputs via numeric textboxes or synchronized sliders
- Reviewing calculated partition sizes and selecting **Reset** to restore Cisco-documented defaults

## Location

https://github.com/flim226/crosswork/tree/main/sample-scripts/cp_disk_calculator

The calculator lives in [`cp_disk_calculator.html`](cp_disk_calculator.html).
