import { stableLineId } from './line-id.mjs';
import {
  asciiLower, compareTuples, makeRules, normalize, relevanceTier, snippet, versionKey,
} from './search-rules.mjs';

const candidateLimit = 500;
const typePriority = ['开拓任务', 'Gap', '同行任务', '支线任务'];
const chinese = /^[\u3400-\u9fff]{1,2}$/;
const performancePattern = /^(?:Config\/Level\/Mission\/\d+\/Act\/Act|Story\/Mission\/\d+\/Story|Story\/Discussion\/Mission\/\d+\/DS)(\d+)\.json$/;
const yieldTask = () => new Promise(resolve => setTimeout(resolve, 0));

function sqlVersion(version) {
  const [major, minor] = version.split('.');
  return [parseInt(major || '', 10) || 0, parseInt(minor === undefined ? major : minor, 10) || 0];
}

function resultScore(rank) {
  // Reproduce the web API's six-decimal rounding, including the 1/128 tie.
  const denominator = 61 + rank;
  const whole = Math.floor(1000000 / denominator);
  const remainder = 2 * (1000000 % denominator);
  return (whole + Number(remainder > denominator || (remainder === denominator && whole % 2 === 1))) / 1000000;
}

function filterValues(filters, key, plural) {
  const val = filters[plural || key + 's'] || filters[key];
  return Array.isArray(val) ? val.map(String) : val ? [String(val)] : [];
}

function matchesFilters(mission, filters) {
  return [['world'], ['type'], ['version'], ['id', 'mission_ids']].every(([field, plural]) => {
    const key = field === 'id' ? 'mission_id' : field;
    const values = filterValues(filters, key, plural);
    return !values.length || values.some(value => mission[field] === (field === 'version' && value === 'unknown' ? '' : value));
  });
}

function asciiPhrases(text, pretokenized) {
  const runs = text.match(/[a-z0-9_]+/g) || [];
  const tokens = pretokenized ? runs : Array.from(new Set(runs)).sort();
  return tokens.join('_').match(/[a-z0-9]+/g) || [];
}

function phraseFrequency(phrase, words) {
  let count = 0;
  for (let i = 0; i <= words.length - phrase.length; i++) {
    if (phrase.every((word, j) => word === words[i + j])) count++;
  }
  return count;
}

function collectDeduplicated(items) {
  const seen = new Set();
  const mirrors = new Map();
  const output = [];
  for (const item of items) {
    const performance = performancePattern.exec(item.script);
    if (performance && item.sentence_id) {
      const key = item.mission_id + '\n' + performance[1] + '\n' + item.sentence_id;
      if (!mirrors.has(key)) mirrors.set(key, item.script);
      if (mirrors.get(key) !== item.script) continue;
    }
    if (items.length > 5) {
      const key = JSON.stringify([item.mission_id, item.speaker, item.text]);
      if (seen.has(key)) continue;
      seen.add(key);
    }
    output.push(item);
  }
  return output;
}

export function createArchive(manifest, loadShard) {
  const rules = makeRules(manifest);
  const missionNumbers = new Map(manifest.missions.map(([mission], index) => [mission.id, index]));
  const missions = new Map();
  const pending = new Map();
  const { scripts, speakers, kinds, nodes, texts, dialogueOrigins, scriptMissions } = manifest;
  let lastKey = '';
  let lastResult;
  const originalText = raw => typeof raw[7] === 'number' ? texts[raw[7]] : raw[7];

  async function load(index) {
    if (!pending.has(index)) {
      const work = Promise.resolve().then(() => loadShard(index)).then(rows => {
        if (!rows) throw new Error('剧情数据没有正确加载');
        for (const [, chunks] of rows) {
          // IDs are stored as deltas within a task. Restore their original values once.
          let chunkId = 0, sentenceId = 0;
          for (const chunk of chunks) {
            chunkId += chunk[0];
            chunk[0] = chunkId;
            for (const line of chunk[4]) if (typeof line[2] === 'number') {
              sentenceId += line[2];
              line[2] = sentenceId;
            }
          }
        }
        for (const row of rows) missions.set(row[0], row);
        return rows;
      }).catch(error => { pending.delete(index); throw error; });
      pending.set(index, work);
    }
    return pending.get(index);
  }

  function lineOf(raw, scriptIndex, missionId, chunkId) {
    const [sequence, position, sentence, speaker, kind, branch, trigger] = raw;
    const script = scripts[scriptIndex];
    const sentenceId = String(sentence);
    const lineKind = kinds[kind];
    const origin = dialogueOrigins[scriptIndex];
    return {
      id: stableLineId(script, sequence, position, sentenceId, lineKind), mission_id: missionId,
      script, sequence, position, sentence_id: sentenceId, speaker: speakers[speaker], kind: lineKind,
      branch: nodes[branch], trigger: nodes[trigger], text: originalText(raw),
      chunk_id: chunkId, search_text: raw[8] === undefined ? normalize(originalText(raw)) : raw[8],
      ...(origin && origin[1].includes(sentenceId) ? { dialogue_origin: origin[0] } : {}),
    };
  }

  async function missionDetails(missionId, lineId, offset = 0, limit = 1000) {
    const number = missionNumbers.get(missionId);
    if (number === undefined) throw new Error('当前数据中没有找到该任务');
    const [mission, shard] = manifest.missions[number];
    await load(shard);
    const [, chunks, readOrder, mirrors, edgeRows, ordered, optional, recalls, sceneLabels] = missions.get(number);
    const lines = [];
    for (const [chunkId, script, , , packed] of chunks) {
      for (const row of packed) lines.push(lineOf(row, script, missionId, chunkId));
    }
    const displayed = readOrder.length ? readOrder.map(index => lines[index]) : lines;
    if (lineId && mirrors.length) {
      const alias = mirrors.find(([from]) => lines[from].id === lineId);
      if (alias) lineId = lines[alias[1]].id;
    }
    limit = Math.max(1, Math.min(1000, limit));
    offset = Math.max(0, offset);
    let missing = false;
    if (lineId) {
      const index = displayed.findIndex(line => line.id === lineId);
      missing = index < 0;
      if (!missing) offset = Math.max(0, index - Math.floor(limit / 3));
      else lineId = null;
    }
    const labels = new Map(sceneLabels.map(([index, label]) => [scripts[index], label]));
    const page = displayed.slice(offset, offset + limit);
    for (const line of page) if (labels.has(line.script)) line.scene_label = labels.get(line.script);
    const pageScripts = new Set(page.map(line => line.script));
    const edges = [];
    for (const [index, group] of edgeRows) {
      if (!pageScripts.has(scripts[index])) continue;
      for (const [from, to, kind] of group) edges.push({
        script: scripts[index], source: nodes[from], target: nodes[to], kind: kinds[kind],
      });
    }
    return {
      mission, lines: page, edges, recalls, ordered_scripts: ordered.map(index => scripts[index]),
      optional_scripts: optional.map(index => scripts[index]), anchor_line_id: lineId || null,
      anchor_missing: missing, total: displayed.length, offset,
      has_more: offset + page.length < displayed.length, snapshot: manifest.snapshot,
    };
  }

  async function bookmarkDetails(missionId, lineId, script) {
    const original = missionNumbers.has(missionId) ? await missionDetails(missionId, lineId) : null;
    if (original && !original.anchor_missing) return original;
    // Older packages stored the mission ID alongside a stable ID that does not depend on task ownership.
    const owner = scriptMissions[scripts.indexOf(script)];
    const candidates = Array.isArray(owner) ? owner : owner === undefined ? [] : [owner];
    if (lineId) for (const number of candidates) {
      const current = manifest.missions[number][0].id;
      if (current === missionId) continue;
      const detail = await missionDetails(current, lineId);
      if (!detail.anchor_missing) return detail;
    }
    if (original) return original;
    throw new Error('当前档案中没有找到收藏的台词');
  }

  function searchTextOf(line) {
    return line[8] === undefined ? normalize(originalText(line)) : line[8];
  }

  function sourceOf(row, normalized) {
    return row[5] === undefined ? row[4].map((line, i) => (speakers[line[3]] ? speakers[line[3]] + '：' : '')
      + (normalized ? normalized[i] : searchTextOf(line))).join('\n') : row[5];
  }

  function documentOf(mission, row) {
    const [id, script, branch, , rawLines] = row;
    const searchable = rawLines.map(searchTextOf);
    return { id, mission, script, branch, rawLines, searchable, source: sourceOf(row, searchable) };
  }

  async function gather(filters, variants, characters, cancelled) {
    const plans = variants.map(rules.ftsPlans);
    const allTerms = new Set();
    for (const plan of plans) {
      for (const group of [...plan.all, ...plan.content, ...plan.any, ...[].concat(...plan.partial)]) {
        for (const term of group) allTerms.add(term);
      }
    }
    const hits = new Map(Array.from(allTerms, term => [term, new Uint16Array(manifest.chunks)]));
    const termCounts = new Map(Array.from(allTerms, term => [term, 0]));
    const han = Array.from(allTerms).filter(term => chinese.test(term));
    const ascii = Array.from(allTerms).filter(term => !chinese.test(term))
      .map(term => [term, term.match(/[a-z0-9]+/g) || []]).filter(([, phrase]) => phrase.length > 0);
    const literals = variants.map(variant => ({ value: variant.value, like: asciiLower(variant.value), rows: [] }));
    const prefix = characters.map(asciiLower);
    const eligible = [];
    let index = 0;
    for (let start = 0; start < manifest.shards.length; start += 4) {
      const batch = manifest.shards.slice(start, start + 4);
      const blocks = await Promise.all(batch.map((_, i) => load(start + i)));
      for (const block of blocks) {
        if (cancelled()) throw new Error('搜索已取消');
        for (const [missionIndex, chunks] of block) {
          const mission = manifest.missions[missionIndex][0];
          const context = [mission.world, mission.chapter, mission.name].filter(Boolean).join(' · ');
          const missionName = asciiLower(mission.name);
          const [major, minor] = sqlVersion(mission.version);
          const allowed = mission.id !== 'unlinked' && matchesFilters(mission, filters);
          for (const row of chunks) {
            const docIndex = index++;
            const possible = allowed && scripts[row[1]] !== 'unlinked' && (!prefix.length
              || row[4].some(line => prefix.some(character => asciiLower(speakers[line[3]]).startsWith(character))));
            if (!possible && !allTerms.size) continue;
            if (possible) eligible.push([docIndex, mission, row]);
            if (!variants.length) continue;
            const raw = sourceOf(row);
            if (possible) {
              const likeText = asciiLower(raw);
              for (const literal of literals) {
                const inText = Number(likeText.includes(literal.like));
                if (inText || missionName.includes(literal.like) || mission.version === literal.value) {
                  literal.rows.push([[-major, -minor, -inText, row[0]], mission, row]);
                }
              }
            }
            if (!allTerms.size) continue;
            const pretokenized = row[6] !== undefined;
            const ftsText = pretokenized ? ' ' + row[6] + ' ' : (context + '\n' + raw).toLowerCase();
            for (const term of han) {
              if (ftsText.includes(pretokenized ? ' ' + term + ' ' : term)) {
                hits.get(term)[docIndex] = 1;
                termCounts.set(term, termCounts.get(term) + 1);
              }
            }
            if (ascii.length) {
              const words = asciiPhrases(ftsText, pretokenized);
              for (const [term, phrase] of ascii) {
                const frequency = phraseFrequency(phrase, words);
                if (frequency) {
                  hits.get(term)[docIndex] = frequency;
                  termCounts.set(term, termCounts.get(term) + 1);
                }
              }
            }
          }
        }
        await yieldTask();
      }
    }
    return { plans, hits, termCounts, eligible, literals };
  }

  function ftsCandidates(groups, limit, eligible, hits, idf, candidates) {
    if (!groups.length || !groups[0].length) return 0;
    const ranks = [];
    for (const [index, mission, row] of eligible) {
      // SQLite FTS5 BM25: k1=1.2, b=0.75, with one indexed column.
      let rank = 0;
      let matches = true;
      const lengthPenalty = 1.2 * (0.25 + 0.75 * row[3] / manifest.avgDocLength);
      for (const group of groups) {
        let groupRank = 0;
        for (const term of group) {
          const freq = hits.get(term)[index];
          if (freq) groupRank += idf.get(term) * (freq * 2.2) / (freq + lengthPenalty);
        }
        if (!groupRank) { matches = false; break; }
        rank += groupRank;
      }
      if (matches) ranks.push([rank, row[0], mission, row]);
    }
    ranks.sort((a, b) => b[0] - a[0] || a[1] - b[1]);
    for (const [rank, id, mission, row] of ranks.slice(0, limit)) {
      const previous = candidates.get(id);
      if (!previous || previous[0] < rank) candidates.set(id, [rank, mission, row]);
    }
    return Math.min(ranks.length, limit);
  }

  function rankCandidates(variants, plans, eligible, hits, termCounts, literals, characters) {
    const candidates = new Map();
    const idf = new Map();
    for (const [term, count] of termCounts) {
      const calculated = Math.log((manifest.chunks - count + 0.5) / (count + 0.5));
      idf.set(term, calculated > 0 ? calculated : 0.000001);
    }
    for (const [index, literal] of literals.entries()) {
      literal.rows.sort((a, b) => compareTuples(a[0], b[0]));
      for (const [, mission, row] of literal.rows.slice(0, candidateLimit * 2)) {
        if (!candidates.has(row[0])) candidates.set(row[0], [0, mission, row]);
      }
      const plan = plans[index];
      const cap = candidateLimit * 3;
      ftsCandidates(plan.all, cap, eligible, hits, idf, candidates);
      const contentCount = ftsCandidates(plan.content, cap, eligible, hits, idf, candidates);
      if (contentCount < candidateLimit) for (const partial of plan.partial) {
        ftsCandidates(partial, 100, eligible, hits, idf, candidates);
      }
      ftsCandidates(plan.any, cap, eligible, hits, idf, candidates);
    }
    const ranked = [];
    for (const [bm25, mission, row] of candidates.values()) {
      const doc = documentOf(mission, row);
      const speakerNames = Array.from(new Set(doc.rawLines.map(line => speakers[line[3]])));
      const metadata = doc.mission.world + ' ' + doc.mission.chapter + ' ' + doc.mission.version;
      const fields = { text: doc.searchable.join('\n'), speaker: speakerNames.join(','), mission: doc.mission.name, metadata };
      const [score, match, field] = rules.bestScore(variants, fields);
      if (!score) continue;
      const lineHit = doc.searchable.some(text => rules.bestScore(variants, { text })[0] >= 265);
      const characterHit = characters.length > 0 && doc.source.split('\n').some(line => {
        const colon = line.indexOf('：');
        return colon >= 0 && rules.matchesSpeaker(line.slice(0, colon), characters)
          && rules.bestScore(variants, { text: line.slice(colon + 1) })[0] > 0;
      });
      const scene = Math.max(...variants.map(variant => rules.sceneEvidence(variant, doc.source, speakerNames)));
      const type = doc.mission.type;
      const combined = score + (lineHit ? 4 : 0) + (characterHit ? 35 : 0) + [0, 6, 12, 20][scene]
        + (type && type !== '其他任务' ? 1.5 : 0) + Math.min(3.9, bm25 * 0.15);
      const tier = relevanceTier([combined, match, field]);
      const [major, minor] = versionKey(doc.mission.version);
      const key = tier === 4 ? [-tier, -major, -minor, -combined, doc.id]
        : [-tier, -Math.floor(combined / 8), -major, -minor, -combined, doc.id];
      ranked.push({ key, doc, match, field });
    }
    ranked.sort((a, b) => compareTuples(a.key, b.key));
    return ranked.slice(0, candidateLimit);
  }

  function filterOnly(eligible, characters) {
    const rows = eligible.map(([, mission, row]) => {
      const [major, minor] = sqlVersion(mission.version);
      const type = typePriority.indexOf(mission.type);
      return { key: [-major, -minor, type < 0 ? typePriority.length : type], mission, row };
    });
    rows.sort((a, b) => compareTuples(a.key, b.key)
      || (a.mission.id < b.mission.id ? 1 : a.mission.id > b.mission.id ? -1 : a.row[0] - b.row[0]));
    return rows.slice(0, candidateLimit).map(({ mission, row }) => ({
      doc: documentOf(mission, row), match: 'exact', field: characters.length ? 'speaker' : 'metadata',
    }));
  }

  function resultFor({ doc, match, field }, rank, variants, query, characters) {
    const script = scripts[doc.script];
    const sorted = doc.rawLines.map((row, index) => ({ row, index, position: row[1],
      speaker: speakers[row[3]], searchText: doc.searchable[index] }));
    sorted.sort((a, b) => a.position - b.position || a.index - b.index);
    const hit = rules.chooseLine(sorted, variants, query, characters);
    const line = hit && lineOf(hit.row, doc.script, doc.mission.id, doc.id);
    const mission = doc.mission;
    return {
      chunk_id: doc.id, mission_id: mission.id, mission_name: mission.name, chapter: mission.chapter,
      world: mission.world, type: mission.type, version: mission.version, version_status: mission.version_status,
      version_source: mission.version_source, line_id: line ? line.id : '', sentence_id: line ? line.sentence_id : '',
      script, branch: nodes[doc.branch], speaker: line ? line.speaker : '', kind: line ? line.kind : 'dialogue',
      text: snippet(hit ? hit.searchText : doc.source, query),
      ...(line && Array.from(line.text).length <= 350 ? { display_text: line.text } : {}),
      ...(line?.dialogue_origin ? { dialogue_origin: line.dialogue_origin } : {}),
      matched_field: field, match_type: match, score: resultScore(rank),
    };
  }

  async function search(rawQuery, filters = {}, offset = 0, limit = 30, cancelled = () => false) {
    const start = Date.now();
    const query = Array.from(normalize(rawQuery)).slice(0, 500).join('');
    const key = JSON.stringify([query, filters]);
    let value = lastKey === key ? lastResult : null;
    if (!value) {
      const variants = query ? rules.variantsFor(query) : [];
      const characters = rules.expandedCharacters(filters);
      const { plans, hits, termCounts, eligible, literals } = await gather(filters, variants, characters, cancelled);
      const lexical = query ? rankCandidates(variants, plans, eligible, hits, termCounts, literals, characters)
        : Object.values(filters).some(Boolean) ? filterOnly(eligible, characters) : [];
      value = {
        results: collectDeduplicated(lexical.map((entry, rank) => resultFor(entry, rank, variants, query, characters))),
        truncated: lexical.length >= candidateLimit,
      };
      lastKey = key;
      lastResult = value;
    }
    return {
      results: value.results.slice(offset, offset + limit), total: value.results.length, truncated: value.truncated,
      search_mode: 'text', notice: '', snapshot: manifest.snapshot, elapsed_ms: Date.now() - start,
    };
  }

  return { search, missionDetails, bookmarkDetails };
}
