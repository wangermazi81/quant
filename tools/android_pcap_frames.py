# -*- coding: utf-8 -*-

import argparse
import hashlib
import ipaddress
import json
import math
import struct
from collections import Counter, defaultdict
from pathlib import Path


PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": ("<", 1_000_000),
    b"\xa1\xb2\xc3\xd4": (">", 1_000_000),
    b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000),
    b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000),
}
FD_MAGIC = b"\xfd\xfd\xfd\xfd"


def iter_pcap_packets(path):
    data = Path(path).read_bytes()
    if len(data) < 24 or data[:4] not in PCAP_MAGIC:
        raise ValueError("unsupported pcap header")
    endian, resolution = PCAP_MAGIC[data[:4]]
    _, _, _, _, _, _, linktype = struct.unpack(endian + "IHHIIII", data[:24])
    offset = 24
    while offset + 16 <= len(data):
        ts_sec, ts_frac, caplen, _ = struct.unpack(endian + "IIII", data[offset:offset + 16])
        offset += 16
        payload = data[offset:offset + caplen]
        offset += caplen
        if len(payload) == caplen:
            yield ts_sec + ts_frac / resolution, linktype, payload


def _ipv4_tcp(packet):
    timestamp, linktype, payload = packet
    if linktype != 101 or not payload or payload[0] >> 4 != 4:
        return None
    ihl = (payload[0] & 15) * 4
    if len(payload) < ihl + 20 or payload[9] != 6:
        return None
    total_len = struct.unpack("!H", payload[2:4])[0]
    ip_payload = payload[:min(total_len, len(payload))]
    tcp = ip_payload[ihl:]
    src_port, dst_port, seq, _, offset_flags = struct.unpack("!HHIIH", tcp[:14])
    doff = (offset_flags >> 12) * 4
    body = tcp[doff:] if len(tcp) >= doff else b""
    if not body:
        return None
    return (
        str(ipaddress.IPv4Address(ip_payload[12:16])),
        src_port,
        str(ipaddress.IPv4Address(ip_payload[16:20])),
        dst_port,
        seq,
        timestamp,
        body,
    )


def collect_tcp_streams(path, ports=None):
    port_set = {int(port) for port in ports or []}
    streams = defaultdict(list)
    packet_count = 0
    for packet in iter_pcap_packets(path):
        item = _ipv4_tcp(packet)
        if not item:
            continue
        flow = item[:4]
        if port_set and flow[1] not in port_set and flow[3] not in port_set:
            continue
        packet_count += 1
        streams[flow].append(item[4:])
    return streams, packet_count


def reassemble_segments(segments):
    data = bytearray()
    marks = []
    next_seq = None
    for seq, timestamp, payload in sorted(segments, key=lambda item: item[0]):
        if next_seq is None:
            append = payload
            next_seq = seq
        elif seq < next_seq:
            overlap = next_seq - seq
            if overlap >= len(payload):
                continue
            append = payload[overlap:]
        else:
            append = payload
        start = len(data)
        data.extend(append)
        next_seq = max(next_seq, seq + len(payload))
        marks.append((start, len(data), timestamp))
    return bytes(data), marks


def _timestamp_for_offset(marks, offset):
    for start, end, timestamp in marks:
        if start <= offset < end:
            return timestamp
    return marks[-1][2] if marks else None


def _entropy(data):
    if not data:
        return 0.0
    total = len(data)
    return round(-sum((count / total) * math.log2(count / total) for count in Counter(data).values()), 4)


def parse_fd_frames(stream, marks=None):
    frames = []
    offset = 0
    marks = marks or []
    while True:
        start = stream.find(FD_MAGIC, offset)
        if start < 0 or start + 13 > len(stream):
            break
        try:
            body_len = int(stream[start + 4:start + 12].decode("ascii"), 16)
        except ValueError:
            offset = start + 1
            continue
        body_start = start + 13
        body_end = body_start + body_len
        if body_end > len(stream):
            break
        body = stream[body_start:body_end]
        frames.append({
            "timestamp": _timestamp_for_offset(marks, start),
            "offset": start,
            "length": body_len,
            "flag": stream[start + 12],
            "prefix_hex": body[:16].hex(" "),
            "sha256_16": hashlib.sha256(body).hexdigest()[:16],
            "entropy": _entropy(body),
        })
        offset = body_end
    return frames


def _length_summary(lengths):
    if not lengths:
        return {"min": 0, "max": 0, "average": 0.0, "top": []}
    return {"min": min(lengths), "max": max(lengths), "average": round(sum(lengths) / len(lengths), 2), "top": Counter(lengths).most_common(12)}


def summarize_pcap(path, ports=None, max_frames_per_flow=80):
    ports = {int(port) for port in (ports or [8002, 8887, 9528])}
    streams, packet_count = collect_tcp_streams(path, ports)
    flows = []
    for flow, segments in sorted(streams.items(), key=lambda item: item[0]):
        stream, marks = reassemble_segments(segments)
        frames = parse_fd_frames(stream, marks)
        if not frames:
            continue
        direction = "client_to_server" if flow[3] in ports else "server_to_client" if flow[1] in ports else "unknown"
        flows.append({
            "flow": f"{flow[0]}:{flow[1]} -> {flow[2]}:{flow[3]}",
            "direction": direction,
            "packet_count": len(segments),
            "payload_bytes": len(stream),
            "first_timestamp": min(ts for _, ts, _ in segments),
            "last_timestamp": max(ts for _, ts, _ in segments),
            "frame_count": len(frames),
            "lengths": _length_summary([frame["length"] for frame in frames]),
            "prefix_counts": Counter(frame["prefix_hex"] for frame in frames).most_common(12),
            "frames": frames[:max_frames_per_flow],
        })
    return {"input": str(path), "tcp_payload_packets": packet_count, "flow_count": len(flows), "frame_count": sum(flow["frame_count"] for flow in flows), "ports": sorted(ports), "flows": flows}


def main():
    parser = argparse.ArgumentParser(description="Extract FD-framed TCP payloads from PCAPdroid RAW-IP captures")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ports", default="8002,8887,9528")
    parser.add_argument("--max-frames-per-flow", type=int, default=80)
    args = parser.parse_args()
    ports = [int(item) for item in args.ports.split(",") if item.strip()]
    report = summarize_pcap(args.input, ports=ports, max_frames_per_flow=args.max_frames_per_flow)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"frames={report['frame_count']} flows={report['flow_count']} output={args.output}")


if __name__ == "__main__":
    main()
