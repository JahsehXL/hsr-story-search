"""Portable web service for the Star Rail story search application."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .embeddings import EmbeddingError, embed
from .runtime import Runtime, db_connect
from .settings import atomic_json

from . import __version__ as VERSION


class Login(BaseModel):
    code: str = Field(min_length=1, max_length=512)
    admin: bool = False


class Bootstrap(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class SearchRequest(BaseModel):
    query: str = Field(default="", max_length=1000)
    filters: dict = Field(default_factory=dict)
    mode: str = "hybrid"
    limit: int = Field(default=30, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1000)
    snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{24}$")
    query_vector: list[float] | None = Field(default=None, max_length=32768)


class TrustedClientAddressMiddleware:
    """Interpret forwarding headers only while the original peer is still available."""

    def __init__(self, app, trusted_hosts: str):
        self.app = app
        self.trusted = []
        for host in trusted_hosts.split(","):
            try:
                self.trusted.append(ipaddress.ip_network(host.strip(), strict=False))
            except ValueError:
                continue

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            peer = scope.get("client")
            host = peer[0] if peer else "unknown"
            try:
                address = ipaddress.ip_address(host)
                trusted = any(address in network for network in self.trusted)
            except ValueError:
                trusted = False
            if trusted:
                headers = dict(scope.get("headers", []))
                for forwarded in (headers.get(b"x-real-ip", b""), headers.get(b"x-forwarded-for", b"").split(b",")[0]):
                    try:
                        host = str(ipaddress.ip_address(forwarded.decode("latin1").strip()))
                        break
                    except ValueError:
                        continue
                scheme = headers.get(b"x-forwarded-proto", b"").decode("latin1").strip()
                if scheme in {"http", "https"}:
                    scope["scheme"] = scheme
            scope["client"] = (host, peer[1] if peer else 0)
            scope["state"] = {**scope.get("state", {}), "client_ip": host}
        await self.app(scope, receive, send)


class MissionUpdate(BaseModel):
    version: str = Field(default="", max_length=40)
    source: str = Field(default="", max_length=1000)
    status: str = "unknown"


class UploadStart(BaseModel):
    size: int = Field(ge=1, le=4 * 1024**3)


def create_app(data_dir: Path | str | None = None, mode: str = "local", bootstrap_token: str | None = None) -> FastAPI:
    root = Path(data_dir or os.environ.get("HSR_DATA_DIR", "data"))
    if mode not in {"local", "server"}:
        raise ValueError("mode 必须为 local 或 server")
    runtime = Runtime(root, mode)
    sessions = {}
    session_lock = threading.RLock()
    rates = defaultdict(deque)
    rate_lock = threading.Lock()
    search_slots = threading.BoundedSemaphore(8)
    upload_lock = threading.Lock()
    cookie_path = os.environ.get("HSR_PUBLIC_PREFIX", "/")
    if not cookie_path.startswith("/") or not cookie_path.endswith("/"):
        raise ValueError("HSR_PUBLIC_PREFIX 必须以 / 开始和结束")
    admin_code = os.environ.get("HSR_ADMIN_CODE", "")
    access_code = os.environ.get("HSR_ACCESS_CODE", "")
    if mode == "server" and (len(admin_code) < 12 or (access_code and len(access_code) < 8)):
        raise ValueError("服务器模式需要 HSR_ADMIN_CODE（至少12字符），可选访问码至少8字符")
    public_readonly = mode == "server" and not access_code
    salt = secrets.token_bytes(32)
    def code_hash(code):
        return hashlib.pbkdf2_hmac("sha256", code.encode(), salt, 120000)
    admin_hash = code_hash(admin_code) if admin_code else None
    access_hash = code_hash(access_code) if access_code else None
    bootstrap_hash = hashlib.sha256(bootstrap_token.encode()).digest() if bootstrap_token else None

    @asynccontextmanager
    async def lifespan(app):
        yield
        runtime.close()

    app = FastAPI(title="星轨剧情检索", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime

    def limited(bucket: str, maximum: int):
        with rate_lock:
            timestamp = time.monotonic()
            queue = rates[bucket]
            while queue and queue[0] < timestamp - 60:
                queue.popleft()
            if len(queue) >= maximum:
                raise HTTPException(429, "请求过于频繁，请稍后重试")
            queue.append(timestamp)
            if len(rates) > 5000:
                for key in list(rates):
                    if not rates[key] or rates[key][-1] < timestamp - 60:
                        rates.pop(key, None)

    def session(request: Request) -> dict | None:
        if mode == "local" and bootstrap_hash is None:
            return {"admin": True, "expires": float("inf"), "id": "local"}
        token = request.cookies.get("hsr_session", "")
        with session_lock:
            value = sessions.get(token)
            if value and value["expires"] > time.time():
                return value
            sessions.pop(token, None)
        return None

    def authenticated(request: Request) -> dict:
        value = session(request)
        if not value:
            raise HTTPException(401, "请先输入访问码，或重新从本地启动器打开")
        return value

    def administrator(request: Request) -> dict:
        value = authenticated(request)
        if not value["admin"]:
            raise HTTPException(403, "此操作需要管理员登录")
        return value

    def reader(request: Request) -> dict:
        value = session(request)
        if value:
            return value
        if public_readonly:
            return {"admin": False, "id": "ip:" + request.state.client_ip}
        raise HTTPException(401, "请先输入访问码，或重新从本地启动器打开")

    def establish(request: Request, response: Response, admin: bool):
        token = secrets.token_urlsafe(32)
        with session_lock:
            timestamp = time.time()
            for existing in list(sessions):
                if sessions[existing]["expires"] <= timestamp:
                    sessions.pop(existing)
            if len(sessions) >= 4096:
                raise HTTPException(503, "会话数量已满，请稍后再试")
            sessions[token] = {"admin": admin, "expires": timestamp + 12 * 3600, "id": secrets.token_hex(8)}
        secure = mode == "server"
        response.set_cookie("hsr_session", token, max_age=12 * 3600, httponly=True, secure=secure, samesite="strict", path=cookie_path)
        return {"authenticated": True, "admin": admin}

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        body_exceeded = False
        if request.url.path.startswith("/api/"):
            if mode == "local":
                try:
                    host = urlsplit("http://" + request.headers.get("host", "")).hostname
                except ValueError:
                    host = None
                if host not in {"127.0.0.1", "localhost", "::1"}:
                    return JSONResponse({"detail": "本地模式仅允许回环地址访问"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
            route = request.url.path
            public_routes = {"/api/v1/status", "/api/v1/health", "/api/v1/auth/login", "/api/v1/auth/bootstrap", "/api/v1/auth/logout"}
            public_query = public_readonly and (route in {"/api/v1/catalog", "/api/v1/search"} or route.startswith("/api/v1/missions/"))
            if route not in public_routes and not public_query:
                current_session = session(request)
                if not current_session:
                    return JSONResponse({"detail": "请先输入访问码，或重新从本地启动器打开"}, status_code=401)
                if route.startswith("/api/v1/admin/") and not current_session["admin"]:
                    return JSONResponse({"detail": "此操作需要管理员登录"}, status_code=403)
            if route == "/api/v1/admin/upload":
                maximum = 4 * 1024**3 + 1024**2
            elif route.startswith("/api/v1/admin/upload/"):
                maximum = 8 * 1024**2
            else:
                maximum = 1024**2
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    length = int(declared)
                except ValueError:
                    return JSONResponse({"detail": "Content-Length 无效"}, status_code=400)
                if length < 0:
                    return JSONResponse({"detail": "Content-Length 无效"}, status_code=400)
                if length > maximum:
                    return JSONResponse({"detail": "请求内容超过大小限制"}, status_code=413)
            original_receive = request._receive
            received = 0
            async def bounded_receive():
                nonlocal received, body_exceeded
                message = await original_receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > maximum:
                        body_exceeded = True
                        raise HTTPException(413, "请求内容超过大小限制")
                return message
            request._receive = bounded_receive
        response = await call_next(request)
        if body_exceeded:
            response = JSONResponse({"detail": "请求内容超过大小限制"}, status_code=413)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)[:800]}, status_code=400)

    @app.exception_handler(EmbeddingError)
    async def embedding_error(request, exc):
        return JSONResponse({"detail": str(exc)[:800]}, status_code=502)

    @app.get("/api/v1/health")
    def health():
        return {"ok": True, "version": VERSION}

    @app.get("/api/v1/status")
    def status(request: Request):
        value = session(request)
        if not value and not public_readonly:
            return {"mode": mode, "public_readonly": False, "authenticated": False, "admin": False, "data": {"ready": False, "stats": {}, "snapshot": ""}, "embedding": {"ready": False, "index_ready": False, "server_key_ready": False, "compatible": False, "model": "", "dimension": 0, "indexed": 0, "total": 0}, "jobs": [], "version": VERSION}
        admin = bool(value and value["admin"])
        return {"mode": mode, "public_readonly": public_readonly, "authenticated": True, "admin": admin, "data": runtime.status_data(), "embedding": runtime.embedding_status(), "jobs": runtime.public_jobs() if admin else [], "version": VERSION}

    @app.post("/api/v1/auth/bootstrap")
    def bootstrap(payload: Bootstrap, request: Request, response: Response):
        nonlocal bootstrap_hash
        with session_lock:
            if mode != "local" or bootstrap_hash is None or not hmac.compare_digest(hashlib.sha256(payload.token.encode()).digest(), bootstrap_hash):
                raise HTTPException(401, "本地启动凭据无效，请重新打开启动器")
            bootstrap_hash = None
        return establish(request, response, True)

    @app.post("/api/v1/auth/login")
    def login(payload: Login, request: Request, response: Response):
        limited("login:" + (request.client.host if request.client else "unknown"), 10)
        expected = admin_hash if payload.admin else access_hash
        if expected is None or not hmac.compare_digest(code_hash(payload.code), expected):
            raise HTTPException(401, "访问码或管理员凭据错误")
        return establish(request, response, payload.admin)

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, response: Response):
        with session_lock:
            sessions.pop(request.cookies.get("hsr_session", ""), None)
        response.delete_cookie("hsr_session", path=cookie_path)
        return {"ok": True}

    @app.get("/api/v1/catalog")
    def catalog(user=Depends(reader)):
        path, _ = runtime.active_path()
        if not path:
            return {"worlds": [], "versions": [], "types": [], "characters": []}
        from .data import get_catalog
        return get_catalog(path)

    @app.post("/api/v1/search")
    def search(payload: SearchRequest, request: Request, user=Depends(reader)):
        limited("search:" + user["id"], runtime.settings.value["limits"]["search_per_minute"])
        start = time.monotonic()
        if not search_slots.acquire(timeout=5.0):
            raise HTTPException(429, "搜索排队等待超时，请稍后重试", headers={"Retry-After": "2"})
        try:
            query = payload.query.strip()
            has_filters = any(bool(v) for v in (payload.filters or {}).values())
            if not query and not has_filters:
                raise ValueError("请输入搜索内容")
            if payload.mode not in {"text", "hybrid", "fuzzy"}:
                raise ValueError("搜索模式无效")
            path, snapshot = runtime.active_path(payload.snapshot)
            if not path:
                raise ValueError("请先导入剧情数据")
            visitor_key = request.headers.get("X-Embedding-Key", "")
            if len(visitor_key) > 4096 or any(character in visitor_key for character in ("\r", "\n")):
                raise ValueError("访客嵌入密钥格式无效")
            visitor_key = visitor_key.strip()
            if payload.query_vector is not None:
                runtime.validate_query_vector(path, payload.query_vector)
            from .data import version_sort_key
            from .search import lexical_search
            lexical = lexical_search(path, query, payload.filters, limit=500, allow_fuzzy=payload.mode == "fuzzy")
            semantic = []
            notice = ""
            search_mode = "fuzzy" if payload.mode == "fuzzy" else "text"
            if payload.mode == "hybrid" and query:
                if runtime.embedding_status(path)["index_ready"]:
                    try:
                        semantic = runtime.semantic_search(path, query, payload.filters, limit=500, query_vector=payload.query_vector, visitor_key=visitor_key)
                        search_mode = "hybrid"
                    except Exception:
                        notice = "语义搜索暂不可用，已显示文字搜索结果。请检查语义搜索密钥与模型配置。"
                else:
                    notice = "语义索引尚未就绪或模型配置不匹配，当前使用文字搜索。"
            if not semantic:
                raw_ordered = []
                for rank, item in enumerate(lexical, 1):
                    copy_item = dict(item)
                    copy_item["score"] = round(1 / (60 + rank), 6)
                    raw_ordered.append(copy_item)
            else:
                scores = defaultdict(float)
                candidates = {}
                lex_ids = set()
                sem_ids = set()
                for rank, item in enumerate(lexical, 1):
                    identifier = item["chunk_id"]
                    lex_ids.add(identifier)
                    scores[identifier] += 1.2 / (50 + rank)
                    candidates[identifier] = dict(item)
                for rank, item in enumerate(semantic, 1):
                    identifier = item["chunk_id"]
                    sem_ids.add(identifier)
                    scores[identifier] += 1.3 / (50 + rank)
                    if identifier not in candidates:
                        candidates[identifier] = dict(item)
                def _hybrid_key(item: dict) -> tuple:
                    cid = item["chunk_id"]
                    mtype = item.get("match_type", "")
                    mfield = item.get("matched_field", "")
                    ver_maj, ver_min = version_sort_key(item.get("version", ""))
                    sc = scores[cid]
                    in_both = 1 if (cid in lex_ids and cid in sem_ids) else 0
                    if mtype == "exact" and mfield in {"text", "speaker", "mission"}:
                        field_rank = 2 if mfield == "text" else (1 if mfield == "speaker" else 0)
                        return (3, ver_maj, ver_min, field_rank, in_both, sc)
                    if mtype == "exact":
                        return (2, ver_maj, ver_min, 0, in_both, sc)
                    return (1, in_both, sc, ver_maj, ver_min, 0)
                raw_ordered = sorted(candidates.values(), key=_hybrid_key, reverse=True)
                for item in raw_ordered:
                    item["score"] = round(scores[item["chunk_id"]], 6)
            if len(raw_ordered) > 5:
                ordered = []
                seen_dedup = set()
                for item in raw_ordered:
                    dedup_key = (item.get("mission_id"), item.get("speaker"), item.get("text"))
                    if dedup_key in seen_dedup:
                        continue
                    seen_dedup.add(dedup_key)
                    ordered.append(item)
            else:
                ordered = raw_ordered
            return {"results": ordered[payload.offset:payload.offset + payload.limit], "total": len(ordered), "truncated": len(lexical) >= 500 or len(semantic) >= 500, "search_mode": search_mode, "notice": notice, "elapsed_ms": round((time.monotonic() - start) * 1000), "snapshot": snapshot}
        finally:
            search_slots.release()

    @app.get("/api/v1/missions/{identifier}")
    def mission(identifier: str, line_id: str | None = None, script: str | None = None, snapshot: str | None = None, offset: int = 0, limit: int = 1000, user=Depends(reader)):
        path, snapshot = runtime.active_path(snapshot)
        if not path:
            raise ValueError("请先导入剧情数据")
        from .data import get_mission
        result = get_mission(path, identifier, line_id=line_id, script=script, offset=max(0, offset), limit=max(1, min(limit, 1000)))
        result["snapshot"] = snapshot
        return result

    @app.get("/api/v1/admin/settings")
    def settings(user=Depends(administrator)):
        return runtime.settings.public()

    @app.put("/api/v1/admin/settings")
    def save_settings(payload: dict, user=Depends(administrator)):
        runtime.upgrade_embedding_state()
        return runtime.settings.update(payload)

    @app.post("/api/v1/admin/embedding/test")
    def test_embedding(payload: dict, user=Depends(administrator)):
        text = str(payload.get("text") or "在星穹列车上与伙伴交谈")[:2000]
        start = time.monotonic()
        result = embed([text], runtime.settings.public(), runtime.settings.key(), query=True, consume=runtime.consume_api)
        return {"ok": True, "dimension": len(result[0]), "elapsed_ms": round((time.monotonic() - start) * 1000), "model": runtime.settings.value["embedding"]["model"]}

    @app.post("/api/v1/admin/jobs")
    def new_job(payload: dict, user=Depends(administrator)):
        return runtime.add_job(payload)

    @app.get("/api/v1/admin/jobs")
    def jobs(user=Depends(administrator)):
        return {"jobs": runtime.public_jobs()}

    @app.post("/api/v1/admin/jobs/{identifier}/{action}")
    def control(identifier: str, action: str, user=Depends(administrator)):
        return runtime.control_job(identifier, action)

    @app.post("/api/v1/admin/upload/start")
    def start_upload(payload: UploadStart, user=Depends(administrator)):
        identifier = secrets.token_hex(16)
        atomic_json(runtime.root / "uploads" / (identifier + ".json"), {"size": payload.size})
        return {"upload_id": identifier, "offset": 0, "size": payload.size, "complete": False}

    def upload_state(identifier: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("上传标识无效")
        metadata = runtime.root / "uploads" / (identifier + ".json")
        if not metadata.is_file():
            raise ValueError("上传任务不存在")
        size = json.loads(metadata.read_text(encoding="utf-8"))["size"]
        complete = runtime.root / "uploads" / (identifier + ".zip")
        partial = runtime.root / "uploads" / (identifier + ".part")
        offset = complete.stat().st_size if complete.exists() else partial.stat().st_size if partial.exists() else 0
        return {"upload_id": identifier, "offset": offset, "size": size, "complete": complete.is_file()}

    @app.get("/api/v1/admin/upload/{identifier}")
    def get_upload(identifier: str, user=Depends(administrator)):
        with upload_lock:
            return upload_state(identifier)

    @app.put("/api/v1/admin/upload/{identifier}")
    async def upload_chunk(identifier: str, request: Request, offset: int = 0, user=Depends(administrator)):
        block = await request.body()
        if not block or len(block) > 8 * 1024**2:
            raise ValueError("上传分块应为 1 字节到 8 MiB")
        with upload_lock:
            state = upload_state(identifier)
            if state["complete"]:
                return state
            if offset != state["offset"]:
                raise HTTPException(409, "上传位置已变化，请获取进度后继续")
            if offset + len(block) > state["size"]:
                raise ValueError("分块超过声明的文件大小")
            partial = runtime.root / "uploads" / (identifier + ".part")
            with partial.open("ab") as output:
                output.write(block)
            if partial.stat().st_size == state["size"]:
                if not __import__("zipfile").is_zipfile(partial):
                    raise ValueError("请选择有效的 ZIP 数据文件")
                os.replace(partial, partial.with_suffix(".zip"))
            return upload_state(identifier)

    @app.post("/api/v1/admin/upload")
    async def upload(file: UploadFile, user=Depends(administrator)):
        identifier = secrets.token_hex(16)
        destination = runtime.root / "uploads" / (identifier + ".zip")
        size = 0
        with destination.open("xb") as output:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > 4 * 1024**3:
                    raise HTTPException(413, "上传大小超过 4 GiB")
                output.write(block)
        if not __import__("zipfile").is_zipfile(destination):
            raise ValueError("请选择 ZIP 数据文件")
        return {"upload_id": identifier, "size": size}

    @app.get("/api/v1/admin/package")
    def export(user=Depends(administrator)):
        path = runtime.export_package()
        return FileResponse(path, filename=path.name, media_type="application/zip")

    @app.put("/api/v1/admin/missions/{identifier}")
    def update_mission(identifier: str, payload: MissionUpdate, user=Depends(administrator)):
        snapshot = runtime.update_mission(identifier, payload.version.strip(), payload.source.strip(), payload.status)
        return {"ok": True, "snapshot": snapshot}

    @app.get("/api/v1/admin/diagnostics")
    def diagnostics(user=Depends(administrator)):
        import platform
        import sqlite3
        return {"version": VERSION, "platform": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "data": runtime.status_data(), "embedding": runtime.embedding_status(), "api_usage": runtime.usage, "jobs": runtime.public_jobs()}

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    static = bundle_root / "frontend" / "dist"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="web")
    else:
        @app.get("/")
        def development_page():
            return JSONResponse({"message": "网页尚未构建，请在 frontend 目录运行 npm run build", "version": VERSION})
    app.add_middleware(TrustedClientAddressMiddleware, trusted_hosts=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1,::1"))
    return app


def main():
    parser = argparse.ArgumentParser(description="Star Rail Story Search")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=os.environ.get("HSR_DATA_DIR", "data"))
    parser.add_argument("--mode", choices=("local", "server"), default=os.environ.get("HSR_MODE", "local"))
    args = parser.parse_args()
    if args.mode == "local" and args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("本地模式仅支持回环监听地址")
    app = create_app(Path(args.data_dir), mode=args.mode, bootstrap_token=None)
    if args.mode == "local":
        import webbrowser
        threading.Timer(1, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
