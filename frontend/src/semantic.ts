import type { Status } from './types.ts';

export const VISITOR_KEY_STORAGE = 'hsr-user-api-key';
export const VISITOR_ENDPOINT_STORAGE = 'hsr-user-api-base-url';
export const SILICONFLOW_EMBEDDINGS_URL = 'https://api.siliconflow.cn/v1/embeddings';

type KeyStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
export interface SemanticContext { snapshot: string; embedding: Status['embedding'] }
export interface PreparedSemanticQuery { queryVector?: number[]; fallbackKey?: string }

export function normalizeVisitorKey(value: string): string {
  const key = value.trim();
  if (key.length > 4096 || /[\x00-\x20\x7f]/.test(key)) throw new Error('密钥格式无效，请重新粘贴完整 API Key');
  return key;
}

export function normalizeVisitorEndpoint(value: string): string {
  const raw = value.trim();
  if (!raw) return '';
  if (raw.length > 1024 || /[\x00-\x20\x7f]/.test(raw)) throw new Error('接口地址格式无效');
  let parsed: URL;
  try { parsed = new URL(raw); } catch { throw new Error('接口地址格式无效，请输入完整 http(s):// 地址'); }
  const isLocal = parsed.hostname === 'localhost' || parsed.hostname === '127.0.0.1';
  if (parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && isLocal)) {
    throw new Error('自定义接口请使用 https:// 协议（本机调试支持 http://localhost）');
  }
  const normalizedPath = parsed.pathname.replace(/\/+$/, '');
  parsed.pathname = normalizedPath.endsWith('/embeddings') ? normalizedPath : `${normalizedPath}/embeddings`;
  parsed.search = '';
  parsed.hash = '';
  return parsed.toString();
}

export function loadVisitorEndpoint(storage: KeyStorage): string {
  try { return normalizeVisitorEndpoint(storage.getItem(VISITOR_ENDPOINT_STORAGE) || ''); }
  catch { return ''; }
}

export function saveVisitorEndpoint(storage: KeyStorage, value: string): string {
  const endpoint = normalizeVisitorEndpoint(value);
  try {
    if (endpoint) storage.setItem(VISITOR_ENDPOINT_STORAGE, endpoint);
    else storage.removeItem(VISITOR_ENDPOINT_STORAGE);
  } catch { throw new Error('浏览器无法保存接口地址，请检查本地存储权限'); }
  return endpoint;
}

export function loadVisitorKey(storage: KeyStorage): string {
  try { return normalizeVisitorKey(storage.getItem(VISITOR_KEY_STORAGE) || ''); }
  catch { return ''; }
}

export function saveVisitorKey(storage: KeyStorage, value: string): string {
  const key = normalizeVisitorKey(value);
  try {
    if (key) storage.setItem(VISITOR_KEY_STORAGE, key);
    else storage.removeItem(VISITOR_KEY_STORAGE);
  } catch { throw new Error('浏览器无法保存密钥，请检查本地存储权限'); }
  return key;
}

export function indexReady(embedding: Status['embedding']): boolean {
  return embedding.index_ready ?? (embedding.compatible && embedding.indexed > 0 && embedding.indexed === embedding.total);
}

export function serverKeyReady(embedding: Status['embedding']): boolean {
  return embedding.server_key_ready ?? embedding.ready;
}

export function validateQueryVector(value: unknown, dimension: number): number[] {
  if (!Array.isArray(value) || !Number.isInteger(dimension) || dimension < 1 || dimension > 32768 || value.length !== dimension ||
      !value.every(item => typeof item === 'number' && Number.isFinite(item)) || !value.some(item => item !== 0)) {
    throw new Error('查询向量与剧情索引不匹配');
  }
  return value;
}

export function createSemanticSearchClient(fetcher: typeof fetch = fetch, timeoutMs = 20000) {
  const cache = new Map<string, Promise<number[]>>();
  let generation = 0;

  async function direct(query: string, key: string, context: SemanticContext, endpoint: string): Promise<number[]> {
    const embedding = context.embedding;
    const identity = embedding.index_identity;
    const payload: Record<string, unknown> = {
      ...(identity?.extra_body || {}),
      model: embedding.index_model || 'BAAI/bge-m3',
      input: [(identity?.query_prefix || '') + query],
      encoding_format: 'float',
    };
    delete payload.dimensions;
    if (embedding.index_request_dimensions) payload.dimensions = embedding.index_request_dimensions;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    const targetUrl = endpoint || SILICONFLOW_EMBEDDINGS_URL;
    try {
      const response = await fetcher(targetUrl, {
        method: 'POST',
        headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        credentials: 'omit',
        referrerPolicy: 'no-referrer',
        cache: 'no-store',
        signal: controller.signal,
      });
      if (!response.ok) throw new Error('浏览器直连暂不可用');
      const body = await response.json();
      if (!Array.isArray(body.data) || body.data.length !== 1 || body.data[0]?.index !== 0) throw new Error('查询向量格式无效');
      return validateQueryVector(body.data[0].embedding, embedding.dimension);
    } finally { clearTimeout(timeout); }
  }

  return {
    clear() { generation++; cache.clear(); },
    async prepare(query: string, rawKey: string, context: SemanticContext, rawEndpoint = ''): Promise<PreparedSemanticQuery> {
      const key = normalizeVisitorKey(rawKey);
      const endpoint = normalizeVisitorEndpoint(rawEndpoint);
      if (!key || !indexReady(context.embedding)) return {};
      const currentGeneration = generation;
      const cacheKey = JSON.stringify([context.snapshot, context.embedding.index_identity, context.embedding.index_model, context.embedding.dimension, context.embedding.index_request_dimensions, query, key, endpoint]);
      let pending = cache.get(cacheKey);
      if (!pending) {
        if (cache.size >= 32) cache.delete(cache.keys().next().value!);
        pending = direct(query, key, context, endpoint);
        cache.set(cacheKey, pending);
      }
      try {
        const queryVector = await pending;
        if (currentGeneration !== generation) throw new Error('语义搜索密钥已更改，请重新搜索');
        return { queryVector };
      } catch (failure) {
        if (cache.get(cacheKey) === pending) cache.delete(cacheKey);
        if (currentGeneration !== generation) throw new Error('语义搜索密钥已更改，请重新搜索');
        if (endpoint) throw new Error(failure instanceof Error ? `直连自定义接口失败（${failure.message}），请检查接口地址、CORS 设置或密钥` : '直连自定义接口失败');
        return { fallbackKey: key };
      }
    },
  };
}
