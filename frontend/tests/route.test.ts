import test from 'node:test';
import assert from 'node:assert/strict';
import { buildMissionHash, buildSearchHash, hasActiveFilters, parseRoute } from '../src/route.ts';

test('parseRoute and buildSearchHash round-trip query, filters, and mode', () => {
  const empty = { query: '', filters: { world: '', version: '', type: '', character: '' }, mode: 'text' as const };
  assert.equal(hasActiveFilters(empty.filters), false);
  assert.equal(buildSearchHash(empty), '#/');

  const filterOnly = {
    query: '',
    filters: { world: '', version: '1.0', type: '', character: '' },
    mode: 'text' as const,
  };
  const filterHash = buildSearchHash(filterOnly);
  assert.equal(filterHash, '#/search?version=1.0');
  const parsedFilter = parseRoute(filterHash);
  assert.deepEqual(parsedFilter.filters, filterOnly.filters);
  assert.equal(parsedFilter.query, '');
  assert.equal(parsedFilter.mode, 'text');

  const full = {
    query: '失控',
    filters: { world: '雅利洛-Ⅵ', version: '1.0', type: '支线任务', character: '尼古莱' },
    mode: 'fuzzy' as const,
  };
  const fullHash = buildSearchHash(full);
  const parsedFull = parseRoute(fullHash);
  assert.equal(parsedFull.query, '失控');
  assert.deepEqual(parsedFull.filters, full.filters);
  assert.equal(parsedFull.mode, 'fuzzy');

  const missionHash = buildMissionHash(full, {
    mission_id: '2011101',
    line_id: 'abc123',
    script: 'Config/Level/Mission/2011101/Act/Act201110101.json',
    snapshot: '505ddbc5151e4fdb8a634822',
  });
  const parsedMission = parseRoute(missionHash);
  assert.equal(parsedMission.mission, '2011101');
  assert.equal(parsedMission.line, 'abc123');
  assert.equal(parsedMission.snapshot, '505ddbc5151e4fdb8a634822');
  assert.deepEqual(parsedMission.filters, full.filters);
  assert.equal(parsedMission.mode, 'fuzzy');
  assert.equal(buildSearchHash({ query: parsedMission.query, filters: parsedMission.filters, mode: parsedMission.mode }), fullHash);

  assert.equal(parseRoute('#/search?mode=invalid&q=hi').mode, 'text');
});
