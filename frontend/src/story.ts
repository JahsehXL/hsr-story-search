import type { DialogueLine, MissionResponse } from './types.ts';

export interface ChoiceSet {
  id: string;
  options: DialogueLine[];
}

export type StorySegment = { kind: 'dialogue'; lines: DialogueLine[] } | { kind: 'choice'; choice: ChoiceSet };

/** Bound each mounted dialogue block without splitting a set of player answers. */
export function storyBlocks(segments: StorySegment[]): StorySegment[] {
  const blocks: StorySegment[] = [];
  for (const segment of segments) {
    if (segment.kind === 'choice') {
      blocks.push(segment);
      continue;
    }
    for (let start = 0; start < segment.lines.length; start += 16) {
      blocks.push({ kind: 'dialogue', lines: segment.lines.slice(start, start + 16) });
    }
  }
  return blocks;
}

export interface StoryGroup {
  key: string;
  script: string;
  branch: string;
  node: string;
  sequence: number;
  lines: DialogueLine[];
  segments: StorySegment[];
  altSlot?: string;
  altTag?: string;
  altKind?: 'cond' | 'rand';
}

export interface StoryModel {
  groups: StoryGroup[];
  byKey: Map<string, StoryGroup>;
  byLine: Map<string, StoryGroup>;
  byBranch: Map<string, StoryGroup[]>;
  parents: Map<string, { group: StoryGroup; choice: ChoiceSet; option: DialogueLine }[]>;
  continuations: Map<string, string[]>;
  altGroups: Map<string, StoryGroup[]>;
}

const altBranchPattern = /^(.+#alt:\d+)#((?:cond|rand):\d+(?:#(?:cond|rand):\d+)*)$/;

export function parseAltBranch(branch: string): { prefix: string; tag: string; kind: 'cond' | 'rand' } | null {
  const match = altBranchPattern.exec(branch);
  if (!match) return null;
  const tag = match[2];
  return { prefix: match[1], tag, kind: tag.startsWith('rand:') ? 'rand' : 'cond' };
}

export function formatAltLabel(tag: string): string {
  return tag
    .split('#')
    .map(part => {
      const [kind, num] = part.split(':');
      return `${kind === 'rand' ? '随机' : '条件'} ${num}`;
    })
    .join(' · ');
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

function groupsAtDestination(model: StoryModel, script: string, target: string): StoryGroup[] {
  const key = branchKey(script, target);
  return model.byBranch.get(key) ||
    (model.continuations.get(key) || []).flatMap(node => model.byBranch.get(branchKey(script, node)) || []);
}

export function buildStoryModel(lines: DialogueLine[], edges: MissionResponse['edges'] = []): StoryModel {
  const groups: StoryGroup[] = [];
  const keyCounts = new Map<string, number>();
  for (const line of lines) {
    let group = groups[groups.length - 1];
    if (!group || group.script !== line.script || group.sequence !== line.sequence || group.branch !== line.branch) {
      const baseKey = JSON.stringify([line.script, line.sequence, line.branch]);
      const occurrence = (keyCounts.get(baseKey) || 0) + 1;
      keyCounts.set(baseKey, occurrence);
      const key = occurrence === 1 ? baseKey : JSON.stringify([line.script, line.sequence, line.branch, occurrence]);
      const alt = parseAltBranch(line.branch);
      group = {
        key,
        script: line.script,
        branch: line.branch,
        node: line.branch || `sequence:${line.sequence}`,
        sequence: line.sequence,
        lines: [],
        segments: [],
        ...(alt ? { altSlot: JSON.stringify([line.script, line.sequence, alt.prefix]), altTag: alt.tag, altKind: alt.kind } : {}),
      };
      groups.push(group);
    }
    group.lines.push(line);
    const previous = group.segments[group.segments.length - 1];
    if (line.kind.includes('option')) {
      if (previous?.kind === 'choice' && Math.floor(previous.choice.options[0].position / 100) === Math.floor(line.position / 100)) previous.choice.options.push(line);
      else group.segments.push({ kind: 'choice', choice: { id: line.id, options: [line] } });
    } else if (previous?.kind === 'dialogue') previous.lines.push(line);
    else group.segments.push({ kind: 'dialogue', lines: [line] });
  }
  const model: StoryModel = { groups, byKey: new Map(), byLine: new Map(), byBranch: new Map(), parents: new Map(), continuations: new Map(), altGroups: new Map() };
  const rawAltGroups = new Map<string, StoryGroup[]>();
  for (const group of groups) {
    model.byKey.set(group.key, group);
    const key = branchKey(group.script, group.node);
    model.byBranch.set(key, [...(model.byBranch.get(key) || []), group]);
    if (group.altSlot) {
      rawAltGroups.set(group.altSlot, [...(rawAltGroups.get(group.altSlot) || []), group]);
    }
    for (const line of group.lines) model.byLine.set(line.id, group);
  }
  for (const [slot, items] of rawAltGroups) {
    if (items.length >= 2) model.altGroups.set(slot, items);
  }
  const relays = new Map<string, Set<string>>();
  for (const edge of edges) {
    if (edge.kind !== 'continuation') continue;
    const key = branchKey(edge.script, edge.source);
    if (!relays.has(key)) relays.set(key, new Set());
    relays.get(key)!.add(edge.target);
  }
  for (const [source, targets] of relays) {
    const [script] = JSON.parse(source) as [string, string];
    const pending = [...targets];
    const visited = new Set<string>();
    const destinations = new Set<string>();
    for (const node of pending) {
      if (visited.has(node)) continue;
      visited.add(node);
      const key = branchKey(script, node);
      if (model.byBranch.has(key)) destinations.add(node);
      else pending.push(...(relays.get(key) || []));
    }
    model.continuations.set(source, [...destinations]);
  }
  for (const group of groups) {
    for (const segment of group.segments) {
      if (segment.kind !== 'choice') continue;
      for (const option of segment.choice.options) {
        if (!option.trigger) continue;
        const targets = new Set(groupsAtDestination(model, group.script, option.trigger).map(next => next.node));
        for (const node of targets) {
          const target = branchKey(group.script, node);
          model.parents.set(target, [...(model.parents.get(target) || []), { group, choice: segment.choice, option }]);
        }
      }
    }
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
      const next = groupsAtDestination(model, group.script, target);
      if (next.length) pending.push(...next.map(item => item.key));
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
      pending.push(...groupsAtDestination(model, group.script, target).map(item => item.key));
    }
  }
  return { rootKey: focus.rootKey, selections };
}

export function resolveFullMissionStory(
  model: StoryModel,
  hitLineId = '',
  customSelections: Record<string, string> = {},
  customAltSelections: Record<string, string> = {},
) {
  const reverseContinuations = new Map<string, string[]>();
  const childTargets = new Set<string>();
  const entryTargets = new Set<string>();
  const neighbors = new Map<string, Set<string>>();

  function connect(source: string, target: string) {
    if (source === target || !model.byBranch.has(source) || !model.byBranch.has(target)) return;
    if (!neighbors.has(source)) neighbors.set(source, new Set());
    if (!neighbors.has(target)) neighbors.set(target, new Set());
    neighbors.get(source)!.add(target);
    neighbors.get(target)!.add(source);
  }

  for (const [targetKey, parentList] of model.parents.entries()) {
    for (const parent of parentList) connect(branchKey(parent.group.script, parent.group.node), targetKey);
    if (parentList.some(p => branchKey(p.group.script, p.group.node) !== targetKey)) {
      childTargets.add(targetKey);
    }
  }
  for (const [sourceKey, targets] of model.continuations.entries()) {
    const [script, sourceNode] = JSON.parse(sourceKey) as [string, string];
    if (!model.byBranch.has(sourceKey)) {
      // A setup node can enter a dialogue loop whose first group has an incoming backlink.
      if (sourceNode.startsWith('sequence:')) {
        for (const target of targets) entryTargets.add(branchKey(script, target));
      }
      continue;
    }
    for (const target of targets) {
      if (target === 'sequence:0') continue;
      const targetKey = branchKey(script, target);
      if (targetKey !== sourceKey) {
        connect(sourceKey, targetKey);
        childTargets.add(targetKey);
        const list = reverseContinuations.get(targetKey) || [];
        if (!list.includes(sourceNode)) reverseContinuations.set(targetKey, [...list, sourceNode]);
      }
    }
  }

  const hitChoices: Record<string, string> = {};
  const hitAltSelections: Record<string, string> = {};
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
      if (curr.altSlot && model.altGroups.has(curr.altSlot) && !hitAltSelections[curr.altSlot]) {
        hitAltSelections[curr.altSlot] = curr.key;
      }
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
  const defaultAltSelections: Record<string, string> = {};
  for (const [slot, items] of model.altGroups) {
    if (items[0]) defaultAltSelections[slot] = items[0].key;
  }

  const initialSelections: Record<string, string> = { ...defaultSelections, ...hitChoices };
  const selections: Record<string, string> = { ...initialSelections, ...customSelections };
  const initialAltSelections: Record<string, string> = { ...defaultAltSelections, ...hitAltSelections };
  const altSelections: Record<string, string> = { ...initialAltSelections, ...customAltSelections };
  const isAltAllowed = (group: StoryGroup) =>
    !group.altSlot || !model.altGroups.has(group.altSlot) || altSelections[group.altSlot] === group.key;

  const rootKeys = new Set(model.groups.filter(group => {
    const key = branchKey(group.script, group.node);
    return group.node === 'sequence:0' || entryTargets.has(key) || !childTargets.has(key);
  }).map(group => group.key));
  const covered = new Set<string>();
  function coverComponent(group: StoryGroup) {
    const pending = [branchKey(group.script, group.node)];
    while (pending.length) {
      const key = pending.pop()!;
      if (covered.has(key)) continue;
      covered.add(key);
      for (const next of neighbors.get(key) || []) pending.push(next);
    }
  }
  for (const group of model.groups) {
    if (rootKeys.has(group.key)) coverComponent(group);
  }
  for (const group of model.groups) {
    // A later, independent loop can lack an entry even when the script has another scene.
    if (!covered.has(branchKey(group.script, group.node))) {
      rootKeys.add(group.key);
      coverComponent(group);
    }
  }
  const roots = model.groups.filter(group => rootKeys.has(group.key));

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

      const sameBranchRuns = model.byBranch.get(branchKey(group.script, group.node)) || [group];
      const runs = sameBranchRuns.filter(item => item.sequence === group.sequence);
      const runIndex = runs.length > 1 ? runs.indexOf(group) : 0;
      const continuation = model.continuations.get(branchKey(group.script, group.node)) || [];
      const altPrefix = `${group.node}#alt:`;
      let nextRunKey = '';
      if (runs.length > 1 && runIndex >= 0) {
        const minPos = runIndex === 0 ? -Infinity : runs[runIndex].lines[0]?.position ?? 0;
        const maxPos = runIndex + 1 < runs.length ? runs[runIndex + 1].lines[0]?.position ?? Infinity : Infinity;
        for (const nextTarget of continuation) {
          if (nextTarget.startsWith(altPrefix)) {
            const firstPos = groupsAtDestination(model, group.script, nextTarget).find(item => item.sequence === group.sequence)?.lines[0]?.position ?? 0;
            if (firstPos >= minPos && firstPos < maxPos && !targets.includes(nextTarget)) {
              targets.push(nextTarget);
            }
          } else if (runIndex === runs.length - 1 && (!choices.length || targets.length === 0)) {
            if (!targets.includes(nextTarget)) targets.push(nextTarget);
          }
        }
        if (runIndex + 1 < runs.length) {
          nextRunKey = runs[runIndex + 1].key;
        }
      } else {
        for (const nextTarget of continuation) {
          if (nextTarget.startsWith(altPrefix)) {
            if (!targets.includes(nextTarget)) targets.push(nextTarget);
          } else if (!choices.length || targets.length === 0) {
            if (!targets.includes(nextTarget)) targets.push(nextTarget);
          }
        }
      }

      const nextKeys: string[] = [];
      for (const target of targets) {
        for (const item of groupsAtDestination(model, group.script, target)) {
          if (!visited.has(item.key) && isAltAllowed(item)) nextKeys.push(item.key);
        }
      }
      if (nextRunKey && !visited.has(nextRunKey) && !nextKeys.includes(nextRunKey)) {
        nextKeys.push(nextRunKey);
      }
      if (nextKeys.length) {
        pending.unshift(...nextKeys);
      }
    }
  }

  for (const root of roots) {
    if (!visited.has(root.key) && isAltAllowed(root)) {
      traverseFrom([root.key]);
    }
  }

  if (hitGroup && !visited.has(hitGroup.key) && Object.keys(customSelections).length === 0 && Object.keys(customAltSelections).length === 0) {
    traverseFrom([hitGroup.key]);
  }

  return { groups, selections, initialSelections, altSelections, initialAltSelections };
}

export function sceneStatusLabel(script: string, explicitLabel?: string): string {
  if (explicitLabel) return explicitLabel;
  if (script.startsWith('Story/BattlePerformance/')) return '战斗对白';
  return '';
}

export function isTrailingExtraScene(script: string, orderedScripts?: ReadonlySet<string>, optionalScripts?: ReadonlySet<string>): boolean {
  return Boolean(optionalScripts?.has(script) || (script.startsWith('Recovered/') && !orderedScripts?.has(script)));
}

export function shouldShowTrailingExtraDivider(
  previousScript: string | undefined,
  currentScript: string,
  orderedScripts?: ReadonlySet<string>,
  optionalScripts?: ReadonlySet<string>,
): boolean {
  const extra = isTrailingExtraScene(currentScript, orderedScripts, optionalScripts);
  return extra && (previousScript ? !isTrailingExtraScene(previousScript, orderedScripts, optionalScripts) : Boolean(optionalScripts?.has(currentScript)));
}

