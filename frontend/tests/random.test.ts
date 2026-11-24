import test from 'node:test';
import assert from 'node:assert/strict';
import { rememberRandomLine, restoredRandomResult, restoredRandomResponse } from '../src/random.ts';
import { buildMissionHash, parseRoute } from '../src/route.ts';
import type { MissionResponse, Result } from '../src/types.ts';

const snapshot = 'a'.repeat(24);
const filters = { world: '空间站', version: '1.0', type: 'Main', character: '老杨' };
const selection: Result = {
  chunk_id: 0, mission_id: '123', mission_name: '', chapter: '', world: '', version: '',
  version_status: 'unknown', speaker: '', text: '', line_id: 'old-anchor', script: 'Act/1.json',
  branch: '', match_type: 'random', score: 0,
};

test('random links retain the selected line, snapshot and filters without a search query', () => {
  const hash = buildMissionHash({ filters, query: '尚未提交的搜索词', mode: 'hybrid' }, { ...selection, snapshot }, 'random');
  assert.match(hash, /^#\/random\?/);
  assert.doesNotMatch(hash, /[?&]q=/);
  const route = parseRoute(hash + '&q=unrelated');
  assert.equal(route.path, '/random');
  assert.equal(route.query, '');
  assert.deepEqual(route.filters, filters);
  assert.equal(route.line, selection.line_id);
  assert.equal(route.mission, selection.mission_id);
  assert.equal(route.script, selection.script);
  assert.equal(route.snapshot, snapshot);
});

test('a restored random link uses its resolved anchor as the single result', () => {
  const detail: MissionResponse = {
    snapshot, anchor_line_id: 'resolved-anchor', lines: [{
      id: 'resolved-anchor', mission_id: '123', script: 'Act/1.json', sequence: 1, position: 0,
      sentence_id: '412', speaker: '瓦尔特', text: '我们即将回到星穹列车了', kind: 'dialogue', branch: '', trigger: '',
    }], edges: [], total: 1, mission: {
      id: '123', name: '启程之前', chapter: '序章', world: '空间站', type: 'Main', version: '1.0',
      version_source: '', version_status: 'verified',
    },
  };
  const restored = restoredRandomResult(selection, detail);
  assert.equal(restored?.mission_name, '启程之前');
  assert.equal(restored?.display_text, detail.lines[0].text);
  assert.equal(restored?.line_id, selection.line_id);
  assert.equal(restored?.match_type, 'random');
  assert.deepEqual(restoredRandomResponse(snapshot, restored).results, [restored]);
  assert.equal(restoredRandomResult(selection, { ...detail, anchor_missing: true }), undefined);
  assert.equal(restoredRandomResponse(snapshot).total, 0);
});

test('recent draws stay unique and bounded with the current draw first', () => {
  const recent = Array.from({ length: 30 }, (_, index) => `line-${index}`);
  assert.deepEqual(rememberRandomLine(recent, 'new'), ['new', ...recent.slice(0, 29)]);
  const revisited = rememberRandomLine(recent, 'line-10');
  assert.equal(revisited[0], 'line-10');
  assert.equal(new Set(revisited).size, 30);
  assert.deepEqual(rememberRandomLine(recent, ''), recent);
});
