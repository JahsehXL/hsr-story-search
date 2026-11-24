import type { MissionResponse, Result, SearchResponse } from './types.ts';

const historyKey = 'hsr-recent-random-lines';

export function loadRandomHistory(): string[] {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(historyKey) || '[]');
    return Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string' && Boolean(id)).slice(0, 30) : [];
  } catch { return []; }
}

export function rememberRandomLine(recent: string[], lineId: string): string[] {
  return lineId ? [lineId, ...recent.filter(id => id !== lineId)].slice(0, 30) : recent;
}

export function saveRandomHistory(recent: string[]): void {
  try { sessionStorage.setItem(historyKey, JSON.stringify(recent)); } catch { /* Keep the in-memory history. */ }
}

export function restoredRandomResult(selection: Result, detail: MissionResponse): Result | undefined {
  if (detail.anchor_missing) return;
  const line = detail.lines.find(value => value.id === (detail.anchor_line_id || selection.line_id));
  if (!line) return;
  const mission = detail.mission;
  return {
    ...selection, mission_name: mission.name, chapter: mission.chapter, world: mission.world,
    type: mission.type, version: mission.version, version_status: mission.version_status,
    speaker: line.speaker, text: line.text, display_text: line.text, branch: line.branch,
    kind: line.kind, dialogue_origin: line.dialogue_origin, match_type: 'random',
  };
}

export function restoredRandomResponse(snapshot: string, result?: Result): SearchResponse {
  return { results: result ? [result] : [], total: result ? 1 : 0, search_mode: 'random', elapsed_ms: 0, snapshot };
}
