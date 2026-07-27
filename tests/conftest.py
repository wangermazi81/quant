# -*- coding: utf-8 -*-
"""pytest 全局兜底。

这里的东西都是「每个测试文件本来要自己记得做、但漏一次就出事」的防护，
统一上提到 conftest，各文件里的重复样板可以逐步删掉。

1) 环境变量守卫：必须在**任何项目模块被 import 之前**设好。
   main.py 在 import 时会建表/迁移/起调度/实例化 THSOperator（用 pywinauto
   附着真实同花顺下单窗口）。此前靠 17 个测试文件各自记得设 QUANT_TEST_IMPORT，
   任何一个新文件忘记设、又恰好按字母序先被收集，本地全量测试就会在这台
   生产 Windows 机上真的去连下单程序 —— 离实盘误操作只差一步。

2) 生产库隔离：把 users.db / paper_trading.db 指到临时目录。
   曾有测试（test_live_signal_sizing）没打桩 DB_PATH，每跑一次全量测试就往
   真实生产库里写幽灵用户配置与日志，还会和运行中的服务抢 SQLite 写锁。
   用 session 级而非 function 级：保留同文件内跨用例共享状态的既有行为，
   只把「写到哪」换掉。各测试自己的 function 级打桩仍然优先生效。

3) AI 配置隔离：ai_analyst.CONFIG_FILE 指到临时文件。
   否则测试会读到真实 data/ai_config.json —— 断言失败时会把**真实 api_key
   明文打进测试输出**（CI 日志留存即等于泄密）。

4) 出网拦截：默认禁止连非本地地址。
   data/datasource_config.json 缺省即 tushare/replay，任何没打桩的取数测试
   都会发真实 HTTPS；replay 代理慢时单个用例挂起数十秒，本机全量测试根本跑不完
   （实测会在 test_lhb_engine 卡死）。现在改为立刻抛错，快速失败且可诊断。
   确实需要联网的用例加 @pytest.mark.allow_network。
"""
import os
import json
import socket
import sys
from pathlib import Path

import pytest

# ── 1) 环境变量守卫：必须在 import 项目模块之前 ─────────────────────
os.environ.setdefault("QUANT_TEST_IMPORT", "1")
os.environ.setdefault("THS_NO_SCHEDULER", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# baseline_test.py 与 test_ai_api.py 是「对运行中的 5001 服务」的独立冒烟脚本
# （直接用 python tests/xxx.py 运行，依赖 ADMIN_PASSWORD / 实时服务），
# 它们在模块级 sys.exit/SystemExit，会打断 pytest 收集，故从 pytest 中排除。
collect_ignore = ["baseline_test.py", "test_ai_api.py"]


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_network: 允许该用例连非本地地址（默认全部拦截）")


# ── 2)(3) 生产库 / AI 配置隔离 ────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def _isolate_persistent_state(tmp_path_factory):
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    data_dir = tmp_path_factory.mktemp("isolated_data")

    from core import user_manager as um
    from broker import paper_broker as pb

    mp.setattr(um, "DB_PATH", data_dir / "users.db", raising=False)
    mp.setattr(pb, "DB_PATH", data_dir / "paper_trading.db", raising=False)


    from analytics import datasource_config
    datasource_path = data_dir / "datasource_config.json"
    datasource_path.write_text(json.dumps({
        "schema_version": 2,
        "provider": "current",
        "tushare_enabled": False,
        "tushare_mode": "replay",
        "tushare_token": "",
        "fallback_enabled": False,
    }), encoding="utf-8")
    mp.setattr(datasource_config, "CONFIG_FILE", datasource_path, raising=False)
    datasource_config._RAW_CACHE, datasource_config._RAW_CACHE_SIG = None, None
    try:
        from analytics import ai_analyst
        mp.setattr(ai_analyst, "CONFIG_FILE", data_dir / "ai_config.json", raising=False)
    except Exception:                       # pragma: no cover - 依赖缺失时不阻断
        pass

    # 真实密钥主要来自 .env → os.environ（ai_analyst.load_config 的第一优先级），
    # 只隔离配置文件是不够的：断言失败时仍会把真 key 明文打进测试输出。
    # 注意：不要删 SECRET_KEY —— main._load_or_create_secret_key 在缺失时会往
    # 真实 data/.secret_key 写文件，反而污染生产目录。
    for _k in ("AI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
               "AI_BASE_URL", "AI_MODEL", "AI_PROVIDER", "TUSHARE_TOKEN", "TUSHARE_REPLAY_API_KEY"):
        mp.delenv(_k, raising=False)

    yield data_dir
    mp.undo()


@pytest.fixture(autouse=True)
def _clear_process_caches():
    """清掉跨测试残留的进程级缓存，避免用例互相污染。"""
    yield
    try:
        from broker import paper_broker as pb
        pb._brokers.clear()
    except Exception:
        pass
    try:
        import app_state
        app_state.user_states.clear()
    except Exception:
        pass


# ── 4) 出网拦截 ──────────────────────────────────────────────────
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


def _host_of(address):
    if isinstance(address, tuple) and address:
        return address[0]
    return address


@pytest.fixture(autouse=True)
def _block_outbound_network(request, monkeypatch):
    if request.node.get_closest_marker("allow_network"):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _refuse(host):
        raise RuntimeError(
            f"测试默认禁止出网（目标 {host!r}）。请打桩该数据源；"
            f"确需联网请给用例加 @pytest.mark.allow_network。")

    def guarded_connect(self, address, *a, **kw):
        host = _host_of(address)
        if isinstance(host, str) and host not in _LOCAL_HOSTS:
            _refuse(host)
        return real_connect(self, address, *a, **kw)

    def guarded_connect_ex(self, address, *a, **kw):
        host = _host_of(address)
        if isinstance(host, str) and host not in _LOCAL_HOSTS:
            _refuse(host)
        return real_connect_ex(self, address, *a, **kw)

    def guarded_getaddrinfo(host, *a, **kw):
        if isinstance(host, str) and host not in _LOCAL_HOSTS:
            _refuse(host)
        return real_getaddrinfo(host, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo, raising=False)
