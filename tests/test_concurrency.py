import inspect
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from test_runtime import make_database


@pytest.fixture
def search_client(tmp_path):
    source = tmp_path / "source.db"
    make_database(source)
    app = create_app(tmp_path / "data", mode="local", bootstrap_token="concurrency-test-bootstrap")
    app.state.runtime.install_snapshot(source)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/v1/search")
    slots = inspect.getclosurevars(endpoint).nonlocals["search_slots"]
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        response = client.post("/api/v1/auth/bootstrap", json={"token": "concurrency-test-bootstrap"})
        assert response.status_code == 200
        yield client, slots


def test_eight_searches_enter_and_ninth_waits_then_succeeds(search_client):
    client, slots = search_client
    releases = {f"query-{index}": threading.Event() for index in range(8)}
    eight_entered = threading.Event()
    ninth_waiting = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    acquire_calls = 0
    original_acquire = slots.acquire

    def acquire(*args, **kwargs):
        nonlocal acquire_calls
        with lock:
            acquire_calls += 1
            if acquire_calls == 9:
                ninth_waiting.set()
        return original_acquire(*args, **kwargs)

    def lexical(path, query, *args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 8:
                eight_entered.set()
        try:
            if query in releases:
                assert releases[query].wait(timeout=5), "Test search was not released within its deadline"
            return []
        finally:
            with lock:
                active -= 1

    def request(query):
        return client.post("/api/v1/search", json={"query": query, "mode": "text"})

    with patch.object(slots, "acquire", side_effect=acquire), patch("backend.search.lexical_search", side_effect=lexical):
        with ThreadPoolExecutor(max_workers=9) as executor:
            futures = [executor.submit(request, query) for query in releases]
            try:
                assert eight_entered.wait(timeout=3), "All eight search slots should reach retrieval concurrently"
                ninth = executor.submit(request, "ninth-query")
                assert ninth_waiting.wait(timeout=3), "Ninth request should reach the semaphore"
                with pytest.raises(TimeoutError):
                    ninth.result(timeout=.08)
                releases["query-0"].set()
                response = ninth.result(timeout=3)
                assert response.status_code == 200
                assert response.json()["elapsed_ms"] >= 50
            finally:
                for release in releases.values():
                    release.set()
            assert all(future.result(timeout=3).status_code == 200 for future in futures)
    assert peak == 8
    assert active == 0


def test_search_exception_releases_its_slot(search_client):
    client, slots = search_client
    with patch("backend.search.lexical_search", side_effect=RuntimeError("simulated retrieval failure")):
        response = client.post("/api/v1/search", json={"query": "failure", "mode": "text"})
    assert response.status_code == 500
    acquired = 0
    try:
        for _ in range(8):
            assert slots.acquire(blocking=False), "Failed retrieval must release its semaphore slot"
            acquired += 1
        assert not slots.acquire(blocking=False)
    finally:
        for _ in range(acquired):
            slots.release()
    with patch("backend.search.lexical_search", return_value=[]):
        assert client.post("/api/v1/search", json={"query": "recovered", "mode": "text"}).status_code == 200


def test_queue_timeout_uses_bounded_wait_and_retry_after(search_client):
    client, slots = search_client
    with patch.object(slots, "acquire", return_value=False) as acquire, patch.object(slots, "release") as release:
        with patch("backend.search.lexical_search") as lexical:
            response = client.post("/api/v1/search", json={"query": "queued", "mode": "text"})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2"
    acquire.assert_called_once_with(timeout=5.0)
    release.assert_not_called()
    lexical.assert_not_called()
