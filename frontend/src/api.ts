import type { PreparedSemanticQuery } from './semantic';

export function apiUrl(path: string): string {
  const base = new URL('api/v1/', document.baseURI);
  return new URL(path.replace(/^\//, ''), base).toString();
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (typeof options.body === 'string' && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  let response: Response;
  try { response = await fetch(apiUrl(path), { ...options, headers, credentials: 'same-origin' }); }
  catch (failure) {
    if (options.signal?.aborted) throw failure;
    throw new Error('无法连接检索服务，请检查网络后重试');
  }
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const data = await response.json();
      if (!headers.has('X-Embedding-Key')) message = typeof data.detail === 'string' ? data.detail : typeof data.error === 'string' ? data.error : message;
    } catch { /* Preserve the HTTP error when the response is not JSON. */ }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function post<T>(path: string, body: unknown = {}, options: RequestInit = {}): Promise<T> {
  return request<T>(path, { ...options, method: 'POST', body: JSON.stringify(body) });
}

export function searchRequest<T>(body: Record<string, unknown>, prepared: PreparedSemanticQuery = {}, signal?: AbortSignal): Promise<T> {
  return post<T>('search', { ...body, query_vector: prepared.queryVector }, {
    signal,
    ...(prepared.fallbackKey ? { headers: { 'X-Embedding-Key': prepared.fallbackKey } } : {}),
  });
}

export function downloadJson(name: string, content: unknown): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(content, null, 2)], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
