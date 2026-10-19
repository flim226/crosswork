# snmp_pcap_bitrate.py

Compute per-interface bitrates from SNMP interface octet-counter polls captured in a pcap file.

Point the script at a packet capture taken on an SNMP poller (for example a Crosswork collector). It lists every interface whose `ifHCOutOctets` or `ifOutOctets` counter was polled. For each poll it reports the counter value, the delta from the previous poll, and the resulting bitrate:

```text
bitrate (bps) = (current_counter - previous_counter) / elapsed_seconds * 8
```

This lets you check the bitrates a poller should be calculating directly from the SNMP traffic it exchanged with each device.

## Requirements

- Python 3.10 or later.
- No third-party packages. The script uses only the Python standard library.
- `tcpdump` or Wireshark, to take the capture.

## Capturing the pcap

Run the capture on the SNMP poller's interface that faces the devices:

```bash
# Capture until Ctrl-C
tcpdump -i eth1 -v -w crosswork1.eth1.cap 'udp port 161 or (ip[6:2] & 0x1fff != 0)'

# Capture for 3600 seconds, then stop
timeout 3600 tcpdump -i eth1 -v -w crosswork1.eth1.cap 'udp port 161 or (ip[6:2] & 0x1fff != 0)'
```

The `(ip[6:2] & 0x1fff != 0)` part of the filter matters. Large SNMP responses, such as GetBulk replies, are often split into IP fragments. Only the first fragment carries the UDP header, so a plain `port 161` filter keeps the first fragment and drops the rest. The script then cannot reassemble those responses, and their counters are silently missing from the output. For IPv6 agents, use `'udp port 161 or (ip6 and ip6[6] == 44)'`.

To stop after a fixed time, use `timeout`. tcpdump's `-W` option sets the number of files kept when rotating, not a duration.

## Usage

```bash
python3 snmp_pcap_bitrate.py CAPTURE [options]
```

Common examples:

```bash
# Full per-poll table plus the per-interface summary
python3 snmp_pcap_bitrate.py crosswork1.eth1.cap

# Only the list of polled interfaces with average and peak bitrate
python3 snmp_pcap_bitrate.py crosswork1.eth1.cap --summary-only

# One device and interface, also saved to CSV
python3 snmp_pcap_bitrate.py crosswork1.eth1.cap --device node-2 --interface '^HundredGigE0/0/0/12/1$' --csv node-2.csv

# Inbound instead of outbound, with every bitrate shown in Gbps
python3 snmp_pcap_bitrate.py crosswork1.eth1.cap --direction in --unit Gbps
```

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--direction {out,in}` | `out` | Use the Out counters (`ifHCOutOctets` / `ifOutOctets`) or the In counters (`ifHCInOctets` / `ifInOctets`). |
| `--counter {auto,hc,32,both}` | `auto` | `auto` uses the 64-bit HC counter when the device returns it and falls back to the 32-bit counter otherwise. `hc` and `32` force one type; `both` reports each. |
| `--device REGEX` | | Only show devices whose hostname or IP matches. Case-insensitive. |
| `--interface REGEX` | | Only show interfaces whose name matches. Case-insensitive. |
| `--csv FILE` | | Also write the detailed table to a CSV file. |
| `--summary-only` | | Print only the per-interface summary. |
| `--include-first` | | Also show each interface's first poll, which has no delta or bitrate. |
| `--min-interval SECONDS` | `0.5` | Ignore a poll that arrives this soon after the previous one, such as a retransmitted response. |
| `--port PORT` | `161` | UDP port the SNMP agents respond from. Repeat for several ports. |
| `--unit UNIT` | `auto` | Show every bitrate in one unit: `bps`, `kbps`, `Mbps`, `Gbps`, `Tbps`, `Pbps` or `Ebps`. `auto` picks a unit per value. |
| `--utc` | | Show timestamps in UTC instead of local time. |

## Sample output

The output below comes from a synthetic capture built to exercise the edge cases. It contains one router polled four times, 300 seconds apart, run with `--counter both --include-first`:

```text
# 4 packets, 4 SNMP responses decoded, 0 undecodable; 1 agents, 2 interfaces with out octet counters
timestamp                hostname    ip        interface               counter                             value                delta  elapsed_s     bitrate  note
-----------------------  ----------  --------  ----------------------  -------------  --------------------------  -------------------  ---------  ----------  -----------
2026-10-19 12:52:08.123  big-router  10.0.0.1  FourHundredGigE0/0/0/0  ifHCOutOctets  18,446,000,000,000,000,000                    -          -           -
2026-10-19 12:57:08.123  big-router  10.0.0.1  FourHundredGigE0/0/0/0  ifHCOutOctets  18,446,375,000,000,000,000  375,000,000,000,000    300.000  10.00 Tbps
2026-10-19 13:02:08.123  big-router  10.0.0.1  FourHundredGigE0/0/0/0  ifHCOutOctets  18,446,744,073,709,551,615  369,073,709,551,615    300.000   9.84 Tbps
2026-10-19 13:07:08.123  big-router  10.0.0.1  FourHundredGigE0/0/0/0  ifHCOutOctets  18,446,744,073,709,551,615                    0    300.000       0 bps
2026-10-19 12:52:08.123  big-router  10.0.0.1  Gi0/0/0/1               ifOutOctets                 4,000,000,000                    -          -           -
2026-10-19 12:57:08.123  big-router  10.0.0.1  Gi0/0/0/1               ifOutOctets                   205,032,704          500,000,000    300.000  13.33 Mbps  32-bit wrap
2026-10-19 13:02:08.123  big-router  10.0.0.1  Gi0/0/0/1               ifOutOctets                   705,032,704          500,000,000    300.000  13.33 Mbps
2026-10-19 13:07:08.123  big-router  10.0.0.1  Gi0/0/0/1               ifOutOctets                 1,205,032,704          500,000,000    300.000  13.33 Mbps

Polled interfaces summary
hostname    ip        ifIndex  interface               counter        samples  avg_poll_s  avg_bitrate  max_bitrate
----------  --------  -------  ----------------------  -------------  -------  ----------  -----------  -----------
big-router  10.0.0.1        1  FourHundredGigE0/0/0/0  ifHCOutOctets        4       300.0    6.61 Tbps   10.00 Tbps
big-router  10.0.0.1        2  Gi0/0/0/1               ifOutOctets          4       300.0   13.33 Mbps   13.33 Mbps
```

What the sample shows:

- **`FourHundredGigE0/0/0/0` (64-bit counter).** The counter starts close to the 64-bit maximum and grows at 10 Tbps, which the second poll reports as `10.00 Tbps`. The third poll's value is the 64-bit maximum, 18,446,744,073,709,551,615, which is displayed exactly. The synthetic data holds it there, so the third poll shows a smaller delta (`9.84 Tbps`) and the fourth shows zero.
- **`Gi0/0/0/1` (32-bit counter).** The counter passes the 32-bit limit between the first and second polls and restarts near zero. The script corrects for the wrap, reports the true 500,000,000-byte delta, and labels the row `32-bit wrap`.
- **First poll of each interface.** It has no previous value to compare against, so delta and bitrate show `-`. These rows appear only with `--include-first`.

The first line, starting with `#`, goes to standard error. Redirecting standard output to a file therefore captures only the tables.

## Output reference

### Per-poll table

| Column | Meaning |
| --- | --- |
| `timestamp` | Capture time of the SNMP response packet, to the millisecond. |
| `hostname` | Device `sysName`, if the poller requested it. Otherwise `-`. |
| `ip` | IP address the SNMP response came from. |
| `interface` | `ifName`, falling back to `ifDescr`, then `ifIndex N`. |
| `counter` | The counter used for this row, for example `ifHCOutOctets`. |
| `value` | Counter value returned by the device. |
| `delta` | Octets since the previous poll of the same interface and counter. |
| `elapsed_s` | Seconds since that previous poll. |
| `bitrate` | `delta × 8 ÷ elapsed_s`, scaled to a readable unit. |
| `note` | `32-bit wrap` or `counter reset`. See [Counter wraps and resets](#counter-wraps-and-resets). |

### Summary table

The summary has one row per device, interface and counter, with the number of polls (`samples`), the average poll interval in seconds (`avg_poll_s`), and the average and highest bitrate seen.

### CSV file

`--csv` writes the per-poll table with unformatted numbers, so it can be loaded into a spreadsheet or pandas. Its columns are `timestamp`, `epoch`, `hostname`, `ip`, `ifindex`, `interface`, `counter`, `value`, `delta`, `elapsed_s`, `bitrate_bps` and `note`. The CSV always includes each interface's first poll.

## How it works

1. **Read the capture.** Supports pcap (microsecond and nanosecond) and pcapng. If the file was cut off mid-packet, for example because tcpdump was killed, the script prints a warning and ignores the partial packet.
2. **Decode the packets.** Handles Ethernet with VLAN tags, Linux cooked capture and raw IP. It reassembles IPv4 and IPv6 fragments and keeps UDP packets sent from the SNMP port.
3. **Decode SNMP responses.** Handles SNMPv1, SNMPv2c and SNMPv3 without encryption. Encrypted SNMPv3 (authPriv) responses can't be read and are counted as undecodable.
4. **Track names.** Records `sysName`, `ifName` and `ifDescr` wherever they appear in the capture, so a name is resolved even if it was polled after the counters.
5. **Calculate rates.** Builds a time series per device, interface and counter, then calculates the delta and bitrate between consecutive polls.

All counter, timestamp and bitrate arithmetic is exact. Timestamps are integer nanoseconds and bitrates are exact fractions, rounded only for display. Full 64-bit counter values and bitrates beyond 10 Tbps are therefore shown without floating-point rounding errors.

## Counter wraps and resets

- **32-bit counters (`ifOutOctets`).** A value lower than the previous one is treated as a single wrap past 2^32, corrected, and noted as `32-bit wrap`.
- **64-bit counters (`ifHCOutOctets`).** A 64-bit counter takes years to wrap at realistic speeds, so a lower value is treated as a device reboot or counter clear. The row is noted as `counter reset` and its bitrate is left blank.

32-bit counters are not reliable on fast links. A 32-bit octet counter wraps every 2^32 bytes, which takes about 34 seconds at 1 Gbps, 3.4 seconds at 10 Gbps, and 3.4 milliseconds at 10 Tbps. If it wraps more than once between polls, the extra wraps are invisible and the bitrate will be too low. Use the default `--counter auto`, which prefers the 64-bit counter, and only force `--counter 32` for slow links.

## Limitations

- **Capture time, not device time.** Timestamps are when the poller received each response. Network delay and the device's own counter update interval add some jitter to individual bitrates. The averages in the summary are more stable.
- **Missing fragments.** If the capture is missing the later fragments of a response, that response is silently skipped. See [Capturing the pcap](#capturing-the-pcap).
- **Encrypted SNMPv3.** authPriv responses cannot be decoded.
- **No credentials needed or shown.** SNMP community strings travel in clear text inside the capture. The script never prints them or writes them to the CSV. Treat the pcap file itself as sensitive.
