import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.embeddings import EmbeddingError, embed
from backend.runtime import ClosingConnection, Runtime, safe_extract


def make_database(path):
    with sqlite3.connect(path, factory=ClosingConnection) as connection:
        connection.executescript("""
        CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT);
        INSERT INTO info VALUES('schema_version','1');
        CREATE TABLE missions(id TEXT PRIMARY KEY,name TEXT,chapter TEXT,world TEXT,type TEXT,version TEXT,version_source TEXT,version_status TEXT);
        CREATE TABLE lines(id TEXT PRIMARY KEY,mission_id TEXT,script TEXT,sequence INTEGER,position INTEGER,sentence_id TEXT,speaker TEXT,text TEXT,search_text TEXT,kind TEXT,branch TEXT,trigger TEXT,chunk_id INTEGER);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY,stable_id TEXT,mission_id TEXT,script TEXT,sequence INTEGER,branch TEXT,text TEXT,input_text TEXT,content_hash TEXT,line_ids TEXT);
        CREATE TABLE chunk_lines(chunk_id INTEGER,line_id TEXT);
        CREATE TABLE edges(script TEXT,source TEXT,target TEXT,kind TEXT);
        CREATE TABLE recalls(mission_id TEXT,name TEXT);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(terms);
        INSERT INTO missions VALUES('1','任务','章节','空间站','Main','','','unknown');
        INSERT INTO chunks VALUES(1,'a','1','Act1',0,'','回到列车','回到列车','a','["a"]');
        INSERT INTO chunks VALUES(2,'b','1','Act1',1,'stay','继续调查','继续调查','b','["b"]');
        INSERT INTO lines VALUES('a','1','Act1',0,0,'1','三月七','回到列车','回到列车','simple_talk','','',1);
        INSERT INTO lines VALUES('b','1','Act1',1,0,'2','瓦尔特','继续调查','继续调查','simple_talk','stay','',2);
        INSERT INTO chunk_lines VALUES(1,'a'),(2,'b');
        """)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "source.db"
        make_database(self.database)
        self.runtime = Runtime(self.root / "data", "local")

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_snapshot_package_roundtrip_and_tamper_traversal_guards(self):
        first = self.runtime.install_snapshot(self.database)
        self.runtime.update_mission("1", "1.0", "https://example.com/source", "verified")
        package = self.runtime.export_package()
        self.assertTrue(package.is_file())
        with zipfile.ZipFile(package) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "story.db"})

        bad_pkg = self.root / "tampered.zip"
        with zipfile.ZipFile(package) as original, zipfile.ZipFile(bad_pkg, "w") as output:
            output.writestr("manifest.json", original.read("manifest.json"))
            output.writestr("story.db", b"tampered")
        with self.assertRaises(ValueError):
            self.runtime.install_package(bad_pkg, self.root / "tampered")

        traversal = self.root / "bad.zip"
        with zipfile.ZipFile(traversal, "w") as archive:
            archive.writestr("../escape", "bad")
        with self.assertRaises(ValueError):
            safe_extract(traversal, self.root / "unpack")
        self.assertFalse((self.root / "escape").exists())

        snapshot = self.runtime.install_package(package, self.root / "unpacked")
        path, current = self.runtime.active_path()
        self.assertEqual(snapshot, current)
        with sqlite3.connect(path, factory=ClosingConnection) as connection:
            self.assertEqual(connection.execute("SELECT version FROM missions").fetchone()[0], "1.0")
        self.runtime.rollback()
        self.assertNotEqual(self.runtime.active_path()[1], current)
        self.assertTrue(self.runtime.active_path(first)[0].is_file())

    def test_api_budget_and_secret_redaction(self):
        self.runtime.settings.update({"embedding": {"api_key": "secret-test-key"}, "limits": {"daily_api_calls": 1}})
        self.assertEqual(self.runtime.settings.key(), "secret-test-key")
        self.assertNotIn("secret-test-key", json.dumps(self.runtime.settings.public()))
        self.runtime.consume_api()
        with self.assertRaises(ValueError):
            self.runtime.consume_api()
        self.runtime.settings.update({"limits": {"daily_api_calls": 0}})
        self.runtime.consume_api()

    def test_vector_index_cache_compatibility_and_filtered_search(self):
        config = {"base_url": "https://example.com/v1", "model": "test", "dimensions": 3, "api_key": "secret"}
        self.runtime.settings.update({"embedding": config})
        job = {"id": "test", "status": "running"}
        self.runtime.controls["test"] = {"pause": threading.Event(), "cancel": threading.Event()}

        def fake(texts, *args, **kwargs):
            return [[1., 0., 0.] if "列车" in text else [0., 1., 0.] for text in texts]

        with patch("backend.runtime.embed", side_effect=fake) as mock:
            self.runtime.build_index(self.database, job)
            self.runtime.install_snapshot(self.database)
            path, _ = self.runtime.active_path()
            self.assertTrue(self.runtime.embedding_status()["ready"])
            results = self.runtime.semantic_search(path, "列车", {"character": "三月七"})
            self.assertEqual([item["chunk_id"] for item in results], [1])
            count = mock.call_count
            self.runtime.build_index(self.database, job)
            self.assertEqual(mock.call_count, count)
        self.runtime.settings.update({"embedding": {"model": "different"}})
        self.assertFalse(self.runtime.embedding_status()["compatible"])


class AuthTests(unittest.TestCase):
    def test_keyword_and_hybrid_fallback_require_explicit_fuzzy_mode(self):
        from backend.data import search_tokens

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            make_database(source)
            with sqlite3.connect(source, factory=ClosingConnection) as connection:
                connection.execute("INSERT INTO chunks_fts(rowid,terms) VALUES(1,?)", (search_tokens("回到列车"),))
            app = create_app(root / "data", bootstrap_token="search-mode-bootstrap")
            app.state.runtime.install_snapshot(source)
            with TestClient(app, base_url="http://127.0.0.1") as client:
                client.post("/api/v1/auth/bootstrap", json={"token": "search-mode-bootstrap"})
                for mode in ("text", "hybrid"):
                    response = client.post("/api/v1/search", json={"query": "回到列午", "mode": mode})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["results"], [])
                    self.assertEqual(response.json()["search_mode"], "text")
                approximate = client.post("/api/v1/search", json={"query": "回到列午", "mode": "fuzzy"})
                self.assertEqual(approximate.status_code, 200)
                self.assertEqual(approximate.json()["search_mode"], "fuzzy")
                self.assertEqual(approximate.json()["results"][0]["match_type"], "fuzzy")
                exact = client.post("/api/v1/search", json={"query": "回到列车", "mode": "text"})
                self.assertEqual(exact.json()["results"][0]["match_type"], "exact")

    def test_search_pins_snapshot_across_metadata_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            make_database(source)
            app = create_app(root / "data", bootstrap_token="search-bootstrap")
            runtime = app.state.runtime
            snapshot = runtime.install_snapshot(source)
            with TestClient(app, base_url="http://127.0.0.1") as client:
                client.post("/api/v1/auth/bootstrap", json={"token": "search-bootstrap"})
                before = client.post("/api/v1/search", json={"query": "列车", "mode": "text", "snapshot": snapshot})
                self.assertEqual(before.status_code, 200)
                self.assertEqual(before.json()["snapshot"], snapshot)
                mission = before.json()["results"][0]["mission_id"]
                endpoint = f"/api/v1/admin/missions/{mission}"
                update = {"version": "9.9", "source": "test source", "status": "verified", "snapshot": snapshot}
                saved = client.put(endpoint, json=update)
                self.assertEqual(saved.status_code, 200)
                rejected = client.put(endpoint, json={**update, "version": "8.8"})
                self.assertEqual(rejected.status_code, 409)
                pinned = client.post("/api/v1/search", json={"query": "列车", "mode": "text", "snapshot": snapshot}).json()
                current = client.post("/api/v1/search", json={"query": "列车", "mode": "text"}).json()
                self.assertEqual(pinned["snapshot"], snapshot)
                self.assertNotEqual(current["snapshot"], snapshot)
                self.assertEqual(current["snapshot"], saved.json()["snapshot"])
                self.assertNotEqual(pinned["results"][0]["version"], "9.9")
                self.assertEqual(current["results"][0]["version"], "9.9")

    def test_chunked_upload_auth_offsets_and_completion(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("example.txt", "story content")
        content = stream.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory), bootstrap_token="upload-bootstrap")
            with TestClient(app, base_url="http://127.0.0.1") as client:
                self.assertEqual(client.post("/api/v1/admin/upload/start", json={"size": len(content)}).status_code, 401)
                client.post("/api/v1/auth/bootstrap", json={"token": "upload-bootstrap"})
                state = client.post("/api/v1/admin/upload/start", json={"size": len(content)}).json()
                route = "/api/v1/admin/upload/" + state["upload_id"]
                midpoint = len(content) // 2
                first = client.put(route + "?offset=0", content=content[:midpoint])
                self.assertEqual((first.status_code, first.json()["offset"], first.json()["complete"]), (200, midpoint, False))
                self.assertEqual(client.put(route + "?offset=0", content=content[:midpoint]).status_code, 409)
                self.assertEqual(client.get(route).json()["offset"], midpoint)
                original_open = Path.open
                partial_name = state["upload_id"] + ".part"

                @contextmanager
                def interrupted_write(path, *args, **kwargs):
                    with original_open(path, *args, **kwargs) as output:
                        class PartialWriter:
                            def write(self, block):
                                output.write(block[:3])
                                raise OSError("disk full")
                        yield PartialWriter()

                def open_file(path, *args, **kwargs):
                    if path.name == partial_name and args == ("ab",):
                        return interrupted_write(path, *args, **kwargs)
                    return original_open(path, *args, **kwargs)

                with patch.object(Path, "open", open_file):
                    interrupted = client.put(route + f"?offset={midpoint}", content=content[midpoint:])
                self.assertEqual(interrupted.status_code, 503)
                self.assertEqual(client.get(route).json()["offset"], midpoint)
                bad = client.put(route + f"?offset={midpoint}", content=bytes(len(content) - midpoint))
                self.assertEqual(bad.status_code, 400)
                self.assertEqual(client.get(route).json()["offset"], midpoint)
                last = client.put(route + f"?offset={midpoint}", content=content[midpoint:])
                self.assertTrue(last.json()["complete"])
                self.assertEqual(client.get(route).json()["offset"], len(content))
                self.assertEqual((Path(directory) / "uploads" / (state["upload_id"] + ".zip")).read_bytes(), content)
                recovered = client.post("/api/v1/admin/upload/start", json={"size": len(content)}).json()
                (Path(directory) / "uploads" / (recovered["upload_id"] + ".part")).write_bytes(content)
                self.assertTrue(client.get("/api/v1/admin/upload/" + recovered["upload_id"]).json()["complete"])

    def test_bootstrap_origin_explicit_mode_role_separation_and_upload_limits(self):
        from starlette.formparsers import MultiPartParser

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HSR_MODE": "server"}):
            app = create_app(Path(directory), mode="local", bootstrap_token="bootstrap-test")
            with TestClient(app, base_url="http://127.0.0.1") as client:
                self.assertEqual(client.get("/api/v1/status").json()["mode"], "local")
                self.assertFalse(client.get("/api/v1/status").json()["authenticated"])
                self.assertEqual(client.get("/api/v1/admin/settings").status_code, 401)
                with patch.object(MultiPartParser, "parse", side_effect=AssertionError("unauthorized body was parsed")):
                    self.assertEqual(client.post("/api/v1/admin/upload", files={"file": ("data.zip", b"example", "application/zip")}).status_code, 401)
                self.assertEqual(client.post("/api/v1/auth/bootstrap", json={"token": "bootstrap-test"}, headers={"Origin": "https://evil.example"}).status_code, 403)
                self.assertEqual(client.post("/api/v1/auth/bootstrap", json={"token": "bootstrap-test"}).status_code, 200)
                self.assertTrue(client.get("/api/v1/status").json()["admin"])
                self.assertEqual(client.post("/api/v1/auth/bootstrap", json={"token": "bootstrap-test"}).status_code, 401)
                self.assertEqual(client.get("/api/v1/admin/settings").status_code, 200)
                self.assertEqual(client.get("/api/v1/status", headers={"Host": "evil.example"}).status_code, 403)
                self.assertEqual(client.put("/api/v1/admin/settings", content=b"{}", headers={"Content-Length": "999999999999", "Content-Type": "application/json"}).status_code, 413)
                self.assertEqual(client.put("/api/v1/admin/settings", content=b"{}", headers={"Content-Length": "invalid", "Content-Type": "application/json"}).status_code, 400)
                self.assertEqual(client.put("/api/v1/admin/settings", content=(b'{"embedding":{"model":"' + b"x" * (1024 * 1024) + b'"}}'), headers={"Content-Type": "application/json"}).status_code, 413)

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HSR_ADMIN_CODE": "admin-password-long", "HSR_ACCESS_CODE": "friend-code-long"}):
            app = create_app(Path(directory), mode="server")
            with TestClient(app, base_url="https://story.example") as client:
                response = client.post("/api/v1/auth/login", json={"code": "friend-code-long", "admin": False})
                self.assertEqual(response.status_code, 200)
                self.assertIn("Secure", response.headers["set-cookie"])
                self.assertEqual(client.get("/api/v1/admin/settings").status_code, 403)
                self.assertEqual(client.post("/api/v1/auth/login", json={"code": "admin-password-long", "admin": True}).status_code, 200)
                self.assertEqual(client.get("/api/v1/admin/settings").status_code, 200)


class EmbeddingTests(unittest.TestCase):
    def test_response_order_and_invalid_dimension(self):
        config = {"base_url": "https://example.com/v1", "model": "model", "dimensions": 2, "timeout": 5}
        real_client = httpx.Client

        def client(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [{"index": 1, "embedding": [0., 1.]}, {"index": 0, "embedding": [1., 0.]}]})))

        with patch("backend.embeddings.httpx.Client", side_effect=client):
            self.assertEqual(embed(["a", "b"], config, "secret"), [[1., 0.], [0., 1.]])
            with self.assertRaises(EmbeddingError):
                embed(["a"], config, "secret")
