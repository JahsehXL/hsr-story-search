"""Snapshot storage, portable data packages, and resumable background work."""
from __future__ import annotations

import concurrent.futures
import contextlib
import copy
import hashlib
import json
import os
import ipaddress
import re
import shutil
import socket
import sqlite3
import stat
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx
import sqlite_vec

from .embeddings import embed
from .settings import Settings, atomic_json, embedding_signature
from .version_metadata import apply_versions


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextlib.contextmanager
def public_https_stream(client: httpx.Client, url: str):
    """Bound redirects and refuse internal-network update destinations."""
    for _ in range(6):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("更新地址必须使用公开 HTTPS 地址")
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError("更新地址不能指向本机或内部网络")
        with client.stream("GET", url, headers={"User-Agent": "HSR-Story-Search/0.1"}) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("下载重定向缺少目标地址")
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            yield response
            return
    raise ValueError("更新地址重定向次数过多")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, factory=ClosingConnection)


def db_connect(path: Path) -> sqlite3.Connection:
    connection = connect(path)
    connection.row_factory = sqlite3.Row
    connection.enable_load_extension(True)
    try:
        sqlite_vec.load(connection)
    finally:
        connection.enable_load_extension(False)
    return connection


def read_info(path: Path, key: str, default=None):
    with connect(path) as connection:
        row = connection.execute("SELECT value FROM info WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return row[0]


def safe_extract(archive: Path, destination: Path, max_bytes: int = 8 * 1024**3) -> None:
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if len(members) > 300000 or sum(item.file_size for item in members) > max_bytes:
            raise ValueError("压缩包超过支持的文件数量或解压大小")
        root = destination.resolve()
        seen = set()
        for member in members:
            name = member.filename.replace("\\", "/")
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or ":" in name or "\x00" in name:
                raise ValueError("压缩包包含不安全路径")
            normalized = name.casefold().rstrip("/")
            if normalized in seen:
                raise ValueError("压缩包包含重复路径")
            seen.add(normalized)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("压缩包不允许包含符号链接")
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(root):
                raise ValueError("压缩包路径越界")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)


class JobCancelled(Exception):
    pass


class Runtime:
    def __init__(self, root: Path, mode: str):
        self.root = root.resolve()
        self.mode = mode
        self.settings = Settings(self.root)
        for directory in ("snapshots", "staging", "uploads", "exports"):
            (self.root / directory).mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.jobs_lock = threading.RLock()
        self.jobs_path = self.root / "jobs.json"
        self.jobs = json.loads(self.jobs_path.read_text(encoding="utf-8")) if self.jobs_path.exists() else []
        for job in self.jobs:
            if job["status"] in {"running", "queued", "paused"}:
                job.update(status="interrupted", message="程序重启，可继续此任务")
        self.controls: dict[str, dict] = {}
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="hsr-job")
        self.query_cache: dict[str, tuple[float, list[float]]] = {}
        self.usage_path = self.root / "api-usage.json"
        self.usage = json.loads(self.usage_path.read_text(encoding="utf-8")) if self.usage_path.exists() else {}
        self._save_jobs()

    def active_path(self, snapshot: str | None = None) -> tuple[Path | None, str]:
        with self.lock:
            if not snapshot:
                pointer = self.root / "current.json"
                if not pointer.exists():
                    return None, ""
                snapshot = json.loads(pointer.read_text(encoding="utf-8"))["snapshot"]
            if not re.fullmatch(r"[a-f0-9]{24}", snapshot):
                raise ValueError("数据快照标识无效")
            path = self.root / "snapshots" / snapshot / "story.db"
            if not path.is_file():
                raise ValueError("指定数据快照不存在")
            return path, snapshot

    def install_snapshot(self, database: Path, source: str = "") -> str:
        with self.lock:
            self.validate_database(database)
            snapshot = uuid.uuid4().hex[:24]
            directory = self.root / "snapshots" / snapshot
            directory.mkdir()
            shutil.copy2(database, directory / "story.db")
            with connect(directory / "story.db") as connection:
                apply_versions(connection)
            overlay_path = self.root / "mission-overrides.json"
            if overlay_path.exists():
                overlays = json.loads(overlay_path.read_text(encoding="utf-8"))
                with connect(directory / "story.db") as connection:
                    for identifier, fields in overlays.items():
                        connection.execute("UPDATE missions SET version=?,version_source=?,version_status=? WHERE id=?", (*fields, identifier))
            previous = ""
            pointer = self.root / "current.json"
            if pointer.exists():
                previous = json.loads(pointer.read_text(encoding="utf-8"))["snapshot"]
            atomic_json(directory / "snapshot.json", {"snapshot": snapshot, "created_at": now(), "source": source})
            atomic_json(pointer, {"snapshot": snapshot, "previous": previous})
            return snapshot

    @staticmethod
    def validate_database(path: Path) -> None:
        with db_connect(path) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("剧情数据库完整性校验失败")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"missions", "lines", "chunks", "info", "edges", "chunk_lines", "chunks_fts", "recalls"}.issubset(tables):
                raise ValueError("数据包不是受支持的剧情数据库")
            version = connection.execute("SELECT value FROM info WHERE key='schema_version'").fetchone()
            if not version or json.loads(version[0]) != 1:
                raise ValueError("剧情数据库结构版本不受支持")
            required = {
                "missions": {"id", "name", "chapter", "world", "type", "version", "version_source", "version_status"},
                "lines": {"id", "mission_id", "script", "sequence", "position", "sentence_id", "speaker", "text", "search_text", "kind", "branch", "trigger", "chunk_id"},
                "chunks": {"id", "stable_id", "mission_id", "script", "sequence", "branch", "text", "input_text", "content_hash", "line_ids"},
                "chunk_lines": {"chunk_id", "line_id"},
                "edges": {"script", "source", "target", "kind"},
            }
            for table, columns in required.items():
                actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not columns.issubset(actual):
                    raise ValueError(f"剧情数据库缺少必要字段：{table}")
            embedding_row = connection.execute("SELECT value FROM info WHERE key='embedding'").fetchone()
            if embedding_row:
                metadata = json.loads(embedding_row[0])
                dimension = int(metadata.get("dimension", 0))
                chunks = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                if not 1 <= dimension <= 32768 or metadata.get("count") != chunks or not re.fullmatch(r"[a-f0-9]{64}", str(metadata.get("signature", ""))):
                    raise ValueError("向量索引元数据与剧情数据不一致")
                if "chunk_vectors" not in tables or connection.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] != chunks:
                    raise ValueError("向量索引条数与剧情数据不一致")

    def status_data(self) -> dict:
        path, snapshot = self.active_path()
        if not path:
            return {"ready": False, "stats": {}, "snapshot": ""}
        with connect(path) as connection:
            stats = {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in ("missions", "lines", "chunks")}
            stats["characters"] = connection.execute("SELECT COUNT(DISTINCT speaker) FROM lines").fetchone()[0]
            stats["narrative_lines"] = connection.execute("SELECT COUNT(*) FROM lines WHERE script!='unlinked' AND mission_id!='unlinked'").fetchone()[0]
            stats["narrative_chunks"] = connection.execute("SELECT COUNT(*) FROM chunks WHERE script!='unlinked' AND mission_id!='unlinked'").fetchone()[0]
        return {"ready": True, "stats": stats, "snapshot": snapshot}

    def embedding_status(self, path: Path | None = None) -> dict:
        if path is None:
            path, _ = self.active_path()
        config = self.settings.public()
        meta = read_info(path, "embedding", {}) if path else {}
        dimension = int(meta.get("dimension", 0))
        configured_dimension = config["embedding"].get("dimensions", 0)
        compatible = bool(meta and (not configured_dimension or configured_dimension == dimension) and meta.get("signature") == embedding_signature(config, dimension))
        total = 0
        if path:
            with connect(path) as connection:
                total = connection.execute("SELECT COUNT(*) FROM chunks WHERE script!='unlinked' AND mission_id!='unlinked'").fetchone()[0]
        return {"ready": bool(compatible and self.settings.key() and meta.get("count", 0) == total), "compatible": compatible, "model": config["embedding"]["model"], "dimension": dimension or configured_dimension, "indexed": meta.get("count", 0), "total": total}

    def consume_api(self) -> None:
        with self.lock:
            day = datetime.now(timezone.utc).date().isoformat()
            count = self.usage.get(day, 0)
            maximum = self.settings.value["limits"]["daily_api_calls"]
            if maximum and count >= maximum:
                raise ValueError("已达到今日 API 请求上限，可在设置中调整")
            self.usage = {day: count + 1}
            atomic_json(self.usage_path, self.usage)

    def _save_jobs(self):
        with self.jobs_lock:
            atomic_json(self.jobs_path, self.jobs)

    def public_jobs(self) -> list[dict]:
        with self.jobs_lock:
            return [{key: value for key, value in job.items() if key not in {"payload"}} for job in self.jobs[-30:]][::-1]

    def add_job(self, payload: dict) -> dict:
        kinds = {"import_directory", "import_upload", "download_source", "build_index", "install_package", "check_update", "rollback"}
        if payload.get("kind") not in kinds:
            raise ValueError("未知任务类型")
        with self.jobs_lock:
            if any(job["status"] in {"queued", "running", "paused"} for job in self.jobs):
                raise ValueError("已有数据任务正在运行，请先完成或取消")
            job = {"id": uuid.uuid4().hex, "kind": payload["kind"], "payload": payload, "status": "queued", "phase": "queued", "done": 0, "total": 0, "message": "等待开始", "error": "", "created_at": now()}
            self.jobs.append(job)
            self.controls[job["id"]] = {"pause": threading.Event(), "cancel": threading.Event()}
            self._save_jobs()
            self.executor.submit(self._run_job, job)
            return {key: value for key, value in job.items() if key != "payload"}

    def control_job(self, identifier: str, action: str) -> dict:
        with self.jobs_lock:
            job = next((job for job in self.jobs if job["id"] == identifier), None)
            if not job or action not in {"pause", "resume", "cancel"}:
                raise ValueError("任务或操作不存在")
            controls = self.controls.get(identifier)
            if action == "resume" and job["status"] in {"failed", "interrupted", "cancelled"}:
                if any(item["status"] in {"running", "queued", "paused"} for item in self.jobs):
                    raise ValueError("请先完成当前任务")
                controls = {"pause": threading.Event(), "cancel": threading.Event()}
                self.controls[identifier] = controls
                job.update(status="queued", error="", message="准备继续")
                self.executor.submit(self._run_job, job)
            elif not controls or job["status"] not in {"queued", "running", "paused"}:
                raise ValueError("此任务当前不能执行该操作")
            elif action == "pause":
                controls["pause"].set()
                job["status"] = "paused"
            elif action == "resume":
                controls["pause"].clear()
                job["status"] = "running"
            else:
                controls["cancel"].set()
                controls["pause"].clear()
                job["message"] = "正在安全取消"
            self._save_jobs()
            return {key: value for key, value in job.items() if key != "payload"}

    def _checkpoint(self, job: dict) -> None:
        controls = self.controls[job["id"]]
        while controls["pause"].is_set():
            if controls["cancel"].is_set():
                raise JobCancelled()
            time.sleep(0.1)
        if controls["cancel"].is_set():
            raise JobCancelled()

    def _progress(self, job: dict, event: dict) -> None:
        self._checkpoint(job)
        with self.jobs_lock:
            job.update({key: event[key] for key in ("phase", "done", "total", "message") if key in event})
            self._save_jobs()

    def _download(self, url: str, target: Path, job: dict, expected_size: int | None = None) -> None:
        if not url.startswith("https://"):
            raise ValueError("下载地址必须使用 HTTPS")
        maximum = 4 * 1024**3
        with httpx.Client(timeout=120, follow_redirects=False, trust_env=False) as client:
            with public_https_stream(client, url) as response:
                total = int(response.headers.get("Content-Length", 0))
                if total > maximum or (expected_size and total and total != expected_size):
                    raise ValueError("下载大小超过限制或与清单不一致")
                done = 0
                last_report = 0
                with target.open("wb") as output:
                    for block in response.iter_bytes(1024 * 1024):
                        self._checkpoint(job)
                        done += len(block)
                        if done > maximum:
                            raise ValueError("下载超过 4 GiB 限制")
                        output.write(block)
                        if done - last_report >= 8 * 1024**2:
                            self._progress(job, {"phase": "download", "done": done, "total": total, "message": "正在下载数据"})
                            last_report = done
                if expected_size and done != expected_size:
                    raise ValueError("下载文件大小与清单不符")

    def _run_job(self, job: dict):
        try:
            job.update(status="paused" if self.controls[job["id"]]["pause"].is_set() else "running", error="")
            self._save_jobs()
            self._checkpoint(job)
            kind = job["kind"]
            payload = job["payload"]
            work = self.root / "staging" / (job["id"] + "-" + uuid.uuid4().hex[:8])
            work.mkdir()
            if kind == "rollback":
                self.rollback()
            elif kind == "check_update":
                manifest = self.fetch_update_manifest()
                job["result"] = manifest
                job["message"] = "已取得更新清单，可安装数据包"
            elif kind == "install_package":
                if payload.get("upload_id"):
                    archive = self.upload_path(payload["upload_id"])
                else:
                    manifest = self.fetch_update_manifest()
                    archive = work / "package.zip"
                    self._download(manifest["package_url"], archive, job, manifest.get("size"))
                    if checksum(archive) != manifest["sha256"]:
                        raise ValueError("下载数据包 SHA-256 不匹配")
                self.install_package(archive, work / "package")
            elif kind == "build_index":
                current, _ = self.active_path()
                if not current:
                    raise ValueError("请先导入剧情数据")
                output = work / "story.db"
                shutil.copy2(current, output)
                self.build_index(output, job)
                self._checkpoint(job)
                self.install_snapshot(output, "embedding-index")
            else:
                if kind == "download_source":
                    repo = self.settings.value["updates"]["repository"]
                    with httpx.Client(timeout=60, follow_redirects=True, trust_env=False) as client:
                        response = client.get(f"https://api.github.com/repos/{repo}/commits/HEAD", headers={"User-Agent": "HSR-Story-Search/0.1"})
                        response.raise_for_status()
                        commit = response.json()["sha"]
                    archive = work / "source.zip"
                    self._download(f"https://codeload.github.com/{repo}/zip/{commit}", archive, job)
                    source = work / "source"
                    safe_extract(archive, source)
                    candidates = [path.parent for path in source.glob("*/TextMap") if path.is_dir()]
                    if len(candidates) != 1:
                        raise ValueError("下载数据的目录结构不受支持")
                    source = candidates[0]
                    job["source_commit"] = commit
                elif kind == "import_upload":
                    source = work / "source"
                    safe_extract(self.upload_path(payload.get("upload_id", "")), source)
                    if not (source / "TextMap").is_dir():
                        candidates = [path.parent for path in source.glob("*/TextMap") if path.is_dir()]
                        if len(candidates) != 1:
                            raise ValueError("压缩包中没有唯一的剧情数据目录")
                        source = candidates[0]
                else:
                    source = Path(str(payload.get("source", ""))).expanduser().resolve()
                    if not (source / "TextMap").is_dir():
                        raise ValueError("请选择包含 TextMap、ExcelOutput、Story 的数据目录")
                from .data import import_data
                output = work / "story.db"
                import_data(source, output, lambda event: self._progress(job, event))
                if kind == "download_source":
                    with connect(output) as connection:
                        connection.execute("INSERT OR REPLACE INTO info(key,value) VALUES(?,?)", ("source_commit", json.dumps(job["source_commit"])))
                if self.settings.key() and self.settings.value["embedding"]["model"]:
                    self.build_index(output, job)
                self._checkpoint(job)
                self.install_snapshot(output, str(source))
            self._checkpoint(job)
            job.update(status="completed", phase="completed", message=job.get("message") if kind == "check_update" else "任务完成", finished_at=now())
        except JobCancelled:
            job.update(status="cancelled", message="任务已取消，可重新继续", finished_at=now())
        except Exception as exc:
            message = str(exc)
            key = self.settings.key()
            if key:
                message = message.replace(key, "[redacted]")
            job.update(status="failed", error=message[:800], message="任务失败，可修正设置后继续", finished_at=now())
        finally:
            self._save_jobs()

    def upload_path(self, identifier: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("上传标识无效")
        path = self.root / "uploads" / (identifier + ".zip")
        if not path.is_file():
            raise ValueError("上传文件不存在")
        return path

    def build_index(self, path: Path, job: dict) -> None:
        config = self.settings.public()
        key = self.settings.key()
        namespace = embedding_signature(config, int(config["embedding"].get("dimensions", 0)))
        with connect(self.root / "vector-cache.db") as cache, db_connect(path) as connection:
            cache.execute("CREATE TABLE IF NOT EXISTS vectors(namespace TEXT, hash TEXT, vector BLOB, dimension INTEGER, PRIMARY KEY(namespace,hash))")
            rows = connection.execute("SELECT id,content_hash,input_text FROM chunks WHERE script!='unlinked' AND mission_id!='unlinked' ORDER BY id").fetchall()
            total = len(rows)
            if not total:
                raise ValueError("没有可建立向量索引的剧情片段")
            connection.execute("DROP TABLE IF EXISTS chunk_vectors")
            connection.commit()
            dimension = 0
            completed = 0
            cached_count = 0
            batch_size = config["embedding"]["batch_size"]
            for start in range(0, total, batch_size):
                self._checkpoint(job)
                batch = rows[start:start + batch_size]
                vectors = {}
                missing = []
                for row in batch:
                    cached = cache.execute("SELECT vector,dimension FROM vectors WHERE namespace=? AND hash=?", (namespace, row["content_hash"])).fetchone()
                    if cached:
                        import struct
                        vectors[row["id"]] = list(struct.unpack(f"<{cached[1]}f", cached[0]))
                        cached_count += 1
                    else:
                        missing.append(row)
                if missing:
                    concurrency = config["embedding"]["concurrency"]
                    groups = [missing[index::concurrency] for index in range(concurrency) if missing[index::concurrency]]
                    def request_group(group):
                        return embed([row["input_text"] for row in group], config, key, consume=self.consume_api, checkpoint=lambda: self._checkpoint(job))
                    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                        returned_groups = list(pool.map(request_group, groups))
                    returned_by_id = {row["id"]: vector for group, returned in zip(groups, returned_groups) for row, vector in zip(group, returned)}
                    returned = [returned_by_id[row["id"]] for row in missing]
                    for row, vector in zip(missing, returned):
                        vectors[row["id"]] = vector
                        cache.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)", (namespace, row["content_hash"], sqlite_vec.serialize_float32(vector), len(vector)))
                    cache.commit()
                for row in batch:
                    vector = vectors[row["id"]]
                    if not dimension:
                        dimension = len(vector)
                        connection.execute(f"CREATE VIRTUAL TABLE chunk_vectors USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{dimension}] distance_metric=cosine)")
                    if len(vector) != dimension:
                        raise ValueError("API 模型的向量维度发生变化，请核对模型版本并重建")
                    connection.execute("INSERT INTO chunk_vectors(chunk_id,embedding) VALUES(?,?)", (row["id"], sqlite_vec.serialize_float32(vector)))
                completed += len(batch)
                connection.commit()
                self._progress(job, {"phase": "embedding", "done": completed, "total": total, "message": f"已处理 {completed}/{total} 个片段，复用缓存 {cached_count}"})
            metadata = {"model": config["embedding"]["model"], "dimension": dimension, "signature": embedding_signature(config, dimension), "count": total, "created_at": now()}
            connection.execute("INSERT OR REPLACE INTO info(key,value) VALUES('embedding',?)", (json.dumps(metadata),))
            connection.commit()

    def semantic_search(self, path: Path, query: str, filters: dict, limit: int = 100) -> list[dict]:
        config = self.settings.public()
        meta = read_info(path, "embedding", {})
        cache_key = meta["signature"] + query
        cached = self.query_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            vector = cached[1]
        else:
            vector = embed([query], config, self.settings.key(), query=True, consume=self.consume_api)[0]
            if len(vector) != meta["dimension"]:
                raise ValueError("查询向量与索引维度不匹配，请检查模型")
            with self.lock:
                if len(self.query_cache) >= 256:
                    self.query_cache.pop(next(iter(self.query_cache)))
                self.query_cache[cache_key] = (time.monotonic() + 300, vector)
        from .search import filter_sql
        condition, arguments = filter_sql(filters)
        blob = sqlite_vec.serialize_float32(vector)
        with db_connect(path) as connection:
            if condition:
                sql = "SELECT chunk_id,distance FROM chunk_vectors WHERE embedding MATCH ? AND k=? AND chunk_id IN (SELECT c.id FROM chunks c JOIN missions m ON m.id=c.mission_id WHERE 1=1 " + condition + ") ORDER BY distance"
                rows = connection.execute(sql, (blob, limit, *arguments)).fetchall()
            else:
                rows = connection.execute("SELECT chunk_id,distance FROM chunk_vectors WHERE embedding MATCH ? AND k=? ORDER BY distance", (blob, limit)).fetchall()
        from .search import get_chunks
        results = {item["chunk_id"]: item for item in get_chunks(path, [row["chunk_id"] for row in rows], query)}
        output = []
        for row in rows:
            item = results.get(row["chunk_id"])
            if item:
                item.update(match_type="semantic", score=1 - row["distance"])
                output.append(item)
        return output

    def fetch_update_manifest(self) -> dict:
        url = self.settings.value["updates"]["manifest_url"]
        if not url:
            raise ValueError("请先配置更新清单地址")
        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
            with public_https_stream(client, url) as response:
                maximum = 1024 * 1024
                if int(response.headers.get("Content-Length", 0)) > maximum:
                    raise ValueError("更新清单过大")
                body = bytearray()
                for block in response.iter_bytes(65536):
                    body.extend(block)
                    if len(body) > maximum:
                        raise ValueError("更新清单过大")
                manifest = json.loads(body)
        if not isinstance(manifest, dict) or not re.fullmatch(r"[a-fA-F0-9]{64}", str(manifest.get("sha256", ""))) or not str(manifest.get("package_url", "")).startswith("https://"):
            raise ValueError("更新清单缺少有效 package_url 或 sha256")
        return {key: manifest[key] for key in ("package_url", "sha256", "size", "version", "description") if key in manifest}

    def export_package(self) -> Path:
        with self.lock:
            return self._export_package()

    def _export_package(self) -> Path:
        database, snapshot = self.active_path()
        if not database:
            raise ValueError("没有可以导出的数据")
        target = self.root / "exports" / f"hsr-data-{snapshot}.zip"
        if target.exists():
            return target
        meta = {"format_version": 1, "snapshot": snapshot, "created_at": now(), "files": {"story.db": {"sha256": checksum(database), "size": database.stat().st_size}}, "embedding": read_info(database, "embedding", {})}
        temporary = target.with_suffix(".tmp")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            archive.writestr("manifest.json", json.dumps(meta, ensure_ascii=False))
            archive.write(database, "story.db")
        os.replace(temporary, target)
        return target

    def install_package(self, archive: Path, work: Path) -> str:
        safe_extract(archive, work)
        manifest_path = work / "manifest.json"
        if not manifest_path.is_file() or manifest_path.stat().st_size > 1024 * 1024:
            raise ValueError("数据包缺少有效清单")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format_version") != 1:
            raise ValueError("数据包格式版本不受支持")
        info = manifest.get("files", {}).get("story.db", {})
        database = work / "story.db"
        if not database.is_file() or database.stat().st_size != info.get("size") or checksum(database) != info.get("sha256"):
            raise ValueError("剧情数据文件大小或 SHA-256 校验失败")
        return self.install_snapshot(database, "data-package")

    def rollback(self) -> None:
        with self.lock:
            pointer = self.root / "current.json"
            if not pointer.exists():
                raise ValueError("没有可以恢复的上一版数据")
            value = json.loads(pointer.read_text(encoding="utf-8"))
            previous = value.get("previous")
            if not previous:
                raise ValueError("没有可以恢复的上一版数据")
            self.active_path(previous)
            atomic_json(pointer, {"snapshot": previous, "previous": value["snapshot"]})

    def update_mission(self, identifier: str, version: str, source: str, status: str) -> str:
        with self.lock:
            path, _ = self.active_path()
            if not path:
                raise ValueError("请先导入数据")
            if status not in {"unknown", "verified", "inferred"}:
                raise ValueError("版本核验状态无效")
            if status == "verified" and (not version or not source):
                raise ValueError("已核实版本需要填写版本和依据")
            overlay_path = self.root / "mission-overrides.json"
            overlays = json.loads(overlay_path.read_text(encoding="utf-8")) if overlay_path.exists() else {}
            with connect(path) as connection:
                if not connection.execute("SELECT 1 FROM missions WHERE id=?", (identifier,)).fetchone():
                    raise ValueError("任务不存在")
            overlays[identifier] = [version[:40], source[:1000], status]
            atomic_json(overlay_path, overlays)
            return self.install_snapshot(path, "mission-metadata")

    def close(self):
        for controls in self.controls.values():
            controls["cancel"].set()
            controls["pause"].clear()
        self.executor.shutdown(wait=False, cancel_futures=True)
