import test from 'node:test';
import assert from 'node:assert/strict';
import { createSemanticSearchClient, indexReady, loadVisitorKey, normalizeVisitorKey, saveVisitorKey, serverKeyReady, SILICONFLOW_EMBEDDINGS_URL, validateQueryVector, VISITOR_KEY_STORAGE } from '../src/semantic.ts';
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

test('visitor key is the only storage entry and can be modified or removed', () => {
  const local = storage();
  assert.equal(saveVisitorKey(local, '  visitor-test-key  '), 'visitor-test-key');
  assert.deepEqual([...local.values], [[VISITOR_KEY_STORAGE, 'visitor-test-key']]);
  assert.equal(loadVisitorKey(local), 'visitor-test-key');
  saveVisitorKey(local, 'changed-test-key');
  assert.equal(loadVisitorKey(local), 'changed-test-key');
  saveVisitorKey(local, '');
  assert.equal(local.values.size, 0);
});

test('invalid keys and disabled storage produce safe feedback', () => {
  assert.throws(() => normalizeVisitorKey('secret\r\nheader'), /密钥格式无效/);
  assert.throws(() => normalizeVisitorKey('x'.repeat(4097)), /密钥格式无效/);
  const blocked = { getItem() { throw new Error('disabled'); }, setItem() { throw new Error('disabled'); }, removeItem() { throw new Error('disabled'); } };
  assert.equal(loadVisitorKey(blocked), '');
  assert.throws(() => saveVisitorKey(blocked, 'visitor-test-key'), /浏览器无法保存密钥/);
});

test('index readiness permits visitor semantics without an administrator key', () => {
  const embedding = context().embedding;
  assert.equal(indexReady(embedding), true);
  assert.equal(serverKeyReady(embedding), false);
  assert.equal(indexReady({ ...embedding, index_ready: false }), false);
  assert.equal(indexReady({ ...embedding, index_ready: undefined, indexed: 1 }), false);
});

test('browser queries the fixed SiliconFlow endpoint and returns only the vector', async () => {
  let calls = 0;
  const client = createSemanticSearchClient(async (url, options) => {
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
  const prepared = await client.prepare('星穹列车', 'visitor-test-key', context());
  assert.deepEqual(prepared, { queryVector: [1, 0, 0] });
  assert.equal(JSON.stringify(prepared).includes('visitor-test-key'), false);
  await client.prepare('星穹列车', 'visitor-test-key', context());
  assert.equal(calls, 1);
});

test('query instruction and requested output dimensions follow the index', async () => {
  const setting = context();
  setting.embedding.index_model = 'Qwen/test-model';
  setting.embedding.index_request_dimensions = 3;
  setting.embedding.index_identity!.query_prefix = 'Query: ';
  setting.embedding.index_identity!.extra_body = { task: 'retrieval', dimensions: 999, model: 'wrong', input: ['wrong'] };
  const client = createSemanticSearchClient(async (_url, options) => {
    const body = JSON.parse(String(options?.body));
    assert.deepEqual(body, { task: 'retrieval', model: 'Qwen/test-model', input: ['Query: 旅途'], encoding_format: 'float', dimensions: 3 });
    return vectorResponse();
  });
  assert.deepEqual(await client.prepare('旅途', 'test-key', setting), { queryVector: [1, 0, 0] });
});

test('snapshot, identity and key changes invalidate cached query vectors', async () => {
  let calls = 0;
  const client = createSemanticSearchClient(async () => { calls++; return vectorResponse(); });
  const first = context();
  await client.prepare('旅途', 'key-a', first);
  await client.prepare('旅途', 'key-b', first);
  await client.prepare('旅途', 'key-b', { ...first, snapshot: 'snapshot-b' });
  const changed = context();
  changed.embedding.index_identity!.query_prefix = 'query: ';
  await client.prepare('旅途', 'key-b', changed);
  client.clear();
  await client.prepare('旅途', 'key-a', first);
  assert.equal(calls, 5);
});

test('CORS, network, HTTP and invalid-vector failures request a server fallback', async () => {
  const fetchers: (typeof fetch)[] = [
    async () => { throw new TypeError('Failed to fetch'); },
    async () => new Response('provider-private-test-key', { status: 401 }),
    async () => vectorResponse([1, 0]),
    async () => vectorResponse([0, 0, 0]),
  ];
  for (const fetcher of fetchers) {
    const client = createSemanticSearchClient(fetcher);
    assert.deepEqual(await client.prepare('旅途', 'visitor-test-key', context()), { fallbackKey: 'visitor-test-key' });
  }
});

test('clearing a key while its query is pending prevents a stale fallback', async () => {
  let resolve: ((response: Response) => void) | undefined;
  const client = createSemanticSearchClient(() => new Promise<Response>(done => { resolve = done; }));
  const pending = client.prepare('旅途', 'visitor-test-key', context());
  client.clear();
  resolve!(vectorResponse());
  await assert.rejects(pending, /密钥已更改/);
});

test('no visitor key or no ready index makes no browser provider call', async () => {
  const client = createSemanticSearchClient(async () => { throw new Error('unexpected provider request'); });
  assert.deepEqual(await client.prepare('旅途', '', context()), {});
  const setting = context();
  setting.embedding.index_ready = false;
  assert.deepEqual(await client.prepare('旅途', 'visitor-test-key', setting), {});
});

test('query vectors reject invalid dimensions, non-finite values and zero vectors', () => {
  for (const value of [[1, 0], [Infinity, 0, 0], [NaN, 0, 0], [0, 0, 0], ['1', 0, 0], null]) {
    assert.throws(() => validateQueryVector(value, 3), /查询向量/);
  }
  assert.deepEqual(validateQueryVector([1, 0, 0], 3), [1, 0, 0]);
});

test('a stalled direct request aborts and enables the single-query proxy fallback', async () => {
  const client = createSemanticSearchClient((_url, options) => new Promise((_resolve, reject) => {
    options!.signal!.addEventListener('abort', () => reject(new Error('aborted')));
  }), 1);
  assert.deepEqual(await client.prepare('旅途', 'visitor-test-key', context()), { fallbackKey: 'visitor-test-key' });
});

test('successful direct vectors reach the search endpoint without the visitor key', async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { baseURI: 'https://story.example/tools/hsr/' } });
  globalThis.fetch = async (url, options) => {
    assert.equal(url, 'https://story.example/tools/hsr/api/v1/search');
    assert.equal(new Headers(options?.headers).has('X-Embedding-Key'), false);
    assert.deepEqual(JSON.parse(String(options?.body)), { query: '旅途', mode: 'hybrid', query_vector: [1, 0, 0] });
    return new Response(JSON.stringify({ total: 1 }));
  };
  try { assert.deepEqual(await searchRequest({ query: '旅途', mode: 'hybrid' }, { queryVector: [1, 0, 0] }), { total: 1 }); }
  finally {
    globalThis.fetch = originalFetch;
    if (originalDocument) Object.defineProperty(globalThis, 'document', originalDocument);
    else Reflect.deleteProperty(globalThis, 'document');
  }
});

test('proxy fallback sends the visitor key only as a header and redacts server errors', async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { baseURI: 'https://story.example/' } });
  globalThis.fetch = async (url, options) => {
    assert.equal(String(url).includes('visitor-test-key'), false);
    assert.equal(new Headers(options?.headers).get('X-Embedding-Key'), 'visitor-test-key');
    const body = JSON.parse(String(options?.body));
    assert.equal('query_vector' in body, false);
    assert.equal(String(options?.body).includes('visitor-test-key'), false);
    return new Response(JSON.stringify({ detail: 'provider echoed visitor-test-key' }), { status: 502 });
  };
  try {
    await assert.rejects(searchRequest({ query: '旅途', mode: 'hybrid' }, { fallbackKey: 'visitor-test-key' }), failure => {
      assert.equal((failure as Error).message, '请求失败（502）');
      return true;
    });
  } finally {
    globalThis.fetch = originalFetch;
    if (originalDocument) Object.defineProperty(globalThis, 'document', originalDocument);
    else Reflect.deleteProperty(globalThis, 'document');
  }
});
