import test from 'node:test';
import assert from 'node:assert/strict';
import { buildStoryModel, focusStory, resolveFullMissionStory, resolveStory, shareStoryAnchor, storyBlocks, windowStoryGroup, withDefaultChoices } from '../src/story.ts';
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

test('reader blocks bound long passages and preserve every line and complete choices', () => {
  const lines = Array.from({ length: 1000 }, (_, index) => line(`long-${index}`, 0, 'start'));
  const options = [line('choice-a', 0, 'start', { kind: 'option' }), line('choice-b', 0, 'start', { kind: 'option' })];
  const blocks = storyBlocks([{ kind: 'dialogue', lines }, { kind: 'choice', choice: { id: 'choice-a', options } }]);
  assert.equal(blocks.length, 64);
  assert.ok(blocks.every(block => block.kind === 'choice' || block.lines.length <= 16));
  assert.deepEqual(blocks.flatMap(block => block.kind === 'dialogue' ? block.lines : block.choice.options), [...lines, ...options]);
  assert.equal(blocks.at(-1)?.kind, 'choice');
});

test('reader blocks keep a late hit addressable and do not alter resolved branches', () => {
  const model = buildStoryModel(fixture());
  const resolved = resolveFullMissionStory(model, 'follow-b');
  const blocks = resolved.groups.flatMap(group => storyBlocks(group.segments));
  const renderedLines = blocks.flatMap(block => block.kind === 'dialogue' ? block.lines : block.choice.options);
  assert.equal(renderedLines.some(item => item.id === 'follow-b'), true);
  assert.equal(renderedLines.some(item => item.id === 'follow-a'), false);
  assert.deepEqual(storyBlocks([]), []);
});

test('a hit option shows its context and selected follow-up without unrelated answers', () => {
  const model = buildStoryModel(fixture());
  const focused = focusStory(model, 'answer-a');
  const visible = resolveStory(model, focused);
  assert.deepEqual(visible.groups.map(group => group.sequence), [0, 1]);
  assert.equal(focused.selections['answer-a'], 'answer-a');
  assert.equal(visible.groups.flatMap(group => group.lines).some(item => item.id === 'unrelated'), false);
});

test('a hit follow-up infers a unique parent answer and never loses the hit', () => {
  const model = buildStoryModel(fixture());
  const focused = focusStory(model, 'follow-b');
  assert.equal(focused.selections['answer-a'], 'answer-b');
  assert.deepEqual(resolveStory(model, focused).groups.map(group => group.sequence), [0, 2]);
});

test('switching one answer hides the previous follow-up', () => {
  const model = buildStoryModel(fixture());
  const focused = focusStory(model, 'answer-a');
  focused.selections['answer-a'] = 'answer-b';
  assert.deepEqual(resolveStory(model, focused).groups.map(group => group.sequence), [0, 2]);
});

test('a plain passage leaves unanswered choices unselected', () => {
  const model = buildStoryModel(fixture());
  const focused = focusStory(model, 'intro');
  assert.deepEqual(focused.selections, {});
  assert.deepEqual(resolveStory(model, focused).groups.map(group => group.sequence), [0]);
});

test('multiple choice sets retain independent selections', () => {
  const lines = fixture();
  lines.splice(3, 0, line('interlude', 0, 'start'), line('answer-c', 0, 'start', { kind: 'option', trigger: 'c' }));
  lines.push(line('follow-c', 4, 'c'));
  const model = buildStoryModel(lines);
  const focused = focusStory(model, 'answer-c');
  assert.equal(focused.selections['answer-c'], 'answer-c');
  assert.equal(focused.selections['answer-a'], undefined);
  assert.deepEqual(resolveStory(model, focused).groups.map(group => group.sequence), [0, 4]);
});

test('shared destinations do not invent a selected answer', () => {
  const lines = fixture();
  lines[2].trigger = 'a';
  const model = buildStoryModel(lines);
  const focused = focusStory(model, 'follow-a');
  assert.deepEqual(focused.selections, {});
  assert.deepEqual(resolveStory(model, focused).groups.map(group => group.sequence), [1]);
});

test('adjacent OptionList tasks are separate cards even without intervening dialogue', () => {
  const model = buildStoryModel([
    line('first-task-a', 0, 'start', { kind: 'option', position: 10000, trigger: 'a' }),
    line('first-task-b', 0, 'start', { kind: 'option', position: 10001, trigger: 'b' }),
    line('second-task', 0, 'start', { kind: 'option', position: 20000, trigger: 'c' }),
  ]);
  assert.equal(model.groups[0].segments.length, 2);
  assert.deepEqual(focusStory(model, 'second-task').selections, { 'second-task': 'second-task' });
});

test('continuation cycles terminate and same branch names stay within their script', () => {
  const model = buildStoryModel([
    line('one', 0, 'a'), line('two', 1, 'b'), line('other-script', 0, 'b', { script: 'script-b' }),
  ], [
    { script: 'script-a', source: 'a', target: 'b', kind: 'continuation' },
    { script: 'script-a', source: 'b', target: 'a', kind: 'continuation' },
  ]);
  assert.deepEqual(resolveStory(model, focusStory(model, 'one')).groups.map(group => group.lines[0].id), ['one', 'two']);
});

test('missing targets remain explicit and a partial page still preserves its hit', () => {
  const model = buildStoryModel([line('answer', 0, '', { kind: 'option', trigger: 'not-loaded' })]);
  const visible = resolveStory(model, focusStory(model, 'answer'));
  assert.equal(visible.groups[0].lines[0].id, 'answer');
  assert.deepEqual(visible.missingTargets, ['not-loaded']);
});

test('a long passage opens around a late hit instead of its first lines', () => {
  const lines = Array.from({ length: 92 }, (_, index) => line(`line-${index}`, 0, '', { position: index }));
  const group = buildStoryModel(lines).groups[0];
  const window = windowStoryGroup(group, 'line-91');
  assert.deepEqual(window.lines.map(item => item.id), ['line-88', 'line-89', 'line-90', 'line-91']);
  assert.equal(window.canExpand, true);
  assert.equal(window.hiddenBefore, 88);
  const middle = windowStoryGroup(group, 'line-50');
  assert.deepEqual(middle.lines.map(item => item.id), Array.from({ length: 10 }, (_, index) => `line-${47 + index}`));
});

test('an option hit keeps its whole choice card and nearby dialogue', () => {
  const intro = Array.from({ length: 20 }, (_, index) => line(`intro-${index}`, 0, '', { position: index }));
  const choices = Array.from({ length: 8 }, (_, index) => line(`answer-${index}`, 0, '', { position: 10000 + index, kind: 'option', trigger: `follow-${index}` }));
  const after = Array.from({ length: 10 }, (_, index) => line(`after-${index}`, 0, '', { position: 20000 + index }));
  const model = buildStoryModel([...intro, ...choices, ...after, line('followup', 1, 'follow-7')]);
  const group = model.groups[0];
  const window = windowStoryGroup(group, 'answer-7');
  const card = window.segments.find(segment => segment.kind === 'choice');
  assert.equal(card?.kind === 'choice' ? card.choice.options.length : 0, 8);
  assert.equal(window.lines[0].id, 'intro-17');
  assert.equal(window.lines[window.lines.length - 1].id, 'after-5');
  assert.deepEqual(resolveStory(model, focusStory(model, 'answer-7')).groups.map(item => item.sequence), [0, 1]);
});

test('expanding restores the complete passage while another passage remains complete by default', () => {
  const group = buildStoryModel(Array.from({ length: 30 }, (_, index) => line(`line-${index}`, 0, '', { position: index }))).groups[0];
  const expanded = windowStoryGroup(group, 'line-29', true);
  assert.equal(expanded.lines.length, 30);
  assert.equal(expanded.expanded, true);
  assert.equal(windowStoryGroup(group, 'not-in-this-passage').lines.length, 30);
});

test('the share anchor follows the current answer after switching paths', () => {
  const model = buildStoryModel(fixture());
  const focused = focusStory(model, 'answer-a');
  const original = resolveStory(model, focused).groups.flatMap(group => group.lines);
  assert.equal(shareStoryAnchor(original, focused.selections, 'answer-a')?.id, 'answer-a');
  focused.selections['answer-a'] = 'answer-b';
  const switched = resolveStory(model, focused).groups.flatMap(group => group.lines);
  assert.equal(shareStoryAnchor(switched, focused.selections, 'answer-a')?.id, 'answer-b');
});

test('plain passage sharing preserves a visible hit and otherwise uses its first line', () => {
  const lines = [line('first', 0, ''), line('hit', 0, ''), line('last', 0, '')];
  assert.equal(shareStoryAnchor(lines, {}, 'hit')?.id, 'hit');
  assert.equal(shareStoryAnchor(lines, {}, 'old-hidden-hit')?.id, 'first');
  assert.equal(shareStoryAnchor([], {}, 'missing'), undefined);
});

test('a child hit also trims a long parent around its selected answer', () => {
  const introduction = Array.from({ length: 92 }, (_, index) => line(`intro-${index}`, 0, 'start', { position: index }));
  const answer = line('selected-answer', 0, 'start', { kind: 'option', trigger: 'child', position: 10000 });
  const model = buildStoryModel([...introduction, answer, line('child-hit', 1, 'child')]);
  const focus = focusStory(model, 'child-hit');
  const visible = resolveStory(model, focus);
  const parent = windowStoryGroup(visible.groups[0], 'child-hit', false, focus.selections);
  assert.deepEqual(parent.lines.map(item => item.id), ['intro-89', 'intro-90', 'intro-91', 'selected-answer']);
  assert.equal(parent.canExpand, true);
  assert.equal(visible.groups[1].lines[0].id, 'child-hit');
});

test('sharing a visible follow-up preserves its precise hit', () => {
  const model = buildStoryModel(fixture());
  const focus = focusStory(model, 'follow-a');
  const lines = resolveStory(model, focus).groups.flatMap(group => group.lines);
  assert.equal(shareStoryAnchor(lines, focus.selections, 'follow-a')?.id, 'follow-a');
});

test('withDefaultChoices auto-selects the first option while preserving explicit hits', () => {
  const model = buildStoryModel(fixture());
  const introFocus = withDefaultChoices(model, focusStory(model, 'intro'));
  assert.equal(introFocus.selections['answer-a'], 'answer-a');
  assert.deepEqual(resolveStory(model, introFocus).groups.map(group => group.sequence), [0, 1]);

  const hitB = withDefaultChoices(model, focusStory(model, 'follow-b'));
  assert.equal(hitB.selections['answer-a'], 'answer-b');
  assert.deepEqual(resolveStory(model, hitB).groups.map(group => group.sequence), [0, 2]);
});

test('resolveFullMissionStory renders all root scenes across scripts and selects ancestor choices leading to hit', () => {
  const lines = [
    line('s1-root', 0, '', { script: 'DS01' }),
    line('s2-root', 0, '', { script: 'Act02' }),
    line('s2-opt1', 0, '', { script: 'Act02', kind: 'option', trigger: 'b1' }),
    line('s2-opt2', 0, '', { script: 'Act02', kind: 'option', trigger: 'b2' }),
    line('s2-b1', 1, 'b1', { script: 'Act02' }),
    line('s2-b2', 2, 'b2', { script: 'Act02' }),
    line('s3-root', 0, '', { script: 'DS03' }),
  ];
  const model = buildStoryModel(lines);
  const full = resolveFullMissionStory(model, 's2-b2');
  assert.deepEqual(full.groups.map(g => g.lines[0].id), ['s1-root', 's2-root', 's2-b2', 's3-root']);
  assert.equal(full.selections['s2-opt1'], 's2-opt2');

  const switched = resolveFullMissionStory(model, 's2-b2', { 's2-opt1': 's2-opt1' });
  assert.deepEqual(switched.groups.map(g => g.lines[0].id), ['s1-root', 's2-root', 's2-b1', 's3-root']);
});

test('a setup node without lines keeps the scene and shared answers in script order', () => {
  const lines = [
    line('intro', 1, 'intro'),
    line('answer-a', 2, 'choice', { kind: 'option', trigger: 'shared' }),
    line('answer-b', 2, 'choice', { kind: 'option', trigger: 'shared' }),
    line('hit', 3, 'shared'),
    line('next-scene', 0, '', { script: 'script-b' }),
  ];
  const model = buildStoryModel(lines, [
    { script: 'script-a', source: 'sequence:0', target: 'intro', kind: 'continuation' },
    { script: 'script-a', source: 'intro', target: 'choice', kind: 'continuation' },
  ]);
  for (const answer of ['answer-a', 'answer-b']) {
    const resolved = resolveFullMissionStory(model, 'hit', { 'answer-a': answer });
    assert.deepEqual(resolved.groups.map(group => group.lines[0].id), ['intro', 'answer-a', 'hit', 'next-scene']);
  }
});

test('a dialogue loop entered through setup remains visible when switching answers', () => {
  const model = buildStoryModel([
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
  const opened = resolveFullMissionStory(model, 'option-a');
  const switched = resolveFullMissionStory(model, 'option-a', { 'option-a': 'option-b' });
  assert.deepEqual(opened.groups.map(group => group.lines[0].id), ['option-a', 'response-a', 'next-scene']);
  assert.deepEqual(switched.groups.map(group => group.lines[0].id), ['option-a', 'response-b', 'next-scene']);
});

test('only the chosen path renders in a script consisting of a closed question loop', () => {
  const model = buildStoryModel([
    line('option-a', 1, 'hub', { kind: 'option', trigger: 'first' }),
    line('option-b', 1, 'hub', { kind: 'option', trigger: 'second' }),
    line('response-a', 2, 'first'),
    line('response-b', 3, 'second'),
    line('outside', 0, '', { script: 'script-b' }),
  ], [
    { script: 'script-a', source: 'first', target: 'hub', kind: 'continuation' },
    { script: 'script-a', source: 'second', target: 'hub', kind: 'continuation' },
  ]);
  const switched = resolveFullMissionStory(model, '', { 'option-a': 'option-b' });
  assert.deepEqual(switched.groups.map(group => group.lines[0].id), ['option-a', 'response-b', 'outside']);
});

test('a separate question loop stays visible after an earlier scene in the same script', () => {
  const model = buildStoryModel([
    line('setup', 0, ''),
    line('option-a', 1, 'hub', { kind: 'option', trigger: 'first' }),
    line('option-b', 1, 'hub', { kind: 'option', trigger: 'second' }),
    line('response-a', 2, 'first'),
    line('response-b', 3, 'second'),
  ], [
    { script: 'script-a', source: 'first', target: 'hub', kind: 'continuation' },
    { script: 'script-a', source: 'second', target: 'hub', kind: 'continuation' },
  ]);
  assert.deepEqual(resolveFullMissionStory(model).groups.map(group => group.lines[0].id), ['setup', 'option-a', 'response-a']);
  const switched = resolveFullMissionStory(model, '', { 'option-a': 'option-b' });
  assert.deepEqual(switched.groups.map(group => group.lines[0].id), ['setup', 'option-a', 'response-b']);
});

test('a question reached through a silent randomizer keeps its options and responses', () => {
  const model = buildStoryModel([
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
  const opened = resolveFullMissionStory(model, 'question');
  assert.deepEqual(opened.groups.map(group => group.lines[0].id), ['intro', 'question']);
  const switched = resolveFullMissionStory(model, 'question', { agree: 'decline' });
  assert.deepEqual(switched.groups.map(group => group.lines[0].id), ['intro', 'exit']);
});

test('a setup sequence numbered later in the file starts at the question before its answers', () => {
  const model = buildStoryModel([
    line('correct', 1, 'success'),
    line('retry', 2, 'wrong'),
    line('option-a', 6, 'question', { kind: 'option', trigger: 'success' }),
    line('option-b', 6, 'question', { kind: 'option', trigger: 'wrong' }),
  ], [
    { script: 'script-a', source: 'sequence:5', target: 'question', kind: 'continuation' },
    { script: 'script-a', source: 'wrong', target: 'question', kind: 'continuation' },
  ]);
  assert.deepEqual(resolveFullMissionStory(model).groups.map(group => group.lines[0].id), ['option-a', 'correct']);
  assert.deepEqual(resolveFullMissionStory(model, '', { 'option-a': 'option-b' }).groups.map(group => group.lines[0].id), ['option-a', 'retry']);
});

test('mutually exclusive alt branches default to branch 1, auto-focus hits on branch 2, and support switching', () => {
  const model = buildStoryModel([
    line('cond-1', 3, 'seq3#alt:1#cond:1', { position: 10000 }),
    line('cond-2', 3, 'seq3#alt:1#cond:2', { position: 10100 }),
  ], [
    { script: 'script-a', source: 'sequence:3', target: 'seq3#alt:1#cond:1', kind: 'continuation' },
    { script: 'script-a', source: 'sequence:3', target: 'seq3#alt:1#cond:2', kind: 'continuation' },
  ]);
  const defaultView = resolveFullMissionStory(model);
  assert.deepEqual(defaultView.groups.map(group => group.lines[0].id), ['cond-1']);
  assert.equal(model.altGroups.size, 1);

  const hitSecond = resolveFullMissionStory(model, 'cond-2');
  assert.deepEqual(hitSecond.groups.map(group => group.lines[0].id), ['cond-2']);

  const slot = defaultView.groups[0].altSlot!;
  const secondKey = model.altGroups.get(slot)![1].key;
  const switched = resolveFullMissionStory(model, '', {}, { [slot]: secondKey });
  assert.deepEqual(switched.groups.map(group => group.lines[0].id), ['cond-2']);
});

test('a sequence interrupted by an alt block preserves before, chosen alt, after, and continuation order', () => {
  const model = buildStoryModel([
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
  const def = resolveFullMissionStory(model);
  assert.deepEqual(def.groups.map(group => group.lines[0].id), ['start', 'before', 'alt-1', 'after', 'next-scene']);
  const hitAlt2 = resolveFullMissionStory(model, 'alt-2');
  assert.deepEqual(hitAlt2.groups.map(group => group.lines[0].id), ['start', 'before', 'alt-2', 'after', 'next-scene']);
});

test('hitting a downstream line triggered from an option inside an alt branch auto-selects both the alt branch and option', () => {
  const model = buildStoryModel([
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
  const hitDeep = resolveFullMissionStory(model, 'reply-b');
  assert.deepEqual(hitDeep.groups.map(group => group.lines[0].id), ['alt-opt-1', 'reply-b']);
  assert.equal(hitDeep.selections['alt-opt-1'], 'alt-opt-2');
});

test('adjacent option tasks inside a container with 100-stride positions form separate choice cards', () => {
  const model = buildStoryModel([
    line('c1-a', 0, 'start', { kind: 'option', position: 10000, trigger: 'a' }),
    line('c1-b', 0, 'start', { kind: 'option', position: 10001, trigger: 'b' }),
    line('c2-a', 0, 'start', { kind: 'option', position: 10100, trigger: 'c' }),
  ]);
  assert.equal(model.groups[0].segments.length, 2);
});

