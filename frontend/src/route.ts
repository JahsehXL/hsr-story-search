import type { Filters, SearchMode } from './types.ts';

export interface ParsedRoute {
  path: string;
  query: string;
  filters: Filters;
  mode: SearchMode;
  mission: string;
  line: string;
  script: string;
  snapshot: string;
}

export interface SearchCriteria {
  query: string;
  filters: Filters;
  mode: SearchMode;
}

const VALID_MODES = new Set<SearchMode>(['text', 'hybrid', 'fuzzy']);

export function hasActiveFilters(filters: Filters): boolean {
  return Boolean(filters.world || filters.version || filters.type || filters.character);
}

export function parseRoute(hash: string): ParsedRoute {
  const [path, search = ''] = hash.replace(/^#/, '').split('?');
  const params = new URLSearchParams(search);
  const rawMode = params.get('mode') || '';
  const mode: SearchMode = VALID_MODES.has(rawMode as SearchMode) ? (rawMode as SearchMode) : 'text';
  return {
    path,
    query: params.get('q') || '',
    filters: {
      world: params.get('world') || '',
      version: params.get('version') || '',
      type: params.get('type') || '',
      character: params.get('character') || '',
    },
    mode,
    mission: params.get('mission') || '',
    line: params.get('line') || '',
    script: params.get('script') || '',
    snapshot: params.get('snapshot') || '',
  };
}

function criteriaParams(criteria: SearchCriteria): URLSearchParams {
  const params = new URLSearchParams();
  const q = criteria.query.trim();
  if (q) params.set('q', q);
  if (criteria.filters.world) params.set('world', criteria.filters.world);
  if (criteria.filters.version) params.set('version', criteria.filters.version);
  if (criteria.filters.type) params.set('type', criteria.filters.type);
  if (criteria.filters.character) params.set('character', criteria.filters.character);
  if (criteria.mode && criteria.mode !== 'text') params.set('mode', criteria.mode);
  return params;
}

export function buildSearchHash(criteria: SearchCriteria): string {
  if (!criteria.query.trim() && !hasActiveFilters(criteria.filters)) {
    return '#/';
  }
  const query = criteriaParams(criteria).toString();
  return query ? `#/search?${query}` : '#/search';
}

export function buildMissionHash(
  criteria: SearchCriteria,
  selection: { mission_id: string; line_id?: string; script?: string; snapshot?: string },
): string {
  const params = criteriaParams(criteria);
  if (selection.mission_id) params.set('mission', selection.mission_id);
  if (selection.line_id) params.set('line', selection.line_id);
  if (selection.script) params.set('script', selection.script);
  if (selection.snapshot) params.set('snapshot', selection.snapshot);
  return `#/search?${params.toString()}`;
}
