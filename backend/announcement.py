"""Server-managed web announcement and changelog storage."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import threading


DEFAULT_ANNOUNCEMENT: dict = {
    "id": "2026-11-22",
    "title": "站点公告与更新日志",
    "note_title": "写在前面",
    "note_body": (
        "这个项目我一直在持续维护和更新，平时也在不断核对剧情、版本和搜索体验。\n\n"
        "可能你之前搜不到、或者显示不太对的某段剧情，今天再来看看就已经能搜到了；你想要的新功能也一直在陆续补上。\n\n"
        "平时可以多留意下面的更新日志。如果发现漏词、版本不对、分支显示奇怪，或者有什么好想法，都很欢迎随时点「意见反馈」告诉我。"
    ),
    "changelog_title": "最近更新",
    "changelog_body": (
        "## 2026-11-22 [最新]\n"
        "- 新增站点公告与更新日志，方便随时查看近期变动。\n"
        "- 修正了一批任务和场景的版本与地点归属（例如二相乐园、星穹列车相关对话）。\n"
        "- 区分「旁白」与「开拓者」台词，筛选开拓者时不再混入无署名旁白。\n"
        "- 搜索完整任务名（如「失控」）时，顶部新增「同名任务直达」入口。\n"
        "- 角色筛选支持搜索全库全部角色；刷新页面或返回时也会保留当前筛选。\n"
        "- 修复部分嵌套条件分支阅读时漏掉前一句对白的问题。\n\n"
        "## 2026-11-20\n"
        "- 核对并修正一批剧情的所属任务，同一段对白涉及多个出处时会一并列出。\n"
        "- 优化「描述找剧情」与「错字容错」的排序效果，更容易命中想找的片段。\n"
        "- 补录更多剧情配音台词，任务内多段演出的衔接更贴合游戏实际流程。\n\n"
        "## 2026-11-19\n"
        "- 阅读器标出演出分界并减少多余分隔线，连续读长剧情更清爽。\n"
        "- 调整任务内剧情排列顺序，更好还原游戏内推进顺序。"
    ),
    "auto_popup": True,
    "snooze_days": 7,
    "updated_at": "2026-11-22T00:00:00+00:00",
}


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    text = json.dumps(value, ensure_ascii=False, indent=2)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, 0o600) if os.name == "posix" else os.open(temporary, flags)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    os.replace(temporary, path)
    if os.name == "posix":
        os.chmod(path, 0o600)


def _clean_multiline(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


class AnnouncementStore:
    """Stores a single editable site announcement + changelog overwritten by admins."""

    def __init__(self, root: Path):
        self.path = root / "announcement.json"
        self.lock = threading.RLock()

    def get(self) -> dict:
        with self.lock:
            result = dict(DEFAULT_ANNOUNCEMENT)
            if not self.path.is_file():
                return result
            try:
                stored = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return result
            if not isinstance(stored, dict):
                return result
            for key in ("id", "title", "note_title", "note_body", "changelog_title", "changelog_body", "updated_at"):
                if isinstance(stored.get(key), str):
                    result[key] = stored[key]
            if isinstance(stored.get("auto_popup"), bool):
                result["auto_popup"] = stored["auto_popup"]
            raw_days = stored.get("snooze_days")
            if isinstance(raw_days, int) and not isinstance(raw_days, bool) and 1 <= raw_days <= 30:
                result["snooze_days"] = raw_days
            return result

    def save(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("公告数据格式无效")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("公告标题不能为空")
        if len(title) > 120:
            raise ValueError("公告标题不能超过 120 字")
        note_title = str(payload.get("note_title") or "").strip()
        if len(note_title) > 80:
            raise ValueError("寄语小标题不能超过 80 字")
        note_body = _clean_multiline(payload.get("note_body"))
        if len(note_body) > 8000:
            raise ValueError("寄语正文不能超过 8000 字")
        changelog_title = str(payload.get("changelog_title") or "").strip()
        if len(changelog_title) > 80:
            raise ValueError("更新日志小标题不能超过 80 字")
        changelog_body = _clean_multiline(payload.get("changelog_body"))
        if len(changelog_body) > 40000:
            raise ValueError("更新日志不能超过 40000 字")
        if not note_body and not changelog_body:
            raise ValueError("寄语正文和更新日志不能同时为空")
        auto_popup = bool(payload.get("auto_popup", True))
        raw_days = payload.get("snooze_days", 7)
        if isinstance(raw_days, bool):
            raise ValueError("静默天数需在 1 到 30 之间")
        try:
            snooze_days = int(raw_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("静默天数需在 1 到 30 之间") from exc
        if not 1 <= snooze_days <= 30:
            raise ValueError("静默天数需在 1 到 30 之间")
        bump_version = bool(payload.get("bump_version", True))

        with self.lock:
            current = self.get()
            now = datetime.now(timezone.utc)
            version_id = f"{now.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}" if bump_version else current["id"]
            saved = {
                "id": version_id,
                "title": title,
                "note_title": note_title,
                "note_body": note_body,
                "changelog_title": changelog_title,
                "changelog_body": changelog_body,
                "auto_popup": auto_popup,
                "snooze_days": snooze_days,
                "updated_at": now.isoformat(),
            }
            _atomic_json(self.path, saved)
            return saved
