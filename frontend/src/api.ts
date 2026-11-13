const base = new URL('api/v1/', document.baseURI);

export function apiUrl(path: string): string {
  return new URL(path.replace(/^\//, ''), base).toString();
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (typeof options.body === 'string' && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = await fetch(apiUrl(path), { ...options, headers, credentials: 'same-origin' });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const data = await response.json();
      message = typeof data.detail === 'string' ? data.detail : typeof data.error === 'string' ? data.error : message;
    } catch { /* Preserve the HTTP error when the response is not JSON. */ }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function post<T>(path: string, body: unknown = {}): Promise<T> {
  return request<T>(path, { method: 'POST', body: JSON.stringify(body) });
}

export function downloadJson(name: string, content: unknown): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(content, null, 2)], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
