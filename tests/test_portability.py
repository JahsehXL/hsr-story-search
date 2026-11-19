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


def test_signatures_ignore_transport_and_credentials_but_preserve_semantics():
    config = copy.deepcopy(DEFAULTS)
    original = embedding_signature(config, 1024)
    changed = copy.deepcopy(config)
    changed["embedding"].update(base_url="https://other.example/v1", api_key="another-key", batch_size=64, concurrency=4, timeout=120)
    assert embedding_signature(changed, 1024) == original
    assert legacy_embedding_signature(changed, 1024) != legacy_embedding_signature(config, 1024)
    for field, value in (("model", "other-model"), ("query_prefix", "other instruction"),
                         ("document_prefix", "document instruction"), ("extra_body", {"task": "other"})):
        variant = copy.deepcopy(config)
        variant["embedding"][field] = value
        assert embedding_signature(variant, 1024) != original
    assert embedding_signature(config, 512) != original

    nested = copy.deepcopy(config)
    nested["embedding"]["extra_body"] = {"task": "retrieval", "nested": [{"mode": "query"}]}
    nested_sig = embedding_signature(nested, 1024)
    with_creds = copy.deepcopy(nested)
    with_creds["embedding"]["extra_body"].update(api_key="not-for-export", Authorization="Bearer private")
    with_creds["embedding"]["extra_body"]["nested"][0]["password"] = "hidden-password"
    assert embedding_signature(with_creds, 1024) == nested_sig
    assert legacy_embedding_signature(with_creds, 1024) != legacy_embedding_signature(nested, 1024)
    with_creds["embedding"]["extra_body"]["task"] = "classification"
    assert embedding_signature(with_creds, 1024) != nested_sig


def test_default_settings_and_bge_request_format(tmp_path):
    fresh = Settings(tmp_path / "fresh")
    assert fresh.value["embedding"]["base_url"] == "https://api.siliconflow.cn/v1"
    assert fresh.value["embedding"]["model"] == "BAAI/bge-m3"
    assert fresh.value["embedding"]["dimensions"] == 0
    assert fresh.value["embedding"]["batch_size"] == 32

    blank_root = tmp_path / "blank"
    blank_root.mkdir()
    (blank_root / "settings.json").write_text(json.dumps({"embedding": {
        "base_url": "", "model": "", "dimensions": 0, "batch_size": 16, "concurrency": 3,
    }}), encoding="utf-8")
    blank = Settings(blank_root)
    assert blank.value["embedding"]["model"] == fresh.value["embedding"]["model"]
    assert (blank.value["embedding"]["batch_size"], blank.value["embedding"]["concurrency"]) == (32, 3)

    fresh.update({"embedding": {"base_url": "https://custom.example/v1", "model": "custom", "batch_size": 7, "query_prefix": "own prefix"}})
    restored = Settings(tmp_path / "fresh")
    assert (restored.value["embedding"]["model"], restored.value["embedding"]["batch_size"], restored.value["embedding"]["query_prefix"]) == ("custom", 7, "own prefix")
    assert restored.public()["recommended_embedding"]["model"] == "BAAI/bge-m3"

    response = httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.] + [0.] * 1023}]})
    with patch("backend.embeddings.httpx.Client") as client_class:
        client = client_class.return_value.__enter__.return_value
        client.post.return_value = response
        vectors = embed(["剧情台词"], DEFAULTS, "own-test-key", query=True)
    payload = client.post.call_args.kwargs["json"]
    assert payload["model"] == "BAAI/bge-m3" and payload["input"] == ["剧情台词"]
    assert "dimensions" not in payload and len(vectors[0]) == 1024


def test_portable_package_reuse_redacts_credentials_preserves_auto_dim_and_blocks_tampering(runtime, tmp_path):
    source = tmp_path / "source.db"
    make_database(source)
    runtime.settings.update({"embedding": {
        "dimensions": 0,
        "extra_body": {
            "task": "retrieval", "api_key": "not-for-export", "nested": [{"token": "private-token", "mode": "query"}],
            "credentials": {"key": "credential-container-private"}, "headers": {"X-API-Key": "header-private"},
            "client_secret": "client-private", "user": "account-private",
        },
    }})
    job = job_for(runtime)
    with patch("backend.runtime.embed", side_effect=fake_vectors) as provider:
        runtime.build_index(source, job)
        runtime.install_snapshot(source)
        assert provider.call_count == 1
        assert runtime.embedding_status()["index_identity"]["dimension"] == 3
        assert runtime.embedding_status()["index_request_dimensions"] == 0
        runtime.settings.update({"embedding": {"base_url": "https://provider-b.example/v1", "api_key": "test-key-b"}})
        assert runtime.embedding_status()["ready"]
        runtime.build_index(source, job)
        assert provider.call_count == 1
        package = runtime.export_package()

    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["embedding"]["identity"]["extra_body"] == {"task": "retrieval", "nested": [{"mode": "query"}]}
    serialized = json.dumps([read_info(source, "embedding"), manifest, runtime.embedding_status()])
    for private in ("not-for-export", "private-token", "credential-container-private", "header-private", "client-private", "account-private"):
        assert private not in serialized

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
        assert metadata["signature_version"] == 2 and "base_url" not in metadata["identity"] and "api_key" not in metadata["identity"]
        with patch("backend.runtime.embed", side_effect=fake_vectors) as query:
            assert [item["chunk_id"] for item in recipient.semantic_search(path, "列车", {"character": "三月七"})] == [1]
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

    tampered = tmp_path / "tampered.db"
    shutil.copy2(source, tampered)
    bad_meta = read_info(tampered, "embedding")
    bad_meta["identity"]["query_prefix"] = "changed without updating signature"
    with sqlite3.connect(tampered) as connection:
        connection.execute("UPDATE info SET value=? WHERE key='embedding'", (json.dumps(bad_meta),))
    with pytest.raises(ValueError, match="模型参数"):
        runtime.install_snapshot(tampered)


def test_legacy_signature_upgrade_without_recomputing_and_rejects_mismatch(tmp_path):
    for idx, dimension in enumerate((0, 3)):
        root = tmp_path / f"case-{idx}"
        root.mkdir()
        source = root / "source.db"
        make_database(source)
        original = configured_runtime(root / "data")
        original.settings.update({"embedding": {"dimensions": dimension}})
        with patch("backend.runtime.embed", side_effect=fake_vectors):
            original.build_index(source, job_for(original))
        original.install_snapshot(source)
        path, snapshot = original.active_path()
        config = original.settings.public()
        metadata = read_info(path, "embedding")
        metadata["signature"] = legacy_embedding_signature(config, metadata["dimension"])
        metadata.pop("signature_version")
        metadata.pop("identity")
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE info SET value=? WHERE key='embedding'", (json.dumps(metadata),))
        with sqlite3.connect(original.root / "vector-cache.db") as connection:
            connection.execute("UPDATE vectors SET namespace=?", (legacy_embedding_signature(config, int(config["embedding"]["dimensions"])),))
        with db_connect(path) as connection:
            vectors = connection.execute("SELECT chunk_id,embedding FROM chunk_vectors ORDER BY chunk_id").fetchall()
        original.close()

        with patch("backend.runtime.embed", side_effect=AssertionError("Migration must not invoke embedding API")):
            upgraded = Runtime(root / "data", "local")
            try:
                active, current = upgraded.active_path()
                assert current != snapshot and read_info(active, "embedding")["signature_version"] == 2
                assert upgraded.embedding_status()["ready"]
                with db_connect(active) as connection:
                    assert connection.execute("SELECT chunk_id,embedding FROM chunk_vectors ORDER BY chunk_id").fetchall() == vectors
                assert read_info(path, "embedding").get("signature_version", 1) == 1
                upgraded.settings.update({"embedding": {"base_url": "https://another-provider.example/v1"}})
                assert upgraded.embedding_status()["ready"]
                upgraded.build_index(source, job_for(upgraded))
            finally:
                upgraded.close()

    mismatch_root = tmp_path / "mismatch"
    mismatch_root.mkdir()
    source = mismatch_root / "source.db"
    make_database(source)
    orig = configured_runtime(mismatch_root / "data")
    with patch("backend.runtime.embed", side_effect=fake_vectors):
        orig.build_index(source, job_for(orig))
    orig.install_snapshot(source)
    path, snapshot = orig.active_path()
    meta = read_info(path, "embedding")
    meta["signature"] = legacy_embedding_signature(orig.settings.public(), meta["dimension"])
    meta.pop("signature_version")
    meta.pop("identity")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE info SET value=? WHERE key='embedding'", (json.dumps(meta),))
    orig.settings.update({"embedding": {"query_prefix": "different query preparation"}})
    orig.close()
    restored = Runtime(mismatch_root / "data", "local")
    try:
        r_path, r_current = restored.active_path()
        assert r_current == snapshot and read_info(r_path, "embedding").get("signature_version", 1) == 1
        assert not restored.embedding_status()["compatible"]
    finally:
        restored.close()


def test_mirror_download_commit_lookup_cancellation_and_ssrf_guards(runtime, tmp_path):
    for address in ("http://mirror.example", "https://user:secret@mirror.example", "https://mirror.example/?token=x", "https://mirror.example/#section", "https://"):
        with pytest.raises(ValueError):
            Settings(tmp_path).update({"updates": {"github_mirror": address}})

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
    assert requested == [f"https://mirror.example/https://github.com/owner/repo/archive/{commit}.zip", f"https://codeload.github.com/owner/repo/zip/{commit}"]
    assert zipfile.is_zipfile(target)

    lookup_urls = []

    @contextmanager
    def fallback_stream(client, url):
        lookup_urls.append(url)
        if len(lookup_urls) == 1:
            raise httpx.ConnectError("direct access unavailable")
        yield httpx.Response(200, json={"sha": "B" * 40})

    with patch("backend.runtime.public_https_stream", side_effect=fallback_stream):
        assert runtime._source_commit("owner/repo", "https://mirror.example", job_for(runtime)) == "b" * 40
    assert lookup_urls == ["https://api.github.com/repos/owner/repo/commits/HEAD", "https://mirror.example/https://api.github.com/repos/owner/repo/commits/HEAD"]

    @contextmanager
    def sha_stream(client, url):
        assert client.headers["Accept"] == "application/vnd.github.sha"
        yield httpx.Response(200, content=("A" * 40 + "\n").encode())

    with patch("backend.runtime.public_https_stream", side_effect=sha_stream):
        assert runtime._source_commit("owner/repo", "", job_for(runtime)) == "a" * 40

    for body in (b'{"sha":"main"}', b"x" * (1024**2 + 1)):
        @contextmanager
        def bad_stream(client, url, payload=body):
            yield httpx.Response(200, content=payload)
        with patch("backend.runtime.public_https_stream", side_effect=bad_stream), pytest.raises(ValueError):
            runtime._source_commit("owner/repo", "", job_for(runtime))

    with patch.object(runtime, "_download", side_effect=JobCancelled) as mock_dl, pytest.raises(JobCancelled):
        runtime._download_source("owner/repo", commit, "https://mirror.example", tmp_path / "archive.zip", job_for(runtime))
    assert mock_dl.call_count == 1
    job = job_for(runtime)
    runtime.controls[job["id"]]["cancel"].set()
    with patch("backend.runtime.public_https_stream") as mock_stream, pytest.raises(JobCancelled):
        runtime._source_commit("owner/repo", "https://mirror.example", job)
    mock_stream.assert_not_called()

    with patch("backend.runtime.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
        with httpx.Client() as client, patch.object(client, "stream") as request:
            with pytest.raises(ValueError):
                with public_https_stream(client, "https://mirror.example/archive"):
                    pass
            request.assert_not_called()
