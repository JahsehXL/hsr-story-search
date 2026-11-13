import test from 'node:test';
import assert from 'node:assert/strict';
import { buildStoryModel, focusStory, resolveStory, shareStoryAnchor, windowStoryGroup } from '../src/story.ts';
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
