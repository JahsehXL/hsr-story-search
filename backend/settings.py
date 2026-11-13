"""Persistent public settings and platform-protected API credentials."""
from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {
    "embedding": {"base_url": "", "model": "", "dimensions": 0, "query_prefix": "", "document_prefix": "", "extra_body": {}, "batch_size": 16, "concurrency": 1, "timeout": 60},
    "updates": {"repository": "DimbreathBot/TurnBasedGameData", "manifest_url": ""},
    "limits": {"search_per_minute": 30, "daily_api_calls": 0},
}


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        return data
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise OSError("Windows 无法读取或保护当前用户的 API 密钥")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


class Settings:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "settings.json"
        self.secret_path = root / "api-key.bin"
        self.value = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            for section in DEFAULTS:
                self.value[section].update(saved.get(section, {}))

    def key(self) -> str:
        if self.secret_path.exists():
            return protect(self.secret_path.read_bytes(), decrypt=True).decode("utf-8")
        return os.environ.get("HSR_EMBEDDING_API_KEY", "")

    def public(self) -> dict:
        with self.lock:
            result = copy.deepcopy(self.value)
        result["embedding"]["api_key_set"] = bool(self.key())
        return result

    def update(self, incoming: dict) -> dict:
        with self.lock:
            value = copy.deepcopy(self.value)
            for section in DEFAULTS:
                fields = incoming.get(section, {})
                if not isinstance(fields, dict):
                    raise ValueError("设置分组格式错误")
                for key in DEFAULTS[section]:
                    if key in fields:
                        value[section][key] = fields[key]
            embedding = value["embedding"]
            embedding["base_url"] = str(embedding["base_url"]).strip().rstrip("/")
            embedding["model"] = str(embedding["model"]).strip()
            if embedding["base_url"] and not embedding["base_url"].startswith(("https://", "http://")):
                raise ValueError("API 地址必须使用 HTTP 或 HTTPS")
            if embedding["base_url"]:
                parsed = urlsplit(embedding["base_url"])
                if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError("API 地址应为接口基础地址，密钥请填写在专用字段")
            for field in ("query_prefix", "document_prefix"):
                embedding[field] = str(embedding[field])
                if len(embedding[field]) > 4000:
                    raise ValueError("嵌入指令前缀过长")
            for field, minimum, maximum in [("dimensions", 0, 32768), ("batch_size", 1, 128), ("concurrency", 1, 4), ("timeout", 5, 300)]:
                embedding[field] = int(embedding[field])
                if not minimum <= embedding[field] <= maximum:
                    raise ValueError(f"{field} 超出支持范围")
            if not isinstance(embedding["extra_body"], dict):
                raise ValueError("额外参数应为 JSON 对象")
            if set(embedding["extra_body"]) & {"model", "input", "dimensions"}:
                raise ValueError("额外参数不能覆盖 model、input 或 dimensions")
            for field in ("search_per_minute", "daily_api_calls"):
                value["limits"][field] = int(value["limits"][field])
                minimum = 1 if field == "search_per_minute" else 0
                if value["limits"][field] < minimum:
                    raise ValueError("请求上限无效，API 每日上限可填写 0 表示不限")
            repository = str(value["updates"]["repository"]).strip()
            import re
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
                raise ValueError("仓库格式应为 owner/repository")
            value["updates"]["repository"] = repository
            manifest_url = str(value["updates"]["manifest_url"]).strip()
            if manifest_url and not manifest_url.startswith("https://"):
                raise ValueError("更新清单地址必须使用 HTTPS")
            value["updates"]["manifest_url"] = manifest_url
            key = incoming.get("embedding", {}).get("api_key", "")
            if key:
                self.secret_path.write_bytes(protect(str(key).strip().encode("utf-8")))
                os.chmod(self.secret_path, 0o600)
            self.value = value
            atomic_json(self.path, value)
        return self.public()


def embedding_signature(settings: dict, dimension: int) -> str:
    embedding = settings.get("embedding", settings)
    identity = {key: embedding.get(key) for key in ("base_url", "model", "query_prefix", "document_prefix", "extra_body")}
    identity.update(dimension=dimension, preprocessing="hsr-chunks-v1")
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
