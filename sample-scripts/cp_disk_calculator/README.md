# Crosswork Planning Disk Calculator

`cp_disk_calculator.html` is a self-contained, client-side disk sizing calculator for Cisco Crosswork Planning. It estimates the initial disk partition layout from the four storage-related OVF parameters used by the installer.

The calculator runs entirely in a web browser. It has no server component, build step, external JavaScript library, or network dependency.

## Use the calculator

1. Open [`cp_disk_calculator.html`](./cp_disk_calculator.html) in a modern web browser.
2. Enter values in the numeric textboxes or adjust the corresponding sliders.
3. Review the calculated partition sizes, data-disk visualization, and formula details.
4. Select **Reset** to restore the Cisco-documented defaults.

The textboxes and sliders are synchronized. Values are displayed in decimal GB and are calculated as whole GB values to match the installer’s integer arithmetic.

## Inputs

| Calculator input | Installer property | Default | Allowed range | Description |
|---|---|---:|---:|---|
| `logsfs` | `logfs` | 20 GB | 1–1,000 GB | Size of the log disk. The calculator maps this to the entire `/dev/sdb` disk. |
| `ddatafs` | `ddatafs` | 485 GB | 450–8,000 GB | Total `/dev/sdc` data-disk capacity before partitioning. It is not the final ddatafs partition size. |
| `corefs` | `corefs` | 18 GB | 0–1,000 GB | Size of the corefs partition on the data disk. |
| `bckup_min_percent` | `bckup_min_percent` | 35% | 1–80% | Minimum backup percentage used to calculate the ddatafs allocation. |

The defaults are referenced from the [Cisco Crosswork Planning 7.2.x Installation Guide – Installation Parameters](https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/install-guide/cisco-crosswork-planning-7-2-installation-guide/install-crosswork-planning/installation-parameters.html).

## Calculation


The extrafs backup offset is fixed at **50 GB**. This represents the reference deployment’s backup-marked `dregfs` partition on `/dev/sdd` and corresponds to `extra_sz` in the source formula. It is not part of the `/dev/sdc` partition table.

The final sizes are calculated as follows:

```text
extra_sz   = 50 GB
datafs_sz  = floor((ddatafs - extra_sz) × (100 - bckup_min_percent) / 100)
backupfs   = (ddatafs - corefs) - datafs_sz
```

The `floor` operation represents the truncation performed by the shell implementation’s integer arithmetic.

The resulting layout is:

| Device partition | Filesystem | Size |
|---|---|---:|
| `/dev/sdb1` | `logfs` | `logsfs` |
| `/dev/sdc1` | `corefs` | `corefs` |
| `/dev/sdc2` | `ddatafs` | calculated `datafs_sz` |
| `/dev/sdc3` | `backupfs` | calculated remainder |

## Default result

With the Cisco defaults and the fixed 50 GB extrafs offset:

```text
logsfs              = 20 GB
ddatafs             = 485 GB
corefs              = 18 GB
bckup_min_percent   = 35%

ddatafs partition   = floor((485 - 50) × 65 / 100) = 282 GB
backupfs partition  = 485 - 18 - 282 = 185 GB
```

## Validation

The calculator reports an invalid layout when:

- an input is outside its displayed range;
- the data disk is smaller than the fixed 50 GB extrafs offset;
- `corefs` is larger than the total data disk; or
- the calculated backupfs remainder is negative.

## Scope and limitations

- The calculator covers initial provisioning only.
- It does not model the separate post-deployment resize formula, which uses a fixed 65/35 split after the corefs partition.
- The 50 GB `extra_sz` value is based on the reference deployment. If another deployment has a different total of backup-marked extrafs partitions, update `EXTRA_BACKUP_GB` in the HTML file before using the calculator for that deployment.
- The HTML file includes the CSS and JavaScript inline, so it can be copied and used as a standalone artifact.


