# -*- coding: utf-8 -*-
"""实盘桥接 API 蓝图（从 main.py 迁出，工单4）。

桥接器（tools/ths_bridge.py，跑在 Windows 交易机）轮询信号 → xiadan 执行 → 回传；
前端经这些接口开关总闸、查当日委托、跟踪单条信号状态。
路由路径与行为与 main.py 原实现逐行等价。
"""
import json
import logging
import hmac
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

try:
    import psutil
except Exception:
    psutil = None

from flask import Blueprint, jsonify, request, session

from app_state import require_auth, require_root_admin, get_user_state, _add_log, get_operator
from core import user_manager as um
from core.bridge_state import _live_enabled, set_live_enabled as _set_live_enabled
from core.daban_state import _DABAN_PEAKS, _LIVE_SELL_PENDING
from analytics import data_manager as dm
from strategy_engine import strategy_manager as sm

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
THS_BRIDGE_SCRIPT = PROJECT_ROOT / 'tools' / 'ths_bridge.py'
THS_BRIDGE_UI_LOG = PROJECT_ROOT / 'logs' / 'bridge_ui.log'
THS_BRIDGE_CONFIG = PROJECT_ROOT / 'tools' / 'bridge_config.json'
THS_ADB_EXE = PROJECT_ROOT / '.android-sdk' / 'platform-tools' / ('adb.exe' if platform.system() == 'Windows' else 'adb')
MOBILE_WEB_PORT = 5001
MOBILE_HOST_PORT = int(os.environ.get('THS_MOBILE_HOST_PORT') or os.environ.get('THS_BIND_PORT') or MOBILE_WEB_PORT)
MOBILE_LEGACY_TASK_TIMEOUT_SEC = 45
_MOBILE_LEGACY_ACTION_LOCK = threading.Lock()
_MOBILE_LEGACY_STATE_LOCK = threading.Lock()
_MOBILE_LEGACY_STATE = {
    "running": False,
    "task_id": "",
    "action": "",
    "target": "",
    "ok": None,
    "msg": "",
    "result": None,
    "started_at": 0.0,
    "updated_at": "",
}



def _load_bridge_config():
    try:
        with THS_BRIDGE_CONFIG.open("r", encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _save_bridge_config(cfg):
    _save_json(THS_BRIDGE_CONFIG, cfg)


def _android_profile_path(cfg=None):
    cfg = cfg or _load_bridge_config()
    profile = cfg.get("android_profile") or "tools/mobile_ths_profile.json"
    path = Path(profile)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _load_android_profile(cfg=None):
    path = _android_profile_path(cfg)
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_android_profile(profile, cfg=None):
    _save_json(_android_profile_path(cfg), profile)


def _request_json():
    return request.get_json(silent=True) or {}


def _mobile_legacy_state_snapshot():
    with _MOBILE_LEGACY_STATE_LOCK:
        return dict(_MOBILE_LEGACY_STATE)


def _mobile_legacy_state_update(**kwargs):
    with _MOBILE_LEGACY_STATE_LOCK:
        _MOBILE_LEGACY_STATE.update(kwargs)
        _MOBILE_LEGACY_STATE["updated_at"] = datetime.now().isoformat(timespec="seconds")


def _mark_mobile_legacy_ready(action, target, ok, msg):
    _mobile_legacy_state_update(
        running=False,
        task_id=f"{action}-{int(time.time() * 1000)}",
        action=action,
        target=str(target or ""),
        ok=bool(ok),
        msg=str(msg or ""),
        result=None,
        started_at=time.time(),
    )


def _mobile_legacy_state_age_sec(state=None):
    state = state or _mobile_legacy_state_snapshot()
    started_at = float(state.get("started_at") or 0.0)
    if started_at <= 0:
        return 0.0
    return max(0.0, time.time() - started_at)


def _start_mobile_legacy_task(action, target, worker):
    task_id = f"{action}-{int(time.time() * 1000)}"
    with _MOBILE_LEGACY_STATE_LOCK:
        state = dict(_MOBILE_LEGACY_STATE)
        stale = bool(state.get("running")) and _mobile_legacy_state_age_sec(state) > MOBILE_LEGACY_TASK_TIMEOUT_SEC
        if state.get("running") and not stale:
            action_name = state.get('action') or 'unknown'
            msg = f"执行中：{action_name}，请等完成后再操作"
            return {
                "ok": True,
                "accepted": False,
                "busy": True,
                "status": "busy",
                "msg": msg,
                "task": state,
            }
        if stale:
            _MOBILE_LEGACY_STATE.update(
                running=False,
                ok=False,
                msg=f"\u4e0a\u6b21\u4efb\u52a1\u8d85\u65f6\u5df2\u91ca\u653e\uff1a{state.get('action') or 'unknown'}",
                result=None,
            )
        _MOBILE_LEGACY_STATE.update(
            running=True,
            task_id=task_id,
            action=action,
            target=str(target or ""),
            ok=None,
            msg="\u6267\u884c\u4e2d",
            result=None,
            started_at=time.time(),
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )

    def finish(**kwargs):
        with _MOBILE_LEGACY_STATE_LOCK:
            if _MOBILE_LEGACY_STATE.get("task_id") != task_id:
                return
            _MOBILE_LEGACY_STATE.update(kwargs)
            _MOBILE_LEGACY_STATE["updated_at"] = datetime.now().isoformat(timespec="seconds")

    def run():
        try:
            result = worker() or {}
            finish(
                running=False,
                action=action,
                target=str(target or ""),
                ok=bool(result.get("ok", True)),
                msg=str(result.get("msg") or result),
                result=result,
            )
        except Exception as e:
            finish(
                running=False,
                action=action,
                target=str(target or ""),
                ok=False,
                msg=str(e),
                result={"ok": False, "msg": str(e)},
            )

    threading.Thread(target=run, name=f"mobile-ths-{action}", daemon=True).start()
    return {
        "ok": True,
        "accepted": True,
        "busy": False,
        "action": action,
        "target": target,
        "msg": f"{action}\u5df2\u63d0\u4ea4\u540e\u53f0\u6267\u884c",
        "task": _mobile_legacy_state_snapshot(),
    }

def _normalize_mobile_mode(mode):
    value = str(mode or "").strip().lower()
    if value in {"real", "live", "a", "a股", "实盘", "actual"}:
        return "live"
    if value in {"sim", "simulation", "mock", "paper", "模拟", "模拟盘"}:
        return "simulation"
    return value or "simulation"


def _bridge_token_uid():
    token = (request.headers.get("X-Bridge-Token") or request.args.get("bridge_token") or "").strip()
    if not token:
        return None
    cfg = _load_bridge_config()
    expected = str(cfg.get("bridge_token") or "").strip()
    if not expected or not hmac.compare_digest(token, expected):
        return None
    try:
        uid = int(cfg.get("bridge_user_id") or 1)
    except (TypeError, ValueError):
        uid = 1
    return uid if uid > 0 else 1


def _bridge_request_uid():
    token_uid = _bridge_token_uid()
    if token_uid:
        return token_uid
    return session.get("user_id")


def require_bridge_or_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _bridge_request_uid():
            return jsonify({"ok": False, "error": "请先登录", "code": 401}), 401
        return f(*args, **kwargs)
    return wrapper

def require_bridge_token(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _bridge_token_uid():
            return jsonify({"ok": False, "error": "bridge token required", "code": 403}), 403
        return f(*args, **kwargs)
    return wrapper


def _adb_command(args, timeout=8):
    exe = str(THS_ADB_EXE if THS_ADB_EXE.exists() else 'adb')
    try:
        result = subprocess.run(
            [exe] + list(args),
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
            check=False,
        )
        return {
            'ok': result.returncode == 0,
            'code': result.returncode,
            'stdout': result.stdout or '',
            'stderr': result.stderr or '',
        }
    except FileNotFoundError as exc:
        return {'ok': False, 'code': 127, 'stdout': '', 'stderr': str(exc)}
    except subprocess.TimeoutExpired as exc:
        return {'ok': False, 'code': 124, 'stdout': exc.stdout or '', 'stderr': exc.stderr or 'adb timeout'}


def _adb_usb_devices():
    result = _adb_command(['devices', '-l'], timeout=8)
    devices = []
    if result.get('ok'):
        for line in result.get('stdout', '').splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == 'device' and not parts[0].startswith('*'):
                devices.append({'serial': parts[0], 'raw': line.strip()})
    return devices, result


def _ensure_usb_reverse(cfg=None, profile=None, port=MOBILE_WEB_PORT):
    cfg = cfg or _load_bridge_config()
    profile = profile or _load_android_profile(cfg)
    devices, devices_result = _adb_usb_devices()
    if not devices:
        return {
            'ok': False,
            'msg': '\u672a\u53d1\u73b0 USB \u8c03\u8bd5\u8bbe\u5907',
            'devices': [],
            'adb': devices_result,
        }
    configured = str(cfg.get('device_serial') or profile.get('device_serial') or '').strip()
    serials = {item['serial'] for item in devices}
    serial = configured if configured in serials else devices[0]['serial']
    host_port = int(os.environ.get('THS_MOBILE_HOST_PORT') or os.environ.get('THS_BIND_PORT') or MOBILE_HOST_PORT)
    reverse_result = _adb_command(['-s', serial, 'reverse', f'tcp:{port}', f'tcp:{host_port}'], timeout=8)
    reverse_list = _adb_command(['-s', serial, 'reverse', '--list'], timeout=8)
    expected = f'{serial} tcp:{port} tcp:{host_port}'
    ok = bool(reverse_result.get('ok'))
    return {
        'ok': ok,
        'msg': '\u5df2\u5efa\u7acb USB \u53cd\u5411\u901a\u9053' if ok else '\u5efa\u7acb USB \u53cd\u5411\u901a\u9053\u5931\u8d25',
        'device_serial': serial,
        'devices': devices,
        'reverse': reverse_list.get('stdout', '').strip(),
        'expected': expected,
        'adb': reverse_result,
    }


def _bridge_python_exe():
    anaconda = Path('D:/Anaconda/python.exe')
    if anaconda.exists():
        return str(anaconda)
    return sys.executable


def _bridge_env():
    env = os.environ.copy()
    platform_tools = PROJECT_ROOT / '.android-sdk' / 'platform-tools'
    if platform_tools.exists():
        env['PATH'] = str(platform_tools) + os.pathsep + env.get('PATH', '')
        env['ANDROID_HOME'] = str(PROJECT_ROOT / '.android-sdk')
        env['ANDROID_SDK_ROOT'] = str(PROJECT_ROOT / '.android-sdk')
    env['PYTHONUTF8'] = '1'
    return env




def _build_android_operator_for_web():
    cfg = _load_bridge_config()
    if str(cfg.get("executor_type") or "").lower() != "android":
        cfg["executor_type"] = "android"
    from tools.ths_bridge import build_operator, DEFAULT_CONFIG
    merged = {**DEFAULT_CONFIG, **cfg}
    return build_operator(merged)

def _bridge_processes():
    script_path = str(THS_BRIDGE_SCRIPT.resolve()).lower()
    root_path = str(PROJECT_ROOT.resolve()).lower()

    if psutil is not None:
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                name = (proc.info.get('name') or '').lower()
                cmdline = proc.info.get('cmdline') or []
                cmd = ' '.join(str(part) for part in cmdline)
                cmd_lower = cmd.lower()
                cwd = ''
                try:
                    cwd = (proc.cwd() or '').lower()
                except Exception:
                    pass
                if 'python' in name and 'ths_bridge.py' in cmd_lower:
                    if script_path in cmd_lower or root_path in cmd_lower or cwd == root_path:
                        processes.append({'pid': int(proc.info['pid']), 'command': cmd})
            except Exception:
                continue
        return processes

    if platform.system() == 'Windows':
        root = str(PROJECT_ROOT).replace("'", "''")
        ps_script = f'''
$needle = 'ths_bridge.py'
$root = '{root}'
Get-CimInstance Win32_Process |
  Where-Object {{ $_.Name -match 'python' -and $_.CommandLine -match [regex]::Escape($needle) -and $_.CommandLine -match [regex]::Escape($root) }} |
  Select-Object ProcessId,CommandLine |
  ConvertTo-Json -Compress
'''
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_script],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        raw = (result.stdout or '').strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception:
            return []
        if isinstance(data, dict):
            data = [data]
        return [
            {'pid': int(p.get('ProcessId')), 'command': p.get('CommandLine') or ''}
            for p in data
            if p.get('ProcessId')
        ]

    result = subprocess.run(
        ['pgrep', '-af', 'ths_bridge.py'],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    processes = []
    for line in (result.stdout or '').splitlines():
        parts = line.strip().split(maxsplit=1)
        if parts and parts[0].isdigit() and str(PROJECT_ROOT) in line:
            processes.append({'pid': int(parts[0]), 'command': parts[1] if len(parts) > 1 else ''})
    return processes


def bridge_process_status_payload():
    processes = _bridge_processes()
    return {
        'running': bool(processes),
        'pid': processes[0]['pid'] if processes else None,
        'pids': [p['pid'] for p in processes],
        'processes': processes,
    }


def stop_bridge_process():
    processes = _bridge_processes()
    if not processes:
        return {'stopped': 0, 'pids': []}
    pids = [p['pid'] for p in processes]
    if platform.system() == 'Windows':
        subprocess.run(
            ['powershell', '-NoProfile', '-Command', 'Stop-Process -Id ' + ','.join(map(str, pids)) + ' -Force'],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        subprocess.run(['kill', '-TERM'] + [str(pid) for pid in pids], check=False)
    return {'stopped': len(pids), 'pids': pids}


def start_bridge_process():
    status = bridge_process_status_payload()
    if status['running']:
        return {'pid': status['pid'], 'already_running': True}

    creationflags = subprocess.CREATE_NO_WINDOW if platform.system() == 'Windows' else 0
    THS_BRIDGE_UI_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = THS_BRIDGE_UI_LOG.open('ab')
    proc = subprocess.Popen(
        [_bridge_python_exe(), str(THS_BRIDGE_SCRIPT)],
        cwd=str(PROJECT_ROOT),
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        env=_bridge_env(),
    )
    return {'pid': proc.pid, 'already_running': False}


bp = Blueprint("bridge", __name__)


def _as_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", ""))
    except Exception:
        return default


def _execution_account_base(s, broker_cfg, mobile_profile):
    """统一仓位金额基数：手机最近资金 > 实盘配置资金 > 账户面板资金。"""
    last_account = mobile_profile.get("last_account_snapshot") or {}
    for key in ("total_asset", "equity", "asset", "balance"):
        amount = _as_float(last_account.get(key))
        if amount and amount > 0:
            return amount, "mobile_snapshot"

    live_cash = _as_float((broker_cfg.get("config") or {}).get("live_cash"))
    if live_cash and live_cash > 0:
        return live_cash, "live_cash"

    capital = _as_float(s.get("capital"), 0.0) or 0.0
    return capital, "account_capital"


def _execution_position_policy(s, broker_cfg, mobile_profile, bridge_cfg):
    active_strategy = s.get("active_strategy") or ""
    try:
        pos_cfg = sm.get_position_config(active_strategy)
    except Exception:
        logger.exception("读取策略仓位规则失败")
        pos_cfg = {}

    max_daily = int(_as_float(pos_cfg.get("max_daily"), 0) or 0)
    sizing = str(pos_cfg.get("sizing") or "equal").lower()
    if max_daily > 0 and sizing == "equal":
        ratio = 1.0 / max_daily
        mode = "strategy"
    else:
        ratio = _as_float(s.get("position_ratio"), 0.2) or 0.2
        mode = "account"

    account_base, account_source = _execution_account_base(s, broker_cfg, mobile_profile)
    target_amount = round(account_base * ratio, 2) if account_base else None
    max_hold = int(_as_float(pos_cfg.get("max_hold"), 0) or 0)
    max_positions = max_hold or max_daily or int(_as_float(s.get("max_positions"), 0) or 0)
    max_order_amount = _as_float(bridge_cfg.get("max_order_amount"))
    if max_order_amount is None:
        try:
            from config import MAX_ORDER_AMOUNT
            max_order_amount = _as_float(MAX_ORDER_AMOUNT)
        except Exception:
            max_order_amount = None

    return {
        "mode": mode,
        "sizing": sizing,
        "max_daily": max_daily,
        "max_hold": max_hold,
        "max_positions": max_positions,
        "target_ratio": round(ratio, 6),
        "target_amount": target_amount,
        "account_base": round(account_base, 2) if account_base else None,
        "account_source": account_source,
        "max_order_amount": max_order_amount,
    }


@bp.route("/api/execution/state", methods=["GET"])
@require_auth
def execution_state():
    uid = session["user_id"]
    s = get_user_state(uid)
    broker_cfg = um.get_user_broker_config(uid)
    bridge_cfg = _load_bridge_config()
    mobile_profile = _load_android_profile(bridge_cfg)
    bridge_process = bridge_process_status_payload()
    mobile_task = _mobile_legacy_state_snapshot()
    broker_type = str(broker_cfg.get("broker_type") or s.get("broker_type") or "paper").lower()
    executor_type = str(bridge_cfg.get("executor_type") or "android").lower()
    mobile_mode = mobile_profile.get("trade_mode") or mobile_profile.get("trade_mode_target") or "unknown"
    confirm_mode = bridge_cfg.get("confirm_mode") or mobile_profile.get("confirm_mode") or "manual"
    connection_mode = bridge_cfg.get("connection_mode") or mobile_profile.get("connection_mode") or "usb"
    live_trade_enabled = bool(_live_enabled(uid))
    active_target = "mobile" if executor_type in {"android", "mobile", "phone"} else ("desktop" if broker_type in {"live", "real"} else "paper")

    operator = get_operator()
    trade_settings = None
    try:
        trade_settings = operator.get_trade_settings() if operator else None
    except Exception:
        trade_settings = None

    last_account = mobile_profile.get("last_account_snapshot") or {}
    positions = last_account.get("positions") if isinstance(last_account, dict) else []
    if not isinstance(positions, list):
        positions = []

    return jsonify({
        "ok": True,
        "strategy": {
            "running": bool(s.get("running") or s.get("vibe_active")),
            "auto_execute": bool(s.get("auto_execute")),
            "active_strategy": s.get("active_strategy") or "",
            "today_trades": int(s.get("today_trades") or 0),
            "queue_count": len(s.get("queue") or []),
        },
        "execution": {
            "active_target": active_target,
            "executor_type": executor_type,
            "broker_type": broker_type,
            "live_trade_enabled": live_trade_enabled,
        },
        "mobile": {
            "bridge_running": bool(bridge_process.get("running")),
            "bridge_pid": bridge_process.get("pid"),
            "connection_mode": connection_mode,
            "trade_mode": mobile_mode,
            "confirm_mode": confirm_mode,
            "device_serial": mobile_profile.get("device_serial") or bridge_cfg.get("device_serial") or "",
            "app_package": mobile_profile.get("app_package") or "com.hexin.plat.android",
            "task": mobile_task,
            "account": {
                "ok": bool(last_account.get("ok")) if isinstance(last_account, dict) else False,
                "available": _as_float(last_account.get("available")) if isinstance(last_account, dict) else None,
                "total_asset": _as_float(last_account.get("total_asset")) if isinstance(last_account, dict) else None,
                "market_value": _as_float(last_account.get("market_value")) if isinstance(last_account, dict) else None,
                "positions_count": len(positions),
            },
        },
        "desktop": {
            "xiadan_ready": bool(operator),
            "trade_mode": "live" if broker_type in {"live", "real"} else "paper",
            "trade_settings": trade_settings,
        },
        "position_policy": _execution_position_policy(s, broker_cfg, mobile_profile, bridge_cfg),
    })


@bp.route("/api/bridge/process", methods=["GET"])
@require_auth
def bridge_process_status():
    return jsonify({"ok": True, **bridge_process_status_payload()})


# 桥接进程是全局单例、服务机主本人的实盘执行：启停权限收敛到 root admin，
# 否则任一登录用户都能在实盘运行中 kill 掉它（已下发信号无人执行、持仓失去自动止损）。
@bp.route("/api/bridge/process/start", methods=["POST"])
@require_root_admin
def bridge_process_start():
    result = start_bridge_process()
    return jsonify({"ok": True, **result, **bridge_process_status_payload()})


@bp.route("/api/bridge/process/stop", methods=["POST"])
@require_root_admin
def bridge_process_stop():
    result = stop_bridge_process()
    return jsonify({"ok": True, **result, **bridge_process_status_payload()})


def _mobile_profile_payload(cfg=None, profile=None):
    cfg = cfg or _load_bridge_config()
    profile = profile or _load_android_profile(cfg)
    task = _mobile_legacy_state_snapshot()
    return {
        "ok": True,
        "backend": "jianwei-alpha",
        "executor_type": cfg.get("executor_type", "android"),
        "connection_mode": cfg.get("connection_mode") or profile.get("connection_mode") or "usb",
        "trade_mode": profile.get("trade_mode") or "unknown",
        "confirm_mode": cfg.get("confirm_mode") or profile.get("confirm_mode") or "manual",
        "device_serial": profile.get("device_serial") or cfg.get("device_serial") or "",
        "app_package": profile.get("app_package") or "com.hexin.plat.android",
        "bridge_running": bool(bridge_process_status_payload().get("running")),
        "mobile_task": task,
        "last_account_snapshot": profile.get("last_account_snapshot") or {},
    }


def _set_mobile_connection(mode):
    mode = str(mode or "usb").strip().lower() or "usb"
    cfg = _load_bridge_config()
    profile = _load_android_profile(cfg)
    cfg["connection_mode"] = mode
    profile["connection_mode"] = mode
    usb_status = None
    if mode == "usb":
        usb_status = _ensure_usb_reverse(cfg, profile)
        if usb_status.get("device_serial"):
            cfg["device_serial"] = usb_status["device_serial"]
            profile["device_serial"] = usb_status["device_serial"]
    _save_bridge_config(cfg)
    _save_android_profile(profile, cfg)
    ok = True if usb_status is None else bool(usb_status.get("ok"))
    msg = usb_status.get("msg") if usb_status is not None else f"连接方式已切换为 {mode}"
    _mark_mobile_legacy_ready("connection", mode, ok, msg)
    return {"ok": ok, "connection_mode": mode, "device_serial": cfg.get("device_serial"), "usb": usb_status, "msg": msg}


def _set_mobile_confirm_mode(mode):
    mode = "auto" if str(mode or "auto").strip().lower() in {"auto", "自动", "true", "1", "yes", "on"} else "manual"
    cfg = _load_bridge_config()
    profile = _load_android_profile(cfg)
    cfg["confirm_mode"] = mode
    profile["confirm_mode"] = mode
    _save_bridge_config(cfg)
    _save_android_profile(profile, cfg)
    msg = "已切换为全自动确认" if mode == "auto" else "已切换为手动确认"
    _mark_mobile_legacy_ready("confirm_mode", mode, True, msg)
    return {"ok": True, "confirm_mode": mode, "msg": msg}


def _mobile_account_worker(uid, cfg, profile):
    def worker():
        operator = _build_android_operator_for_web()
        if hasattr(operator, "query_account_snapshot"):
            result = operator.query_account_snapshot() or {}
        elif hasattr(operator, "get_account"):
            result = operator.get_account() or {}
        elif hasattr(operator, "query_funds"):
            result = operator.query_funds() or {}
        else:
            result = {"ok": False, "msg": "当前执行器缺少账户读取能力"}
        if not isinstance(result, dict):
            result = {"ok": True, "data": result}
        result.setdefault("ok", True)
        result.setdefault("source", "mobile_ui")
        result["trade_mode"] = profile.get("trade_mode") or cfg.get("trade_mode") or "simulation"
        result["connection_mode"] = cfg.get("connection_mode") or profile.get("connection_mode") or "usb"
        result["device_serial"] = profile.get("device_serial") or cfg.get("device_serial") or ""
        result["bridge_running"] = bool(bridge_process_status_payload().get("running"))
        result["confirm_mode"] = cfg.get("confirm_mode") or profile.get("confirm_mode") or "manual"
        try:
            total_asset = result.get("total_asset") or result.get("asset") or 0
            if uid and result.get("ok") and total_asset:
                broker_cfg = um.get_user_broker_config(uid)
                broker_type = broker_cfg.get("broker_type", "paper")
                broker_inner = dict(broker_cfg.get("config") or {})
                broker_inner["live_cash"] = float(total_asset)
                um.save_user_broker_config(uid, broker_type, broker_inner)
                result["live_cash_synced"] = True
        except Exception as e:
            result["live_cash_sync_error"] = str(e)
        fresh_profile = _load_android_profile(cfg)
        fresh_profile["last_account_snapshot"] = result
        _save_android_profile(fresh_profile, cfg)
        return result
    return worker


def _mobile_open_app_worker():
    def worker():
        operator = _build_android_operator_for_web()
        if not hasattr(operator, "open_app"):
            return {"ok": False, "msg": "当前执行器缺少 open_app"}
        return operator.open_app()
    return worker


def _mobile_switch_mode_worker(mode):
    mode = _normalize_mobile_mode(mode)
    cfg = _load_bridge_config()
    profile = _load_android_profile(cfg)
    profile["trade_mode"] = mode
    profile["trade_mode_target"] = mode
    _save_android_profile(profile, cfg)

    def worker():
        operator = _build_android_operator_for_web()
        if not hasattr(operator, "switch_trade_mode"):
            return {"ok": False, "msg": "当前执行器缺少 switch_trade_mode"}
        result = operator.switch_trade_mode(mode)
        if result.get("ok"):
            fresh_cfg = _load_bridge_config()
            fresh_profile = _load_android_profile(fresh_cfg)
            fresh_profile["trade_mode"] = mode
            fresh_profile["trade_mode_target"] = mode
            _save_android_profile(fresh_profile, fresh_cfg)
        return result
    return mode, worker


def _mobile_restart_bridge_worker():
    def worker():
        stopped = stop_bridge_process()
        started = start_bridge_process()
        return {"ok": True, "msg": "桥接器已后台重启", "stopped": stopped, "started": started, **bridge_process_status_payload()}
    return worker


# ── legacy 手机执行入口 ───────────────────────────────────────────
# 鉴权：本组曾整组无装饰器（任意人可调），与下方新版 /api/bridge/mobile/* 对齐补齐。
# 方法：改状态的一律只收 POST——GET 可被 <img>/顶层导航跨站触发，
#       SameSite=Lax 对顶层 GET 仍会带上 session cookie。只读的两条保留 GET。
@bp.route("/api/mobile_ths/profile", methods=["GET", "POST"])
@require_bridge_or_auth
def mobile_ths_legacy_profile():
    return jsonify(_mobile_profile_payload())


@bp.route("/api/mobile_ths/connection", methods=["POST"])
@require_bridge_or_auth
def mobile_ths_legacy_connection():
    data = _request_json()
    mode = (data.get("connection_mode") or data.get("mode") or "usb")
    return jsonify(_set_mobile_connection(mode))


@bp.route("/api/mobile_ths/confirm_mode", methods=["POST"])
@require_bridge_or_auth
def mobile_ths_legacy_confirm_mode():
    data = _request_json()
    mode = (data.get("confirm_mode") or data.get("mode") or "auto")
    return jsonify(_set_mobile_confirm_mode(mode))


@bp.route("/api/mobile_ths/account", methods=["GET", "POST"])
@require_bridge_or_auth
def mobile_ths_legacy_account():
    cfg = _load_bridge_config()
    profile = _load_android_profile(cfg)
    try:
        uid = int(cfg.get('bridge_user_id') or session.get('user_id') or 0)
    except Exception:
        uid = 0
    worker = _mobile_account_worker(uid, cfg, profile)
    result = _start_mobile_legacy_task('account', '', worker)
    result['last_account_snapshot'] = profile.get('last_account_snapshot') or {}
    return jsonify(result)


@bp.route("/api/mobile_ths/open_ths", methods=["POST"])
@require_bridge_or_auth
def mobile_ths_legacy_open_ths():
    return jsonify(_start_mobile_legacy_task("open_ths", "", _mobile_open_app_worker()))


@bp.route("/api/mobile_ths/mode", methods=["POST"])
@require_bridge_or_auth
def mobile_ths_legacy_mode():
    data = _request_json()
    mode, worker = _mobile_switch_mode_worker(data.get("mode") or data.get("trade_mode"))
    return jsonify(_start_mobile_legacy_task("switch_mode", mode, worker))


@bp.route("/api/mobile_ths/restart_bridge", methods=["POST"])
@require_bridge_or_auth
def mobile_ths_legacy_restart_bridge():
    return jsonify(_start_mobile_legacy_task("restart_bridge", "", _mobile_restart_bridge_worker()))




@bp.route("/api/bridge/mobile/open-app", methods=["POST"])
@require_bridge_or_auth
def bridge_mobile_open_app():
    try:
        return jsonify(_mobile_open_app_worker()())
    except Exception as e:
        return jsonify({"ok": False, "msg": f"打开手机同花顺失败: {e}"})


@bp.route("/api/bridge/mobile/mode", methods=["POST"])
@require_bridge_or_auth
def bridge_mobile_mode_switch():
    data = request.json or {}
    mode, worker = _mobile_switch_mode_worker(data.get("mode") or request.args.get("mode") or "simulation")
    try:
        result = worker()
        result.setdefault("trade_mode", mode)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"切换手机同花顺模式失败: {e}"})


@bp.route("/api/bridge/signals", methods=["GET"])
@require_bridge_token
def bridge_get_signals():
    uid = _bridge_request_uid()
    # ★ 实盘下单总闸：关闭时不给桥接器任何信号（桥接器照常轮询但拿不到单 → 不下单）
    # 缺省为「关」（安全默认）：新用户/重装后必须在面板显式开闸。这里记一条日志，
    # 免得出现「桥接器在跑却一直不下单」时无从判断是没开闸还是真没信号。
    if not _live_enabled(uid):
        logger.info("[bridge] uid=%s 实盘总闸未开启，本次不下发信号（面板可一键开启）", uid)
        return jsonify({"ok": True, "signals": [], "live_disabled": True})
    cfg = _load_bridge_config()
    limit = 1 if str(cfg.get('confirm_mode', 'manual')).lower() == 'manual' else None
    signals = um.get_pending_signals(uid, limit=limit)
    return jsonify({"ok": True, "signals": signals})


@bp.route("/api/bridge/live_enabled", methods=["GET"])
@require_auth
def get_live_enabled():
    return jsonify({"ok": True, "enabled": _live_enabled(session["user_id"])})


@bp.route("/api/bridge/live_enabled", methods=["POST"])
@require_auth
def set_live_enabled():
    uid = session["user_id"]
    en  = bool((request.json or {}).get("enabled", True))
    _set_live_enabled(uid, en)
    _add_log(uid, ("✅ 已开启实盘下单（桥接器恢复执行信号）" if en
                   else "🛑 已关闭实盘下单（桥接器不再收到任何信号，不会下单到同花顺）"), "WARN")
    return jsonify({"ok": True, "enabled": en})


@bp.route("/api/bridge/today_orders", methods=["GET"])
@require_root_admin
def bridge_today_orders():
    """读 xiadan 当日委托表，返回每笔精确状态（已成/已报/部成/已撤/废单）

    Query 参数:
      code     - 可选, 只查该 code 的委托
      limit    - 可选, 最多读多少行（默认 50）
      navigate - 可选, 1/true 时 F4 后自校准点击左侧「当日委托」再截图（拿真实成交
                 状态）；默认不导航=只截 F4 默认的资金股票页（不向实盘发送鼠标点击）

    返回:
      {ok, screenshot, abs_path, orders:[{code,name,action,status,numbers,raw}],
       funds, positions, tabs:[...], ocr_lines:[...], msg}

    实现：xiadan 委托表是自绘控件读不到，桥接器按 F4 切到查询页 → 截图 →
    本地 Windows OCR(winocr) 逐行识别 → 解析结构化 → 按 F1 切回买入页（约 2-3 秒）。
    未装 winocr 时 orders 为空，仅返回截图。
    """
    if platform.system() != "Windows":
        return jsonify({"ok": False, "msg": "服务端非 Windows，无法读 xiadan"})
    operator = get_operator()
    if not operator:
        return jsonify({"ok": False, "msg": "xiadan 操作员未初始化（DRY_RUN 或 Linux）"})

    code = (request.args.get("code") or "").strip() or None
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    # navigate=1：F4 后自校准点击左侧「当日委托」再截图（拿真实成交状态）。
    # 默认 0=只截 F4 默认的资金股票页（安全，不向实盘客户端发送鼠标点击）。
    navigate = (request.args.get("navigate") or "").strip().lower() in ("1", "true", "yes", "on")

    try:
        r = operator.query_today_orders(code_filter=code, max_rows=limit, navigate=navigate)
        # code 可靠、OCR 名称易错或缺失 → 用本地名称库按 code 纠正/补全（持仓同样处理）
        for lst in (r.get("orders") or [], r.get("positions") or []):
            for it in lst:
                c = (it.get("code") or "").strip()
                if c:
                    nm = dm.get_stock_name(c)
                    if nm and nm != c:
                        it["name"] = nm
        # ★ 缓存 OCR 实际持仓（当日有效）：自动扫描把这些票算"已持有"不再买入。
        #   兜住"实际已买入但回传丢失/误报失败"的票——只要查过一次实盘账户就拦得住。
        try:
            if r.get("ok") and r.get("positions"):
                _s = get_user_state(session["user_id"])
                _s["live_ocr_pos"] = {
                    "date":  datetime.now().strftime("%Y-%m-%d"),
                    "codes": [p.get("code") for p in r["positions"] if p.get("code")],
                }
        except Exception as _e:
            # 这层缓存是「今天实盘到底持有哪些票」的判断依据，静默失败会让
            # 后续去重/风控按空持仓处理，必须留痕（A-9）。
            logger.warning(f"[bridge] 缓存实盘 OCR 持仓失败: {_e}")
        return jsonify(r)
    except Exception as e:
        logger.exception(f"读当日委托失败: {e}")
        return jsonify({"ok": False, "msg": f"调用异常: {e}"})


@bp.route("/api/bridge/signals/<int:signal_id>/status", methods=["GET"])
@require_auth
def bridge_signal_status(signal_id):
    """前端轮询单条信号当前状态（pending/sent/executed/failed/cancelled）"""
    uid = session["user_id"]
    sig = um.get_trade_signal(signal_id)
    if not sig or sig.get("user_id") != uid:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({
        "ok": True,
        "signal": {
            "id":          sig["id"],
            "code":        sig["code"],
            "name":        sig["name"],
            "action":      sig["action"],
            "price":       sig["price"],
            "shares":      sig["shares"],
            "status":      sig["status"],     # pending/sent/executed/failed/cancelled
            "report_msg":  sig.get("msg") or "",
            "exec_price":  sig.get("exec_price") or sig["price"],
            "created_at":  sig.get("created_at"),
            "updated_at":  sig.get("updated_at"),
        }
    })


@bp.route("/api/bridge/signals/<int:signal_id>/report", methods=["POST"])
@require_bridge_token
def bridge_report_signal(signal_id):
    uid = _bridge_request_uid()
    sig = um.get_trade_signal(signal_id)
    if not sig or sig.get("user_id") != uid:
        return jsonify({"ok": False, "error": "not found"}), 404

    data = request.json or {}
    status = data.get("status")
    msg = data.get("msg", "")
    # 桥接器若能从今日委托/成交页读到真实成交均价就一并回传，
    # 台账据此记成本；读不到则回退信号价并在返回里标 price_source=signal。
    exec_price = data.get("exec_price")

    # 校验并更新状态
    res = um.report_trade_signal(signal_id, status, msg, exec_price=exec_price)
    if not res.get("ok"):
        return jsonify(res)

    action_labels = {"BUY": "买入", "SELL": "卖出", "CANCEL": "撤单"}
    action_label = action_labels.get(res["action"], res["action"])
    # executed=✅ 成功 / submitted=⏳ 已提交未确认 / cancelled=🧹 已撤 / 其他=❌ 失败
    if status == "executed":
        sign = "✅"; log_level = "buy"
    elif status == "submitted":
        sign = "⏳"; log_level = "info"
    elif status == "cancelled":
        sign = "🧹"; log_level = "info"
    else:
        sign = "❌"; log_level = "error"
    log_msg = f"{action_label} {res['code']} {res['name'] or ''} {res['shares']}股 @{res['price']:.2f} — 执行完毕: {msg}"
    _add_log(uid, f"{sign} [实盘回传] {log_msg}", log_level)

    # 同步更新云端的仓位视图缓存
    s = get_user_state(uid)
    code = res["code"]
    if status == "executed":
        if res["action"] == "BUY":
            s["executed"] += 1
            s["today_trades"] = s.get("today_trades", 0) + 1
            # 成本一律以**真实成交均价**为准；桥接器没回传时才退回信号价。
            cost_px = float(res.get("exec_price") or res["price"])
            s["positions"][code] = {
                "name": res["name"],
                "shares": res["shares"],
                "cost": cost_px,
                "price_source": res.get("price_source", "signal"),
                "date": datetime.now().strftime("%Y-%m-%d"),
            }
            # ── 实盘持仓台账（打板 Ride 自动卖出的数据源）──
            # 买入成交确认 → 记台账（带当前策略，da_ban 才会被 Ride 自动管理卖出）。
            try:
                stg = s.get("active_strategy") or ""
                um.record_live_position(uid, code, res["name"] or "",
                                        cost_px, int(res["shares"]), strategy=stg)
                if res.get("price_source") != "exec":
                    # 止损/止盈/回落都基于这个成本价算，用信号价顶替时要留痕，
                    # 否则事后无从判断风控基准是不是漂了。
                    logger.warning(
                        "[live台账 uid=%s] %s 未拿到成交价，成本按信号价 %.3f 记账"
                        "（止损止盈基准可能偏离真实成交 1%%-3%%）", uid, code, cost_px)
            except Exception as _e:
                logger.warning(f"[live台账 uid={uid}] 记录买入失败（不影响下单）: {_e}")
            # 同步执行队列状态：找到这条队列项，标记 executed（含旧版误标 done 的）
            for q in s.get("queue", []):
                if q.get("code") == code and q.get("status") in ("pending", "submitted", "sent", "done"):
                    q["status"] = "executed"
                    break
        elif res["action"] == "SELL":
            s["executed"] += 1
            s["positions"].pop(code, None)
            # 卖出成交 → 移除台账 + 清峰值 + 解锁卖出去重
            try:
                um.remove_live_position(uid, code)
            except Exception as _e:
                logger.warning(f"[live台账 uid={uid}] 移除持仓失败: {_e}")
            _DABAN_PEAKS.pop((uid, code), None)
            _LIVE_SELL_PENDING.discard((uid, code))
    elif status == "submitted":
        # 已报未成交：同步队列为 submitted，前端如实显示"已报实盘"而非"已买"
        for q in s.get("queue", []):
            if q.get("code") == code and q.get("status") in ("pending", "sent", "done"):
                q["status"] = "submitted"
                break
    elif status == "failed":
        # 卖出失败：解锁去重，下轮可重发；若失败原因是"无持仓"说明买单其实没成交=幽灵→清台账
        if res["action"] == "SELL":
            _LIVE_SELL_PENDING.discard((uid, code))
            if any(k in (msg or "") for k in ("无持仓", "持仓不足", "可用不足", "可用为0", "数量为0")):
                try:
                    um.remove_live_position(uid, code)
                except Exception as e:
                    # 幽灵持仓没清掉 → 台账残留会持续占名额/触发无效卖出信号
                    logger.warning(f"[实盘回传 uid={uid}] 清理幽灵持仓失败 {code}: {e}")
                _DABAN_PEAKS.pop((uid, code), None)
        # ★ 桥接器执行失败：把执行队列里的"已买"假象清掉 ★
        # 之前 _auto_buy 把队列项标成 sent/submitted，前端显示"已入队/已买"
        # 现在桥接器明确失败 → 重置回 pending 状态，让用户能看清楚没买进去
        for q in s.get("queue", []):
            if q.get("code") == code and q.get("status") in ("pending", "submitted", "sent"):
                q["status"] = "failed"
                q["fail_msg"] = (msg or "桥接器执行失败")[:80]
                break
    return jsonify({"ok": True})
