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
    assert "systemctl enable --now quant-scheduler" in script
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

    up_to_date_branch = update_script.split('if [ "$LOCAL" = "$REMOTE" ]; then', 1)[1].split("fi", 1)[0]
    assert 'systemctl restart "$SERVICE"' in up_to_date_branch


def test_first_time_init_installs_independent_data_scheduler():
    init_script = Path("deploy/server_init.sh").read_text(encoding="utf-8")
    update_script = Path("deploy/server_update.sh").read_text(encoding="utf-8")

    assert "deploy/quant-scheduler.service" in init_script
    assert "/etc/systemd/system/quant-scheduler.service" in init_script
    assert "systemctl enable quant-scheduler" in init_script
    assert "systemctl restart quant-scheduler" in init_script
    assert 's|/opt/quant|$APP_DIR|g' in init_script
    assert 's|/opt/quant|$APP_DIR|g' in update_script
