import test from 'node:test';
import assert from 'node:assert/strict';
import { buildStoryModel, focusStory, resolveFullMissionStory, resolveStory, sceneStatusLabel, shareStoryAnchor, shouldShowSceneDivider, storyBlocks, windowStoryGroup, withDefaultChoices } from '../src/story.ts';
import type { DialogueLine } from '../src/types.ts';

function line(id: string, sequence: number, branch: string, values: Partial<DialogueLine> = {}): DialogueLine {
  return { id, mission_id: '1', script: 'script-a', sequence, position: 0, sentence_id: id, speaker: '角色', text: id, kind: 'dialogue', branch, trigger: '', ...values };
}

function fixture() {
  return [
    line('intro', 0, 'start'),
    line('answer-a', 0, 'start', { kind: 'option', trigger: 'a' }),
    line('answer-b', 0, 'start', { kind: 'option', trigger: 'b' }),
    line('follow-a', 1, 'a'),
    line('follow-b', 2, 'b'),
    line('unrelated', 3, 'else'),
  ];
}

test('storyBlocks splits long passages and keeps resolved hits addressable', () => {
  const lines = Array.from({ length: 1000 }, (_, index) => line(`long-${index}`, 0, 'start'));
  const options = [line('choice-a', 0, 'start', { kind: 'option' }), line('choice-b', 0, 'start', { kind: 'option' })];
  const blocks = storyBlocks([{ kind: 'dialogue', lines }, { kind: 'choice', choice: { id: 'choice-a', options } }]);
  assert.equal(blocks.length, 64);
  assert.ok(blocks.every(block => block.kind === 'choice' || block.lines.length <= 16));
  assert.deepEqual(blocks.flatMap(block => block.kind === 'dialogue' ? block.lines : block.choice.options), [...lines, ...options]);
  assert.equal(blocks.at(-1)?.kind, 'choice');

  const resolved = resolveFullMissionStory(buildStoryModel(fixture()), 'follow-b');
  const rendered = resolved.groups.flatMap(group => storyBlocks(group.segments))
    .flatMap(block => block.kind === 'dialogue' ? block.lines : block.choice.options);
  assert.equal(rendered.some(item => item.id === 'follow-b'), true);
  assert.equal(rendered.some(item => item.id === 'follow-a'), false);
  assert.deepEqual(storyBlocks([]), []);
});

test('windowStoryGroup centers on late hits, preserves whole choice cards, expands, and trims parents', () => {
  const group = buildStoryModel(Array.from({ length: 92 }, (_, index) => line(`line-${index}`, 0, '', { position: index }))).groups[0];
  const tail = windowStoryGroup(group, 'line-91');
  assert.deepEqual(tail.lines.map(item => item.id), ['line-88', 'line-89', 'line-90', 'line-91']);
  assert.equal(tail.canExpand, true);
  assert.equal(tail.hiddenBefore, 88);
  assert.deepEqual(windowStoryGroup(group, 'line-50').lines.map(item => item.id), Array.from({ length: 10 }, (_, index) => `line-${47 + index}`));

  const intro = Array.from({ length: 20 }, (_, index) => line(`intro-${index}`, 0, '', { position: index }));
  const choices = Array.from({ length: 8 }, (_, index) => line(`answer-${index}`, 0, '', { position: 10000 + index, kind: 'option', trigger: `follow-${index}` }));
  const after = Array.from({ length: 10 }, (_, index) => line(`after-${index}`, 0, '', { position: 20000 + index }));
  const optModel = buildStoryModel([...intro, ...choices, ...after, line('followup', 1, 'follow-7')]);
  const optWin = windowStoryGroup(optModel.groups[0], 'answer-7');
  const card = optWin.segments.find(segment => segment.kind === 'choice');
  assert.equal(card?.kind === 'choice' ? card.choice.options.length : 0, 8);
  assert.equal(optWin.lines[0].id, 'intro-17');
  assert.equal(optWin.lines[optWin.lines.length - 1].id, 'after-5');
  assert.deepEqual(resolveStory(optModel, focusStory(optModel, 'answer-7')).groups.map(item => item.sequence), [0, 1]);

  const shortGroup = buildStoryModel(Array.from({ length: 30 }, (_, index) => line(`line-${index}`, 0, '', { position: index }))).groups[0];
  const expanded = windowStoryGroup(shortGroup, 'line-29', true);
  assert.equal(expanded.lines.length, 30);
  assert.equal(expanded.expanded, true);
  assert.equal(windowStoryGroup(shortGroup, 'not-in-this-passage').lines.length, 30);

  const parentIntro = Array.from({ length: 92 }, (_, index) => line(`intro-${index}`, 0, 'start', { position: index }));
  const answer = line('selected-answer', 0, 'start', { kind: 'option', trigger: 'child', position: 10000 });
  const parentModel = buildStoryModel([...parentIntro, answer, line('child-hit', 1, 'child')]);
  const focus = focusStory(parentModel, 'child-hit');
  const visible = resolveStory(parentModel, focus);
  const parentWin = windowStoryGroup(visible.groups[0], 'child-hit', false, focus.selections);
  assert.deepEqual(parentWin.lines.map(item => item.id), ['intro-89', 'intro-90', 'intro-91', 'selected-answer']);
  assert.equal(parentWin.canExpand, true);
  assert.equal(visible.groups[1].lines[0].id, 'child-hit');
});

test('focusStory and resolveStory manage options, inference, multi-choice, shared targets, and separate option cards', () => {
  const model = buildStoryModel(fixture());
  const focused = focusStory(model, 'answer-a');
  const visible = resolveStory(model, focused);
  assert.deepEqual(visible.groups.map(group => group.sequence), [0, 1]);
  assert.equal(focused.selections['answer-a'], 'answer-a');
  assert.equal(visible.groups.flatMap(group => group.lines).some(item => item.id === 'unrelated'), false);

  focused.selections['answer-a'] = 'answer-b';
  assert.deepEqual(resolveStory(model, focused).groups.map(group => group.sequence), [0, 2]);

  const followFocus = focusStory(model, 'follow-b');
  assert.equal(followFocus.selections['answer-a'], 'answer-b');
  assert.deepEqual(resolveStory(model, followFocus).groups.map(group => group.sequence), [0, 2]);

  const plainFocus = focusStory(model, 'intro');
  assert.deepEqual(plainFocus.selections, {});
  assert.deepEqual(resolveStory(model, plainFocus).groups.map(group => group.sequence), [0]);

  const multi = fixture();
  multi.splice(3, 0, line('interlude', 0, 'start'), line('answer-c', 0, 'start', { kind: 'option', trigger: 'c' }));
  multi.push(line('follow-c', 4, 'c'));
  const multiModel = buildStoryModel(multi);
  const multiFocus = focusStory(multiModel, 'answer-c');
  assert.equal(multiFocus.selections['answer-c'], 'answer-c');
  assert.equal(multiFocus.selections['answer-a'], undefined);
  assert.deepEqual(resolveStory(multiModel, multiFocus).groups.map(group => group.sequence), [0, 4]);

  const shared = fixture();
  shared[2].trigger = 'a';
  const sharedModel = buildStoryModel(shared);
  const sharedFocus = focusStory(sharedModel, 'follow-a');
  assert.deepEqual(sharedFocus.selections, {});
  assert.deepEqual(resolveStory(sharedModel, sharedFocus).groups.map(group => group.sequence), [1]);

  const adjacent = buildStoryModel([
    line('first-task-a', 0, 'start', { kind: 'option', position: 10000, trigger: 'a' }),
    line('first-task-b', 0, 'start', { kind: 'option', position: 10001, trigger: 'b' }),
    line('second-task', 0, 'start', { kind: 'option', position: 20000, trigger: 'c' }),
  ]);
  assert.equal(adjacent.groups[0].segments.length, 2);
  assert.deepEqual(focusStory(adjacent, 'second-task').selections, { 'second-task': 'second-task' });
});

test('continuation cycles terminate safely and missing targets remain explicit', () => {
  const cycled = buildStoryModel([
    line('one', 0, 'a'), line('two', 1, 'b'), line('other-script', 0, 'b', { script: 'script-b' }),
  ], [
    { script: 'script-a', source: 'a', target: 'b', kind: 'continuation' },
    { script: 'script-a', source: 'b', target: 'a', kind: 'continuation' },
  ]);
  assert.deepEqual(resolveStory(cycled, focusStory(cycled, 'one')).groups.map(group => group.lines[0].id), ['one', 'two']);

  const partial = buildStoryModel([line('answer', 0, '', { kind: 'option', trigger: 'not-loaded' })]);
  const visible = resolveStory(partial, focusStory(partial, 'answer'));
  assert.equal(visible.groups[0].lines[0].id, 'answer');
  assert.deepEqual(visible.missingTargets, ['not-loaded']);
});

test('shareStoryAnchor and withDefaultChoices track active branches and hits', () => {
  const model = buildStoryModel(fixture());
  const focused = focusStory(model, 'answer-a');
  const original = resolveStory(model, focused).groups.flatMap(group => group.lines);
  assert.equal(shareStoryAnchor(original, focused.selections, 'answer-a')?.id, 'answer-a');
  focused.selections['answer-a'] = 'answer-b';
  const switched = resolveStory(model, focused).groups.flatMap(group => group.lines);
  assert.equal(shareStoryAnchor(switched, focused.selections, 'answer-a')?.id, 'answer-b');

  const followLines = resolveStory(model, focusStory(model, 'follow-a')).groups.flatMap(group => group.lines);
  assert.equal(shareStoryAnchor(followLines, focusStory(model, 'follow-a').selections, 'follow-a')?.id, 'follow-a');

  const plain = [line('first', 0, ''), line('hit', 0, ''), line('last', 0, '')];
  assert.equal(shareStoryAnchor(plain, {}, 'hit')?.id, 'hit');
  assert.equal(shareStoryAnchor(plain, {}, 'old-hidden-hit')?.id, 'first');
  assert.equal(shareStoryAnchor([], {}, 'missing'), undefined);

  const introDefault = withDefaultChoices(model, focusStory(model, 'intro'));
  assert.equal(introDefault.selections['answer-a'], 'answer-a');
  assert.deepEqual(resolveStory(model, introDefault).groups.map(group => group.sequence), [0, 1]);
  const hitDefault = withDefaultChoices(model, focusStory(model, 'follow-b'));
  assert.equal(hitDefault.selections['answer-a'], 'answer-b');
  assert.deepEqual(resolveStory(model, hitDefault).groups.map(group => group.sequence), [0, 2]);
});

test('resolveFullMissionStory handles cross-script roots, setup nodes, loops, randomizers, and late questions', () => {
  const multiScript = buildStoryModel([
    line('s1-root', 0, '', { script: 'DS01' }),
    line('s2-root', 0, '', { script: 'Act02' }),
    line('s2-opt1', 0, '', { script: 'Act02', kind: 'option', trigger: 'b1' }),
    line('s2-opt2', 0, '', { script: 'Act02', kind: 'option', trigger: 'b2' }),
    line('s2-b1', 1, 'b1', { script: 'Act02' }),
    line('s2-b2', 2, 'b2', { script: 'Act02' }),
    line('s3-root', 0, '', { script: 'DS03' }),
  ]);
  const full = resolveFullMissionStory(multiScript, 's2-b2');
  assert.deepEqual(full.groups.map(g => g.lines[0].id), ['s1-root', 's2-root', 's2-b2', 's3-root']);
  assert.equal(full.selections['s2-opt1'], 's2-opt2');
  assert.deepEqual(resolveFullMissionStory(multiScript, 's2-b2', { 's2-opt1': 's2-opt1' }).groups.map(g => g.lines[0].id), ['s1-root', 's2-root', 's2-b1', 's3-root']);

  const setupNoLines = buildStoryModel([
    line('intro', 1, 'intro'),
    line('answer-a', 2, 'choice', { kind: 'option', trigger: 'shared' }),
    line('answer-b', 2, 'choice', { kind: 'option', trigger: 'shared' }),
    line('hit', 3, 'shared'),
    line('next-scene', 0, '', { script: 'script-b' }),
  ], [
    { script: 'script-a', source: 'sequence:0', target: 'intro', kind: 'continuation' },
    { script: 'script-a', source: 'intro', target: 'choice', kind: 'continuation' },
  ]);
  for (const answer of ['answer-a', 'answer-b']) {
    assert.deepEqual(resolveFullMissionStory(setupNoLines, 'hit', { 'answer-a': answer }).groups.map(g => g.lines[0].id), ['intro', 'answer-a', 'hit', 'next-scene']);
  }

  const setupLoop = buildStoryModel([
    line('option-a', 1, 'hub', { kind: 'option', trigger: 'first' }),
    line('option-b', 1, 'hub', { kind: 'option', trigger: 'second' }),
    line('response-a', 2, 'first'),
    line('response-b', 3, 'second'),
    line('next-scene', 0, '', { script: 'script-b' }),
  ], [
    { script: 'script-a', source: 'sequence:0', target: 'setup', kind: 'continuation' },
    { script: 'script-a', source: 'setup', target: 'hub', kind: 'continuation' },
    { script: 'script-a', source: 'first', target: 'hub', kind: 'continuation' },
    { script: 'script-a', source: 'second', target: 'hub', kind: 'continuation' },
  ]);
  assert.deepEqual(resolveFullMissionStory(setupLoop, 'option-a').groups.map(g => g.lines[0].id), ['option-a', 'response-a', 'next-scene']);
  assert.deepEqual(resolveFullMissionStory(setupLoop, 'option-a', { 'option-a': 'option-b' }).groups.map(g => g.lines[0].id), ['option-a', 'response-b', 'next-scene']);

  const closedLoop = buildStoryModel([
    line('setup', 0, ''),
    line('option-a', 1, 'hub', { kind: 'option', trigger: 'first' }),
    line('option-b', 1, 'hub', { kind: 'option', trigger: 'second' }),
    line('response-a', 2, 'first'),
    line('response-b', 3, 'second'),
  ], [
    { script: 'script-a', source: 'first', target: 'hub', kind: 'continuation' },
    { script: 'script-a', source: 'second', target: 'hub', kind: 'continuation' },
  ]);
  assert.deepEqual(resolveFullMissionStory(closedLoop).groups.map(g => g.lines[0].id), ['setup', 'option-a', 'response-a']);
  assert.deepEqual(resolveFullMissionStory(closedLoop, '', { 'option-a': 'option-b' }).groups.map(g => g.lines[0].id), ['setup', 'option-a', 'response-b']);

  const randomizer = buildStoryModel([
    line('intro', 0, ''),
    line('agree', 0, '', { kind: 'option', trigger: 'relay-a' }),
    line('decline', 0, '', { kind: 'option', trigger: 'declined' }),
    line('question', 2, 'question'),
    line('exit', 3, 'declined'),
  ], [
    { script: 'script-a', source: 'relay-a', target: 'relay-b', kind: 'continuation' },
    { script: 'script-a', source: 'relay-b', target: 'relay-a', kind: 'continuation' },
    { script: 'script-a', source: 'relay-b', target: 'question', kind: 'continuation' },
  ]);
  assert.deepEqual(resolveFullMissionStory(randomizer, 'question').groups.map(g => g.lines[0].id), ['intro', 'question']);
  assert.deepEqual(resolveFullMissionStory(randomizer, 'question', { agree: 'decline' }).groups.map(g => g.lines[0].id), ['intro', 'exit']);

  const lateSetup = buildStoryModel([
    line('correct', 1, 'success'),
    line('retry', 2, 'wrong'),
    line('option-a', 6, 'question', { kind: 'option', trigger: 'success' }),
    line('option-b', 6, 'question', { kind: 'option', trigger: 'wrong' }),
  ], [
    { script: 'script-a', source: 'sequence:5', target: 'question', kind: 'continuation' },
    { script: 'script-a', source: 'wrong', target: 'question', kind: 'continuation' },
  ]);
  assert.deepEqual(resolveFullMissionStory(lateSetup).groups.map(g => g.lines[0].id), ['option-a', 'correct']);
  assert.deepEqual(resolveFullMissionStory(lateSetup, '', { 'option-a': 'option-b' }).groups.map(g => g.lines[0].id), ['option-a', 'retry']);
});

test('mutually exclusive alt branches, inline alt blocks, nested options, and 100-stride cards', () => {
  const simpleAlt = buildStoryModel([
    line('cond-1', 3, 'seq3#alt:1#cond:1', { position: 10000 }),
    line('cond-2', 3, 'seq3#alt:1#cond:2', { position: 10100 }),
  ], [
    { script: 'script-a', source: 'sequence:3', target: 'seq3#alt:1#cond:1', kind: 'continuation' },
    { script: 'script-a', source: 'sequence:3', target: 'seq3#alt:1#cond:2', kind: 'continuation' },
  ]);
  const defAlt = resolveFullMissionStory(simpleAlt);
  assert.deepEqual(defAlt.groups.map(group => group.lines[0].id), ['cond-1']);
  assert.equal(simpleAlt.altGroups.size, 1);
  assert.deepEqual(resolveFullMissionStory(simpleAlt, 'cond-2').groups.map(group => group.lines[0].id), ['cond-2']);
  const slot = defAlt.groups[0].altSlot!;
  assert.deepEqual(resolveFullMissionStory(simpleAlt, '', {}, { [slot]: simpleAlt.altGroups.get(slot)![1].key }).groups.map(g => g.lines[0].id), ['cond-2']);

  const interrupted = buildStoryModel([
    line('start', 0, '', { position: 0 }),
    line('before', 1, 'hub', { position: 0 }),
    line('alt-1', 1, 'hub#alt:1#cond:1', { position: 10000 }),
    line('alt-2', 1, 'hub#alt:1#cond:2', { position: 10100 }),
    line('after', 1, 'hub', { position: 20000 }),
    line('next-scene', 2, 'ending', { position: 0 }),
  ], [
    { script: 'script-a', source: 'sequence:0', target: 'hub', kind: 'continuation' },
    { script: 'script-a', source: 'hub', target: 'hub#alt:1#cond:1', kind: 'continuation' },
    { script: 'script-a', source: 'hub', target: 'hub#alt:1#cond:2', kind: 'continuation' },
    { script: 'script-a', source: 'hub', target: 'ending', kind: 'continuation' },
  ]);
  assert.deepEqual(resolveFullMissionStory(interrupted).groups.map(g => g.lines[0].id), ['start', 'before', 'alt-1', 'after', 'next-scene']);
  assert.deepEqual(resolveFullMissionStory(interrupted, 'alt-2').groups.map(g => g.lines[0].id), ['start', 'before', 'alt-2', 'after', 'next-scene']);

  const nestedOpt = buildStoryModel([
    line('cond-1', 0, 'sequence:0#alt:0#cond:1', { position: 0 }),
    line('alt-opt-1', 0, 'sequence:0#alt:0#cond:2', { position: 100, kind: 'option', trigger: 'reply-a' }),
    line('alt-opt-2', 0, 'sequence:0#alt:0#cond:2', { position: 101, kind: 'option', trigger: 'reply-b' }),
    line('reply-a', 1, 'reply-a', { position: 0 }),
    line('reply-b', 2, 'reply-b', { position: 0 }),
  ], [
    { script: 'script-a', source: 'sequence:0', target: 'sequence:0#alt:0#cond:1', kind: 'continuation' },
    { script: 'script-a', source: 'sequence:0', target: 'sequence:0#alt:0#cond:2', kind: 'continuation' },
    { script: 'script-a', source: 'reply-b', target: 'deep-b', kind: 'continuation' },
  ]);
  const hitDeep = resolveFullMissionStory(nestedOpt, 'reply-b');
  assert.deepEqual(hitDeep.groups.map(group => group.lines[0].id), ['alt-opt-1', 'reply-b']);
  assert.equal(hitDeep.selections['alt-opt-1'], 'alt-opt-2');

  const stride = buildStoryModel([
    line('c1-a', 0, 'start', { kind: 'option', position: 10000, trigger: 'a' }),
    line('c1-b', 0, 'start', { kind: 'option', position: 10001, trigger: 'b' }),
    line('c2-a', 0, 'start', { kind: 'option', position: 10100, trigger: 'c' }),
  ]);
  assert.equal(stride.groups[0].segments.length, 2);
});

test('scene labels and dividers hide boundaries between confirmed ordered scripts', () => {
  const ordered = new Set(['Story/Discussion/Mission/1054706/DS105470630.json', 'Recovered/Mission/1054706/1570628.json']);
  assert.equal(sceneStatusLabel('Recovered/Mission/1054706/1570628.json', undefined, ordered), '');
  assert.equal(sceneStatusLabel('Recovered/Mission/1054706/1570635.json', undefined, ordered), '补录对白 · 场景位置待核实');
  assert.equal(sceneStatusLabel('Story/BattlePerformance/123/B.json', undefined, ordered), '战斗对白');
  assert.equal(
    shouldShowSceneDivider('Story/Discussion/Mission/1054706/DS105470630.json', 'Recovered/Mission/1054706/1570628.json', ordered),
    false,
  );
  assert.equal(
    shouldShowSceneDivider('Recovered/Mission/1054706/1570628.json', 'Recovered/Mission/1054706/1570635.json', ordered),
    true,
  );
  assert.equal(shouldShowSceneDivider('Recovered/Mission/1054706/1570635.json', undefined, ordered), false);
});
