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
