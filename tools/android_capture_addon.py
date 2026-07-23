# -*- coding: utf-8 -*-

import base64
import json
import os
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "logs" / "mobile_capture" / "capture.jsonl"


def _captured_at():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _headers_dict(headers):
    if headers is None:
        return {}
    return {str(key): str(value) for key, value in headers.items()}


def _content_type(headers):
    for key, value in _headers_dict(headers).items():
        if key.lower() == "content-type":
            return value
    return ""


def _request_metadata(flow):
    request = flow.request
    return {
        "host": str(getattr(request, "pretty_host", None) or getattr(request, "host", "")),
        "path": str(getattr(request, "path", "")),
        "request_headers": _headers_dict(getattr(request, "headers", None)),
    }


def _put_body(record, body, content_type):
    body = body or b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    record["response_size"] = len(body)
    lowered = content_type.lower()
    is_text = (
        "json" in lowered
        or lowered.startswith("text/")
        or "xml" in lowered
        or "javascript" in lowered
        or "x-www-form-urlencoded" in lowered
    )
    if is_text:
        record["response_text"] = body.decode("utf-8", errors="replace")
    else:
        record["response_body_base64"] = base64.b64encode(body).decode("ascii")


def build_http_record(flow, action=None, captured_at=None):
    response = flow.response
    response_headers = _headers_dict(getattr(response, "headers", None))
    content_type = _content_type(response_headers)
    record = {
        "captured_at": captured_at or _captured_at(),
        "action": action or os.environ.get("CAPTURE_ACTION", "unknown"),
        "method": str(getattr(flow.request, "method", "")),
        **_request_metadata(flow),
        "status_code": int(getattr(response, "status_code", 0)),
        "content_type": content_type,
        "response_headers": response_headers,
        "websocket": False,
    }
    _put_body(record, getattr(response, "raw_content", b""), content_type)
    return record


def build_websocket_record(flow, action=None, captured_at=None):
    message = flow.websocket.messages[-1]
    body = message.content
    content_type = "text/plain; charset=utf-8" if isinstance(body, str) else "application/octet-stream"
    record = {
        "captured_at": captured_at or _captured_at(),
        "action": action or os.environ.get("CAPTURE_ACTION", "unknown"),
        "method": "WS",
        **_request_metadata(flow),
        "content_type": content_type,
        "response_headers": {},
        "websocket": True,
        "direction": "client_to_server" if message.from_client else "server_to_client",
    }
    _put_body(record, body, content_type)
    return record


def build_error_record(flow, action=None, captured_at=None):
    error = getattr(flow, "error", None)
    return {
        "captured_at": captured_at or _captured_at(),
        "action": action or os.environ.get("CAPTURE_ACTION", "unknown"),
        "method": str(getattr(flow.request, "method", "")),
        **_request_metadata(flow),
        "response_headers": {},
        "tls_error": str(getattr(error, "msg", error or "unknown transport error")),
        "response_size": 0,
        "websocket": False,
    }


class CaptureAddon:
    def __init__(self, output_path=None):
        self.output_path = Path(output_path or os.environ.get("CAPTURE_OUTPUT", DEFAULT_OUTPUT))

    def _write(self, record):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")

    def response(self, flow):
        self._write(build_http_record(flow))

    def websocket_message(self, flow):
        self._write(build_websocket_record(flow))

    def error(self, flow):
        self._write(build_error_record(flow))


addons = [CaptureAddon()]
