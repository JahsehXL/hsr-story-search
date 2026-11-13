"""Keyword and typo-tolerant candidate retrieval over the dialogue dataset."""

from __future__ import annotations

import re
from pathlib import Path

from rapidfuzz.fuzz import partial_ratio

from .data import normalize_text, open_db, search_tokens, version_sort_key


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
    "阮梅": "阮•梅",
}

KNOWN_ENTITIES = (
    "星际和平公司", "星核猎手", "星穹列车", "匹诺康尼", "翁法罗斯", "二相乐园", "贝洛伯格", "空间站",
    "开拓者", "星期日", "知更鸟", "三月七", "瓦尔特", "卡芙卡", "大黑塔", "螺丝咕姆", "布洛妮娅",
    "杰帕德", "加拉赫", "黑天鹅", "阿格莱雅", "那刻夏", "赛飞儿", "海瑟音", "刻律德菈", "长夜月",
    "大丽花", "不死途", "桂乃芬", "丹恒", "姬子", "帕姆", "流萤", "银狼", "黑塔", "景元", "符玄",
    "白露", "彦卿", "镜流", "罗刹", "停云", "藿藿", "飞霄", "椒丘", "貊泽", "灵砂", "云璃", "希儿",
    "克拉拉", "桑博", "佩拉", "卢卡", "玲可", "花火", "黄泉", "砂金", "米沙", "翡翠", "波提欧",
    "乱破", "缇宝", "万敌", "遐蝶", "风堇", "白厄", "昔涟", "爻光", "火花", "银枝", "托帕", "素裳",
    "青雀", "雪衣", "寒鸦", "阿哈", "欢愉", "仙舟", "罗浮",
)

STOPWORD_PATTERN = re.compile(
    r"关于|为什么|怎么|什么|如何|哪里|哪个|时候|地方|告诉|讨论|争论|对话|剧情|故事|台词|"
    r"她的|他的|我的|你的|他们|她们|我们|你们|自己|可以|因为|所以|但是|然后|如果|或者|以及|还有|"
    r"就是|不是|没有|一个|一些|这个|那个|这些|那些|[的了和与跟同在于是有把被向对给让叫去来到从往着过吗呢吧啊呀么]"
)


def _extract_content_terms(query: str) -> list[str]:
    text = query.lower()
    terms: list[str] = []
    for ent in KNOWN_ENTITIES:
        if ent in text:
            terms.append(ent)
    cleaned = STOPWORD_PATTERN.sub(" ", text)
    for part in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", cleaned):
        for ent in KNOWN_ENTITIES:
            if ent in part and part != ent:
                for sub in part.split(ent):
                    if len(sub) >= 2:
                        terms.append(sub)
                break
        else:
            if len(part) >= 2:
                if len(part) > 4 and re.fullmatch(r"[\u3400-\u9fff]+", part):
                    for idx in range(0, len(part) - 1, 2):
                        terms.append(part[idx:idx + 2])
                    if len(part) % 2 == 1:
                        terms.append(part[-2:])
                else:
                    terms.append(part)
    return list(dict.fromkeys(t for t in terms if len(t) >= 2))


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


def _expand_character_filter(filters: dict | None) -> list[str]:
    if not filters:
        return []
    character = filters.get("characters") or filters.get("character")
    if not character:
        return []
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
    return expanded


def _speaker_matches_filter(speaker: str, expanded_chars: list[str]) -> bool:
    if not expanded_chars:
        return True
    spk = (speaker or "").strip()
    return any(spk == c or spk.startswith(c) for c in expanded_chars)


def _results(conn, ids: list[int], query: str, allow_fuzzy: bool = False, filters: dict | None = None) -> list[dict]:
    if not ids:
        return []
    expanded_chars = _expand_character_filter(filters)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(f"""SELECT c.id chunk_id,c.mission_id,c.script,c.branch,c.text,c.input_text,
        m.name mission_name,m.chapter,m.world,m.type,m.version,m.version_status,m.version_source
        FROM chunks c JOIN missions m ON m.id=c.mission_id WHERE c.id IN ({placeholders})
        AND c.script!='unlinked' AND c.mission_id!='unlinked'""", ids).fetchall()
    results = []
    variants = expand_query(query) if query else [""]
    for row in rows:
        item = dict(row)
        lines = conn.execute("SELECT id,speaker,search_text,kind FROM lines WHERE chunk_id=? ORDER BY position", (item["chunk_id"],)).fetchall()
        line_scores = []
        for idx, line in enumerate(lines):
            spk_ok = 1 if _speaker_matches_filter(line["speaker"], expanded_chars) else 0
            line_compact = _compact(line["search_text"])
            substantive = 1 if len(line_compact) >= 3 else 0
            if not query:
                line_scores.append((spk_ok, 0.0, 0, 0, substantive, -idx))
                continue
            scores = []
            line_text_lower = line["search_text"].lower()
            speaker_compact = _compact(line["speaker"])
            for variant in variants:
                text_score = _score_fields(variant, {"text": line["search_text"]}, allow_fuzzy)[0]
                full_score = _score_fields(variant, {"text": line["search_text"], "speaker": line["speaker"]}, allow_fuzzy)[0]
                score = text_score if text_score > 0 else (full_score * 0.6 if substantive else full_score * 0.2)
                words = re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", variant.lower())
                content_terms = _extract_content_terms(variant)
                coverage = sum(len(word) for word in words if word in line_text_lower or word in line_compact)
                text_sub_cov = sum(len(term) for term in (content_terms or _subterms(variant)) if term in line_compact)
                speaker_cov = sum(len(word) for word in words if word in speaker_compact) if substantive else 0
                scores.append((spk_ok, score, coverage, text_sub_cov, substantive, speaker_cov, -idx))
            line_scores.append(max(scores))
        match = lines[max(range(len(lines)), key=line_scores.__getitem__)] if lines else None
        item["line_id"] = match["id"] if match else ""
        item["speaker"] = match["speaker"] if match else ""
        item["text"] = _snippet(match["search_text"] if match else item["text"], query)
        item["kind"] = match["kind"] if match else "dialogue"
        if not query:
            item["matched_field"] = "speaker" if expanded_chars else "metadata"
            item["match_type"] = "exact"
            item["score"] = 1.0
        else:
            fields = {
                "text": match["search_text"] if match else "",
                "speaker": item["speaker"], "mission": item["mission_name"],
                "metadata": item["world"] + " " + item["chapter"] + " " + (item["version"] or ""),
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
    if best[0] == 0.0 and len(words) == 1 and len(words[0]) >= 5:
        content_terms = _extract_content_terms(words[0])
        if len(content_terms) >= 2:
            compact_fields = {k: _compact(normalize_text(v)) for k, v in fields.items()}
            combined_compact = " ".join(compact_fields.values())
            text_and_mission = compact_fields.get("text", "") + " " + compact_fields.get("mission", "")
            matched_terms = [t for t in content_terms if t in combined_compact]
            topic_terms = [t for t in content_terms if t not in KNOWN_ENTITIES]
            topic_hits = [t for t in topic_terms if t in text_and_mission]
            ratio = len(matched_terms) / len(content_terms)
            topic_ok = (not topic_terms) or len(topic_hits) > 0
            if topic_ok and (len(matched_terms) == len(content_terms) or (len(content_terms) >= 3 and len(matched_terms) >= 2 and ratio >= 0.65)):
                text_hits = sum(1 for t in matched_terms if t in compact_fields.get("text", ""))
                topic_ratio = (len(topic_hits) / len(topic_terms)) if topic_terms else 1.0
                base = 235.0 + ratio * 15.0 + topic_ratio * 20.0 if text_hits > 0 else 190.0 + ratio * 15.0
                target_field = "text" if text_hits > 0 else ("speaker" if any(t in compact_fields.get("speaker", "") for t in matched_terms) else "mission")
                best = (base, "keyword", target_field)
    return best


def _relevance_tier(match_type: str, matched_field: str, score: float) -> int:
    if match_type == "exact" and matched_field in {"text", "speaker", "mission"}:
        return 4
    if match_type == "keyword" and matched_field == "text" and score >= 260.0:
        return 3
    if match_type in {"exact", "keyword"}:
        return 2
    return 1


def lexical_search(path: Path | str, query: str, filters: dict | None = None, limit: int = 100,
                   *, allow_fuzzy: bool = False) -> list[dict]:
    query = normalize_text(query)[:500]
    filters = filters or {}
    limit = max(1, min(limit, 500))
    condition, filter_args = filter_sql(filters)
    if not query:
        if not any(bool(v) for v in filters.values()):
            return []
        with open_db(path) as conn:
            rows = conn.execute(f"""SELECT c.id FROM chunks c JOIN missions m ON m.id=c.mission_id
                WHERE 1=1 {condition}
                ORDER BY CAST(substr(m.version, 1, instr(m.version || '.', '.') - 1) AS INTEGER) DESC,
                CAST(substr(m.version, instr(m.version || '.', '.') + 1) AS INTEGER) DESC,
                CASE WHEN m.type='开拓任务' THEN 0 WHEN m.type='Gap' THEN 1 WHEN m.type='同行任务' THEN 2 WHEN m.type='支线任务' THEN 3 ELSE 4 END,
                m.id DESC, c.id ASC LIMIT ?""", filter_args + [limit]).fetchall()
            ordered = [r[0] for r in rows]
            return _results(conn, ordered, "", False, filters)
    variants = expand_query(query)
    expanded_chars = _expand_character_filter(filters)
    candidates: dict[int, tuple[float, str, str]] = {}
    versions_by_id: dict[int, tuple[int, int]] = {}
    fts_ranks: dict[int, float] = {}
    with open_db(path) as conn:
        candidate_ids: set[int] = set()
        for variant in variants:
            escaped = variant.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            # Literal matches also cover punctuation-only, version queries, and short queries.
            rows = conn.execute(f"""SELECT c.id FROM chunks c JOIN missions m ON m.id=c.mission_id
                WHERE (c.text LIKE ? ESCAPE '\\' OR m.name LIKE ? ESCAPE '\\' OR m.version = ?) {condition}
                ORDER BY CAST(substr(m.version, 1, instr(m.version || '.', '.') - 1) AS INTEGER) DESC,
                CAST(substr(m.version, instr(m.version || '.', '.') + 1) AS INTEGER) DESC,
                CASE WHEN c.text LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END,c.id LIMIT ?""",
                [f"%{escaped}%", f"%{escaped}%", variant] + filter_args + [f"%{escaped}%", max(200, limit * 2)]).fetchall()
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
            content_terms = _extract_content_terms(variant)
            if len(content_terms) >= 2 and not allow_fuzzy:
                ct_bigrams = [t[:2] for t in content_terms if len(t) >= 2][:8]
                if len(ct_bigrams) >= 2:
                    ct_expr = " AND ".join('"' + token.replace('"', '""') + '"' for token in ct_bigrams)
                    ct_rows = conn.execute(f"""SELECT c.id,bm25(chunks_fts) rank FROM chunks_fts
                        JOIN chunks c ON c.id=chunks_fts.rowid JOIN missions m ON m.id=c.mission_id
                        WHERE chunks_fts MATCH ? {condition} ORDER BY rank LIMIT ?""",
                        [ct_expr] + filter_args + [max(250, limit * 3)]).fetchall()
                    for row in ct_rows:
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
        rows = conn.execute(f"""SELECT c.id,c.text,m.name,m.world,m.chapter,m.type,m.version,
            (SELECT group_concat(search_text,'\n') FROM lines WHERE chunk_id=c.id) body,
            (SELECT group_concat(DISTINCT speaker) FROM lines WHERE chunk_id=c.id) speakers
            FROM chunks c JOIN missions m ON m.id=c.mission_id WHERE c.id IN ({placeholders})""", list(candidate_ids)).fetchall()
        for row in rows:
            body_text = row["body"] or ""
            fields = {"text": body_text, "speaker": row["speakers"] or "", "mission": row["name"], "metadata": row["world"] + " " + row["chapter"] + " " + (row["version"] or "")}
            score, match_type, matched_field = max((_score_fields(variant, fields, allow_fuzzy) for variant in variants), key=lambda value: value[0])
            if score > 0:
                line_bonus = max((_score_fields(variant, {"text": line_str}, allow_fuzzy)[0] for variant in variants for line_str in body_text.split("\n") if line_str), default=0.0)
                char_bonus = 0.0
                if expanded_chars:
                    for raw_line in (row["text"] or "").split("\n"):
                        if "：" in raw_line:
                            spk, ltxt = raw_line.split("：", 1)
                            if _speaker_matches_filter(spk, expanded_chars):
                                if max((_score_fields(variant, {"text": ltxt}, allow_fuzzy)[0] for variant in variants), default=0.0) > 0:
                                    char_bonus = 35.0
                                    break
                type_bonus = 1.5 if row["type"] and row["type"] != "其他任务" else 0.0
                bonus = (4.0 if line_bonus >= 265.0 else 0.0) + char_bonus + type_bonus + min(3.9, max(0.0, -fts_ranks.get(row["id"], 0.0)) * 0.15)
                candidates[row["id"]] = (score + bonus, match_type, matched_field)
                versions_by_id[row["id"]] = version_sort_key(row["version"])
        def _lex_sort_key(identifier: int) -> tuple:
            sc, mtype, mfield = candidates[identifier]
            tier = _relevance_tier(mtype, mfield, sc)
            ver_maj, ver_min = versions_by_id[identifier]
            if tier == 4:
                return (-tier, -ver_maj, -ver_min, -sc, identifier)
            return (-tier, -int(sc // 8), -ver_maj, -ver_min, -sc, identifier)
        ordered = sorted(candidates, key=_lex_sort_key)[:limit]
        results = _results(conn, ordered, query, allow_fuzzy, filters)
        for item in results:
            item["score"], item["match_type"], item["matched_field"] = candidates[item["chunk_id"]]
        return results
