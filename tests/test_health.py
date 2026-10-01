from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import main
from app.database import PostgresStore

client = TestClient(main.app)


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("unavailable", ["database", "milvus"])
def test_ready_fails_when_dependency_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    database = Mock()
    milvus = Mock()
    milvus.list_collections.return_value = ["policy_hybrid_v1"]
    if unavailable == "database":
        database.schema_ready.side_effect = RuntimeError("database down")
    else:
        milvus.list_collections.side_effect = RuntimeError("milvus down")
    monkeypatch.setattr(main, "PostgresStore", lambda: database)
    monkeypatch.setattr(main, "MilvusClient", lambda **_: milvus)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"][unavailable] == "unavailable"


def test_ready_fails_when_policy_collection_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    database = Mock()
    milvus = Mock()
    milvus.list_collections.return_value = []
    monkeypatch.setattr(main, "PostgresStore", lambda: database)
    monkeypatch.setattr(main, "MilvusClient", lambda **_: milvus)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["milvus"] == "unavailable"


def test_ready_fails_when_database_schema_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    database = PostgresStore("sqlite+pysqlite:///:memory:")
    milvus = Mock()
    milvus.list_collections.return_value = ["policy_hybrid_v1"]
    monkeypatch.setattr(main, "PostgresStore", lambda: database)
    monkeypatch.setattr(main, "MilvusClient", lambda **_: milvus)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "unavailable"

