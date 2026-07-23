# -*- coding: utf-8 -*-

import argparse
import base64
import hashlib
import json
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


def classify_record(record):
    if record.get("tls_error"):
        return "tls_pinning"
    if record.get("websocket"):
        return "websocket"
    content_type = str(record.get("content_type", "")).lower()
    if "json" in content_type:
        return "json"
    if "protobuf" in content_type or "proto" in content_type:
        return "protobuf"
    if "octet-stream" in content_type or record.get("response_body_base64"):
        return "encrypted_payload"
    if record.get("response_text"):
        try:
            json.loads(record["response_text"])
        except (TypeError, json.JSONDecodeError):
            return "text"
        return "json"
    return "unknown"


def _response_size(record):
    if "response_size" in record:
        return int(record["response_size"])
    if "response_text" in record:
        return len(str(record["response_text"]).encode("utf-8"))
    if record.get("response_body_base64"):
        return len(base64.b64decode(record["response_body_base64"]))
    return 0


def _json_objects(records):
    objects = []
    for record in records:
        if classify_record(record) != "json" or not record.get("response_text"):
            continue
        try:
            value = json.loads(record["response_text"])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _field_summary(objects):
    if not objects:
        return [], [], []
    all_keys = sorted(set().union(*(item.keys() for item in objects)))
    stable = []
    changing = []
    for key in all_keys:
        present = [key in item for item in objects]
        values = [item.get(key) for item in objects]
        if all(present) and all(value == values[0] for value in values[1:]):
            stable.append(key)
        else:
            changing.append(key)
    return all_keys, stable, changing


def _hashed_request_headers(records):
    hashes = defaultdict(set)
    for record in records:
        for name, value in record.get("request_headers", {}).items():
            digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
            hashes[str(name)].add(digest)
    return {name: sorted(values) for name, values in sorted(hashes.items())}


def summarize_records(records):
    grouped = defaultdict(list)
    for record in records:
        key = (
            str(record.get("action", "unknown")),
            str(record.get("host", "")),
            str(record.get("path", "")),
            str(record.get("method", "")),
        )
        grouped[key].append(record)

    groups = []
    for (action, host, path, method), items in sorted(grouped.items()):
        classifications = [classify_record(item) for item in items]
        sizes = [_response_size(item) for item in items]
        keys, stable, changing = _field_summary(_json_objects(items))
        groups.append({
            "action": action,
            "host": host,
            "path": path,
            "method": method,
            "classification": Counter(classifications).most_common(1)[0][0],
            "count": len(items),
            "response_size": {
                "min": min(sizes),
                "max": max(sizes),
                "average": round(sum(sizes) / len(sizes), 2),
            },
            "json_top_level_keys": keys,
            "stable_fields": stable,
            "changing_fields": changing,
            "request_header_hashes": _hashed_request_headers(items),
        })
    return {"total_records": len(records), "groups": groups}


def main():
    parser = argparse.ArgumentParser(description="分析 Android APP 抓包 JSONL")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    report = summarize_records(load_records(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已分析 {report['total_records']} 条记录: {args.output}")


if __name__ == "__main__":
    main()
