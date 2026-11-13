import type { DialogueLine, MissionResponse } from './types.ts';

export interface ChoiceSet {
  id: string;
  options: DialogueLine[];
}

export type StorySegment = { kind: 'dialogue'; lines: DialogueLine[] } | { kind: 'choice'; choice: ChoiceSet };

export interface StoryGroup {
  key: string;
  script: string;
  branch: string;
  node: string;
  sequence: number;
  lines: DialogueLine[];
  segments: StorySegment[];
}

export interface StoryModel {
  groups: StoryGroup[];
  byKey: Map<string, StoryGroup>;
  byLine: Map<string, StoryGroup>;
  byBranch: Map<string, StoryGroup[]>;
  parents: Map<string, { group: StoryGroup; choice: ChoiceSet; option: DialogueLine }[]>;
  continuations: Map<string, string[]>;
}

export interface StoryFocus {
  rootKey: string;
  selections: Record<string, string>;
}

export function shareStoryAnchor(lines: DialogueLine[], selections: Record<string, string>, hitId: string) {
  const selectedAnswers = new Set(Object.values(selections));
  const visibleHit = lines.find(line => line.id === hitId && (!line.kind.includes('option') || selectedAnswers.has(line.id)));
  if (visibleHit) return visibleHit;
  for (let index = lines.length - 1; index >= 0; index--) {
    const line = lines[index];
    if (line.kind.includes('option') && selectedAnswers.has(line.id)) return line;
  }
  return lines.find(line => line.id === hitId) || lines[0];
}

export function windowStoryGroup(group: StoryGroup, hitId: string, expanded = false, selections: Record<string, string> = {}) {
  const selectedAnswers = new Set(Object.values(selections));
  const anchor = group.lines.find(line => line.id === hitId) || group.lines.find(line => selectedAnswers.has(line.id));
  const hitIndex = anchor ? group.lines.indexOf(anchor) : -1;
  if (hitIndex < 0 || group.lines.length <= 10) {
    return { segments: group.segments, lines: group.lines, canExpand: false, expanded: false, hiddenBefore: 0, hiddenAfter: 0 };
  }
  let first = hitIndex;
  let last = hitIndex;
  const hitChoice = group.segments.find(segment => segment.kind === 'choice' && segment.choice.options.some(option => option.id === anchor?.id));
  if (hitChoice?.kind === 'choice') {
    first = group.lines.findIndex(line => line.id === hitChoice.choice.options[0].id);
    last = group.lines.findIndex(line => line.id === hitChoice.choice.options[hitChoice.choice.options.length - 1].id);
  }
  const start = Math.max(0, first - 3);
  const end = Math.min(group.lines.length, last + 7);
  const included = new Set(group.lines.slice(start, end).map(line => line.id));
  const segments: StorySegment[] = [];
  for (const segment of group.segments) {
    if (segment.kind === 'choice') {
      if (segment.choice.options.some(option => included.has(option.id))) segments.push(segment);
    } else {
      const lines = segment.lines.filter(line => included.has(line.id));
      if (lines.length) segments.push({ kind: 'dialogue', lines });
    }
  }
  const lines = segments.flatMap(segment => segment.kind === 'choice' ? segment.choice.options : segment.lines);
  const canExpand = lines.length < group.lines.length;
  if (expanded) return { segments: group.segments, lines: group.lines, canExpand, expanded: canExpand, hiddenBefore: 0, hiddenAfter: 0 };
  return { segments, lines, canExpand, expanded: false, hiddenBefore: group.lines.findIndex(line => line.id === lines[0].id), hiddenAfter: group.lines.length - group.lines.findIndex(line => line.id === lines[lines.length - 1].id) - 1 };
}

function branchKey(script: string, branch: string) {
  return JSON.stringify([script, branch]);
}

export function buildStoryModel(lines: DialogueLine[], edges: MissionResponse['edges'] = []): StoryModel {
  const groups: StoryGroup[] = [];
  for (const line of lines) {
    const key = JSON.stringify([line.script, line.sequence, line.branch]);
    let group = groups[groups.length - 1];
    if (!group || group.key !== key) {
      group = { key, script: line.script, branch: line.branch, node: line.branch || `sequence:${line.sequence}`, sequence: line.sequence, lines: [], segments: [] };
      groups.push(group);
    }
    group.lines.push(line);
    const previous = group.segments[group.segments.length - 1];
    if (line.kind.includes('option')) {
      if (previous?.kind === 'choice' && Math.floor(previous.choice.options[0].position / 10000) === Math.floor(line.position / 10000)) previous.choice.options.push(line);
      else group.segments.push({ kind: 'choice', choice: { id: line.id, options: [line] } });
    } else if (previous?.kind === 'dialogue') previous.lines.push(line);
    else group.segments.push({ kind: 'dialogue', lines: [line] });
  }
  const model: StoryModel = { groups, byKey: new Map(), byLine: new Map(), byBranch: new Map(), parents: new Map(), continuations: new Map() };
  for (const group of groups) {
    model.byKey.set(group.key, group);
    const key = branchKey(group.script, group.node);
    model.byBranch.set(key, [...(model.byBranch.get(key) || []), group]);
    for (const line of group.lines) model.byLine.set(line.id, group);
    for (const segment of group.segments) {
      if (segment.kind !== 'choice') continue;
      for (const option of segment.choice.options) {
        if (!option.trigger) continue;
        const target = branchKey(group.script, option.trigger);
        model.parents.set(target, [...(model.parents.get(target) || []), { group, choice: segment.choice, option }]);
      }
    }
  }
  for (const edge of edges) {
    if (edge.kind !== 'continuation') continue;
    const key = branchKey(edge.script, edge.source);
    const targets = model.continuations.get(key) || [];
    if (!targets.includes(edge.target)) model.continuations.set(key, [...targets, edge.target]);
  }
  return model;
}

export function focusStory(model: StoryModel, lineId: string, script = ''): StoryFocus {
  const group = model.byLine.get(lineId) || model.groups.find(item => item.script === script) || model.groups[0];
  if (!group) return { rootKey: '', selections: {} };
  for (const segment of group.segments) {
    if (segment.kind === 'choice' && segment.choice.options.some(line => line.id === lineId)) {
      return { rootKey: group.key, selections: { [segment.choice.id]: lineId } };
    }
  }
  // A shared destination does not identify which answer the player selected.
  const parents = model.parents.get(branchKey(group.script, group.node)) || [];
  if (parents.length === 1 && parents[0].group.key !== group.key) {
    const parent = parents[0];
    return { rootKey: parent.group.key, selections: { [parent.choice.id]: parent.option.id } };
  }
  return { rootKey: group.key, selections: {} };
}

export function resolveStory(model: StoryModel, focus: StoryFocus) {
  const groups: StoryGroup[] = [];
  const missingTargets: string[] = [];
  const visited = new Set<string>();
  const pending = [focus.rootKey];
  while (pending.length) {
    const key = pending.shift()!;
    const group = model.byKey.get(key);
    if (!group || visited.has(key)) continue;
    visited.add(key);
    groups.push(group);
    const targets: string[] = [];
    const choices = group.segments.filter(segment => segment.kind === 'choice');
    for (const segment of choices) {
      if (segment.kind !== 'choice') continue;
      const chosen = segment.choice.options.find(option => option.id === focus.selections[segment.choice.id]);
      if (chosen?.trigger) targets.push(chosen.trigger);
    }
    if (!choices.length) {
      const continuation = model.continuations.get(branchKey(group.script, group.node)) || [];
      // Multiple targets may represent conditions absent from this text snapshot.
      if (continuation.length === 1) targets.push(continuation[0]);
    }
    for (const target of targets) {
      const next = model.byBranch.get(branchKey(group.script, target));
      if (next) pending.push(...next.map(item => item.key));
      else if (!missingTargets.includes(target)) missingTargets.push(target);
    }
  }
  return { groups, missingTargets };
}

export function withDefaultChoices(model: StoryModel, focus: StoryFocus): StoryFocus {
  if (!focus.rootKey) return focus;
  const selections: Record<string, string> = { ...focus.selections };
  const visited = new Set<string>();
  const pending = [focus.rootKey];
  while (pending.length) {
    const key = pending.shift()!;
    const group = model.byKey.get(key);
    if (!group || visited.has(key)) continue;
    visited.add(key);
    const targets: string[] = [];
    const choices = group.segments.filter(segment => segment.kind === 'choice');
    for (const segment of choices) {
      if (segment.kind !== 'choice') continue;
      if (!selections[segment.choice.id] && segment.choice.options[0]) {
        selections[segment.choice.id] = segment.choice.options[0].id;
      }
      const chosen = segment.choice.options.find(option => option.id === selections[segment.choice.id]);
      if (chosen?.trigger) targets.push(chosen.trigger);
    }
    if (!choices.length) {
      const continuation = model.continuations.get(branchKey(group.script, group.node)) || [];
      if (continuation.length === 1) targets.push(continuation[0]);
    }
    for (const target of targets) {
      const next = model.byBranch.get(branchKey(group.script, target));
      if (next) pending.push(...next.map(item => item.key));
    }
  }
  return { rootKey: focus.rootKey, selections };
}

export function resolveFullMissionStory(
  model: StoryModel,
  hitLineId = '',
  customSelections: Record<string, string> = {},
) {
  const reverseContinuations = new Map<string, string[]>();
  const childTargets = new Set<string>();

  for (const [targetKey, parentList] of model.parents.entries()) {
    if (parentList.some(p => branchKey(p.group.script, p.group.node) !== targetKey)) {
      childTargets.add(targetKey);
    }
  }
  for (const [sourceKey, targets] of model.continuations.entries()) {
    const [script, sourceNode] = JSON.parse(sourceKey) as [string, string];
    for (const target of targets) {
      if (target === 'sequence:0') continue;
      const targetKey = branchKey(script, target);
      if (targetKey !== sourceKey) {
        childTargets.add(targetKey);
        const list = reverseContinuations.get(targetKey) || [];
        if (!list.includes(sourceNode)) reverseContinuations.set(targetKey, [...list, sourceNode]);
      }
    }
  }

  const hitChoices: Record<string, string> = {};
  const hitGroup = hitLineId ? model.byLine.get(hitLineId) : undefined;
  if (hitGroup) {
    for (const segment of hitGroup.segments) {
      if (segment.kind === 'choice' && segment.choice.options.some(opt => opt.id === hitLineId)) {
        hitChoices[segment.choice.id] = hitLineId;
      }
    }
    const visitedBack = new Set<string>([hitGroup.key]);
    const queue: StoryGroup[] = [hitGroup];
    while (queue.length) {
      const curr = queue.shift()!;
      const currKey = branchKey(curr.script, curr.node);
      for (const parent of model.parents.get(currKey) || []) {
        if (!hitChoices[parent.choice.id]) {
          hitChoices[parent.choice.id] = parent.option.id;
        }
        if (!visitedBack.has(parent.group.key)) {
          visitedBack.add(parent.group.key);
          queue.push(parent.group);
        }
      }
      for (const sourceNode of reverseContinuations.get(currKey) || []) {
        for (const srcGroup of model.byBranch.get(branchKey(curr.script, sourceNode)) || []) {
          if (!visitedBack.has(srcGroup.key)) {
            visitedBack.add(srcGroup.key);
            queue.push(srcGroup);
          }
        }
      }
    }
  }

  const defaultSelections: Record<string, string> = {};
  for (const group of model.groups) {
    for (const segment of group.segments) {
      if (segment.kind === 'choice' && segment.choice.options[0]) {
        defaultSelections[segment.choice.id] = segment.choice.options[0].id;
      }
    }
  }

  const initialSelections: Record<string, string> = { ...defaultSelections, ...hitChoices };
  const selections: Record<string, string> = { ...initialSelections, ...customSelections };

  const rootGroups = model.groups.filter(
    group => group.node === 'sequence:0' || !childTargets.has(branchKey(group.script, group.node)),
  );
  const roots = rootGroups.length ? rootGroups : model.groups;

  const groups: StoryGroup[] = [];
  const visited = new Set<string>();

  function traverseFrom(startKeys: string[]) {
    const pending = [...startKeys];
    while (pending.length) {
      const key = pending.shift()!;
      const group = model.byKey.get(key);
      if (!group || visited.has(key)) continue;
      visited.add(key);
      groups.push(group);

      const targets: string[] = [];
      const choices = group.segments.filter(segment => segment.kind === 'choice');
      for (const segment of choices) {
        if (segment.kind !== 'choice') continue;
        const chosen =
          segment.choice.options.find(opt => opt.id === selections[segment.choice.id]) ||
          segment.choice.options[0];
        if (chosen?.trigger) targets.push(chosen.trigger);
      }
      const continuation = model.continuations.get(branchKey(group.script, group.node)) || [];
      if (!choices.length || targets.length === 0) {
        for (const nextTarget of continuation) {
          if (!targets.includes(nextTarget)) targets.push(nextTarget);
        }
      }

      const nextKeys: string[] = [];
      for (const target of targets) {
        const next = model.byBranch.get(branchKey(group.script, target));
        if (next) {
          for (const item of next) {
            if (!visited.has(item.key)) nextKeys.push(item.key);
          }
        }
      }
      if (nextKeys.length) {
        pending.unshift(...nextKeys);
      }
    }
  }

  for (const root of roots) {
    if (!visited.has(root.key)) {
      traverseFrom([root.key]);
    }
  }

  if (hitGroup && !visited.has(hitGroup.key) && Object.keys(customSelections).length === 0) {
    traverseFrom([hitGroup.key]);
  }

  return { groups, selections, initialSelections };
}

