const whitespace = /[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/g;
const cjk = /^[\u3400-\u9fff]+$/;
const weights = { text: 300, speaker: 280, mission: 220, metadata: 170 };
const noMatch = [0, 'keyword', 'text'];
const wordsOf = text => text.match(/[\u3400-\u9fff]+|[a-z0-9]+/g) || [];
const escapeRegex = text => text.replace(/[.*+?^\x24{}()|[\]\\]/g, '\\$&');
export const compact = text => wordsOf(text.toLowerCase()).join('');
export const asciiLower = text => text.replace(/[A-Z]/g, letter => letter.toLowerCase());

export function normalize(text) {
  return text.replace(/\{RUBY_B#([^}]*)\}(.*?)\{RUBY_E#\}/g, '$2（$1）')
    .replace(/<\/?(?:color|size|b|i|it|u|s|align|alpha|voffset|font|material|link|noparse|unbreak|unbreakable|icon)(?=[\s=>])[^>]*>/gi, '')
    .replace(/\{(?:F|M)#([^}]*)\}(?=\{[FM]#)/g, '$1 ')
    .replace(/\{(?:F|M)#([^}]*)\}/g, '$1')
    .replace(/\{NICKNAME\}/g, '开拓者').replace(/\\n/g, '\n')
    .normalize('NFKC').replace(whitespace, ' ').replace(/^ | $/g, '');
}

export function compareTuples(left, right) {
  for (let i = 0; i < left.length; i++) {
    if (left[i] < right[i]) return -1;
    if (left[i] > right[i]) return 1;
  }
  return 0;
}

export function versionKey(version) {
  const parts = version.match(/\d+/g) || [];
  return [Number(parts[0] || 0), Number(parts[1] || 0)];
}

export function ftsSourceTokens(text) {
  const tokens = new Set();
  for (const run of text.toLowerCase().match(/[\u3400-\u9fff]+|[a-z0-9_]+/g) || []) {
    if (!cjk.test(run)) tokens.add(run);
    else for (let index = 0; index < run.length; index++) {
      tokens.add(run[index]);
      if (index + 1 < run.length) tokens.add(run.slice(index, index + 2));
    }
  }
  return Array.from(tokens).sort();
}

export function ftsQueryTokens(query) {
  return ftsSourceTokens(query).sort((a, b) => b.length - a.length || (a < b ? -1 : a > b ? 1 : 0)).slice(0, 48);
}

function subterms(text) {
  const terms = new Set();
  for (const run of wordsOf(text.toLowerCase())) {
    if (!cjk.test(run) || run.length <= 2) terms.add(run);
    else for (const size of [4, 3, 2]) {
      for (let i = 0; i + size <= run.length; i++) terms.add(run.slice(i, i + size));
    }
  }
  return Array.from(terms);
}

export function snippet(text, query) {
  const clean = normalize(text);
  const chars = Array.from(clean);
  if (chars.length <= 190) return clean;
  const lower = clean.toLowerCase();
  let match = lower.indexOf(query.toLowerCase());
  if (match < 0) for (const token of subterms(query)) {
    match = lower.indexOf(token);
    if (match >= 0) break;
  }
  const start = Math.max(0, Array.from(lower.slice(0, Math.max(0, match))).length - 50);
  return (start ? '…' : '') + chars.slice(start, start + 185).join('') + (start + 185 < chars.length ? '…' : '');
}

export function relevanceTier([score, matchType, field]) {
  if (matchType === 'exact' && field !== 'metadata') return 4;
  if (matchType === 'keyword' && field === 'text' && score >= 260) return 3;
  return 2;
}

export function makeRules(manifest) {
  const aliases = Object.assign(Object.create(null), manifest.aliases);
  const orderedAliases = Object.keys(aliases).map((alias, index) => [alias, index])
    .sort((a, b) => b[0].length - a[0].length || a[1] - b[1]).map(([alias]) => alias);
  const entities = new Set(manifest.entities);
  const nonSpeakers = new Set(manifest.nonSpeakers);
  const stopwords = new RegExp(manifest.stopwords, 'g');
  const entityPatterns = manifest.entities.map(entity => [
    entity, new RegExp(escapeRegex(entity) + '(?:[说讲问喊劝](?=[\\u3400-\\u9fff]{2,}))?', 'g'),
  ]);
  const synonyms = Object.assign(Object.create(null), manifest.synonyms);
  const termScore = (term, value) => value.includes(term) ? 1
    : (synonyms[term] || []).some(other => value.includes(other)) ? 0.85 : 0;

  function isPunctuation(char) {
    const code = char.codePointAt(0);
    const ranges = manifest.wordRanges;
    let low = 0, high = ranges.length;
    while (low < high) {
      const middle = (low + high) >>> 1;
      if (ranges[middle] <= code) low = middle + 1;
      else high = middle;
    }
    return low % 2 === 0;
  }

  function extractTerms(query) {
    let text = query.toLowerCase();
    const terms = new Set();
    let hasEntity = false;
    for (const [entity, pattern] of entityPatterns) {
      if (!text.includes(entity)) continue;
      terms.add(entity);
      hasEntity = true;
      text = text.replace(pattern, ' ');
    }
    for (const run of wordsOf(text.replace(stopwords, ' '))) {
      if (run.length < 2) continue;
      if ((run.length > 4 || (hasEntity && run.length === 4)) && cjk.test(run)) {
        for (let i = 0; i < run.length - 1; i += 2) terms.add(run.slice(i, i + 2));
        if (run.length % 2) terms.add(run.slice(-2));
      } else terms.add(run);
    }
    return Array.from(terms);
  }

  function variantsFor(query) {
    const original = normalize(query);
    const values = new Set([original]);
    for (const alias of orderedAliases) {
      if (original.includes(alias) && !original.includes(aliases[alias])) {
        values.add(original.split(alias).join(aliases[alias]));
      }
    }
    return Array.from(values, value => {
      const lower = value.toLowerCase();
      const words = wordsOf(lower);
      const joined = words.join('');
      const terms = extractTerms(lower);
      const actors = terms.filter(term => entities.has(term) && !nonSpeakers.has(term));
      const topics = terms.filter(term => !entities.has(term));
      return {
        value, lower, words, joined, terms, actors, topics: actors.length >= 2 ? topics : [],
        scoreTopics: topics, subterms: terms.length ? terms : subterms(lower),
        hasPunctuation: Array.from(lower).some(isPunctuation),
        useContent: (words.length === 1 && joined.length >= 5)
          || (words.length >= 2 && joined.length >= 5 && terms.some(term => entities.has(term))),
      };
    });
  }

  function scoreFields(variant, fields) {
    const { lower, words, joined } = variant;
    if (!lower) return noMatch;
    let best = noMatch;
    const normalized = {};
    const flattened = {};
    for (const field of Object.keys(fields)) {
      normalized[field] = normalize(fields[field]).toLowerCase();
      flattened[field] = compact(normalized[field]);
      const value = normalized[field];
      const flat = flattened[field];
      const lines = field === 'text' && fields[field].includes('\n') ? fields[field].split('\n') : [];
      let candidate;
      if (value.includes(lower) && (!lines.length || lines.some(line => normalize(line).toLowerCase().includes(lower)))) {
        candidate = [weights[field], 'exact', field];
      } else if (joined.length >= 2 && (words.length === 1 || variant.hasPunctuation)
        && flat.includes(joined) && (!lines.length || lines.some(line => compact(normalize(line)).includes(joined)))) {
        candidate = [weights[field] - 5, 'exact', field];
      } else if (words.length && words.every(word => value.includes(word) || flat.includes(word))) {
        candidate = [weights[field] - 30, 'keyword', field];
      }
      if (candidate && candidate[0] > best[0]) best = candidate;
    }
    if (best[0]) return best;
    const combined = Object.values(flattened).join(' ');
    if (words.length > 1 && words.every(word => combined.includes(word))) {
      if (words.some(word => (flattened.text || '').includes(word))) return [255, 'keyword', 'text'];
      if (words.some(word => (flattened.speaker || '').includes(word))) return [235, 'keyword', 'speaker'];
      if (words.some(word => (flattened.mission || '').includes(word))) return [195, 'keyword', 'mission'];
      return [150, 'keyword', 'metadata'];
    }
    const { terms, actors, scoreTopics } = variant;
    if (!variant.useContent || terms.length < 2) return best;
    const scores = terms.map(term => termScore(term, combined));
    const matched = terms.filter((_, i) => scores[i] > 0);
    const weightedRatio = scores.reduce((total, score) => total + score, 0) / terms.length;
    const body = (flattened.text || '') + ' ' + (flattened.mission || '');
    const topicSum = scoreTopics.reduce((total, term) => total + termScore(term, body), 0);
    const actorHits = actors.filter(term => combined.includes(term)).length;
    if ((scoreTopics.length && !topicSum) || (actors.length && !actorHits)
      || !(matched.length === terms.length || (terms.length >= 3 && matched.length >= 2 && weightedRatio >= 0.65))) return best;
    const textHit = matched.some(term => termScore(term, flattened.text || '') > 0);
    const actorRatio = actors.length ? actorHits / actors.length : 1;
    if (textHit) return [225 + weightedRatio * 15 + (scoreTopics.length ? topicSum / scoreTopics.length : 1) * 18 + actorRatio * 14, 'keyword', 'text'];
    return [185 + weightedRatio * 15 + actorRatio * 10, 'keyword',
      matched.some(term => (flattened.speaker || '').includes(term)) ? 'speaker' : 'mission'];
  }

  function bestScore(variants, fields) {
    let best = noMatch;
    for (const variant of variants) {
      const candidate = scoreFields(variant, fields);
      if (candidate[0] > best[0]) best = candidate;
    }
    return best;
  }

  function sceneEvidence({ actors, topics }, raw, speakers) {
    if (!topics.length || !actors.every(actor => speakers.some(speaker => speaker.startsWith(actor)))) return 0;
    let level = 0;
    for (const line of raw.split('\n')) {
      const colon = line.indexOf('：');
      const speaker = colon < 0 ? '' : line.slice(0, colon);
      const flat = compact(colon < 0 ? line : line.slice(colon + 1));
      const allTopics = topics.every(term => termScore(term, flat) > 0);
      const actor = actors.find(value => speaker.startsWith(value));
      const addressedOther = actor && actors.some(other => other !== actor && flat.includes(other));
      if (allTopics && addressedOther) return 3;
      if (allTopics) level = Math.max(level, 2);
      else if (addressedOther && topics.some(term => termScore(term, flat) > 0)) level = Math.max(level, 1);
    }
    return level;
  }

  function ftsPlans(variant) {
    const tokens = ftsQueryTokens(variant.value);
    const bigrams = tokens.filter(term => term.length >= 2).slice(0, 16);
    const termGroup = term => Array.from(new Set([term, ...(synonyms[term] || [])].map(value => value.slice(0, 2))));
    const groups = variant.terms.filter(term => term.length >= 2).slice(0, 8).map(termGroup);
    const actorGroups = variant.terms.filter(term => entities.has(term)).slice(0, 4).map(termGroup);
    const topicGroups = variant.terms.filter(term => !entities.has(term)).slice(0, 4).map(termGroup);
    return {
      all: bigrams.length >= 2 ? bigrams.map(term => [term]) : [],
      content: groups.length >= 2 ? groups : [],
      partial: groups.length >= 3 && actorGroups.length
        ? topicGroups.map(group => [...actorGroups, group]) : [],
      any: [(bigrams.length >= 2 ? bigrams : tokens)],
    };
  }

  function expandedCharacters(filters) {
    const raw = filters.characters || filters.character;
    const values = Array.isArray(raw) ? raw : raw ? [raw] : [];
    const expanded = new Set();
    for (const value of values) {
      const speaker = String(value).trim();
      if (speaker) {
        expanded.add(speaker);
        expanded.add(aliases[speaker] || speaker);
      }
    }
    return Array.from(expanded);
  }

  function matchesSpeaker(speaker, characters) {
    return !characters.length || characters.some(character => speaker.trim().startsWith(character));
  }

  function chooseLine(lines, variants, query, characters) {
    let best, winner;
    for (const [index, line] of lines.entries()) {
      const flat = compact(line.searchText);
      const substantive = Number(flat.length >= 3);
      const speakerOk = Number(matchesSpeaker(line.speaker, characters));
      const evaluations = query ? variants.map(variant => {
        const [rawScore, match] = scoreFields(variant, { text: line.searchText });
        const score = rawScore || scoreFields(variant, { text: line.searchText, speaker: line.speaker })[0] * (substantive ? 0.6 : 0.2);
        const topicLine = Number(variant.topics.length > 0 && variant.topics.every(term => termScore(term, flat) > 0));
        const coverage = variant.words.reduce((sum, word) => sum + (line.searchText.toLowerCase().includes(word) || flat.includes(word) ? word.length : 0), 0);
        const subCoverage = variant.subterms.reduce((sum, term) => sum + term.length * termScore(term, flat), 0);
        const speakerCoverage = substantive
          ? variant.words.reduce((sum, word) => sum + (compact(line.speaker).includes(word) ? word.length : 0), 0) : 0;
        return [Number(match === 'exact'), speakerOk, topicLine, score, coverage, subCoverage, substantive, speakerCoverage, -index];
      }) : [[speakerOk, 0, 0, 0, substantive, -index]];
      for (const evaluation of evaluations) if (!best || compareTuples(evaluation, best) > 0) {
        best = evaluation;
        winner = line;
      }
    }
    return winner;
  }
  return { variantsFor, scoreFields, bestScore, sceneEvidence, ftsPlans, expandedCharacters, matchesSpeaker, chooseLine };
}
