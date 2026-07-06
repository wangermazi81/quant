import runpy
from pathlib import Path


def test_gunicorn_uses_threads_for_ai_trader_sse_without_extra_process_state():
    cfg = runpy.run_path(str(Path(__file__).resolve().parents[1] / "deploy" / "gunicorn.conf.py"))

    assert cfg["workers"] == 1
    assert cfg["worker_class"] == "gthread"
    assert int(cfg.get("threads", 0)) >= 4
