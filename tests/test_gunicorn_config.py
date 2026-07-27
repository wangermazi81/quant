import runpy
from pathlib import Path


def test_gunicorn_uses_threads_for_ai_trader_sse_without_extra_process_state():
    cfg = runpy.run_path(str(Path(__file__).resolve().parents[1] / "deploy" / "gunicorn.conf.py"))

    assert cfg["workers"] == 1
    assert cfg["worker_class"] == "gthread"
    assert int(cfg.get("threads", 0)) >= 4


def test_all_deployment_entrypoints_reference_existing_gunicorn_config():
    root = Path(__file__).resolve().parents[1]
    expected = "deploy/gunicorn.conf.py"
    entrypoints = (
        root / "deploy" / "deploy.sh",
        root / "deploy" / "deploy.bat",
        root / "deploy" / "Procfile",
        root / "deploy" / "render.yaml",
    )

    assert (root / expected).is_file()
    for entrypoint in entrypoints:
        source = entrypoint.read_text(encoding="utf-8")
        assert expected in source, f"{entrypoint.name} 未引用 {expected}"
