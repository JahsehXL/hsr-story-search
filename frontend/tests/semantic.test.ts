import test from 'node:test';
import assert from 'node:assert/strict';
import { createSemanticSearchClient, indexReady, loadVisitorEndpoint, loadVisitorKey, normalizeVisitorEndpoint, normalizeVisitorKey, saveVisitorEndpoint, saveVisitorKey, serverKeyReady, SILICONFLOW_EMBEDDINGS_URL, validateQueryVector, VISITOR_ENDPOINT_STORAGE, VISITOR_KEY_STORAGE } from '../src/semantic.ts';
import type { SemanticContext } from '../src/semantic.ts';
import { searchRequest } from '../src/api.ts';

function context(): SemanticContext {
  return {
    snapshot: 'snapshot-a',
    embedding: {
      ready: false, index_ready: true, server_key_ready: false, compatible: true,
      model: 'BAAI/bge-m3', index_model: 'BAAI/bge-m3', dimension: 3, indexed: 2, total: 2,
      index_request_dimensions: 0,
      index_identity: { model: 'BAAI/bge-m3', dimension: 3, query_prefix: '', document_prefix: '', extra_body: {} },
    },
  };
}

function vectorResponse(vector: unknown = [1, 0, 0]) {
  return new Response(JSON.stringify({ data: [{ index: 0, embedding: vector }] }), { status: 200 });
}

function storage() {
  const values = new Map<string, string>();
  return { values, getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => { values.delete(key); } };
}

test('visitor key storage, validation, and index readiness checks', () => {
  const local = storage();
  assert.equal(saveVisitorKey(local, '  visitor-test-key  '), 'visitor-test-key');
  assert.deepEqual([...local.values], [[VISITOR_KEY_STORAGE, 'visitor-test-key']]);
  assert.equal(loadVisitorKey(local), 'visitor-test-key');
  saveVisitorKey(local, 'changed-test-key');
  assert.equal(loadVisitorKey(local), 'changed-test-key');
  saveVisitorKey(local, '');
  assert.equal(local.values.size, 0);

  assert.throws(() => normalizeVisitorKey('secret\r\nheader'), /密钥格式无效/);
  assert.throws(() => normalizeVisitorKey('x'.repeat(4097)), /密钥格式无效/);
  const blocked = { getItem() { throw new Error('disabled'); }, setItem() { throw new Error('disabled'); }, removeItem() { throw new Error('disabled'); } };
  assert.equal(loadVisitorKey(blocked), '');
  assert.throws(() => saveVisitorKey(blocked, 'visitor-test-key'), /浏览器无法保存密钥/);

  const embedding = context().embedding;
  assert.equal(indexReady(embedding), true);
  assert.equal(serverKeyReady(embedding), false);
  assert.equal(indexReady({ ...embedding, index_ready: false }), false);
  assert.equal(indexReady({ ...embedding, index_ready: undefined, indexed: 1 }), false);
});

test('direct browser embedding requests follow index parameters and invalidate cache on changes', async () => {
  let calls = 0;
  const directClient = createSemanticSearchClient(async (url, options) => {
    calls++;
    assert.equal(url, SILICONFLOW_EMBEDDINGS_URL);
    const headers = new Headers(options?.headers);
    assert.equal(headers.get('Authorization'), 'Bearer visitor-test-key');
    assert.equal(options?.credentials, 'omit');
    assert.equal(options?.referrerPolicy, 'no-referrer');
    const body = JSON.parse(String(options?.body));
    assert.deepEqual(body.input, ['星穹列车']);
    assert.equal(body.model, 'BAAI/bge-m3');
    assert.equal(body.encoding_format, 'float');
    assert.equal('dimensions' in body, false);
    assert.equal('api_key' in body, false);
    return vectorResponse();
  });
  const prepared = await directClient.prepare('星穹列车', 'visitor-test-key', context());
  assert.deepEqual(prepared, { queryVector: [1, 0, 0] });
  assert.equal(JSON.stringify(prepared).includes('visitor-test-key'), false);
  await directClient.prepare('星穹列车', 'visitor-test-key', context());
  assert.equal(calls, 1);

  const setting = context();
  setting.embedding.index_model = 'Qwen/test-model';
  setting.embedding.index_request_dimensions = 3;
  setting.embedding.index_identity!.query_prefix = 'Query: ';
  setting.embedding.index_identity!.extra_body = { task: 'retrieval', dimensions: 999, model: 'wrong', input: ['wrong'] };
  const customClient = createSemanticSearchClient(async (_url, options) => {
    const body = JSON.parse(String(options?.body));
    assert.deepEqual(body, { task: 'retrieval', model: 'Qwen/test-model', input: ['Query: 旅途'], encoding_format: 'float', dimensions: 3 });
    return vectorResponse();
  });
  assert.deepEqual(await customClient.prepare('旅途', 'test-key', setting), { queryVector: [1, 0, 0] });

  let cacheCalls = 0;
  const cachedClient = createSemanticSearchClient(async () => { cacheCalls++; return vectorResponse(); });
  const first = context();
  await cachedClient.prepare('旅途', 'key-a', first);
  await cachedClient.prepare('旅途', 'key-b', first);
  await cachedClient.prepare('旅途', 'key-b', { ...first, snapshot: 'snapshot-b' });
  const changed = context();
  changed.embedding.index_identity!.query_prefix = 'query: ';
  await cachedClient.prepare('旅途', 'key-b', changed);
  cachedClient.clear();
  await cachedClient.prepare('旅途', 'key-a', first);
  assert.equal(cacheCalls, 5);
});

test('provider failures, aborts, stale clears, and invalid query vectors behave safely', async () => {
  const fetchers: (typeof fetch)[] = [
    async () => { throw new TypeError('Failed to fetch'); },
    async () => new Response('provider-private-test-key', { status: 401 }),
    async () => vectorResponse([1, 0]),
    async () => vectorResponse([0, 0, 0]),
  ];
  for (const fetcher of fetchers) {
    assert.deepEqual(await createSemanticSearchClient(fetcher).prepare('旅途', 'visitor-test-key', context()), { fallbackKey: 'visitor-test-key' });
  }

  const stalled = createSemanticSearchClient((_url, options) => new Promise((_resolve, reject) => {
    options!.signal!.addEventListener('abort', () => reject(new Error('aborted')));
  }), 1);
  assert.deepEqual(await stalled.prepare('旅途', 'visitor-test-key', context()), { fallbackKey: 'visitor-test-key' });

  let resolve: ((response: Response) => void) | undefined;
  const pendingClient = createSemanticSearchClient(() => new Promise<Response>(done => { resolve = done; }));
  const pending = pendingClient.prepare('旅途', 'visitor-test-key', context());
  pendingClient.clear();
  resolve!(vectorResponse());
  await assert.rejects(pending, /密钥已更改/);

  const idleClient = createSemanticSearchClient(async () => { throw new Error('unexpected provider request'); });
  assert.deepEqual(await idleClient.prepare('旅途', '', context()), {});
  const disabled = context();
  disabled.embedding.index_ready = false;
  assert.deepEqual(await idleClient.prepare('旅途', 'visitor-test-key', disabled), {});

  for (const value of [[1, 0], [Infinity, 0, 0], [NaN, 0, 0], [0, 0, 0], ['1', 0, 0], null]) {
    assert.throws(() => validateQueryVector(value, 3), /查询向量/);
  }
  assert.deepEqual(validateQueryVector([1, 0, 0], 3), [1, 0, 0]);
});

test('searchRequest routes direct vectors, proxy headers, redaction, and abort signals', async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
  try {
    Object.defineProperty(globalThis, 'document', { configurable: true, value: { baseURI: 'https://story.example/tools/hsr/' } });
    globalThis.fetch = async (url, options) => {
      assert.equal(url, 'https://story.example/tools/hsr/api/v1/search');
      assert.equal(new Headers(options?.headers).has('X-Embedding-Key'), false);
      assert.deepEqual(JSON.parse(String(options?.body)), { query: '旅途', mode: 'hybrid', query_vector: [1, 0, 0] });
      return new Response(JSON.stringify({ total: 1 }));
    };
    assert.deepEqual(await searchRequest({ query: '旅途', mode: 'hybrid' }, { queryVector: [1, 0, 0] }), { total: 1 });

    Object.defineProperty(globalThis, 'document', { configurable: true, value: { baseURI: 'https://story.example/' } });
    globalThis.fetch = async (url, options) => {
      assert.equal(String(url).includes('visitor-test-key'), false);
      assert.equal(new Headers(options?.headers).get('X-Embedding-Key'), 'visitor-test-key');
      const body = JSON.parse(String(options?.body));
      assert.equal('query_vector' in body, false);
      assert.equal(String(options?.body).includes('visitor-test-key'), false);
      return new Response(JSON.stringify({ detail: 'provider echoed visitor-test-key' }), { status: 502 });
    };
    await assert.rejects(searchRequest({ query: '旅途', mode: 'hybrid' }, { fallbackKey: 'visitor-test-key' }), failure => {
      assert.equal((failure as Error).message, '请求失败（502）');
      return true;
    });

    const controller = new AbortController();
    globalThis.fetch = async (_url, options) => {
      assert.equal(options?.signal, controller.signal);
      return new Promise<Response>((_resolve, reject) => {
        options!.signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
      });
    };
    const pending = searchRequest({ query: '旅途', mode: 'text' }, {}, controller.signal);
    controller.abort();
    await assert.rejects(pending, { name: 'AbortError' });
  } finally {
    globalThis.fetch = originalFetch;
    if (originalDocument) Object.defineProperty(globalThis, 'document', originalDocument);
    else Reflect.deleteProperty(globalThis, 'document');
  }
});

test('custom compatible endpoint normalizes /embeddings and never falls back to server proxy', async () => {
  assert.equal(normalizeVisitorEndpoint(''), '');
  assert.equal(normalizeVisitorEndpoint('https://api.example.com/v1/'), 'https://api.example.com/v1/embeddings');
  assert.equal(normalizeVisitorEndpoint('https://api.example.com/v1/embeddings?foo=1'), 'https://api.example.com/v1/embeddings');
  assert.equal(normalizeVisitorEndpoint('http://127.0.0.1:11434/v1'), 'http://127.0.0.1:11434/v1/embeddings');
  assert.throws(() => normalizeVisitorEndpoint('http://remote.example.com/v1'), /https:\/\//);

  const local = storage();
  assert.equal(saveVisitorEndpoint(local, 'https://api.example.com/v1'), 'https://api.example.com/v1/embeddings');
  assert.equal(local.getItem(VISITOR_ENDPOINT_STORAGE), 'https://api.example.com/v1/embeddings');
  assert.equal(loadVisitorEndpoint(local), 'https://api.example.com/v1/embeddings');
  saveVisitorEndpoint(local, '');
  assert.equal(local.values.size, 0);

  const direct = createSemanticSearchClient(async url => {
    assert.equal(url, 'https://api.example.com/v1/embeddings');
    return vectorResponse();
  });
  assert.deepEqual(await direct.prepare('旅途', 'custom-key', context(), 'https://api.example.com/v1'), { queryVector: [1, 0, 0] });

  const failing = createSemanticSearchClient(async () => new Response('{}', { status: 401 }));
  await assert.rejects(failing.prepare('旅途', 'custom-key', context(), 'https://api.example.com/v1'), /直连自定义接口失败/);
});
