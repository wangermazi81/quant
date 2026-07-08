# -*- coding: utf-8 -*-
from pathlib import Path


def test_nginx_sensitive_directory_rule_excludes_api_routes():
    snippet = Path("deploy/nginx/anti_crawl_server_snippet.conf").read_text(encoding="utf-8")

    assert "location ~* ^/(?!api/)" in snippet
    assert "logs?|data" in snippet
    assert "location /api/" in snippet


def test_nginx_sensitive_file_extension_rule_remains_enabled():
    snippet = Path("deploy/nginx/anti_crawl_server_snippet.conf").read_text(encoding="utf-8")

    assert r"\.(sql|sqlite|db|bak|old|zip|tar|gz)$" in snippet
