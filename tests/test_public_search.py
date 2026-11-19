import json
import threading
from unittest.mock import patch

import httpx
import pytest
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


def test_public_readonly_private_and_local_access_modes(public_app, tmp_path, monkeypatch):
    with TestClient(public_app, base_url="https://story.example") as client:
        status = client.get("/api/v1/status").json()
        assert (status["public_readonly"], status["authenticated"], status["admin"], status["jobs"]) == (True, True, False, [])
        assert (status["embedding"]["index_ready"], status["embedding"]["server_key_ready"], status["embedding"]["ready"]) == (True, False, False)
        assert client.get("/api/v1/catalog").status_code == 200
        assert client.get("/api/v1/missions/1").status_code == 200
        assert client.post("/api/v1/search", json={"query": "列车", "mode": "text"}).status_code == 200
        for route in ("settings", "jobs", "diagnostics", "package"):
            assert client.get("/api/v1/admin/" + route).status_code == 401
        assert client.post("/api/v1/admin/embedding/test", json={}).status_code == 401
        assert client.post("/api/v1/auth/login", json={"code": "private-test-admin-code", "admin": True}).status_code == 200
        assert client.get("/api/v1/admin/settings").status_code == 200

    monkeypatch.setenv("HSR_ACCESS_CODE", "private-reader-code")
    with TestClient(create_app(tmp_path / "private", mode="server"), base_url="https://story.example") as client:
        assert client.get("/api/v1/status").json()["authenticated"] is False
        assert client.get("/api/v1/catalog").status_code == 401
        assert client.get("/api/v1/missions/1").status_code == 401
        assert client.post("/api/v1/search", json={"query": "列车"}).status_code == 401
        assert client.post("/api/v1/auth/login", json={"code": "private-reader-code"}).status_code == 200
        assert client.get("/api/v1/catalog").status_code == 200
        assert client.get("/api/v1/admin/settings").status_code == 403

    monkeypatch.delenv("HSR_ACCESS_CODE", raising=False)
    with TestClient(create_app(tmp_path / "local", mode="local", bootstrap_token="test-bootstrap"), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/status").json()["authenticated"] is False
        assert client.get("/api/v1/catalog").status_code == 401
        assert client.post("/api/v1/auth/bootstrap", json={"token": "test-bootstrap"}).status_code == 200
        assert client.get("/api/v1/admin/settings").status_code == 200
        assert client.post("/api/v1/auth/bootstrap", json={"token": "test-bootstrap"}).status_code == 401


def test_public_ip_limits_and_trusted_proxy_forwarding(public_app):
    public_app.state.runtime.settings.update({"limits": {"search_per_minute": 1}})
    for peer, trusted in (("172.20.0.1", True), ("198.51.100.4", False)):
        with TestClient(public_app, base_url="https://story.example", client=(peer, 4000)) as client:
            first = client.post("/api/v1/search", json={"query": "列车", "mode": "text"}, headers={"X-Real-IP": "203.0.113.1", "X-Forwarded-For": "203.0.113.9"})
            same = client.post("/api/v1/search", json={"query": "列车", "mode": "text"}, headers={"X-Real-IP": "203.0.113.1", "X-Forwarded-For": "203.0.113.8"})
            other = client.post("/api/v1/search", json={"query": "列车", "mode": "text"}, headers={"X-Real-IP": "203.0.113.2"})
            assert first.status_code == 200
            assert same.status_code == 429
            assert other.status_code == (200 if trusted else 429)

    with TestClient(public_app, base_url="https://story.example", client=("172.20.0.1", 4000)) as client:
        for forwarded in ("203.0.113.10, 172.20.0.1", "203.0.113.11, 172.20.0.1"):
            assert client.post("/api/v1/search", json={"query": "列车", "mode": "text"}, headers={"X-Real-IP": "invalid", "X-Forwarded-For": forwarded}).status_code == 200


def test_client_query_vector_validation_and_finite_scaling(public_app):
    runtime = public_app.state.runtime
    with TestClient(public_app, base_url="https://story.example") as client, patch("backend.runtime.embed") as embedding:
        ok = client.post("/api/v1/search", json={"query": "完全无字面关联", "mode": "hybrid", "query_vector": [1., 0., 0.]}, headers={"X-Embedding-Key": "visitor-test-key"})
        assert ok.status_code == 200 and ok.json()["search_mode"] == "hybrid" and ok.json()["results"][0]["chunk_id"] == 1
        assert runtime.usage == {} and runtime.query_cache == {}

        for vector in ([1., 0.], [0., 0., 0.], [float("nan"), 0., 1.], [float("inf"), 0., 1.], [-float("inf"), 0., 1.]):
            bad = client.post("/api/v1/search", content=json.dumps({"query": "列车", "mode": "hybrid", "query_vector": vector}), headers={"Content-Type": "application/json"})
            assert bad.status_code == 400

        for vector in ([1e300, 0., 0.], [1e-300, 0., 0.]):
            scaled = client.post("/api/v1/search", json={"query": "列车", "mode": "hybrid", "query_vector": vector})
            assert scaled.status_code == 200 and scaled.json()["search_mode"] == "hybrid"

        embedding.assert_not_called()


def test_visitor_key_provider_sanitization_and_isolation(public_app, caplog):
    runtime = public_app.state.runtime
    runtime.settings.update({"limits": {"daily_api_calls": 1}})
    before = {path: path.read_bytes() for path in runtime.root.rglob("*") if path.is_file()}

    def fake(texts, config, key, **kwargs):
        assert key == "visitor-test-key"
        assert config["embedding"]["base_url"] == "https://api.siliconflow.cn/v1"
        assert config["embedding"]["model"] == "BAAI/bge-m3"
        assert (config["embedding"]["dimensions"], config["embedding"]["timeout"], kwargs["max_attempts"]) == (0, 20, 2)
        assert kwargs.get("consume") is None
        return [[0., 1., 0.]]

    with TestClient(public_app, base_url="https://story.example") as client:
        with patch("backend.runtime.embed", side_effect=fake):
            res = client.post("/api/v1/search", json={"query": "完全无字面关联", "mode": "hybrid"}, headers={"X-Embedding-Key": "visitor-test-key"})
            assert res.status_code == 200 and res.json()["search_mode"] == "hybrid" and res.json()["results"][0]["chunk_id"] == 2
        assert runtime.usage == {} and runtime.query_cache == {}
        assert not runtime.settings.secret_path.exists() and runtime.embedding_status()["server_key_ready"] is False
        assert {path: path.read_bytes() for path in runtime.root.rglob("*") if path.is_file()} == before

        for key in ("x" * 4097, "visitor\rkey", "visitor\nkey"):
            with patch("backend.runtime.embed") as embedding:
                bad = client.post("/api/v1/search", json={"query": "列车", "mode": "hybrid"}, headers={"X-Embedding-Key": key})
                assert bad.status_code == 400 and key not in bad.text
                embedding.assert_not_called()

        with patch("backend.runtime.embed", side_effect=RuntimeError("provider echoed visitor-test-key")):
            err = client.post("/api/v1/search", json={"query": "列车", "mode": "hybrid"}, headers={"X-Embedding-Key": "visitor-test-key"})
            assert err.status_code == 200 and err.json()["search_mode"] == "text"
            assert "visitor-test-key" not in err.text + caplog.text

        runtime.settings.update({"embedding": {"api_key": "server-test-key"}})
        with patch("backend.runtime.embed", side_effect=RuntimeError("authorization failed")) as embedding:
            rejected = client.post("/api/v1/search", json={"query": "列车", "mode": "hybrid"}, headers={"X-Embedding-Key": "visitor-test-key"})
            assert rejected.status_code == 200 and rejected.json()["search_mode"] == "text"
            assert embedding.call_count == 1 and embedding.call_args.args[2] == "visitor-test-key"
        assert runtime.usage == {}


def test_server_key_fallback_and_index_completeness(public_app):
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
    assert sum(runtime.usage.values()) == 1 and len(runtime.query_cache) == 1

    path, _ = runtime.active_path()
    assert runtime.embedding_status()["index_ready"] is True
    with db_connect(path) as connection:
        connection.execute("DELETE FROM chunk_vectors WHERE chunk_id=2")
    assert runtime.embedding_status()["index_ready"] is False


def test_search_collapses_mirrored_scripts_and_preserves_strong_semantic_rank(public_app):
    hit = {"chunk_id": 1, "mission_id": "1020201", "sentence_id": "202110107",
           "script": "Config/Level/Mission/1020201/Act/Act102020117.json", "line_id": "act-line"}
    mirror = {**hit, "chunk_id": 2, "script": "Story/Mission/1020201/Story102020117.json", "line_id": "story-line"}
    other = {**hit, "chunk_id": 3, "script": "Story/Mission/1020201/Story102020118.json", "line_id": "other-line"}
    with TestClient(public_app, base_url="https://story.example") as client:
        with patch("backend.search.lexical_search", return_value=[hit, mirror, other]):
            res = client.post("/api/v1/search", json={"query": "恐怖片", "mode": "fuzzy"})
            assert res.status_code == 200
            assert [result["line_id"] for result in res.json()["results"]] == ["act-line", "other-line"]

        def row(identifier):
            return {"chunk_id": identifier, "mission_id": "1", "speaker": "角色", "text": f"片段{identifier}",
                    "match_type": "keyword", "matched_field": "text", "version": "1.0", "score": 1.0}

        lexical = [row(i) for i in range(1000, 1499)] + [row(2)]
        semantic = [row(1)] + [row(i) for i in range(2000, 2498)] + [row(2)]
        runtime = public_app.state.runtime
        with patch("backend.search.lexical_search", return_value=lexical), patch.object(runtime, "semantic_search", return_value=semantic):
            ranked = client.post("/api/v1/search", json={"query": "一段剧情描述", "mode": "hybrid", "query_vector": [1., 0., 0.]})
            assert ranked.status_code == 200 and ranked.json()["results"][0]["chunk_id"] == 1


def test_embedding_retry_limit_is_bounded_without_changing_default_build_retries():
    requests = []
    original_client = httpx.Client

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(lambda req: requests.append(req) or httpx.Response(503)))

    config = {"base_url": "https://api.siliconflow.cn/v1", "model": "BAAI/bge-m3"}
    with patch("backend.embeddings.httpx.Client", side_effect=client), patch("backend.embeddings._wait"):
        with pytest.raises(EmbeddingError):
            embed(["test"], config, "visitor-test-key", max_attempts=2)
        assert len(requests) == 2
        requests.clear()
        with pytest.raises(EmbeddingError):
            embed(["test"], config, "build-test-key")
        assert len(requests) == 5
