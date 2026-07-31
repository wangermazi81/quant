# -*- coding: utf-8 -*-
from pathlib import Path


def test_server_update_syncs_nginx_anti_crawl_templates():
    script = Path("deploy/server_update.sh").read_text(encoding="utf-8")

    assert "sync_nginx_anti_crawl" in script
    assert "anti_crawl_http.conf" in script
    assert "anti_crawl_server_snippet.conf" in script
    assert "/etc/nginx/snippets/jwalpha-anti-crawl-server.conf" in script
    assert "systemctl reload nginx" in script


def test_server_update_installs_and_restarts_data_scheduler():
    script = Path("deploy/server_update.sh").read_text(encoding="utf-8")

    assert "ensure_data_scheduler_service" in script
    assert "data_scheduler_service_exists" in script
    assert "deploy/quant-scheduler.service" in script
    assert "systemctl enable quant-scheduler" in script
    assert "systemctl enable --now quant-scheduler" not in script
    assert "systemctl start quant-scheduler" in script
    assert "systemctl restart quant-scheduler" in script
    assert 'systemctl cat quant-scheduler.service' in script
    assert 'list-unit-files 2>/dev/null | grep -q "^quant-scheduler.service"' not in script


def test_deploy_splits_web_trading_scheduler_from_data_scheduler():
    update_script = Path("deploy/server_update.sh").read_text(encoding="utf-8")
    init_script = Path("deploy/server_init.sh").read_text(encoding="utf-8")

    assert "ensure_platform_scheduler_split" in update_script
    assert "/etc/systemd/system/$SERVICE.service.d/scheduler-split.conf" in update_script
    assert "Environment=THS_EMBED_SCHEDULER=0" in update_script
    assert "Environment=THS_EMBED_SCHEDULER=0" in init_script

    quiesce_body = update_script.split(
        "quiesce_legacy_schedulers() {",
        1,
    )[1].split("\n}", 1)[0]
    verify_body = update_script.split(
        "verify_scheduler_quiescence() {",
        1,
    )[1].split("\n}", 1)[0]
    assert (
        quiesce_body.index("systemctl stop quant-scheduler")
        < quiesce_body.index("ensure_platform_scheduler_split")
        < quiesce_body.index('systemctl restart "$SERVICE"')
    )

    update_flow = update_script.split(
        'echo "[0/6] Quiesce legacy schedulers"',
        1,
    )[1]
    assert (
        update_flow.index("quiesce_legacy_schedulers")
        < update_flow.index("git fetch origin --prune")
        < update_flow.index("systemctl start quant-scheduler")
    )
    assert (
        quiesce_body.index('systemctl restart "$SERVICE"')
        < quiesce_body.index("verify_scheduler_quiescence")
    )
    assert "check_scheduler_quiescence.py" in verify_body
    updated_branch = update_script.split(
        'echo "[5/6] Restart service"',
        1,
    )[1]
    assert (
        updated_branch.index('systemctl restart "$SERVICE"')
        < updated_branch.index("verify_scheduler_quiescence")
        < updated_branch.index("systemctl restart quant-scheduler")
    )
    up_to_date_branch = update_script.split(
        'if [ "$LOCAL" = "$REMOTE" ]; then',
        1,
    )[1].split("\nfi", 1)[0]
    assert (
        up_to_date_branch.index("verify_scheduler_quiescence")
        < up_to_date_branch.index("systemctl start quant-scheduler")
    )


def test_first_time_init_installs_independent_data_scheduler():
    init_script = Path("deploy/server_init.sh").read_text(encoding="utf-8")
    update_script = Path("deploy/server_update.sh").read_text(encoding="utf-8")

    assert "deploy/quant-scheduler.service" in init_script
    assert "/etc/systemd/system/quant-scheduler.service" in init_script
    assert "systemctl enable quant-scheduler" in init_script
    assert "systemctl restart quant-scheduler" in init_script
    assert 's|/opt/quant|$APP_DIR|g' in init_script
    assert 's|/opt/quant|$APP_DIR|g' in update_script
