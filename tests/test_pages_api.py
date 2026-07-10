import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"


def test_public_changelog_uses_safe_default_for_invalid_limit(monkeypatch):
    import main
    import web.pages as pages

    seen = {}
    monkeypatch.setattr(
        pages.clm,
        "list_changelog",
        lambda limit=50, published_only=True: seen.setdefault(
            "items", [{"limit": limit, "published_only": published_only}]
        ),
    )
    monkeypatch.setattr(pages.clm, "count", lambda: 1)

    main.app.config.update(TESTING=False)
    response = main.app.test_client().get("/api/changelog?limit=abc")
    data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["items"][0]["limit"] == 50
    assert data["items"][0]["published_only"] is True


def test_public_changelog_clamps_limit_to_public_bounds(monkeypatch):
    import main
    import web.pages as pages

    limits = []
    monkeypatch.setattr(
        pages.clm,
        "list_changelog",
        lambda limit=50, published_only=True: limits.append(limit) or [],
    )
    monkeypatch.setattr(pages.clm, "count", lambda: 0)

    main.app.config.update(TESTING=False)
    assert main.app.test_client().get("/api/changelog?limit=-5").status_code == 200
    assert main.app.test_client().get("/api/changelog?limit=9999").status_code == 200

    assert limits == [1, 200]
