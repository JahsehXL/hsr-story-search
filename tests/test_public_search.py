import json
import threading
from unittest.mock import patch

import pytest
import httpx
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.embeddings import EmbeddingError, embed
from backend.runtime import db_connect
from test_runtime import make_database


@pytest.fixture
def public_app(tmp_path, monkeypatch):
    monkeypatch.setenv("HSR_ADMIN_CODE", "private-test-admin-code")
    monkeypatch.delenv("HSR_ACCESS_CODE", raising=False)
    monkeypatch.setenv("HSR_PUBLIC_PREFIX", "/")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1,172.20.0.1")
    app = create_app(tmp_path / "data", mode="server")
    runtime = app.state.runtime
    runtime.settings.update({"embedding": {"base_url": "https://custom-provider.example/v1", "model": "BAAI/bge-m3", "dimensions": 0}})
    source = tmp_path / "source.db"
    make_database(source)
    job = {"id": "test-public", "status": "running"}
    runtime.controls[job["id"]] = {"pause": threading.Event(), "cancel": threading.Event()}
    with patch("backend.runtime.embed", return_value=[[1., 0., 0.], [0., 1., 0.]]):
        runtime.build_index(source, job)
    runtime.install_snapshot(source)
    yield app
    runtime.close()


def test_public_readonly_routes_and_admin_isolation(public_app):
    with TestClient(public_app, base_url="https://story.example") as client:
        status = client.get("/api/v1/status").json()
        assert status["public_readonly"] is True
        assert status["authenticated"] is True
        assert status["admin"] is False
        assert status["jobs"] == []
        assert status["embedding"]["index_ready"] is True
        assert status["embedding"]["server_key_ready"] is False
        assert status["embedding"]["ready"] is False
        assert client.get("/api/v1/catalog").status_code == 200
        assert client.get("/api/v1/missions/1").status_code == 200
        assert client.post("/api/v1/search", json={"query": "列车", "mode": "text"}).status_code == 200
        for route in ("settings", "jobs", "diagnostics", "package"):
            assert client.get("/api/v1/admin/" + route).status_code == 401
        assert client.post("/api/v1/admin/embedding/test", json={}).status_code == 401
        login = client.post("/api/v1/auth/login", json={"code": "private-test-admin-code", "admin": True})
        assert login.status_code == 200
        assert client.get("/api/v1/admin/settings").status_code == 200


def test_optional_reader_code_preserves_private_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HSR_ADMIN_CODE", "private-test-admin-code")
    monkeypatch.setenv("HSR_ACCESS_CODE", "private-reader-code")
    with TestClient(create_app(tmp_path / "private", mode="server"), base_url="https://story.example") as client:
        assert client.get("/api/v1/status").json()["authenticated"] is False
        assert client.get("/api/v1/catalog").status_code == 401
        assert client.get("/api/v1/missions/1").status_code == 401
        assert client.post("/api/v1/search", json={"query": "列车"}).status_code == 401
        assert client.post("/api/v1/auth/login", json={"code": "private-reader-code"}).status_code == 200
        assert client.get("/api/v1/catalog").status_code == 200
        assert client.get("/api/v1/admin/settings").status_code == 403


def test_local_mode_keeps_one_use_bootstrap(tmp_path, monkeypatch):
    monkeypatch.delenv("HSR_ACCESS_CODE", raising=False)
    app = create_app(tmp_path / "local", mode="local", bootstrap_token="test-bootstrap")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/status").json()["authenticated"] is False
        assert client.get("/api/v1/catalog").status_code == 401
        assert client.post("/api/v1/auth/bootstrap", json={"token": "test-bootstrap"}).status_code == 200
        assert client.get("/api/v1/admin/settings").status_code == 200
        assert client.post("/api/v1/auth/bootstrap", json={"token": "test-bootstrap"}).status_code == 401


@pytest.mark.parametrize("peer,trusted", [("172.20.0.1", True), ("198.51.100.4", False)])
def test_public_ip_limits_trust_forwarding_only_from_proxy(public_app, peer, trusted):
    public_app.state.runtime.settings.update({"limits": {"search_per_minute": 1}})
    with TestClient(public_app, base_url="https://story.example", client=(peer, 4000)) as client:
        first = client.post("/api/v1/search", json={"query": "列车", "mode": "text"}, headers={"X-Real-IP": "203.0.113.1", "X-Forwarded-For": "203.0.113.9"})
        same = client.post("/api/v1/search", json={"query": "列车", "mode": "text"}, headers={"X-Real-IP": "203.0.113.1", "X-Forwarded-For": "203.0.113.8"})
        other = client.post("/api/v1/search", json={"query": "列车", "mode": "text"}, headers={"X-Real-IP": "203.0.113.2"})
        assert first.status_code == 200
        assert same.status_code == 429
        assert other.status_code == (200 if trusted else 429)


def test_trusted_proxy_uses_first_forwarded_address_as_fallback(public_app):
    public_app.state.runtime.settings.update({"limits": {"search_per_minute": 1}})
    with TestClient(public_app, base_url="https://story.example", client=("172.20.0.1", 4000)) as client:
        for forwarded in ("203.0.113.1, 172.20.0.1", "203.0.113.2, 172.20.0.1"):
            assert client.post("/api/v1/search", json={"query": "列车", "mode": "text"}, headers={"X-Real-IP": "invalid", "X-Forwarded-For": forwarded}).status_code == 200


def test_client_query_vector_precedes_keys_and_requires_no_admin_key(public_app):
    runtime = public_app.state.runtime
    with TestClient(public_app, base_url="https://story.example") as client, patch("backend.runtime.embed") as embedding:
        response = client.post("/api/v1/search", json={"query": "完全无字面关联", "mode": "hybrid", "query_vector": [1., 0., 0.]}, headers={"X-Embedding-Key": "visitor-test-key"})
        assert response.status_code == 200
        assert response.json()["search_mode"] == "hybrid"
        assert response.json()["results"][0]["chunk_id"] == 1
        embedding.assert_not_called()
        assert runtime.usage == {}
        assert runtime.query_cache == {}


@pytest.mark.parametrize("vector", [[1., 0.], [0., 0., 0.], [float("nan"), 0., 1.], [float("inf"), 0., 1.], [-float("inf"), 0., 1.]])
def test_invalid_query_vectors_are_rejected_without_fallback(public_app, vector):
    with TestClient(public_app, base_url="https://story.example") as client, patch("backend.runtime.embed") as embedding:
        response = client.post("/api/v1/search", content=json.dumps({"query": "列车", "mode": "hybrid", "query_vector": vector}), headers={"Content-Type": "application/json"})
        assert response.status_code == 400
        embedding.assert_not_called()


def test_finite_query_vector_scaling_handles_float32_overflow_and_underflow(public_app):
    with TestClient(public_app, base_url="https://story.example") as client:
        for vector in ([1e300, 0., 0.], [1e-300, 0., 0.]):
            response = client.post("/api/v1/search", json={"query": "列车", "mode": "hybrid", "query_vector": vector})
            assert response.status_code == 200
            assert response.json()["search_mode"] == "hybrid"


def test_visitor_key_uses_fixed_provider_without_persistence_or_admin_quota(public_app):
    runtime = public_app.state.runtime
    runtime.settings.update({"limits": {"daily_api_calls": 1}})
    before = {path: path.read_bytes() for path in runtime.root.rglob("*") if path.is_file()}
    def fake(texts, config, key, **kwargs):
        assert key == "visitor-test-key"
        assert config["embedding"]["base_url"] == "https://api.siliconflow.cn/v1"
        assert config["embedding"]["model"] == "BAAI/bge-m3"
        assert config["embedding"]["dimensions"] == 0
        assert config["embedding"]["timeout"] == 20
        assert kwargs["max_attempts"] == 2
        assert kwargs.get("consume") is None
        return [[0., 1., 0.]]
    with TestClient(public_app, base_url="https://story.example") as client, patch("backend.runtime.embed", side_effect=fake):
        response = client.post("/api/v1/search", json={"query": "完全无字面关联", "mode": "hybrid"}, headers={"X-Embedding-Key": "visitor-test-key"})
        assert response.status_code == 200
        assert response.json()["results"][0]["chunk_id"] == 2
        assert response.json()["search_mode"] == "hybrid"
    assert runtime.usage == {}
    assert runtime.query_cache == {}
    assert {path: path.read_bytes() for path in runtime.root.rglob("*") if path.is_file()} == before


def test_visitor_api_errors_are_sanitized(public_app, caplog):
    with TestClient(public_app, base_url="https://story.example") as client, patch("backend.runtime.embed", side_effect=RuntimeError("provider echoed visitor-test-key")):
        response = client.post("/api/v1/search", json={"query": "列车", "mode": "hybrid"}, headers={"X-Embedding-Key": "visitor-test-key"})
        assert response.status_code == 200
        assert response.json()["search_mode"] == "text"
        assert "visitor-test-key" not in response.text + caplog.text


def test_invalid_visitor_key_never_uses_server_key(public_app):
    runtime = public_app.state.runtime
    runtime.settings.update({"embedding": {"api_key": "server-test-key"}})
    with TestClient(public_app, base_url="https://story.example") as client, patch("backend.runtime.embed", side_effect=RuntimeError("authorization failed")) as embedding:
        response = client.post("/api/v1/search", json={"query": "列车", "mode": "hybrid"}, headers={"X-Embedding-Key": "visitor-test-key"})
        assert response.status_code == 200
        assert response.json()["search_mode"] == "text"
        assert embedding.call_count == 1
        assert embedding.call_args.args[2] == "visitor-test-key"
    assert runtime.usage == {}


@pytest.mark.parametrize("key", ["x" * 4097, "visitor\rkey", "visitor\nkey"])
def test_invalid_guest_key_headers_are_rejected(public_app, key):
    with TestClient(public_app, base_url="https://story.example") as client, patch("backend.runtime.embed") as embedding:
        response = client.post("/api/v1/search", json={"query": "列车", "mode": "hybrid"}, headers={"X-Embedding-Key": key})
        assert response.status_code == 400
        assert key not in response.text
        embedding.assert_not_called()


def test_server_key_fallback_keeps_budget_and_cache(public_app):
    runtime = public_app.state.runtime
    runtime.settings.update({"embedding": {"api_key": "server-test-key"}})
    def fake(texts, config, key, **kwargs):
        assert key == "server-test-key"
        kwargs["consume"]()
        return [[1., 0., 0.]]
    with TestClient(public_app, base_url="https://story.example") as client, patch("backend.runtime.embed", side_effect=fake) as embedding:
        for _ in range(2):
            response = client.post("/api/v1/search", json={"query": "列车", "mode": "hybrid"})
            assert response.json()["search_mode"] == "hybrid"
        assert embedding.call_count == 1
    assert sum(runtime.usage.values()) == 1
    assert len(runtime.query_cache) == 1


def test_index_ready_requires_the_complete_vector_table(public_app):
    runtime = public_app.state.runtime
    path, _ = runtime.active_path()
    assert runtime.embedding_status()["index_ready"] is True
    with db_connect(path) as connection:
        connection.execute("DELETE FROM chunk_vectors WHERE chunk_id=2")
    assert runtime.embedding_status()["index_ready"] is False


def test_guest_key_is_never_a_persistent_api_key(public_app):
    runtime = public_app.state.runtime
    with TestClient(public_app, base_url="https://story.example") as client, patch("backend.runtime.embed", return_value=[[1., 0., 0.]]):
        assert client.post("/api/v1/search", json={"query": "列车", "mode": "hybrid"}, headers={"X-Embedding-Key": "visitor-test-key"}).status_code == 200
    assert not runtime.settings.secret_path.exists()
    assert runtime.embedding_status()["server_key_ready"] is False


def test_embedding_retry_limit_is_bounded_without_changing_default_build_retries():
    requests = []
    def failed(request):
        requests.append(request)
        return httpx.Response(503)
    original_client = httpx.Client
    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(failed))
    config = {"base_url": "https://api.siliconflow.cn/v1", "model": "BAAI/bge-m3"}
    with patch("backend.embeddings.httpx.Client", side_effect=client), patch("backend.embeddings._wait"):
        with pytest.raises(EmbeddingError):
            embed(["test"], config, "visitor-test-key", max_attempts=2)
        assert len(requests) == 2
        requests.clear()
        with pytest.raises(EmbeddingError):
            embed(["test"], config, "build-test-key")
        assert len(requests) == 5


def test_weak_overlap_does_not_override_high_ranked_semantic_result(public_app):
    def result(identifier):
        return {"chunk_id": identifier, "mission_id": "1", "speaker": "角色", "text": f"片段{identifier}",
                "match_type": "keyword", "matched_field": "text", "version": "1.0", "score": 1.0}
    lexical = [result(i) for i in range(1000, 1499)] + [result(2)]
    semantic = [result(1)] + [result(i) for i in range(2000, 2498)] + [result(2)]
    runtime = public_app.state.runtime
    with TestClient(public_app, base_url="https://story.example") as client, \
            patch("backend.search.lexical_search", return_value=lexical), \
            patch.object(runtime, "semantic_search", return_value=semantic):
        response = client.post("/api/v1/search", json={"query": "一段剧情描述", "mode": "hybrid", "query_vector": [1., 0., 0.]})
        assert response.status_code == 200
        assert response.json()["search_mode"] == "hybrid"
        assert response.json()["results"][0]["chunk_id"] == 1
