"""Visitor feedback storage with IP rate limiting, attachment validation and a 1 GiB storage cap."""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import threading
import time


MAX_STORAGE_BYTES = 1 * 1024**3
MAX_ATTACHMENT_BYTES = 5 * 1024**2
MAX_ATTACHMENTS_PER_ITEM = 5
MAX_SUBMISSIONS_PER_HOUR = 5
RATE_WINDOW_SECONDS = 3600
MAX_CONTENT_CHARS = 4000
VALID_CATEGORIES = {"missing", "display", "feature", "other"}
VALID_STATUSES = {"pending", "resolved"}
ALLOWED_CONTEXT_KEYS = (
    "mission_id", "mission_name", "chapter", "world", "version",
    "line_id", "sentence_id", "speaker", "text", "script", "branch",
    "query", "mode",
)
STORAGE_FULL_MESSAGE = "目前反馈存储空间已满，请稍后再试，或前往 GitHub 仓库 / 个人博客联系作者"
RATE_LIMIT_MESSAGE = "同一 IP 1 小时内最多提交 5 次反馈，请稍后再试"


class FeedbackRateLimitError(ValueError):
    """Raised when one IP exceeds 5 feedback submissions within 1 hour."""


class FeedbackFullError(ValueError):
    """Raised when feedback storage reaches the 1 GiB cap."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_image_format(data: bytes) -> tuple[str, str]:
    if not data:
        raise ValueError("图片附件内容为空")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    raise ValueError("附件仅支持 PNG、JPG、WebP 或 GIF 格式的图片")


def mask_ip(raw_ip: str) -> str:
    value = (raw_ip or "").strip()
    if not value or value in {"127.0.0.1", "::1", "localhost", "local", "unknown"}:
        return "本地"
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return "未知"
    if isinstance(addr, ipaddress.IPv4Address):
        parts = addr.exploded.split(".")
        return f"{parts[0]}.{parts[1]}.*.*"
    parts = addr.exploded.split(":")
    return f"{parts[0]}:{parts[1]}:*"


def sanitize_filename(name: str, ext: str) -> str:
    base = Path(str(name or "image")).stem
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", base).strip("._-")[:60] or "image"
    return cleaned + ext


def sanitize_context(context: object) -> dict[str, str]:
    if not isinstance(context, dict):
        return {}
    result: dict[str, str] = {}
    for key in ALLOWED_CONTEXT_KEYS:
        val = context.get(key)
        if val is not None and str(val).strip():
            result[key] = str(val).strip()[:500]
    return result


class FeedbackStore:
    def __init__(self, root: Path):
        self.root = (root / "feedback").resolve()
        self.attachments_dir = self.root / "attachments"
        self.db_path = self.root / "feedback.db"
        self.salt_path = self.root / ".ip_salt"
        self.root.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.salt = self._load_salt()
        self._init_db()

    def _load_salt(self) -> bytes:
        if self.salt_path.is_file():
            raw = self.salt_path.read_bytes()
            if len(raw) >= 32:
                return raw
        raw = secrets.token_bytes(32)
        fd = os.open(self.salt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        return raw

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.lock, self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    ip_hash TEXT NOT NULL,
                    ip_masked TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    attachment_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_ts DESC);
                CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status, created_ts DESC);

                CREATE TABLE IF NOT EXISTS feedback_attachments (
                    id TEXT NOT NULL,
                    feedback_id TEXT NOT NULL REFERENCES feedback(id) ON DELETE CASCADE,
                    idx INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    PRIMARY KEY (feedback_id, id)
                );

                CREATE TABLE IF NOT EXISTS feedback_rate_log (
                    ip_hash TEXT NOT NULL,
                    created_ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_rate ON feedback_rate_log(ip_hash, created_ts);
            """)

    def _hash_ip(self, ip: str) -> str:
        normalized = (ip or "unknown").strip().lower()
        return hmac.new(self.salt, normalized.encode("utf-8"), hashlib.sha256).hexdigest()

    def _cleanup_orphan_dirs(self, conn: sqlite3.Connection) -> None:
        if not self.attachments_dir.is_dir():
            return
        known = {row[0] for row in conn.execute("SELECT id FROM feedback")}
        for child in self.attachments_dir.iterdir():
            if child.name not in known:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)

    def _storage_summary(self, conn: sqlite3.Connection) -> dict:
        row = conn.execute("""
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) AS pending_count,
                COALESCE(SUM(CASE WHEN status='resolved' THEN 1 ELSE 0 END), 0) AS resolved_count,
                COALESCE(SUM(size_bytes), 0) AS used_bytes,
                COALESCE(SUM(attachment_count), 0) AS attachment_count
            FROM feedback
        """).fetchone()
        used_bytes = int(row["used_bytes"] or 0)
        return {
            "used_bytes": used_bytes,
            "max_bytes": MAX_STORAGE_BYTES,
            "used_percent": round(used_bytes * 100 / MAX_STORAGE_BYTES, 2) if MAX_STORAGE_BYTES else 0.0,
            "total_count": int(row["total_count"] or 0),
            "pending_count": int(row["pending_count"] or 0),
            "resolved_count": int(row["resolved_count"] or 0),
            "attachment_count": int(row["attachment_count"] or 0),
            "full": used_bytes >= MAX_STORAGE_BYTES,
            "max_attachment_bytes": MAX_ATTACHMENT_BYTES,
            "max_per_hour": MAX_SUBMISSIONS_PER_HOUR,
        }

    def storage_summary(self) -> dict:
        with self.lock, self._connect() as conn:
            return self._storage_summary(conn)

    def submit(
        self,
        ip: str,
        category: str,
        content: str,
        context: object = None,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> dict:
        cat = str(category or "other").strip().lower()
        if cat not in VALID_CATEGORIES:
            raise ValueError("反馈分类无效")
        text = str(content or "").replace("\r\n", "\n").strip()
        if not text:
            raise ValueError("请输入反馈内容")
        if len(text) > MAX_CONTENT_CHARS:
            raise ValueError(f"反馈内容请控制在 {MAX_CONTENT_CHARS} 字以内")

        clean_ctx = sanitize_context(context)
        context_json = json.dumps(clean_ctx, ensure_ascii=False)
        files = list(attachments or [])
        if len(files) > MAX_ATTACHMENTS_PER_ITEM:
            raise ValueError(f"单次最多附带 {MAX_ATTACHMENTS_PER_ITEM} 张图片")

        validated_files: list[tuple[int, str, str, str, bytes]] = []
        total_att_bytes = 0
        for idx, (orig_name, raw) in enumerate(files):
            if not isinstance(raw, (bytes, bytearray)) or not raw:
                raise ValueError("图片附件内容为空")
            raw_bytes = bytes(raw)
            total_att_bytes += len(raw_bytes)
            if total_att_bytes > MAX_ATTACHMENT_BYTES:
                raise ValueError("图片附件总大小不能超过 5 MB")
            ext, mime = detect_image_format(raw_bytes)
            safe_name = sanitize_filename(orig_name, ext)
            digest = hashlib.sha256(raw_bytes).hexdigest()[:16]
            att_id = f"{idx}_{digest}{ext}"
            validated_files.append((idx, att_id, safe_name, mime, raw_bytes))

        entry_bytes = len(text.encode("utf-8")) + len(context_json.encode("utf-8")) + total_att_bytes
        ip_hash = self._hash_ip(ip)
        ip_masked = mask_ip(ip)
        now_ts = time.time()
        created_at = now_iso()

        with self.lock, self._connect() as conn:
            summary = self._storage_summary(conn)
            if summary["used_bytes"] >= MAX_STORAGE_BYTES or summary["used_bytes"] + entry_bytes > MAX_STORAGE_BYTES:
                raise FeedbackFullError(STORAGE_FULL_MESSAGE)

            conn.execute("DELETE FROM feedback_rate_log WHERE created_ts < ?", (now_ts - RATE_WINDOW_SECONDS,))
            recent = conn.execute(
                "SELECT COUNT(*) FROM feedback_rate_log WHERE ip_hash = ? AND created_ts >= ?",
                (ip_hash, now_ts - RATE_WINDOW_SECONDS),
            ).fetchone()[0]
            if recent >= MAX_SUBMISSIONS_PER_HOUR:
                conn.commit()
                raise FeedbackRateLimitError(RATE_LIMIT_MESSAGE)

            feedback_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(8)
            item_dir = self.attachments_dir / feedback_id
            try:
                if validated_files:
                    item_dir.mkdir(parents=True, exist_ok=False)
                    for _, att_id, _, _, raw_bytes in validated_files:
                        (item_dir / att_id).write_bytes(raw_bytes)

                conn.execute(
                    """INSERT INTO feedback(
                        id, category, content, context_json, status,
                        ip_hash, ip_masked, size_bytes, attachment_count,
                        created_at, created_ts, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        feedback_id, cat, text, context_json,
                        ip_hash, ip_masked, entry_bytes, len(validated_files),
                        created_at, now_ts, created_at,
                    ),
                )
                for idx, att_id, safe_name, mime, raw_bytes in validated_files:
                    conn.execute(
                        """INSERT INTO feedback_attachments(
                            id, feedback_id, idx, filename, stored_name, mime_type, size_bytes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (att_id, feedback_id, idx, safe_name, att_id, mime, len(raw_bytes)),
                    )
                conn.execute("INSERT INTO feedback_rate_log(ip_hash, created_ts) VALUES (?, ?)", (ip_hash, now_ts))
                conn.commit()
            except Exception:
                if item_dir.exists():
                    shutil.rmtree(item_dir, ignore_errors=True)
                raise

            updated_summary = self._storage_summary(conn)
            return {
                "ok": True,
                "id": feedback_id,
                "created_at": created_at,
                "size_bytes": entry_bytes,
                "attachment_count": len(validated_files),
                "storage": updated_summary,
            }

    def _serialize_item(self, row: sqlite3.Row, attachments_by_fid: dict[str, list[dict]]) -> dict:
        try:
            ctx = json.loads(row["context_json"] or "{}")
        except json.JSONDecodeError:
            ctx = {}
        return {
            "id": row["id"],
            "category": row["category"],
            "content": row["content"],
            "context": ctx if isinstance(ctx, dict) else {},
            "status": row["status"],
            "ip_masked": row["ip_masked"],
            "size_bytes": int(row["size_bytes"] or 0),
            "attachment_count": int(row["attachment_count"] or 0),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "attachments": attachments_by_fid.get(row["id"], []),
        }

    def list_items(self, status: str = "all", category: str = "all", limit: int = 100, offset: int = 0) -> dict:
        clauses: list[str] = []
        params: list[object] = []
        if status and status != "all":
            if status not in VALID_STATUSES:
                raise ValueError("筛选状态无效")
            clauses.append("status = ?")
            params.append(status)
        if category and category != "all":
            if category not in VALID_CATEGORIES:
                raise ValueError("筛选分类无效")
            clauses.append("category = ?")
            params.append(category)
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, int(offset))

        with self.lock, self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM feedback {where_sql}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM feedback {where_sql} ORDER BY created_ts DESC, id DESC LIMIT ? OFFSET ?",
                [*params, safe_limit, safe_offset],
            ).fetchall()
            fids = [row["id"] for row in rows]
            attachments_by_fid: dict[str, list[dict]] = {}
            if fids:
                placeholders = ",".join("?" for _ in fids)
                att_rows = conn.execute(
                    f"SELECT * FROM feedback_attachments WHERE feedback_id IN ({placeholders}) ORDER BY idx ASC",
                    fids,
                ).fetchall()
                for att in att_rows:
                    attachments_by_fid.setdefault(att["feedback_id"], []).append({
                        "id": att["id"],
                        "filename": att["filename"],
                        "mime_type": att["mime_type"],
                        "size_bytes": int(att["size_bytes"] or 0),
                    })
            return {
                "storage": self._storage_summary(conn),
                "total": int(total),
                "items": [self._serialize_item(row, attachments_by_fid) for row in rows],
            }

    def update_status(self, feedback_id: str, status: str) -> dict:
        if not re.fullmatch(r"\d{14}_[a-f0-9]{16}", str(feedback_id)):
            raise ValueError("反馈标识无效")
        if status not in VALID_STATUSES:
            raise ValueError("状态无效")
        with self.lock, self._connect() as conn:
            updated = now_iso()
            cur = conn.execute("UPDATE feedback SET status = ?, updated_at = ? WHERE id = ?", (status, updated, feedback_id))
            if cur.rowcount == 0:
                raise ValueError("反馈记录不存在")
            conn.commit()
            return {"ok": True, "id": feedback_id, "status": status, "storage": self._storage_summary(conn)}

    def delete_item(self, feedback_id: str) -> dict:
        if not re.fullmatch(r"\d{14}_[a-f0-9]{16}", str(feedback_id)):
            raise ValueError("反馈标识无效")
        with self.lock:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM feedback WHERE id = ?", (feedback_id,))
                if cur.rowcount == 0:
                    raise ValueError("反馈记录不存在")
                conn.commit()
                shutil.rmtree(self.attachments_dir / feedback_id, ignore_errors=True)
                self._cleanup_orphan_dirs(conn)
            with self._connect() as conn:
                conn.execute("VACUUM")
                return {"ok": True, "deleted": 1, "storage": self._storage_summary(conn)}

    def clear_resolved(self) -> dict:
        with self.lock:
            with self._connect() as conn:
                ids = [row[0] for row in conn.execute("SELECT id FROM feedback WHERE status = 'resolved'").fetchall()]
                if ids:
                    conn.execute("DELETE FROM feedback WHERE status = 'resolved'")
                    conn.commit()
                    for fid in ids:
                        shutil.rmtree(self.attachments_dir / fid, ignore_errors=True)
                self._cleanup_orphan_dirs(conn)
            with self._connect() as conn:
                if ids:
                    conn.execute("VACUUM")
                return {"ok": True, "deleted": len(ids), "storage": self._storage_summary(conn)}

    def get_attachment(self, feedback_id: str, attachment_id: str) -> tuple[Path, str, str]:
        if not re.fullmatch(r"\d{14}_[a-f0-9]{16}", str(feedback_id)):
            raise ValueError("反馈标识无效")
        if not re.fullmatch(r"\d+_[a-f0-9]{16}\.(?:png|jpg|webp|gif)", str(attachment_id)):
            raise ValueError("附件标识无效")
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT stored_name, mime_type, filename FROM feedback_attachments WHERE feedback_id = ? AND id = ?",
                (feedback_id, attachment_id),
            ).fetchone()
            if not row:
                raise ValueError("附件不存在")
            target = (self.attachments_dir / feedback_id / row["stored_name"]).resolve()
            if not target.is_relative_to(self.attachments_dir.resolve()) or not target.is_file():
                raise ValueError("附件文件不存在")
            return target, row["mime_type"], row["filename"]
