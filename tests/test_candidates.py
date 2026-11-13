import json
import sqlite3
from unittest.mock import patch

from backend.data_maintenance import build_candidate
from backend.runtime import Runtime
from test_data_tools import source, provenance
from test_portability import configured_runtime, fake_vectors, job_for


def finish(runtime, payload):
    job = runtime.add_job(payload)
    runtime.executor.submit(lambda: None).result(timeout=30)
    return next(item for item in runtime.public_jobs() if item["id"] == job["id"])


def test_preparation_keeps_current_snapshot_until_explicit_activation(source, tmp_path):
    runtime = Runtime(tmp_path / "data", "local")
    try:
        old = tmp_path / "old.db"
        build_candidate(source, old, provenance())
        baseline = runtime.install_snapshot(old)
        prepared = finish(runtime, {"kind": "prepare_directory", "source": str(source), "provenance": provenance()})
        assert prepared["status"] == "completed", prepared
        assert runtime.active_path()[1] == baseline
        candidates = runtime.public_candidates()
        assert len(candidates) == 1 and candidates[0]["audit"]["ok"]
        activated = finish(runtime, {"kind": "activate_candidate", "candidate_id": candidates[0]["id"]})
        assert activated["status"] == "completed", activated
        assert runtime.active_path()[1] != baseline
        runtime.rollback()
        assert runtime.active_path()[1] == baseline
        stale = finish(runtime, {"kind": "activate_candidate", "candidate_id": candidates[0]["id"]})
        assert stale["status"] == "completed", stale
        changed = finish(runtime, {"kind": "activate_candidate", "candidate_id": candidates[0]["id"]})
        assert changed["status"] == "failed" and "变化" in changed["error"]
    finally:
        runtime.close()


def test_new_candidate_reuses_active_vectors_even_with_empty_cache(source, tmp_path):
    runtime = configured_runtime(tmp_path / "data")
    try:
        old = tmp_path / "old.db"
        build_candidate(source, old, provenance())
        with patch("backend.runtime.embed", side_effect=fake_vectors):
            runtime.build_index(old, job_for(runtime))
        runtime.install_snapshot(old)
        with sqlite3.connect(runtime.root / "vector-cache.db") as cache:
            cache.execute("DELETE FROM vectors")
        candidate = tmp_path / "candidate.db"
        build_candidate(source, candidate, provenance())
        with patch("backend.runtime.embed", side_effect=AssertionError("Expected old snapshot vectors to be reused")):
            runtime.build_index(candidate, job_for(runtime))
        assert runtime.embedding_status(candidate)["index_ready"]
        prepared = finish(runtime, {"kind": "prepare_directory", "source": str(source), "provenance": provenance()})
        identifier = prepared["candidate_id"]
        rejected = finish(runtime, {"kind": "activate_candidate", "candidate_id": identifier})
        assert rejected["status"] == "failed" and "索引" in rejected["error"]
        with patch("backend.runtime.embed", side_effect=AssertionError("Expected cached vectors")):
            indexed = finish(runtime, {"kind": "index_candidate", "candidate_id": identifier})
        assert indexed["status"] == "completed", indexed
        activated = finish(runtime, {"kind": "activate_candidate", "candidate_id": identifier})
        assert activated["status"] == "completed", activated
    finally:
        runtime.close()
