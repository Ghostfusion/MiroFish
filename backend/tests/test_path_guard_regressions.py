from app import create_app
from app.config import Config
from app.services.report_agent import ReportManager


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(tmp_path / "simulations"))
    monkeypatch.setattr(ReportManager, "REPORTS_DIR", str(tmp_path / "uploads" / "reports"))
    (tmp_path / "uploads" / "projects").mkdir(parents=True, exist_ok=True)
    (tmp_path / "uploads" / "reports").mkdir(parents=True, exist_ok=True)
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_posts_route_rejects_platform_path_traversal(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.get(
        "/api/simulation/sim_x/posts",
        query_string={"platform": "../../../../tmp/evil"},
    )

    assert response.status_code == 400
    assert response.json["success"] is False


def test_comments_route_rejects_platform_path_traversal(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.get(
        "/api/simulation/sim_x/comments",
        query_string={"platform": "..\\..\\evil"},
    )

    assert response.status_code == 400


def test_posts_route_accepts_whitelisted_platform(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.get(
        "/api/simulation/sim_x/posts",
        query_string={"platform": "twitter"},
    )

    assert response.status_code == 200
    assert response.json["data"]["platform"] == "twitter"
    assert response.json["data"]["posts"] == []


def test_simulation_routes_reject_unsafe_ids(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.get("/api/simulation/sim%2Ebad/config/download")

    assert response.status_code == 404
    assert response.json["success"] is False


def test_delete_report_route_rejects_traversal(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    sibling = tmp_path / "uploads" / "projects" / "keep.json"
    sibling.write_text("{}", encoding="utf-8")

    response = client.delete("/api/report/..")

    assert response.status_code == 404
    assert sibling.exists()
    assert (tmp_path / "uploads" / "reports").exists()
