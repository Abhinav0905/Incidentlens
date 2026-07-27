from fastapi.testclient import TestClient

from incidentlens.api import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_scenarios_are_listed() -> None:
    response = client.get("/api/v1/scenarios")
    names = {item["name"] for item in response.json()}
    assert {"checkout-secret-rotation", "cache-stampede"} <= names


def test_unknown_scenario_is_404() -> None:
    assert client.get("/api/v1/scenarios/nope").status_code == 404
    response = client.post("/api/v1/incidents/analyze", json={"scenario": "nope"})
    assert response.status_code == 404


def test_default_alias_analyzes() -> None:
    response = client.post("/api/v1/incidents/analyze", json={"scenario": "default"})
    assert response.status_code == 200
    body = response.json()
    assert body["title"]
    assert body["hypotheses"]
    assert body["propagation"]
