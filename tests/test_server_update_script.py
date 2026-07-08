# -*- coding: utf-8 -*-
from pathlib import Path


def test_server_update_syncs_nginx_anti_crawl_templates():
    script = Path("deploy/server_update.sh").read_text(encoding="utf-8")

    assert "sync_nginx_anti_crawl" in script
    assert "anti_crawl_http.conf" in script
    assert "anti_crawl_server_snippet.conf" in script
    assert "/etc/nginx/snippets/jwalpha-anti-crawl-server.conf" in script
    assert "systemctl reload nginx" in script
