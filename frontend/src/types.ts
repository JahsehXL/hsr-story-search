export interface Status {
  mode: 'local' | 'server';
  authenticated: boolean;
  admin: boolean;
  version: string;
  data: { ready: boolean; snapshot: string; stats: Record<string, number | string> };
  embedding: { ready: boolean; compatible: boolean; model: string; dimension: number; indexed: number; total: number };
  jobs?: Job[];
}

export interface Filters { world: string; version: string; type: string; character: string }
export type SearchMode = 'text' | 'hybrid' | 'fuzzy';
export interface Catalog { worlds: string[]; versions: string[]; types: string[]; characters: string[] }
export interface Result {
  chunk_id: number;
  mission_id: string;
  mission_name: string;
  chapter: string;
  world: string;
  version: string;
  version_status: string;
  speaker: string;
  text: string;
  line_id: string;
  script: string;
  branch: string;
  match_type: string;
  kind?: string;
  matched_field?: 'text' | 'speaker' | 'mission' | 'metadata';
  score: number;
}
export interface SearchResponse {
  results: Result[];
  total: number;
  truncated?: boolean;
  search_mode: string;
  notice?: string;
  elapsed_ms: number;
  snapshot: string;
}
export interface Mission {
  id: string;
  name: string;
  chapter: string;
  world: string;
  type: string;
  version: string;
  version_source: string;
  version_status: string;
}
export interface DialogueLine {
  id: string;
  mission_id: string;
  script: string;
  sequence: number;
  position: number;
  sentence_id: string;
  speaker: string;
  text: string;
  kind: string;
  branch: string;
  trigger: string;
}
export interface MissionResponse {
  mission: Mission;
  lines: DialogueLine[];
  edges: { script: string; source: string; target: string; kind: string }[];
  recalls?: { name?: string; title?: string; path?: string; description?: string; verified?: boolean | number }[];
  total: number;
  offset?: number;
  has_more?: boolean;
  snapshot?: string;
}
export interface Job {
  id: string;
  kind: string;
  status: string;
  phase?: string;
  done: number;
  total: number;
  message: string;
  error?: string;
  created_at?: string;
}
export interface Settings {
  embedding: {
    base_url: string;
    model: string;
    dimensions: number | null;
    query_prefix: string;
    document_prefix: string;
    extra_body: Record<string, unknown>;
    batch_size: number;
    concurrency: number;
    timeout: number;
    api_key_set: boolean;
  };
  updates: { repository: string; manifest_url: string };
  limits: { search_per_minute: number; daily_api_calls: number };
}
export interface Bookmark { key: string; result: Result; query: string; saved_at: string; snapshot?: string }
