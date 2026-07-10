import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"


def test_health_endpoint_is_public_and_minimal():
    import main

    main.app.config.update(TESTING=True)
    response = main.app.test_client().get("/api/health")
    data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["status"] == "healthy"
    assert data["service"] == "jwalpha"
    assert "server_time" in data

    payload_text = response.get_data(as_text=True).lower()
    for secret_word in ("secret", "password", "token", "api_key", "database", "users.db"):
        assert secret_word not in payload_text
