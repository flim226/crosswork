#!/usr/bin/env python3
"""Compute per-interface bitrates from SNMP octet-counter polls in a pcap.

Reads a pcap/pcapng file, decodes SNMP (v1/v2c, and unencrypted v3) responses,
and for every (device, interface) builds a time series of ifHCOutOctets /
ifOutOctets (or the In counterparts with --direction in). For each sample it
reports the delta from the previous sample and the bitrate:

    bitrate = (current_counter - previous_counter) / elapsed_seconds * 8

Timestamps are the capture timestamps of the SNMP response packets.
Interface names come from ifName / ifDescr and device names from sysName when
those objects are present in the capture; otherwise ifIndex / IP is shown.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import ipaddress
import re
import struct
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction

# --------------------------------------------------------------------------- #
# OIDs
# --------------------------------------------------------------------------- #
OID_SYSNAME = (1, 3, 6, 1, 2, 1, 1, 5, 0)
OID_IFDESCR = (1, 3, 6, 1, 2, 1, 2, 2, 1, 2)
OID_IFNAME = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 1)
OID_IFALIAS = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 18)

COUNTER_OIDS = {
    "out": {
        "ifHCOutOctets": ((1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 10), 64),
        "ifOutOctets": ((1, 3, 6, 1, 2, 1, 2, 2, 1, 16), 32),
    },
    "in": {
        "ifHCInOctets": ((1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 6), 64),
        "ifInOctets": ((1, 3, 6, 1, 2, 1, 2, 2, 1, 10), 32),
    },
}

# --------------------------------------------------------------------------- #
# pcap / pcapng reader
# --------------------------------------------------------------------------- #
LINKTYPE_NULL = 0
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 101
LINKTYPE_LOOP = 108
LINKTYPE_LINUX_SLL = 113
LINKTYPE_IPV4 = 228
LINKTYPE_IPV6 = 229
LINKTYPE_LINUX_SLL2 = 276


NS_PER_S = 1_000_000_000


def read_packets(path: str):
    """Yield (timestamp_ns, linktype, frame_bytes). Stops cleanly on truncation."""
    with open(path, "rb") as f:
        magic = f.read(4)
        f.seek(0)
        if magic == b"\x0a\x0d\x0d\x0a":
            yield from _read_pcapng(f)
        else:
            yield from _read_pcap(f)


def _read_pcap(f):
    hdr = f.read(24)
    if len(hdr) < 24:
        raise ValueError("file too short to be a pcap")
    magic = hdr[:4]
    formats = {
        b"\xd4\xc3\xb2\xa1": ("<", 1000),
        b"\xa1\xb2\xc3\xd4": (">", 1000),
        b"\x4d\x3c\xb2\xa1": ("<", 1),
        b"\xa1\xb2\x3c\x4d": (">", 1),
    }
    if magic not in formats:
        raise ValueError("unrecognised pcap magic number")
    endian, frac_to_ns = formats[magic]
    linktype = struct.unpack(endian + "I", hdr[20:24])[0] & 0x0FFFFFFF
    rec = struct.Struct(endian + "IIII")
    while True:
        rh = f.read(16)
        if len(rh) < 16:
            return
        sec, frac, incl, _orig = rec.unpack(rh)
        data = f.read(incl)
        if len(data) < incl:
            print("warning: capture is truncated; last packet ignored", file=sys.stderr)
            return
        yield sec * NS_PER_S + frac * frac_to_ns, linktype, data


def _read_pcapng(f):
    endian = "<"
    interfaces: list[tuple[int, Fraction]] = []  # (linktype, ns per timestamp tick)
    while True:
        head = f.read(8)
        if len(head) < 8:
            return
        btype = struct.unpack(endian + "I", head[:4])[0]
        if btype == 0x0A0D0D0A:  # Section Header Block: re-detect endianness
            bom = f.read(4)
            endian = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            blen = struct.unpack(endian + "I", head[4:8])[0]
            body = bom + f.read(blen - 12)
            interfaces = []
        else:
            blen = struct.unpack(endian + "I", head[4:8])[0]
            body = f.read(blen - 8)
        if len(body) < blen - 8 - (4 if btype == 0x0A0D0D0A else 0) or blen < 12:
            print("warning: capture is truncated; last block ignored", file=sys.stderr)
            return
        body = body[:-4]  # trailing block length

        if btype == 0x00000001:  # Interface Description Block
            linktype = struct.unpack(endian + "H", body[:2])[0]
            res = Fraction(1000)
            opts = body[8:]
            while len(opts) >= 4:
                code, olen = struct.unpack(endian + "HH", opts[:4])
                val = opts[4 : 4 + olen]
                if code == 0:
                    break
                if code == 9 and val:  # if_tsresol
                    v = val[0]
                    res = Fraction(NS_PER_S, 2 ** (v & 0x7F) if v & 0x80 else 10**v)
                opts = opts[4 + ((olen + 3) & ~3) :]
            interfaces.append((linktype, res))
        elif btype == 0x00000006:  # Enhanced Packet Block
            if_id, ts_hi, ts_lo, cap_len, _orig = struct.unpack(endian + "IIIII", body[:20])
            linktype, res = interfaces[if_id]
            yield int(((ts_hi << 32) | ts_lo) * res), linktype, body[20 : 20 + cap_len]
        elif btype == 0x00000003:  # Simple Packet Block (no timestamp)
            if interfaces:
                yield 0, interfaces[0][0], body[4:]


# --------------------------------------------------------------------------- #
# Link / IP / UDP decoding with IPv4 + IPv6 fragment reassembly
# --------------------------------------------------------------------------- #
def link_to_ip(linktype: int, frame: bytes) -> bytes | None:
    """Strip the link-layer header and return the IP packet, or None if not IP."""
    if linktype == LINKTYPE_ETHERNET:
        if len(frame) < 14:
            return None
        etype = struct.unpack("!H", frame[12:14])[0]
        off = 14
        while etype in (0x8100, 0x88A8, 0x9100) and len(frame) >= off + 4:
            etype = struct.unpack("!H", frame[off + 2 : off + 4])[0]
            off += 4
        return frame[off:] if etype in (0x0800, 0x86DD) else None
    if linktype in (LINKTYPE_RAW, LINKTYPE_IPV4, LINKTYPE_IPV6):
        return frame
    if linktype in (LINKTYPE_NULL, LINKTYPE_LOOP):
        return frame[4:]
    if linktype == LINKTYPE_LINUX_SLL:
        etype = struct.unpack("!H", frame[14:16])[0]
        return frame[16:] if etype in (0x0800, 0x86DD) else None
    if linktype == LINKTYPE_LINUX_SLL2:
        etype = struct.unpack("!H", frame[0:2])[0]
        return frame[20:] if etype in (0x0800, 0x86DD) else None
    return None


class Reassembler:
    """Minimal IP fragment reassembler keyed on (src, dst, id, proto)."""

    def __init__(self, timeout_ns: int = 30 * NS_PER_S):
        self.timeout = timeout_ns
        self.buffers: dict[tuple, dict] = {}

    def add(self, key, ts, offset, more, payload):
        """Store a fragment; return the reassembled payload once complete, else None."""
        buf = self.buffers.setdefault(key, {"frags": {}, "total": None, "ts": ts})
        buf["frags"][offset] = payload
        buf["ts"] = ts
        if not more:
            buf["total"] = offset + len(payload)
        if buf["total"] is None:
            return None
        data = bytearray()
        for off in sorted(buf["frags"]):
            if off > len(data):
                return None  # hole
            frag = buf["frags"][off]
            data[off : off + len(frag)] = frag
        if len(data) < buf["total"]:
            return None
        del self.buffers[key]
        return bytes(data[: buf["total"]])

    def expire(self, now):
        """Drop incomplete fragment sets older than the timeout."""
        for k in [k for k, b in self.buffers.items() if now - b["ts"] > self.timeout]:
            del self.buffers[k]


def ip_to_udp(pkt: bytes, ts: int, reasm: Reassembler):
    """Return (src_ip, dst_ip, sport, dport, udp_payload) or None."""
    if not pkt:
        return None
    version = pkt[0] >> 4
    if version == 4:
        if len(pkt) < 20:
            return None
        ihl = (pkt[0] & 0x0F) * 4
        total_len = struct.unpack("!H", pkt[2:4])[0]
        ident = struct.unpack("!H", pkt[4:6])[0]
        flags_frag = struct.unpack("!H", pkt[6:8])[0]
        proto = pkt[9]
        src, dst = pkt[12:16], pkt[16:20]
        payload = pkt[ihl:total_len] if total_len else pkt[ihl:]
        more = bool(flags_frag & 0x2000)
        offset = (flags_frag & 0x1FFF) * 8
        if proto != 17:
            return None
        if more or offset:
            payload = reasm.add((src, dst, ident, proto), ts, offset, more, payload)
            if payload is None:
                return None
        src_s, dst_s = str(ipaddress.IPv4Address(src)), str(ipaddress.IPv4Address(dst))
    elif version == 6:
        if len(pkt) < 40:
            return None
        nxt = pkt[6]
        src, dst = pkt[8:24], pkt[24:40]
        payload = pkt[40 : 40 + struct.unpack("!H", pkt[4:6])[0]]
        while nxt in (0, 43, 44, 60):
            if nxt == 44:  # Fragment header
                nh = payload[0]
                off_flags, ident = struct.unpack("!HI", payload[2:8])
                payload = reasm.add(
                    (src, dst, ident, nh), ts, off_flags & 0xFFF8, bool(off_flags & 1), payload[8:]
                )
                if payload is None:
                    return None
                nxt = nh
            else:
                nh, hlen = payload[0], (payload[1] + 1) * 8
                payload, nxt = payload[hlen:], nh
        if nxt != 17:
            return None
        src_s, dst_s = str(ipaddress.IPv6Address(src)), str(ipaddress.IPv6Address(dst))
    else:
        return None
    if len(payload) < 8:
        return None
    sport, dport, ulen = struct.unpack("!HHH", payload[:6])
    return src_s, dst_s, sport, dport, payload[8:ulen] if ulen >= 8 else payload[8:]


# --------------------------------------------------------------------------- #
# BER / SNMP decoding
# --------------------------------------------------------------------------- #
class BerError(Exception):
    """Malformed BER encoding."""


def ber_tlv(buf: bytes, pos: int):
    """Return (tag, value_start, value_end) for the TLV at pos."""
    if pos + 2 > len(buf):
        raise BerError("short TLV")
    tag = buf[pos]
    length = buf[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        if n == 0 or pos + n > len(buf):
            raise BerError("bad length")
        length = int.from_bytes(buf[pos : pos + n], "big")
        pos += n
    end = pos + length
    if end > len(buf):
        raise BerError("TLV overruns buffer")
    return tag, pos, end


def ber_children(buf: bytes, start: int, end: int):
    """Yield (tag, value_start, value_end) for each TLV inside a constructed value."""
    pos = start
    while pos < end:
        tag, vs, ve = ber_tlv(buf, pos)
        yield tag, vs, ve
        pos = ve


def decode_oid(b: bytes) -> tuple[int, ...]:
    """Decode a BER OBJECT IDENTIFIER value into a tuple of sub-identifiers."""
    out: list[int] = []
    val = 0
    for byte in b:
        val = (val << 7) | (byte & 0x7F)
        if not byte & 0x80:
            out.append(val)
            val = 0
    if not out:
        return ()
    first = out[0]
    head = [0, first] if first < 40 else [1, first - 40] if first < 80 else [2, first - 80]
    return tuple(head + out[1:])


# Application tags carrying unsigned integers
_UNSIGNED_TAGS = {0x41, 0x42, 0x43, 0x46, 0x47}  # Counter32, Gauge32, TimeTicks, Counter64, UInt32


def decode_value(tag: int, raw: bytes):
    """Decode an SNMP varbind value; None for noSuchObject/noSuchInstance/endOfMibView."""
    if tag == 0x02:
        return int.from_bytes(raw, "big", signed=True)
    if tag in _UNSIGNED_TAGS:
        return int.from_bytes(raw, "big", signed=False)
    if tag == 0x04:
        return raw
    if tag in (0x80, 0x81, 0x82):  # noSuchObject / noSuchInstance / endOfMibView
        return None
    return raw


def parse_snmp_response(payload: bytes):
    """Return list of (oid_tuple, tag, value) from a Response-PDU, or None."""
    try:
        tag, s, e = ber_tlv(payload, 0)
        if tag != 0x30:
            return None
        items = list(ber_children(payload, s, e))
        if len(items) < 3 or items[0][0] != 0x02:
            return None
        version = int.from_bytes(payload[items[0][1] : items[0][2]], "big")
        if version in (0, 1):
            pdu_tag, ps, pe = items[2]
        elif version == 3:
            if len(items) < 4 or items[3][0] != 0x30:
                return None  # encrypted scopedPDU (authPriv) can't be decoded
            scoped = list(ber_children(payload, items[3][1], items[3][2]))
            if len(scoped) < 3:
                return None
            pdu_tag, ps, pe = scoped[2]
        else:
            return None
        if pdu_tag != 0xA2:  # GetResponse / Response
            return None
        pdu = list(ber_children(payload, ps, pe))
        if len(pdu) < 4 or pdu[3][0] != 0x30:
            return None
        out = []
        for vb_tag, vs, ve in ber_children(payload, pdu[3][1], pdu[3][2]):
            if vb_tag != 0x30:
                continue
            parts = list(ber_children(payload, vs, ve))
            if len(parts) != 2 or parts[0][0] != 0x06:
                continue
            oid = decode_oid(payload[parts[0][1] : parts[0][2]])
            vtag = parts[1][0]
            out.append((oid, vtag, decode_value(vtag, payload[parts[1][1] : parts[1][2]])))
        return out
    except (BerError, IndexError, struct.error):
        return None


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
@dataclass
class Sample:
    """One counter reading."""

    ts: int  # nanoseconds since epoch
    value: int


@dataclass
class Device:
    """Names learned for one SNMP agent."""

    ip: str
    sysname: str | None = None
    ifname: dict[int, str] = field(default_factory=dict)
    ifdescr: dict[int, str] = field(default_factory=dict)
    ifalias: dict[int, str] = field(default_factory=dict)

    def iface_label(self, ifindex: int) -> str:
        """Best available interface name: ifName, then ifDescr, then ifIndex."""
        return self.ifname.get(ifindex) or self.ifdescr.get(ifindex) or f"ifIndex {ifindex}"


def _text(v) -> str | None:
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace").strip("\x00").strip() or None
    return None


def collect(path: str, direction: str, snmp_ports: set[int]):
    """Read the capture and return (devices, counter series, stats)."""
    counters = COUNTER_OIDS[direction]
    devices: dict[str, Device] = {}
    # (device_ip, ifindex, counter_name) -> [Sample]
    series: dict[tuple[str, int, str], list[Sample]] = defaultdict(list)
    reasm = Reassembler()
    stats = {"packets": 0, "snmp_responses": 0, "undecodable": 0}

    for n, (ts, linktype, frame) in enumerate(read_packets(path)):
        stats["packets"] += 1
        if n % 2000 == 0:
            reasm.expire(ts)
        ip = link_to_ip(linktype, frame)
        if ip is None:
            continue
        udp = ip_to_udp(ip, ts, reasm)
        if udp is None:
            continue
        src, _dst, sport, _dport, payload = udp
        if sport not in snmp_ports:
            continue
        varbinds = parse_snmp_response(payload)
        if varbinds is None:
            stats["undecodable"] += 1
            continue
        stats["snmp_responses"] += 1
        dev = devices.setdefault(src, Device(src))
        seen_in_pdu: set[tuple[int, str]] = set()
        for oid, tag, value in varbinds:
            if value is None:
                continue
            if oid == OID_SYSNAME:
                dev.sysname = _text(value) or dev.sysname
                continue
            base, idx = oid[:-1], oid[-1]
            if base == OID_IFNAME:
                if (t := _text(value)):
                    dev.ifname[idx] = t
            elif base == OID_IFDESCR:
                if (t := _text(value)):
                    dev.ifdescr[idx] = t
            elif base == OID_IFALIAS:
                if (t := _text(value)):
                    dev.ifalias[idx] = t
            else:
                for cname, (coid, _bits) in counters.items():
                    if base == coid and isinstance(value, int) and tag in (0x41, 0x46):
                        if (idx, cname) in seen_in_pdu:
                            break
                        seen_in_pdu.add((idx, cname))
                        series[(src, idx, cname)].append(Sample(ts, value))
                        break
    return devices, series, stats


def select_counters(series, mode: str, counters: dict):
    """Pick which counter series to report for each (device, ifindex)."""
    hc_name, lo_name = list(counters)  # HC first, 32-bit second
    have: dict[tuple[str, int], set[str]] = defaultdict(set)
    for dev_ip, idx, cname in series:
        have[(dev_ip, idx)].add(cname)
    chosen = []
    for (dev_ip, idx), names in have.items():
        if mode == "both":
            chosen += [(dev_ip, idx, c) for c in names]
        elif mode == "hc":
            if hc_name in names:
                chosen.append((dev_ip, idx, hc_name))
        elif mode == "32":
            if lo_name in names:
                chosen.append((dev_ip, idx, lo_name))
        else:  # auto: prefer 64-bit
            chosen.append((dev_ip, idx, hc_name if hc_name in names else lo_name))
    return chosen


def build_rows(devices, series, chosen, counters, min_interval_ns: int):
    """Rows carry exact values: elapsed in integer ns, bps as a Fraction."""
    rows = []
    for dev_ip, idx, cname in chosen:
        bits = counters[cname][1]
        modulo = 1 << bits
        samples = sorted(series[(dev_ip, idx, cname)], key=lambda s: s.ts)
        dev = devices[dev_ip]
        prev: Sample | None = None
        for s in samples:
            row = {
                "timestamp": s.ts,
                "hostname": dev.sysname or "",
                "device_ip": dev_ip,
                "ifindex": idx,
                "interface": dev.iface_label(idx),
                "counter": cname,
                "value": s.value,
                "delta": None,
                "elapsed": None,
                "bps": None,
                "note": "",
            }
            if prev is not None:
                elapsed = s.ts - prev.ts
                if elapsed < min_interval_ns:
                    continue  # duplicate / retransmitted response
                delta = s.value - prev.value
                if delta < 0:
                    if bits == 32:
                        delta += modulo
                        row["note"] = "32-bit wrap"
                    else:
                        delta = None
                        row["note"] = "counter reset"
                row["elapsed"] = elapsed
                row["delta"] = delta
                if delta is not None and elapsed > 0:
                    row["bps"] = Fraction(delta * 8 * NS_PER_S, elapsed)
            rows.append(row)
            prev = s
    return rows


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
BPS_UNITS = {
    "bps": 1,
    "kbps": 10**3,
    "Mbps": 10**6,
    "Gbps": 10**9,
    "Tbps": 10**12,
    "Pbps": 10**15,
    "Ebps": 10**18,
}


def fmt_fixed(value: Fraction | int, places: int, grouping: bool = False) -> str:
    """Exact decimal rendering of a rational number (no float rounding)."""
    scaled = round(Fraction(value) * 10**places)
    sign = "-" if scaled < 0 else ""
    whole, frac = divmod(abs(scaled), 10**places)
    whole_s = f"{whole:,}" if grouping else str(whole)
    return f"{sign}{whole_s}.{frac:0{places}d}" if places else f"{sign}{whole_s}"


def human_bps(bps: Fraction | None, unit: str = "auto") -> str:
    """Format a bitrate in the given unit, or auto-scaled."""
    if bps is None:
        return "-"
    if unit == "auto":
        unit = next((u for u, d in reversed(BPS_UNITS.items()) if bps >= d), "bps")
    places = 0 if unit == "bps" else 2
    return f"{fmt_fixed(bps / BPS_UNITS[unit], places, grouping=True)} {unit}"


def fmt_ts(ts_ns: int, utc: bool) -> str:
    """Format a nanosecond timestamp as local or UTC time with milliseconds."""
    tz = dt.timezone.utc if utc else None
    sec, ns = divmod(ts_ns, NS_PER_S)
    stamp = dt.datetime.fromtimestamp(sec, tz).strftime("%Y-%m-%d %H:%M:%S")
    return f"{stamp}.{ns // 1_000_000:03d}"


def fmt_epoch(ts_ns: int) -> str:
    """Format a nanosecond timestamp as epoch seconds with 9 decimal places."""
    sec, ns = divmod(ts_ns, NS_PER_S)
    return f"{sec}.{ns:09d}"


def print_table(headers: list[str], rows: list[list[str]], right: set[int], out=sys.stdout):
    """Print an aligned text table; columns whose index is in `right` are right-aligned."""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))

    def line(cells):
        return "  ".join(
            c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(cells)
        )

    print(line(headers), file=out)
    print("  ".join("-" * w for w in widths), file=out)
    for r in rows:
        print(line(r), file=out)


def write_csv(path: str, rows: list[dict], utc: bool):
    """Write the detailed rows to CSV with unformatted, exact numbers."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["timestamp", "epoch", "hostname", "ip", "ifindex", "interface", "counter",
             "value", "delta", "elapsed_s", "bitrate_bps", "note"]
        )
        for r in rows:
            w.writerow([
                fmt_ts(r["timestamp"], utc), fmt_epoch(r["timestamp"]),
                r["hostname"], r["device_ip"], r["ifindex"], r["interface"],
                r["counter"], r["value"],
                "" if r["delta"] is None else r["delta"],
                "" if r["elapsed"] is None else fmt_fixed(Fraction(r["elapsed"], NS_PER_S), 9),
                "" if r["bps"] is None else fmt_fixed(r["bps"], 3),
                r["note"],
            ])


def summarise(rows: list[dict], unit: str):
    """Build summary-table rows: one per device, interface and counter."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r["hostname"], r["device_ip"], r["ifindex"], r["interface"], r["counter"])
        groups[key].append(r)
    out = []
    for (hostname, ip, idx, iface, cname), rs in groups.items():
        rates = [r["bps"] for r in rs if r["bps"] is not None]
        gaps = [r["elapsed"] for r in rs if r["elapsed"] is not None]
        out.append([
            hostname or "-", ip, str(idx), iface, cname, str(len(rs)),
            fmt_fixed(Fraction(sum(gaps), len(gaps) * NS_PER_S), 1) if gaps else "-",
            human_bps(Fraction(sum(rates), len(rates)), unit) if rates else "-",
            human_bps(max(rates), unit) if rates else "-",
        ])
    return out


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def main(argv=None):
    """Command-line entry point."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("pcap", help="input pcap / pcapng file")
    ap.add_argument("--direction", choices=("out", "in"), default="out",
                    help="use Out (default) or In octet counters")
    ap.add_argument("--counter", choices=("auto", "hc", "32", "both"), default="auto",
                    help="auto: 64-bit HC counter when available, else 32-bit (default)")
    ap.add_argument("--device", help="regex filter on device hostname or IP")
    ap.add_argument("--interface", help="regex filter on interface name")
    ap.add_argument("--csv", metavar="FILE", help="also write the detailed table to a CSV file")
    ap.add_argument("--summary-only", action="store_true",
                    help="print only the per-interface summary")
    ap.add_argument("--include-first", action="store_true",
                    help="show the first sample of each interface (no delta/bitrate) in the table")
    ap.add_argument("--min-interval", type=float, default=0.5,
                    help="ignore samples closer than this many seconds to the previous one "
                         "(default 0.5)")
    ap.add_argument("--port", type=int, action="append",
                    help="SNMP agent UDP source port (repeatable, default 161)")
    ap.add_argument("--utc", action="store_true",
                    help="show timestamps in UTC instead of local time")
    ap.add_argument("--unit", choices=("auto", *BPS_UNITS), default="auto",
                    help="display bitrates in a fixed unit (default: auto-scale per value)")
    args = ap.parse_args(argv)

    counters = COUNTER_OIDS[args.direction]
    devices, series, stats = collect(args.pcap, args.direction, set(args.port or [161]))
    chosen = select_counters(series, args.counter, counters)
    min_interval_ns = int(Fraction(str(args.min_interval)) * NS_PER_S)
    rows = build_rows(devices, series, chosen, counters, min_interval_ns)

    if args.device:
        rx = re.compile(args.device, re.IGNORECASE)
        rows = [r for r in rows if rx.search(r["hostname"]) or rx.search(r["device_ip"])]
    if args.interface:
        rx = re.compile(args.interface, re.IGNORECASE)
        rows = [r for r in rows if rx.search(r["interface"])]
    rows.sort(key=lambda r: (
        _natural_key(r["device_ip"]), _natural_key(r["interface"]), r["counter"], r["timestamp"]
    ))

    print(
        f"# {stats['packets']} packets, {stats['snmp_responses']} SNMP responses decoded, "
        f"{stats['undecodable']} undecodable; {len(devices)} agents, "
        f"{len({(d, i) for d, i, _ in chosen})} interfaces with {args.direction} octet counters",
        file=sys.stderr,
    )

    if not args.summary_only:
        table = []
        for r in rows:
            if r["delta"] is None and not r["note"] and not args.include_first:
                continue
            table.append([
                fmt_ts(r["timestamp"], args.utc), r["hostname"] or "-", r["device_ip"],
                r["interface"], r["counter"],
                f"{r['value']:,}",
                "-" if r["delta"] is None else f"{r['delta']:,}",
                "-" if r["elapsed"] is None else fmt_fixed(Fraction(r["elapsed"], NS_PER_S), 3),
                human_bps(r["bps"], args.unit), r["note"],
            ])
        print_table(
            ["timestamp", "hostname", "ip", "interface", "counter",
             "value", "delta", "elapsed_s", "bitrate", "note"],
            table, right={5, 6, 7, 8},
        )
        print()

    print("Polled interfaces summary")
    print_table(
        ["hostname", "ip", "ifIndex", "interface", "counter",
         "samples", "avg_poll_s", "avg_bitrate", "max_bitrate"],
        summarise(rows, args.unit), right={2, 5, 6, 7, 8},
    )

    if args.csv:
        write_csv(args.csv, rows, args.utc)
        print(f"\nCSV written to {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
