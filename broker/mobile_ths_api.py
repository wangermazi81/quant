# -*- coding: utf-8 -*-

import json
import time
from datetime import datetime
from pathlib import Path

import requests


class MobileTHSApi:
    def __init__(
        self,
        profile_path,
        session_path,
        transport=None,
        refresh_session=None,
    ):
        self.profile_path = Path(profile_path)
        self.session_path = Path(session_path)
        self.transport = transport or requests.Session()
        self.refresh_session = refresh_session
        self.profile = self._read_json(self.profile_path)

    @staticmethod
    def _read_json(path, default=None):
        path = Path(path)
        if not path.exists():
            return {} if default is None else default
        return json.loads(path.read_text(encoding="utf-8"))

    def connect(self):
        required = {"account", "positions", "orders"}
        missing = sorted(required.difference(self.profile))
        return {
            "ok": self.profile_path.exists() and not missing,
            "profile": str(self.profile_path),
            "missing": missing,
        }

    @staticmethod
    def _timestamp(value):
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0.0
        return 0.0

    def _session_is_stale(self, session):
        max_age = float(self.profile.get("session_max_age_sec", 300))
        captured_at = self._timestamp(session.get("captured_at"))
        return time.time() - captured_at > max_age

    def _refresh(self):
        if self.refresh_session is None:
            return False
        refreshed = self.refresh_session()
        if isinstance(refreshed, dict):
            self.session_path.parent.mkdir(parents=True, exist_ok=True)
            self.session_path.write_text(
                json.dumps(refreshed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return True

    def _ensure_fresh_session(self):
        session = self._read_json(self.session_path)
        if self._session_is_stale(session):
            self._refresh()

    @staticmethod
    def _request_kwargs(endpoint, session):
        headers = dict(endpoint.get("headers", {}))
        headers.update(session.get("headers", {}))
        cookies = dict(endpoint.get("cookies", {}))
        cookies.update(session.get("cookies", {}))
        body = dict(endpoint.get("body", {}))
        body.update(session.get("body", {}))
        kwargs = {
            "headers": headers,
            "cookies": cookies,
            "timeout": float(endpoint.get("timeout_sec", 15)),
        }
        method = str(endpoint.get("method", "GET")).upper()
        if body:
            kwargs["params" if method == "GET" else "json"] = body
        return kwargs

    def _request(self, endpoint_name):
        endpoint = self.profile[endpoint_name]
        decoder = str(endpoint.get("decoder", "json")).lower()
        if decoder != "json":
            return {"ok": False, "msg": f"未配置 {decoder} 解码器", "decoder": decoder}

        self._ensure_fresh_session()
        method = str(endpoint.get("method", "GET")).upper()
        url = endpoint["url"]
        for attempt in range(2):
            session = self._read_json(self.session_path)
            response = self.transport.request(
                method,
                url,
                **self._request_kwargs(endpoint, session),
            )
            if response.status_code in {401, 403}:
                if attempt == 0 and self._refresh():
                    continue
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "msg": "会话认证失败",
                }
            if response.status_code >= 400:
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "msg": "只读接口请求失败",
                }
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                return {"ok": False, "msg": f"JSON 响应解析失败: {exc}"}
            return {"ok": True, "payload": payload, "captured_at": time.time()}
        return {"ok": False, "msg": "会话认证失败"}

    @staticmethod
    def _extract(value, path):
        if isinstance(path, str):
            path = [part for part in path.split(".") if part]
        for part in path or []:
            if isinstance(value, list):
                value = value[int(part)]
            else:
                value = value[part]
        return value

    @classmethod
    def _mapped(cls, record, field_map, field):
        path = field_map.get(field, field)
        try:
            return cls._extract(record, path)
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    @staticmethod
    def _number(value):
        if value is None:
            return None
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace(",", "").replace("，", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _integer_or_float(value):
        if value is None:
            return None
        return int(value) if float(value).is_integer() else float(value)

    def get_account(self):
        result = self._request("account")
        if not result.get("ok"):
            return result
        endpoint = self.profile["account"]
        try:
            account = self._extract(result["payload"], endpoint.get("data_path", []))
        except (KeyError, IndexError, TypeError, ValueError):
            return {"ok": False, "field": "data_path", "msg": "账户数据路径不存在"}
        field_map = endpoint.get("field_map", {})
        normalized = {}
        for field in ("available", "total_asset"):
            normalized[field] = self._number(self._mapped(account, field_map, field))
            if normalized[field] is None:
                return {"ok": False, "field": field, "msg": f"缺少必需数值 {field}"}
        return {
            "ok": True,
            **normalized,
            "captured_at": result["captured_at"],
            "source": "app_api",
        }

    def get_positions(self, max_rows=50):
        result = self._request("positions")
        if not result.get("ok"):
            return result
        endpoint = self.profile["positions"]
        try:
            rows = self._extract(result["payload"], endpoint.get("data_path", []))
        except (KeyError, IndexError, TypeError, ValueError):
            return {"ok": False, "field": "data_path", "msg": "持仓数据路径不存在"}
        if not isinstance(rows, list):
            return {"ok": False, "field": "data_path", "msg": "持仓数据不是列表"}
        field_map = endpoint.get("field_map", {})
        positions = []
        for index, row in enumerate(rows[:max_rows] if max_rows else rows):
            shares = self._number(self._mapped(row, field_map, "shares"))
            if shares is None:
                return {
                    "ok": False,
                    "field": f"positions[{index}].shares",
                    "msg": "缺少必需持仓数量",
                }
            code = str(self._mapped(row, field_map, "code") or "").strip()
            if code.isdigit():
                code = code.zfill(6)
            available = self._number(self._mapped(row, field_map, "available_shares"))
            positions.append({
                "code": code,
                "name": str(self._mapped(row, field_map, "name") or ""),
                "shares": self._integer_or_float(shares),
                "available_shares": self._integer_or_float(available),
                "cost_price": self._number(self._mapped(row, field_map, "cost_price")),
                "market_value": self._number(self._mapped(row, field_map, "market_value")),
            })
        return positions

    def get_order_status(self, order_ref=None, code=None):
        result = self._request("orders")
        if not result.get("ok"):
            return {"status": "unknown", **result}
        endpoint = self.profile["orders"]
        try:
            rows = self._extract(result["payload"], endpoint.get("data_path", []))
        except (KeyError, IndexError, TypeError, ValueError):
            return {"ok": False, "status": "unknown", "msg": "委托数据路径不存在"}
        if isinstance(rows, dict):
            rows = [rows]
        field_map = endpoint.get("field_map", {})
        selected = None
        for row in rows or []:
            row_ref = str(self._mapped(row, field_map, "order_ref") or "")
            row_code = str(self._mapped(row, field_map, "code") or "")
            if order_ref is not None and row_ref == str(order_ref):
                selected = row
                break
            if order_ref is None and code is not None and row_code == str(code):
                selected = row
                break
            if order_ref is None and code is None and selected is None:
                selected = row
        if selected is None:
            return {"ok": True, "status": "unknown", "msg": "未找到匹配委托"}

        raw_status = str(self._mapped(selected, field_map, "status") or "")
        status_map = endpoint.get("status_map", {})
        status = status_map.get(raw_status, self._builtin_order_status(raw_status))
        return {
            "ok": True,
            "status": status,
            "raw_status": raw_status,
            "order_ref": str(self._mapped(selected, field_map, "order_ref") or ""),
            "code": str(self._mapped(selected, field_map, "code") or ""),
            "msg": str(self._mapped(selected, field_map, "msg") or ""),
            "captured_at": result["captured_at"],
        }

    @staticmethod
    def _builtin_order_status(raw_status):
        normalized = raw_status.strip().lower()
        if normalized in {"executed", "filled", "success", "成交", "已成", "全成"}:
            return "executed"
        if normalized in {"submitted", "pending", "accepted", "已报", "待报", "部成"}:
            return "submitted"
        if normalized in {"failed", "rejected", "cancelled", "canceled", "废单", "已撤", "拒单"}:
            return "failed"
        return "unknown"

    def get_account_snapshot(self, max_rows=50):
        account = self.get_account()
        if not account.get("ok"):
            return account
        positions = self.get_positions(max_rows=max_rows)
        if isinstance(positions, dict) and not positions.get("ok"):
            return positions
        return {
            "ok": True,
            "available": account["available"],
            "total_asset": account["total_asset"],
            "positions": positions,
            "source": "app_api",
            "captured_at": account["captured_at"],
        }
