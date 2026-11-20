import { readFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import vm from 'node:vm';
import { isDeepStrictEqual } from 'node:util';
import { createArchive } from './archive.mjs';

const dir = process.argv[2];
const payload = JSON.parse(readFileSync(0, 'utf8'));
let manifest;
const shards = new Map();
const self = {
  hsrRegisterCatalog: data => { manifest = data; },
  hsrRegisterIndexPart: (key, offset, values) => {
    const records = manifest[key] || (manifest[key] = []);
    for (let i = 0; i < values.length; i++) records[offset + i] = values[i];
  },
  hsrRegisterShard: (index, rows) => { shards.set(index, rows); },
};
const context = vm.createContext({ self });
const evaluate = async name => vm.runInContext(await readFile(resolve(dir, name), 'utf8'), context);
await evaluate('catalog.js');
for (const filename of manifest.indexFiles) await evaluate(filename);
const archive = createArchive(manifest, async index => {
  await evaluate(manifest.shards[index]);
  return shards.get(index);
});
let failures = 0;
for (const { query, filters, rows, truncated } of payload.queries) {
  const actual = await archive.search(query, filters, 0, 500);
  const ids = JSON.parse(JSON.stringify(actual.results.map(item => [item.chunk_id, item.line_id, item.text,
    item.match_type, item.matched_field, item.score, item.mission_id, item.mission_name, item.version, item.dialogue_origin || null])));
  const first = ids.findIndex((row, index) => !isDeepStrictEqual(row, rows[index]));
  if (first !== -1 || ids.length !== rows.length || actual.truncated !== truncated) {
    failures++;
    const sameIds = ids.filter(row => rows.some(expected => expected[0] === row[0])).length;
    console.log(JSON.stringify({ query, filters, time: actual.elapsed_ms, actual: ids.length,
      expected: rows.length, sameIds, first, got: ids[first], wanted: rows[first] }));
  } else console.log('PASS', JSON.stringify(query), JSON.stringify(filters), ids.length, actual.elapsed_ms + 'ms');
}
for (const test of payload.missions) {
  const detail = await archive.missionDetails(test.id, test.anchor, test.offset, test.limit);
  // Resource arrays were compiled in a separate realm. Compare JSON values.
  const select = (input, keys) => JSON.parse(JSON.stringify(Object.fromEntries(keys.map(key => [key, input[key]]))));
  const keys = Object.keys(test.expected);
  const actual = select(detail, keys);
  for (const key of ['edges', 'recalls']) {
    const byJson = (a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b));
    actual[key].sort(byJson);
    test.expected[key].sort(byJson);
  }
  for (const key of keys) if (!isDeepStrictEqual(actual[key], test.expected[key])) {
    failures++;
    console.log('MISSION FAIL', test.id, test.anchor, key, actual[key]?.length, test.expected[key]?.length);
  }
}
for (const test of payload.bookmarks) {
  const detail = await archive.bookmarkDetails(test.old_mission, test.line, test.script);
  if (detail.mission.id !== test.new_mission || detail.anchor_missing || !detail.anchor_line_id) {
    failures++;
    console.log('BOOKMARK FAIL', test.old_mission, test.new_mission, test.line, detail.mission.id, detail.anchor_line_id);
  }
}
console.log(failures + ' failures across ' + payload.queries.length + ' searches, ' + payload.missions.length
  + ' context pages and ' + payload.bookmarks.length + ' moved-script bookmarks');
process.exitCode = failures ? 1 : 0;
