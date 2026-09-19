from fastapi.testclient import TestClient

from devin_remediation_automation.main import create_app

client = TestClient(create_app())


def test_health_returns_healthy_status() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}
