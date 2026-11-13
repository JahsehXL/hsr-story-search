"""Keyword and typo-tolerant candidate retrieval over the dialogue dataset."""

from __future__ import annotations

import re
from pathlib import Path

from rapidfuzz.fuzz import partial_ratio

from .data import normalize_text, open_db, search_tokens


ALIASES = {
    "老杨": "瓦尔特", "杨叔": "瓦尔特",
    "三月": "三月七", "小三月": "三月七", "巡猎三月": "三月七",
    "饮月君": "丹恒", "饮月": "丹恒", "小青龙": "丹恒", "丹恒•饮月": "丹恒",
    "鸟叔": "星期日", "周日哥": "星期日", "周天哥": "星期日",
    "大黑塔": "黑塔",
    "卡妈": "卡芙卡",
    "萨姆": "流萤",
    "忘归人": "停云",
    "阿刃": "刃",
    "小桂子": "桂乃芬",
    "符太卜": "符玄", "太卜": "符玄",
    "神君": "景元", "将军": "景元",
    "大守护者": "布洛妮娅", "鸭鸭": "布洛妮娅",
    "公司": "星际和平公司", "列车组": "星穹列车",
}


def _compact(text: str) -> str:
    return "".join(re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", text.lower()))


def _subterms(text: str) -> list[str]:
    terms: list[str] = []
    for run in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", run) and len(run) > 2:
            for length in (4, 3, 2):
                if len(run) >= length:
                    terms.extend(run[index:index + length] for index in range(len(run) - length + 1))
        else:
            terms.append(run)
    return list(dict.fromkeys(terms))


def expand_query(query: str) -> list[str]:
    values = [normalize_text(query)]
    for alias, canonical in sorted(ALIASES.items(), key=lambda item: -len(item[0])):
        if alias in values[0] and canonical not in values[0]:
            replacement = values[0].replace(alias, canonical)
            if replacement not in values:
                values.append(replacement)
    return values


def filter_sql(filters: dict, alias: str = "m", chunk_alias: str = "c") -> tuple[str, list]:
    clauses: list[str] = [f"{chunk_alias}.script!='unlinked'", f"{chunk_alias}.mission_id!='unlinked'"]
    args: list = []
    for singular, plural, column in (("world", "worlds", "world"), ("type", "types", "type"),
                                      ("version", "versions", "version"), ("mission_id", "mission_ids", "id")):
        value = filters.get(plural) or filters.get(singular)
        if value:
            values = value if isinstance(value, list) else [value]
            if singular == "version" and "unknown" in values:
                values = ["" if item == "unknown" else item for item in values]
            clauses.append(f"{alias}.{column} IN ({','.join('?' for _ in values)})")
            args.extend(str(item) for item in values)
    character = filters.get("characters") or filters.get("character")
    if character:
        raw_values = character if isinstance(character, list) else [character]
        expanded: list[str] = []
        for item in raw_values:
            clean = str(item).strip()
            if not clean:
                continue
            canonical = ALIASES.get(clean, clean)
            for candidate in (clean, canonical):
                if candidate and candidate not in expanded:
                    expanded.append(candidate)
        if expanded:
            speaker_clauses = [f"fl.speaker IN ({','.join('?' for _ in expanded)})"]
            speaker_args: list[str] = list(expanded)
            for candidate in expanded:
                escaped = candidate.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                speaker_clauses.append("fl.speaker LIKE ? ESCAPE '\\'")
                speaker_args.append(f"{escaped}%")
            clauses.append(f"EXISTS(SELECT 1 FROM lines fl WHERE fl.chunk_id={chunk_alias}.id AND ({' OR '.join(speaker_clauses)}))")
            args.extend(speaker_args)
    return (" AND " + " AND ".join(clauses) if clauses else ""), args


def _snippet(text: str, query: str) -> str:
    clean = normalize_text(text)
    if len(clean) <= 190:
        return clean
    match = clean.lower().find(query.lower())
    if match < 0:
        for token in _subterms(query):
            match = clean.lower().find(token)
            if match >= 0:
                break
    start = max(0, match - 50) if match >= 0 else 0
    return ("…" if start else "") + clean[start:start + 185] + ("…" if start + 185 < len(clean) else "")


def _results(conn, ids: list[int], query: str, allow_fuzzy: bool = False) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(f"""SELECT c.id chunk_id,c.mission_id,c.script,c.branch,c.text,c.input_text,
        m.name mission_name,m.chapter,m.world,m.type,m.version,m.version_status,m.version_source
        FROM chunks c JOIN missions m ON m.id=c.mission_id WHERE c.id IN ({placeholders})
        AND c.script!='unlinked' AND c.mission_id!='unlinked'""", ids).fetchall()
    results = []
    variants = expand_query(query)
    for row in rows:
        item = dict(row)
        lines = conn.execute("SELECT id,speaker,search_text,kind FROM lines WHERE chunk_id=? ORDER BY position", (item["chunk_id"],)).fetchall()
        line_scores = []
        for line in lines:
            scores = []
            line_text_lower = line["search_text"].lower()
            line_compact = _compact(line["search_text"])
            speaker_compact = _compact(line["speaker"])
            for variant in variants:
                score = _score_fields(variant, {"text": line["search_text"], "speaker": line["speaker"]}, allow_fuzzy)[0]
                words = re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", variant.lower())
                # Keywords may span adjacent lines in the same scene. Focus a line
                # containing search terms instead of an unrelated first line.
                coverage = sum(len(word) for word in words if word in line_text_lower or word in line_compact)
                speaker_cov = sum(len(word) for word in words if word in speaker_compact)
                sub_coverage = sum(len(term) for term in _subterms(variant) if term in line_compact or term in speaker_compact)
                scores.append((score, coverage, speaker_cov, sub_coverage))
            line_scores.append(max(scores))
        match = lines[max(range(len(lines)), key=line_scores.__getitem__)] if lines else None
        item["line_id"] = match["id"] if match else ""
        item["speaker"] = match["speaker"] if match else ""
        item["text"] = _snippet(match["search_text"] if match else item["text"], query)
        item["kind"] = match["kind"] if match else "dialogue"
        fields = {
            "text": match["search_text"] if match else "",
            "speaker": item["speaker"], "mission": item["mission_name"],
            "metadata": item["world"] + " " + item["chapter"],
        }
        best_match = max((_score_fields(variant, fields, allow_fuzzy) for variant in variants), key=lambda value: value[0])
        item["matched_field"] = best_match[2]
        item["match_type"] = "keyword"
        item["score"] = 0.0
        item.pop("input_text")
        results.append(item)
    by_id = {item["chunk_id"]: item for item in results}
    return [by_id[identifier] for identifier in ids if identifier in by_id]


def get_chunks(path: Path | str, ids: list[int], query: str = "") -> list[dict]:
    with open_db(path) as conn:
        return _results(conn, ids, normalize_text(query))


def _score_fields(query: str, fields: dict[str, str], allow_fuzzy: bool = False) -> tuple[float, str, str]:
    """Keep scene text ahead of names and avoid fuzzy matches on inherited metadata."""
    query = query.lower()
    best = (0.0, "keyword", "text")
    if not query:
        return best
    words = re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", query)
    query_compact = "".join(words)
    has_punct = any(not ch.isalnum() and not ch.isspace() and not ("\u3400" <= ch <= "\u9fff") for ch in query)
    exact_weights = {"text": 300.0, "speaker": 280.0, "mission": 220.0, "metadata": 170.0}
    for field, value in fields.items():
        value = normalize_text(value).lower()
        value_compact = _compact(value)
        if query in value:
            candidate = (exact_weights[field], "exact", field)
        elif query_compact and len(query_compact) >= 2 and (len(words) == 1 or has_punct) and query_compact in value_compact:
            candidate = (exact_weights[field] - 5.0, "exact", field)
        elif words and all(word in value or word in value_compact for word in words):
            candidate = (exact_weights[field] - 30.0, "keyword", field)
        elif allow_fuzzy and field in {"text", "speaker", "mission"} and len(query) >= 3 and words:
            similarity = max(partial_ratio(query, value), partial_ratio(query_compact, value_compact) if query_compact else 0.0)
            # A single shared character is insufficient evidence for a fuzzy hit.
            threshold = 65 if len(query) >= 4 else 75
            if similarity < threshold:
                continue
            candidate = (similarity + {"text": 95, "speaker": 80, "mission": 45}[field], "fuzzy", field)
        else:
            continue
        if candidate[0] > best[0]:
            best = candidate
    if best[0] == 0.0 and len(words) > 1:
        compact_fields = {k: _compact(normalize_text(v)) for k, v in fields.items()}
        combined_compact = " ".join(compact_fields.values())
        if all(word in combined_compact for word in words):
            if any(word in compact_fields.get("text", "") for word in words):
                best = (255.0, "keyword", "text")
            elif any(word in compact_fields.get("speaker", "") for word in words):
                best = (235.0, "keyword", "speaker")
            elif any(word in compact_fields.get("mission", "") for word in words):
                best = (195.0, "keyword", "mission")
            else:
                best = (150.0, "keyword", "metadata")
    return best


def lexical_search(path: Path | str, query: str, filters: dict | None = None, limit: int = 100,
                   *, allow_fuzzy: bool = False) -> list[dict]:
    query = normalize_text(query)[:500]
    if not query:
        return []
    filters = filters or {}
    limit = max(1, min(limit, 500))
    variants = expand_query(query)
    condition, filter_args = filter_sql(filters)
    candidates: dict[int, tuple[float, str, str]] = {}
    fts_ranks: dict[int, float] = {}
    with open_db(path) as conn:
        candidate_ids: set[int] = set()
        for variant in variants:
            escaped = variant.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            # Literal matches also cover punctuation-only and short queries.
            rows = conn.execute(f"""SELECT c.id FROM chunks c JOIN missions m ON m.id=c.mission_id
                WHERE (c.text LIKE ? ESCAPE '\\' OR m.name LIKE ? ESCAPE '\\') {condition}
                ORDER BY CASE WHEN c.text LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END,c.id LIMIT ?""", [f"%{escaped}%", f"%{escaped}%"] + filter_args + [f"%{escaped}%", max(150, limit * 2)]).fetchall()
            for row in rows:
                candidate_ids.add(row[0])
            tokens = search_tokens(variant).split()
            # Longer terms carry more information than common single characters.
            tokens.sort(key=lambda token: (-len(token), token))
            tokens = tokens[:48]
            if not tokens:
                continue
            bigrams = [token for token in tokens if len(token) >= 2][:16]
            if len(bigrams) >= 2 and not allow_fuzzy:
                and_expr = " AND ".join('"' + token.replace('"', '""') + '"' for token in bigrams)
                and_rows = conn.execute(f"""SELECT c.id,bm25(chunks_fts) rank FROM chunks_fts
                    JOIN chunks c ON c.id=chunks_fts.rowid JOIN missions m ON m.id=c.mission_id
                    WHERE chunks_fts MATCH ? {condition} ORDER BY rank LIMIT ?""",
                    [and_expr] + filter_args + [max(250, limit * 3)]).fetchall()
                for row in and_rows:
                    candidate_ids.add(row["id"])
                    fts_ranks[row["id"]] = min(fts_ranks.get(row["id"], 0.0), float(row["rank"]))
            or_tokens = bigrams if (len(bigrams) >= 2 and not allow_fuzzy) else tokens
            expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in or_tokens)
            rows = conn.execute(f"""SELECT c.id,bm25(chunks_fts) rank FROM chunks_fts
                JOIN chunks c ON c.id=chunks_fts.rowid JOIN missions m ON m.id=c.mission_id
                WHERE chunks_fts MATCH ? {condition} ORDER BY rank LIMIT ?""",
                [expression] + filter_args + [max(250, limit * 3)]).fetchall()
            for row in rows:
                candidate_ids.add(row["id"])
                fts_ranks[row["id"]] = min(fts_ranks.get(row["id"], 0.0), float(row["rank"]))
        if not candidate_ids:
            return []
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = conn.execute(f"""SELECT c.id,m.name,m.world,m.chapter,m.type,
            (SELECT group_concat(search_text,'\n') FROM lines WHERE chunk_id=c.id) body,
            (SELECT group_concat(DISTINCT speaker) FROM lines WHERE chunk_id=c.id) speakers
            FROM chunks c JOIN missions m ON m.id=c.mission_id WHERE c.id IN ({placeholders})""", list(candidate_ids)).fetchall()
        for row in rows:
            body_text = row["body"] or ""
            fields = {"text": body_text, "speaker": row["speakers"] or "", "mission": row["name"], "metadata": row["world"] + " " + row["chapter"]}
            score, match_type, matched_field = max((_score_fields(variant, fields, allow_fuzzy) for variant in variants), key=lambda value: value[0])
            if score > 0:
                line_bonus = max((_score_fields(variant, {"text": line_str}, allow_fuzzy)[0] for variant in variants for line_str in body_text.split("\n") if line_str), default=0.0)
                type_bonus = 1.5 if row["type"] and row["type"] != "其他任务" else 0.0
                bonus = (4.0 if line_bonus >= 265.0 else 0.0) + type_bonus + min(3.9, max(0.0, -fts_ranks.get(row["id"], 0.0)) * 0.15)
                candidates[row["id"]] = (score + bonus, match_type, matched_field)
        ordered = sorted(candidates, key=lambda identifier: (-candidates[identifier][0], identifier))[:limit]
        results = _results(conn, ordered, query, allow_fuzzy)
        for item in results:
            item["score"], item["match_type"], item["matched_field"] = candidates[item["chunk_id"]]
        return results
