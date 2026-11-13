import copy
import json
import shutil
import sqlite3
import threading
import zipfile
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

from backend.embeddings import embed
from backend.runtime import JobCancelled, Runtime, db_connect, public_https_stream, read_info
from backend.settings import DEFAULTS, Settings, embedding_signature, legacy_embedding_signature
from test_runtime import make_database


def configured_runtime(root):
    runtime = Runtime(root, "local")
    runtime.settings.update({"embedding": {
        "base_url": "https://provider-a.example/v1", "model": "shared-test-model",
        "dimensions": 3, "query_prefix": "", "document_prefix": "", "api_key": "test-key-a",
    }})
    return runtime


def job_for(runtime):
    job = {"id": "portability-test", "status": "running"}
    runtime.controls[job["id"]] = {"pause": threading.Event(), "cancel": threading.Event()}
    return job


def fake_vectors(texts, *args, **kwargs):
    return [[1., 0., 0.] if "列车" in text else [0., 1., 0.] for text in texts]


@pytest.fixture
def runtime(tmp_path):
    instance = configured_runtime(tmp_path / "data")
    try:
        yield instance
    finally:
        instance.close()


def test_signatures_ignore_transport_but_preserve_vector_identity():
    config = copy.deepcopy(DEFAULTS)
    original = embedding_signature(config, 1024)
    changed = copy.deepcopy(config)
    changed["embedding"].update(base_url="https://other.example/v1", api_key="another-key", batch_size=64, concurrency=4, timeout=120)
    assert embedding_signature(changed, 1024) == original
    assert legacy_embedding_signature(changed, 1024) != legacy_embedding_signature(config, 1024)
    for field, value in (("model", "other-model"), ("query_prefix", "other instruction"),
                         ("document_prefix", "document instruction"), ("extra_body", {"task": "other"})):
        changed = copy.deepcopy(config)
        changed["embedding"][field] = value
        assert embedding_signature(changed, 1024) != original
    assert embedding_signature(config, 512) != original


def test_signature_ignores_nested_credentials_but_preserves_semantic_parameters():
    config = copy.deepcopy(DEFAULTS)
    config["embedding"]["extra_body"] = {"task": "retrieval", "nested": [{"mode": "query"}]}
    original = embedding_signature(config, 1024)
    with_credentials = copy.deepcopy(config)
    with_credentials["embedding"]["extra_body"].update(api_key="not-for-export", Authorization="Bearer private")
    with_credentials["embedding"]["extra_body"]["nested"][0]["password"] = "hidden-password"
    assert embedding_signature(with_credentials, 1024) == original
    assert legacy_embedding_signature(with_credentials, 1024) != legacy_embedding_signature(config, 1024)
    with_credentials["embedding"]["extra_body"]["task"] = "classification"
    assert embedding_signature(with_credentials, 1024) != original


def test_new_and_empty_settings_use_domestic_preset_without_overwriting_custom_settings(tmp_path):
    fresh = Settings(tmp_path / "fresh")
    assert fresh.value["embedding"]["base_url"] == "https://api.siliconflow.cn/v1"
    assert fresh.value["embedding"]["model"] == "BAAI/bge-m3"
    assert fresh.value["embedding"]["dimensions"] == 0
    assert fresh.value["embedding"]["query_prefix"] == ""
    assert fresh.value["embedding"]["document_prefix"] == ""
    assert fresh.value["embedding"]["batch_size"] == 32
    blank_root = tmp_path / "blank"
    blank_root.mkdir()
    (blank_root / "settings.json").write_text(json.dumps({"embedding": {
        "base_url": "", "model": "", "dimensions": 0, "batch_size": 16, "concurrency": 3,
    }}), encoding="utf-8")
    blank = Settings(blank_root)
    assert blank.value["embedding"]["model"] == fresh.value["embedding"]["model"]
    assert blank.value["embedding"]["batch_size"] == 32
    assert blank.value["embedding"]["concurrency"] == 3
    fresh.update({"embedding": {"base_url": "https://custom.example/v1", "model": "custom", "batch_size": 7, "query_prefix": "own prefix"}})
    restored = Settings(tmp_path / "fresh")
    assert restored.value["embedding"]["model"] == "custom"
    assert restored.value["embedding"]["batch_size"] == 7
    assert restored.value["embedding"]["query_prefix"] == "own prefix"
    assert restored.public()["recommended_embedding"]["model"] == "BAAI/bge-m3"


def test_default_bge_request_omits_dimensions_and_instructions():
    response = httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.] + [0.] * 1023}]})
    with patch("backend.embeddings.httpx.Client") as client_class:
        client = client_class.return_value.__enter__.return_value
        client.post.return_value = response
        vectors = embed(["剧情台词"], DEFAULTS, "own-test-key", query=True)
    payload = client.post.call_args.kwargs["json"]
    assert payload["model"] == "BAAI/bge-m3"
    assert payload["input"] == ["剧情台词"]
    assert "dimensions" not in payload
    assert len(vectors[0]) == 1024


def test_endpoint_change_reuses_author_cache_and_exported_index_works_for_recipient(runtime, tmp_path):
    source = tmp_path / "source.db"
    make_database(source)
    job = job_for(runtime)
    with patch("backend.runtime.embed", side_effect=fake_vectors) as provider:
        runtime.build_index(source, job)
        runtime.install_snapshot(source)
        assert provider.call_count == 1
        runtime.settings.update({"embedding": {"base_url": "https://provider-b.example/v1", "api_key": "test-key-b"}})
        assert runtime.embedding_status()["ready"]
        runtime.build_index(source, job)
        assert provider.call_count == 1
        package = runtime.export_package()
    recipient = Runtime(tmp_path / "recipient", "local")
    try:
        recipient.settings.update({"embedding": {
            **runtime.settings.value["embedding"], "base_url": "https://provider-c.example/v1", "api_key": "recipient-key",
        }})
        with patch("backend.runtime.embed", side_effect=AssertionError("Import must reuse existing vectors")):
            recipient.install_package(package, tmp_path / "unpacked")
            assert recipient.embedding_status()["ready"]
        path, _ = recipient.active_path()
        metadata = read_info(path, "embedding")
        assert metadata["signature_version"] == 2
        assert "base_url" not in metadata["identity"]
        assert "api_key" not in metadata["identity"]
        with patch("backend.runtime.embed", side_effect=fake_vectors) as query:
            matches = recipient.semantic_search(path, "列车", {"character": "三月七"})
            assert [item["chunk_id"] for item in matches] == [1]
            assert query.call_count == 1
        reindexed = tmp_path / "recipient-reindex.db"
        shutil.copy2(path, reindexed)
        with patch("backend.runtime.embed", side_effect=AssertionError("Existing shared vectors must seed the local cache")):
            recipient.build_index(reindexed, job_for(recipient))
            recipient.build_index(reindexed, job_for(recipient))
        recipient.settings.update({"embedding": {"query_prefix": "incompatible prefix"}})
        assert not recipient.embedding_status()["compatible"]
    finally:
        recipient.close()


def make_legacy_index(runtime, source, job):
    with patch("backend.runtime.embed", side_effect=fake_vectors):
        runtime.build_index(source, job)
    runtime.install_snapshot(source)
    path, snapshot = runtime.active_path()
    config = runtime.settings.public()
    metadata = read_info(path, "embedding")
    metadata["signature"] = legacy_embedding_signature(config, metadata["dimension"])
    metadata.pop("signature_version")
    metadata.pop("identity")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE info SET value=? WHERE key='embedding'", (json.dumps(metadata),))
    namespace_dimension = int(config["embedding"]["dimensions"])
    with sqlite3.connect(runtime.root / "vector-cache.db") as connection:
        connection.execute("UPDATE vectors SET namespace=?", (legacy_embedding_signature(config, namespace_dimension),))
    return path, snapshot


@pytest.mark.parametrize("dimension", [0, 3])
def test_legacy_signature_and_cache_upgrade_without_recomputing(tmp_path, dimension):
    source = tmp_path / "source.db"
    make_database(source)
    original = configured_runtime(tmp_path / "data")
    original.settings.update({"embedding": {"dimensions": dimension}})
    path, snapshot = make_legacy_index(original, source, job_for(original))
    with db_connect(path) as connection:
        vectors = connection.execute("SELECT chunk_id,embedding FROM chunk_vectors ORDER BY chunk_id").fetchall()
    original.close()
    with patch("backend.runtime.embed", side_effect=AssertionError("Migration must not invoke embedding API")):
        upgraded = Runtime(tmp_path / "data", "local")
        try:
            active, current = upgraded.active_path()
            assert current != snapshot
            metadata = read_info(active, "embedding")
            assert metadata["signature_version"] == 2
            assert upgraded.embedding_status()["ready"]
            with db_connect(active) as connection:
                assert connection.execute("SELECT chunk_id,embedding FROM chunk_vectors ORDER BY chunk_id").fetchall() == vectors
            assert read_info(path, "embedding").get("signature_version", 1) == 1
            upgraded.settings.update({"embedding": {"base_url": "https://another-provider.example/v1"}})
            assert upgraded.embedding_status()["ready"]
            upgraded.build_index(source, job_for(upgraded))
        finally:
            upgraded.close()


def test_legacy_index_does_not_upgrade_against_unverified_settings(tmp_path):
    source = tmp_path / "source.db"
    make_database(source)
    original = configured_runtime(tmp_path / "data")
    _, snapshot = make_legacy_index(original, source, job_for(original))
    original.settings.update({"embedding": {"query_prefix": "different query preparation"}})
    original.close()
    restored = Runtime(tmp_path / "data", "local")
    try:
        path, current = restored.active_path()
        assert current == snapshot
        assert read_info(path, "embedding").get("signature_version", 1) == 1
        assert not restored.embedding_status()["compatible"]
    finally:
        restored.close()


def test_exported_embedding_metadata_omits_credentials_from_extra_parameters(runtime, tmp_path):
    source = tmp_path / "source.db"
    make_database(source)
    runtime.settings.update({"embedding": {"extra_body": {
        "task": "retrieval", "api_key": "not-for-export", "nested": [{"token": "private-token", "mode": "query"}],
        "credentials": {"key": "credential-container-private"},
        "headers": {"X-API-Key": "header-private"},
        "client_secret": "client-private", "user": "account-private",
    }}})
    with patch("backend.runtime.embed", side_effect=fake_vectors):
        runtime.build_index(source, job_for(runtime))
    runtime.install_snapshot(source)
    with zipfile.ZipFile(runtime.export_package()) as package:
        manifest = json.loads(package.read("manifest.json"))
    extra = manifest["embedding"]["identity"]["extra_body"]
    assert extra == {"task": "retrieval", "nested": [{"mode": "query"}]}
    metadata = read_info(source, "embedding")
    serialized = json.dumps([metadata, manifest, runtime.embedding_status()])
    for private in ("not-for-export", "private-token", "credential-container-private", "header-private", "client-private", "account-private"):
        assert private not in serialized


def test_shared_model_parameters_preserve_automatic_request_dimensions(runtime, tmp_path):
    source = tmp_path / "source.db"
    make_database(source)
    runtime.settings.update({"embedding": {"dimensions": 0}})
    with patch("backend.runtime.embed", side_effect=fake_vectors):
        runtime.build_index(source, job_for(runtime))
    runtime.install_snapshot(source)
    status = runtime.embedding_status()
    assert status["index_identity"]["dimension"] == 3
    assert status["index_request_dimensions"] == 0


def test_v2_identity_tampering_is_rejected(runtime, tmp_path):
    source = tmp_path / "source.db"
    make_database(source)
    with patch("backend.runtime.embed", side_effect=fake_vectors):
        runtime.build_index(source, job_for(runtime))
    metadata = read_info(source, "embedding")
    metadata["identity"]["query_prefix"] = "changed without updating the signature"
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE info SET value=? WHERE key='embedding'", (json.dumps(metadata),))
    with pytest.raises(ValueError, match="模型参数"):
        runtime.install_snapshot(source)


@pytest.mark.parametrize("address", ["http://mirror.example", "https://user:secret@mirror.example", "https://mirror.example/?token=x", "https://mirror.example/#section", "https://"])
def test_mirror_configuration_rejects_unsafe_prefix_syntax(tmp_path, address):
    with pytest.raises(ValueError):
        Settings(tmp_path).update({"updates": {"github_mirror": address}})


def test_mirror_download_uses_pinned_archive_url_and_falls_back_for_html(runtime, tmp_path):
    commit = "a" * 40
    target = tmp_path / "source.zip"
    requested = []

    def download(url, archive, job):
        requested.append(url)
        if len(requested) == 1:
            archive.write_text("<html>upstream error</html>")
        else:
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("repo-" + commit + "/TextMap.json", "{}")

    with patch.object(runtime, "_download", side_effect=download):
        runtime._download_source("owner/repo", commit, "https://mirror.example", target, job_for(runtime))
    assert requested == [
        f"https://mirror.example/https://github.com/owner/repo/archive/{commit}.zip",
        f"https://codeload.github.com/owner/repo/zip/{commit}",
    ]
    assert zipfile.is_zipfile(target)


def test_commit_lookup_falls_back_to_api_capable_mirror(runtime):
    requested = []

    @contextmanager
    def stream(client, url):
        requested.append(url)
        if len(requested) == 1:
            raise httpx.ConnectError("direct access unavailable")
        yield httpx.Response(200, json={"sha": "B" * 40})

    with patch("backend.runtime.public_https_stream", side_effect=stream):
        commit = runtime._source_commit("owner/repo", "https://mirror.example", job_for(runtime))
    assert commit == "b" * 40
    assert requested == [
        "https://api.github.com/repos/owner/repo/commits/HEAD",
        "https://mirror.example/https://api.github.com/repos/owner/repo/commits/HEAD",
    ]


def test_commit_lookup_requests_compact_sha_response(runtime):
    @contextmanager
    def stream(client, url):
        assert client.headers["Accept"] == "application/vnd.github.sha"
        yield httpx.Response(200, content=("A" * 40 + "\n").encode())

    with patch("backend.runtime.public_https_stream", side_effect=stream):
        assert runtime._source_commit("owner/repo", "", job_for(runtime)) == "a" * 40


@pytest.mark.parametrize("body", [b'{"sha":"main"}', b"x" * (1024**2 + 1)])
def test_commit_lookup_rejects_invalid_or_oversized_metadata(runtime, body):
    @contextmanager
    def stream(client, url):
        yield httpx.Response(200, content=body)

    with patch("backend.runtime.public_https_stream", side_effect=stream), pytest.raises(ValueError):
        runtime._source_commit("owner/repo", "", job_for(runtime))


def test_cancellation_does_not_trigger_download_fallback(runtime, tmp_path):
    with patch.object(runtime, "_download", side_effect=JobCancelled) as download, pytest.raises(JobCancelled):
        runtime._download_source("owner/repo", "a" * 40, "https://mirror.example", tmp_path / "archive.zip", job_for(runtime))
    assert download.call_count == 1
    job = job_for(runtime)
    runtime.controls[job["id"]]["cancel"].set()
    with patch("backend.runtime.public_https_stream") as stream, pytest.raises(JobCancelled):
        runtime._source_commit("owner/repo", "https://mirror.example", job)
    stream.assert_not_called()


def test_mirror_fetch_still_blocks_private_network_destinations():
    with patch("backend.runtime.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
        with httpx.Client() as client, patch.object(client, "stream") as request:
            with pytest.raises(ValueError):
                with public_https_stream(client, "https://mirror.example/archive"):
                    pass
            request.assert_not_called()
