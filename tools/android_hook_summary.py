# -*- coding: utf-8 -*-

import argparse
import base64
import json
import math
from collections import Counter, defaultdict
from pathlib import Path


def load_records(path):
    records = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL 第 {line_number} 行格式错误: {exc}") from exc
    return records


def _decode_sample(record):
    value = record.get("sample_base64")
    if not value:
        return b""
    return base64.b64decode(value)


def _entropy(data):
    if not data:
        return 0.0
    total = len(data)
    counts = Counter(data)
    return round(-sum((count / total) * math.log2(count / total) for count in counts.values()), 4)


def _ascii_preview(data, limit=96):
    return "".join(chr(value) if 32 <= value <= 126 else "." for value in data[:limit])


def summarize_records(records, needles=None, max_examples=8):
    needles = [item.encode("utf-8") if isinstance(item, str) else item for item in (needles or [])]
    type_counts = Counter(str(record.get("type", record.get("frida_type", "unknown"))) for record in records)
    status_counts = Counter()
    hook_groups = defaultdict(list)

    for record in records:
        if record.get("type") == "hook_status":
            key = f"{record.get('stage', '')}:{record.get('ok', '')}"
            status_counts[key] += 1
        if record.get("type") == "byte_sample":
            hook_groups[(str(record.get("source", "")), str(record.get("hook", "")))].append(record)

    groups = []
    for (source, hook), items in sorted(hook_groups.items()):
        lengths = [int(item.get("length", 0) or 0) for item in items]
        samples = [_decode_sample(item) for item in items]
        prefixes = Counter(sample[:16].hex(" ") for sample in samples)
        matches = Counter()
        for needle in needles:
            label = needle.decode("utf-8", errors="replace")
            matches[label] = sum(1 for sample in samples if needle in sample)
        examples = []
        for item, sample in zip(items[:max_examples], samples[:max_examples]):
            examples.append({
                "recv_at": item.get("recv_at", ""),
                "ts": item.get("ts", ""),
                "length": int(item.get("length", 0) or 0),
                "sample_len": int(item.get("sample_len", 0) or 0),
                "truncated": bool(item.get("truncated", False)),
                "prefix_hex": sample[:32].hex(" "),
                "entropy": _entropy(sample),
                "sample_sha256_16": item.get("sample_sha256_16", ""),
                "ascii_preview": item.get("ascii_preview") or _ascii_preview(sample),
            })
        groups.append({
            "source": source,
            "hook": hook,
            "count": len(items),
            "length": {
                "min": min(lengths) if lengths else 0,
                "max": max(lengths) if lengths else 0,
                "top": Counter(lengths).most_common(12),
            },
            "prefix_top": prefixes.most_common(12),
            "needle_matches": dict(matches),
            "examples": examples,
        })

    return {
        "total_records": len(records),
        "type_counts": dict(type_counts),
        "status_counts": dict(status_counts),
        "groups": groups,
    }


def main():
    parser = argparse.ArgumentParser(description="汇总 Android Frida hook JSONL")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--needle", action="append", default=[], help="检查样本里是否出现指定明文")
    parser.add_argument("--max-examples", type=int, default=8)
    args = parser.parse_args()

    report = summarize_records(load_records(args.input), needles=args.needle, max_examples=args.max_examples)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已汇总 {report['total_records']} 条 hook 记录 -> {args.output}")


if __name__ == "__main__":
    main()