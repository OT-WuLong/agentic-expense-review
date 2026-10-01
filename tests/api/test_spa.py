"""The built Vue app shares the FastAPI origin without masking API 404s."""

from fastapi.testclient import TestClient

from app import main


def test_history_fallback_and_api_boundary(tmp_path, monkeypatch) -> None:
    (tmp_path / "index.html").write_text("<div>app</div>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/app.js").write_text("console.log('app')", encoding="utf-8")
    monkeypatch.setattr(main, "WEB_ROOT", tmp_path)
    client = TestClient(main.app)

    assert client.get("/approvals/demo").text == "<div>app</div>"
    assert client.get("/assets/app.js").text == "console.log('app')"
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/api/v1/unknown").status_code == 404
