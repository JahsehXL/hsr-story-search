import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { apiUrl, downloadJson, post, request, searchRequest } from './api';
import { createSemanticSearchClient, indexReady, loadVisitorKey, saveVisitorKey, serverKeyReady, VISITOR_KEY_STORAGE } from './semantic';
import type { SemanticContext } from './semantic';
import Reader from './Reader';
import { cleanMissionName, speakerName, errorText, versionText, Highlight, Empty } from './presentation';
import type { Bookmark, Catalog, DialogueLine, Filters, Job, MissionResponse, Result, SearchMode, SearchResponse, Settings, Status } from './types';

const emptyFilters: Filters = { world: '', version: '', type: '', character: '' };
const emptyCatalog: Catalog = { worlds: [], versions: [], types: [], characters: [] };
let bootstrapPromise: Promise<void> | undefined;

function storedVisitorKey(): string {
  try { return loadVisitorKey(localStorage); } catch { return ''; }
}

function bootstrapSession(): Promise<void> {
  bootstrapPromise ??= (async () => {
    const hash = location.hash.slice(1);
    if (hash.startsWith('bootstrap=')) {
      const token = new URLSearchParams(hash).get('bootstrap');
      if (token) await post('auth/bootstrap', { token });
      history.replaceState(null, '', `${location.pathname}${location.search}#/`);
    }
  })();
  return bootstrapPromise;
}
const labels: Record<string, string> = {
  Main: '开拓任务', Branch: '冒险任务', Companion: '同行任务', Daily: '日常任务',
  Interlude: '开拓续闻', Gap: '开拓续闻', exact: '原文命中', keyword: '关键词匹配', fuzzy: '近似文字',
  semantic: '描述相关', hybrid: '混合检索', text: '文字检索', unknown: '版本待核实',
  queued: '等待中', running: '进行中', paused: '已暂停', completed: '已完成',
  failed: '失败', cancelled: '已取消', interrupted: '已中断', succeeded: '已完成',
  import_directory: '导入目录', import_upload: '导入文件', download_source: '更新原始数据',
  build_index: '建立语义索引', install_package: '安装数据包', check_update: '检查数据更新', rollback: '恢复上一版',
};

function readRoute() {
  const [path, search = ''] = location.hash.replace(/^#/, '').split('?');
  const params = new URLSearchParams(search);
  return { path, query: params.get('q') || '', mission: params.get('mission') || '', line: params.get('line') || '',
    script: params.get('script') || '', snapshot: params.get('snapshot') || '' };
}

function formatNumber(value: number | string | undefined) { return typeof value === 'number' ? value.toLocaleString('zh-CN') : value || '—'; }

function matchLabel(result: Result) {
  if (result.matched_field === 'mission') return '任务名称匹配';
  if (result.matched_field === 'speaker') return '角色匹配';
  if (result.matched_field === 'metadata') return '剧情出处匹配';
  return labels[result.match_type] || '相关对白';
}

function SelectField({ label, value, values, onChange, searchable = false }: { label: string; value: string; values: string[]; onChange: (v: string) => void; searchable?: boolean }) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    if (searchable) setTimeout(() => inputRef.current?.focus(), 0);
    const onPointer = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, searchable]);

  const displayValue = value ? (labels[value] || value) : '全部';
  const filtered = searchable && search.trim()
    ? values.filter(v => (labels[v] || v).toLowerCase().includes(search.trim().toLowerCase()))
    : values;

  return <div ref={wrapRef} className={`filter-dropdown ${value ? 'has-value' : ''} ${open ? 'is-open' : ''}`}>
    <button
      type="button"
      className="filter"
      aria-label={`${label}筛选`}
      aria-expanded={open}
      title={value ? `${label}：${displayValue}` : `全部${label}`}
      onClick={() => { setOpen(o => !o); setSearch(''); }}
    >
      <span>{label}</span>
      <strong className="filter-value">{displayValue}</strong>
      <span className="select-arrow" aria-hidden="true">⌄</span>
    </button>
    {open && <div className="filter-menu" role="listbox" aria-label={`${label}选项`}>
      {searchable && <div className="filter-search-box">
        <input
          ref={inputRef}
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder={`搜索或输入${label}…`}
          aria-label={`搜索${label}`}
          onKeyDown={e => {
            if (e.key === 'Enter' && search.trim()) {
              e.preventDefault();
              onChange(filtered[0] || search.trim());
              setOpen(false);
            }
          }}
        />
      </div>}
      <div className="filter-options">
        <button type="button" role="option" aria-selected={!value} className={`filter-option ${!value ? 'is-selected' : ''}`} onClick={() => { onChange(''); setOpen(false); }}>
          <span>全部</span>{!value && <span aria-hidden="true">✓</span>}
        </button>
        {searchable && search.trim() && !values.includes(search.trim()) && <button type="button" role="option" aria-selected={value === search.trim()} className="filter-option" onClick={() => { onChange(search.trim()); setOpen(false); }}>
          <span>查找「{search.trim()}」</span>
        </button>}
        {filtered.map(v => <button key={v} type="button" role="option" aria-selected={value === v} className={`filter-option ${value === v ? 'is-selected' : ''}`} onClick={() => { onChange(v); setOpen(false); }}>
          <span>{labels[v] || v}</span>{value === v && <span aria-hidden="true">✓</span>}
        </button>)}
      </div>
    </div>}
  </div>;
}

function Modal({ title, close, children, wide = false }: { title: string; close: () => void; children: ReactNode; wide?: boolean }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const onClose = useRef(close);
  onClose.current = close;
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose.current();
      if (e.key !== 'Tab') return;
      const items = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary') || []).filter(item => item.getClientRects().length > 0);
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', key);
    return () => { document.removeEventListener('keydown', key); previous?.focus(); };
  }, []);
  return <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) close(); }}><section ref={dialogRef} className={`modal ${wide ? 'modal-wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}>
    <header className="modal-header"><h2>{title}</h2><button className="icon-button" onClick={close} ref={closeRef} aria-label="关闭">×</button></header>{children}
  </section></div>;
}

function Login({ admin = false, local = false, onSuccess }: { admin?: boolean; local?: boolean; onSuccess: () => void }) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError('');
    try { await post('auth/login', { code, admin }); onSuccess(); } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  return <form className="login-form" onSubmit={submit}>
    <div className="login-emblem" aria-hidden="true">✦</div>
    <h1>{admin ? '管理档案' : '进入星轨档案'}</h1>
    <p>{local ? '本地会话已结束，请重新双击启动程序打开页面。' : admin ? '输入管理员凭据以配置数据与搜索服务。' : '输入分享者提供的访问码，继续查找故事。'}</p>
    {!local && <><label className="field"><span>{admin ? '管理员凭据' : '访问码'}</span><input autoFocus type="password" autoComplete="current-password" value={code} onChange={e => setCode(e.target.value)} required placeholder={admin ? '输入管理员凭据' : '输入访问码'} /></label>
      <button className="primary full-width" disabled={busy || !code}>{busy ? '正在验证…' : '进入档案'}<span aria-hidden="true">→</span></button></>}
    {error && <p className="error-message" role="alert">{error}</p>}
  </form>;
}

function VisitorKeyForm({ initialKey, model, save, clear }: { initialKey: string; model: string; save: (key: string) => void; clear: () => void }) {
  const [key, setKey] = useState(initialKey);
  const [error, setError] = useState('');
  return <form className="visitor-key-form" onSubmit={event => {
    event.preventDefault(); setError('');
    try { save(key); } catch (failure) { setError(errorText(failure)); }
  }}>
    <p>语义搜索使用自己的硅基流动 API Key。<a href="https://cloud.siliconflow.cn" target="_blank" rel="noreferrer">注册或获取密钥 ↗</a></p>
    <p>{model === 'BAAI/bge-m3' ? '当前使用硅基流动免费模型 BAAI/bge-m3，政策以服务商为准。' : `当前索引模型为 ${model}。`}<a href="https://www.siliconflow.cn/pricing" target="_blank" rel="noreferrer">查看模型价格 ↗</a></p>
    <label className="field"><span>硅基流动 API Key</span><input type="password" autoComplete="new-password" value={key} onChange={event => setKey(event.target.value)} placeholder="粘贴自己的 API Key" aria-label="语义搜索 API Key" maxLength={4096} /></label>
    <p className="key-storage-note">密钥保存在当前浏览器，可随时修改或清除。查询优先直连硅基流动，直连失败时由本站服务器仅在内存中代发本次查询。</p>
    <div className="form-actions"><button className="primary">保存密钥</button><button className="secondary" type="button" onClick={() => { try { clear(); } catch (failure) { setError(errorText(failure)); } }}>清除密钥</button></div>
    {error && <p className="error-message" role="alert">{error}</p>}
  </form>;
}

export default function App() {
  const initial = useRef(readRoute());
  const [status, setStatus] = useState<Status | null>(null);
  const readable = Boolean(status?.authenticated || (status?.mode === 'server' && status.public_readonly));
  const [bootError, setBootError] = useState('');
  const [catalog, setCatalog] = useState<Catalog>(emptyCatalog);
  const [query, setQuery] = useState(initial.current.query);
  const [searchedQuery, setSearchedQuery] = useState(initial.current.query);
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  const [mode, setMode] = useState<SearchMode>('text');
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState('');
  const [selected, setSelected] = useState<Result | null>(null);
  const [detail, setDetail] = useState<MissionResponse | null>(null);
  const [reading, setReading] = useState(false);
  const [readerError, setReaderError] = useState('');
  const [showSettings, setShowSettings] = useState(false);
  const [showAdminLogin, setShowAdminLogin] = useState(false);
  const [showBookmarks, setShowBookmarks] = useState(false);
  const [showVisitorKey, setShowVisitorKey] = useState(false);
  const [visitorKey, setVisitorKey] = useState(storedVisitorKey);
  const [bookmarks, setBookmarks] = useState<Bookmark[]>(() => { try { return JSON.parse(localStorage.getItem('hsr-bookmarks') || '[]'); } catch { return []; } });
  const [theme, setTheme] = useState(document.documentElement.dataset.theme || 'dark');
  const [toast, setToast] = useState('');
  const searchId = useRef(0);
  const readerId = useRef(0);
  const searchController = useRef<AbortController | null>(null);
  const readerController = useRef<AbortController | null>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const booted = useRef(false);
  const activeSearch = useRef({ filters: emptyFilters, mode: 'text' as SearchMode, query: '' });
  const activeSemantic = useRef<SemanticContext | null>(null);
  const semanticClient = useRef(createSemanticSearchClient());

  function updateVisitorKey(value: string) {
    const key = saveVisitorKey(localStorage, value);
    semanticClient.current.clear();
    searchId.current++;
    setSearching(false);
    setVisitorKey(key);
    setShowVisitorKey(false);
    setToast(key ? '语义搜索密钥已保存，可开始搜索' : '已清除当前浏览器的语义搜索密钥');
  }

  function chooseMode(next: SearchMode) {
    setMode(next);
    if (next === 'hybrid' && status && indexReady(status.embedding) && !visitorKey && !serverKeyReady(status.embedding)) setShowVisitorKey(true);
  }

  const refresh = useCallback(async () => {
    const result = await request<Status>('status');
    setStatus(previous => JSON.stringify(previous) === JSON.stringify(result) ? previous : result);
    return result;
  }, []);
  const clearReader = useCallback(() => {
    readerId.current++;
    readerController.current?.abort();
    setSelected(null);
    setDetail(null);
    setReading(false);
    setReaderError('');
  }, []);
  useEffect(() => () => {
    searchId.current++;
    readerId.current++;
    searchController.current?.abort();
    readerController.current?.abort();
  }, []);
  useEffect(() => {
    let alive = true;
    async function boot() {
      try {
        await bootstrapSession();
        if (alive) await refresh();
      } catch (err) { if (alive) setBootError(errorText(err)); }
    }
    void boot();
    const timer = setInterval(() => { if (alive) void refresh().catch(() => {}); }, 6000);
    return () => { alive = false; clearInterval(timer); };
  }, [refresh]);

  useEffect(() => {
    if (!readable) return;
    let alive = true;
    request<Catalog>('catalog').then(data => { if (alive) setCatalog({ ...emptyCatalog, ...data }); }).catch(() => {});
    return () => { alive = false; };
  }, [readable, status?.data.snapshot]);

  const selectResult = useCallback(async (result: Result, snapshot?: string, updateRoute = true) => {
    const generation = ++readerId.current;
    readerController.current?.abort();
    const controller = new AbortController();
    readerController.current = controller;
    setSelected(result); setDetail(null); setReaderError('');
    if (updateRoute) {
      const p = new URLSearchParams();
      if (searchedQuery) p.set('q', searchedQuery);
      if (result.mission_id) p.set('mission', result.mission_id);
      if (result.line_id) p.set('line', result.line_id);
      if (result.script) p.set('script', result.script);
      if (snapshot) p.set('snapshot', snapshot);
      history.replaceState(null, '', `#/search?${p.toString()}`);
    }
    if (!result.mission_id) { setReading(false); return; }
    setReading(true);
    const p = new URLSearchParams();
    if (result.line_id) p.set('line_id', result.line_id);
    if (snapshot) p.set('snapshot', snapshot);
    try { const value = await request<MissionResponse>(`missions/${encodeURIComponent(result.mission_id)}?${p}`, { signal: controller.signal }); if (generation === readerId.current) setDetail(value); }
    catch (err) { if (generation === readerId.current) setReaderError(errorText(err)); }
    finally { if (generation === readerId.current) setReading(false); }
  }, [searchedQuery]);

  const runSearch = useCallback(async (value: string, offset = 0, overrideFilters?: Filters) => {
    const activeFilters = overrideFilters ?? filters;
    const hasFilter = Object.values(activeFilters).some(Boolean);
    if (!value.trim() && !hasFilter) { searchInput.current?.focus(); return; }
    const criteria = offset > 0 ? activeSearch.current : { query: value.trim(), filters: { ...activeFilters }, mode };
    const semanticContext = offset > 0 ? activeSemantic.current : status ? { snapshot: status.data.snapshot, embedding: status.embedding } : null;
    if (criteria.mode === 'hybrid' && criteria.query && semanticContext && indexReady(semanticContext.embedding) && !visitorKey && !serverKeyReady(semanticContext.embedding)) {
      setShowVisitorKey(true);
      return;
    }
    const generation = ++searchId.current;
    searchController.current?.abort();
    const controller = new AbortController();
    searchController.current = controller;
    setSearching(true); setSearchError('');
    try {
      const prepared = criteria.mode === 'hybrid' && criteria.query && semanticContext ? await semanticClient.current.prepare(criteria.query, visitorKey, semanticContext) : {};
      if (generation !== searchId.current) return;
      const data = await searchRequest<SearchResponse>({ ...criteria, limit: 30, offset, snapshot: offset > 0 ? response?.snapshot : semanticContext?.snapshot || undefined }, prepared, controller.signal);
      if (generation !== searchId.current) return;
      setSearchedQuery(criteria.query);
      setResponse(old => offset > 0 && old ? { ...data, results: [...old.results, ...data.results] } : data);
      if (offset === 0) {
        activeSearch.current = criteria;
        activeSemantic.current = semanticContext;
        clearReader();
        history.replaceState(null, '', criteria.query ? `#/search?q=${encodeURIComponent(criteria.query)}` : '#/search');
      }
    } catch (err) { if (generation === searchId.current) setSearchError(errorText(err)); }
    finally { if (generation === searchId.current) setSearching(false); }
  }, [filters, mode, response?.snapshot, status, visitorKey, clearReader]);

  const applyFilters = useCallback((nextFilters: Filters) => {
    setFilters(nextFilters);
    if (query.trim() || Object.values(nextFilters).some(Boolean)) {
      void runSearch(query, 0, nextFilters);
    } else {
      setResponse(null);
      searchId.current++;
      searchController.current?.abort();
      setSearching(false);
      clearReader();
    }
  }, [query, runSearch, clearReader]);

  useEffect(() => {
    const synchronizeKey = (event: StorageEvent) => {
      if (event.key !== VISITOR_KEY_STORAGE && event.key !== null) return;
      semanticClient.current.clear();
      searchId.current++;
      setSearching(false);
      setVisitorKey(storedVisitorKey());
    };
    window.addEventListener('storage', synchronizeKey);
    return () => window.removeEventListener('storage', synchronizeKey);
  }, []);

  useEffect(() => {
    if (!readable || booted.current) return;
    booted.current = true;
    const route = initial.current;
    if (route.mission) {
      const result: Result = { chunk_id: 0, mission_id: route.mission, mission_name: '', chapter: '', world: '', version: '', version_status: 'unknown', speaker: '', text: '', line_id: route.line, script: route.script, branch: '', match_type: '', score: 0 };
      void selectResult(result, route.snapshot, false);
    } else if (route.query) void runSearch(route.query);
  }, [readable, runSearch, selectResult]);

  useEffect(() => { document.documentElement.dataset.theme = theme; try { localStorage.setItem('hsr-theme', theme); } catch {} }, [theme]);
  useEffect(() => { try { localStorage.setItem('hsr-bookmarks', JSON.stringify(bookmarks)); } catch { setToast('浏览器存储空间不足，请导出收藏备份'); } }, [bookmarks]);
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(''), 3500); return () => clearTimeout(timer); }, [toast]);
  useEffect(() => { const key = (event: KeyboardEvent) => { if ((event.ctrlKey || event.metaKey) && event.key === 'k') { event.preventDefault(); searchInput.current?.focus(); } }; document.addEventListener('keydown', key); return () => document.removeEventListener('keydown', key); }, []);

  async function copy(text: string) { try { await navigator.clipboard.writeText(text); setToast('已复制到剪贴板'); } catch { setToast('无法访问剪贴板，请手动选择文字复制'); } }
  function share(line?: DialogueLine) {
    if (!selected) return;
    const params = new URLSearchParams({ mission: selected.mission_id, line: line?.id || selected.line_id, script: line?.script || selected.script });
    if (searchedQuery) params.set('q', searchedQuery);
    if (detail?.snapshot || response?.snapshot) params.set('snapshot', detail?.snapshot || response!.snapshot);
    void copy(`${location.origin}${location.pathname}#/search?${params}`);
  }
  const bookmarkKey = selected ? `${selected.mission_id}:${selected.line_id}:${selected.script}` : '';
  const bookmarked = bookmarks.some(b => b.key === bookmarkKey);
  function toggleBookmark() {
    if (!selected) return;
    const result = { ...selected, mission_name: detail?.mission.name || selected.mission_name, chapter: detail?.mission.chapter || selected.chapter };
    setBookmarks(old => bookmarked ? old.filter(b => b.key !== bookmarkKey) : [{ key: bookmarkKey, result, query: searchedQuery, saved_at: new Date().toISOString(), snapshot: detail?.snapshot || response?.snapshot }, ...old]);
    setToast(bookmarked ? '已取消收藏' : '已收藏到当前浏览器');
  }
  async function saveVersion(version: string, source: string, state: string) {
    if (!selected) return;
    const saved = await request<{ snapshot: string }>(`admin/missions/${encodeURIComponent(selected.mission_id)}`, { method: 'PUT', body: JSON.stringify({ version, source, status: state }) });
    setResponse(old => old ? { ...old, snapshot: saved.snapshot, results: old.results.map(r => r.mission_id === selected.mission_id ? { ...r, version, version_status: state } : r) } : old);
    await selectResult({ ...selected, version, version_status: state }, saved.snapshot);
    setToast('版本标注已保存'); await refresh();
  }
  async function moreContext() {
    if (!selected || !detail || reading) return;
    const generation = readerId.current;
    setReading(true);
    const p = new URLSearchParams({ offset: String((detail.offset || 0) + detail.lines.length), limit: '1000' });
    if (detail.snapshot || response?.snapshot) p.set('snapshot', detail.snapshot || response!.snapshot);
    const controller = new AbortController();
    readerController.current = controller;
    try { const extra = await request<MissionResponse>(`missions/${encodeURIComponent(selected.mission_id)}?${p}`, { signal: controller.signal }); if (generation === readerId.current) setDetail(old => {
        if (!old) return extra;
        const edges = new Map([...old.edges, ...extra.edges].map(edge => [JSON.stringify(edge), edge]));
        return { ...extra, offset: old.offset, lines: [...old.lines, ...extra.lines], edges: [...edges.values()] };
      }); }
    catch (err) { if (generation === readerId.current) setToast(errorText(err)); } finally { if (generation === readerId.current) setReading(false); }
  }

  if (!status) return <div className="boot-screen"><div className="brand-mark">✦</div><h1>星轨档案</h1><p>{bootError || '正在打开档案…'}</p>{bootError && <button className="secondary" onClick={() => location.reload()}>重新连接</button>}</div>;
  if (!readable) return <div className="login-screen"><div className="login-stars" aria-hidden="true" /><Login local={status.mode === 'local'} onSuccess={() => { void refresh(); }} /><div className="login-footer">星轨档案 · 崩坏：星穹铁道剧情检索</div></div>;

  return <div className={`app-shell ${selected ? 'has-selection' : ''}`}>
    <header className="app-header"><a className="brand" href="#/" onClick={e => { e.preventDefault(); searchId.current++; searchController.current?.abort(); setSearching(false); clearReader(); setResponse(null); setQuery(''); setSearchedQuery(''); history.replaceState(null, '', '#/'); }}><span className="brand-mark" aria-hidden="true">✦</span><span>星轨档案<small>STARRAIL STORY ARCHIVE</small></span></a>
      <div className="header-actions"><span className="connection-label"><span className="status-dot" />{status.mode === 'local' ? '本地档案' : '在线档案'}</span><button className="header-button" aria-label="打开收藏" title="收藏" onClick={() => setShowBookmarks(true)}><span aria-hidden="true">☆</span><span>收藏</span>{bookmarks.length > 0 && <small>{bookmarks.length}</small>}</button><button className="icon-button" onClick={() => setTheme(old => old === 'dark' ? 'light' : 'dark')} aria-label={theme === 'dark' ? '切换浅色主题' : '切换深色主题'} title="切换主题">{theme === 'dark' ? '☼' : '☾'}</button>{status.mode === 'local' && <button className="header-button" aria-label="管理档案" title="管理" onClick={() => setShowSettings(true)}><span aria-hidden="true">⚙</span><span>管理</span></button>}{status.mode === 'server' && !status.public_readonly && <button className="header-button logout-button" title="退出登录" onClick={() => { void post('auth/logout').then(() => refresh()).catch(err => setToast(errorText(err))); }}><span aria-hidden="true">↪</span><span>退出</span></button>}</div>
    </header>
    <section className="search-section">
      <form className="search-form" onSubmit={e => { e.preventDefault(); void runSearch(query); }}>
        <span className="search-symbol" aria-hidden="true">⌕</span>
        <input ref={searchInput} value={query} onChange={e => setQuery(e.target.value)} placeholder={mode === 'hybrid' ? '描述你记得的人物、事件或情节…' : mode === 'fuzzy' ? '输入记得的大致文字，尝试近似匹配…' : '输入台词、任务名或角色（支持空格分词与角色别名）…'} aria-label="搜索剧情" maxLength={1000} />
        {query && <button type="button" className="icon-button clear-query" onClick={() => { setQuery(''); searchInput.current?.focus(); }} aria-label="清空输入" title="清空">×</button>}
        <kbd>Ctrl K</kbd>
        <button className="primary search-submit" disabled={searching || (!query.trim() && !Object.values(filters).some(Boolean))}>{searching ? <span className="spinner" /> : '搜索'}<span aria-hidden="true">↗</span></button>
      </form>
      <div className="search-toolbar">
        <div className="search-methods">
          <div className="mode-tabs" role="group" aria-label="搜索方式">
            <button type="button" aria-pressed={mode === 'text'} onClick={() => chooseMode('text')}>关键词</button>
            <button type="button" aria-pressed={mode === 'hybrid'} disabled={!indexReady(status.embedding)} title={indexReady(status.embedding) ? '按情节描述搜索' : '管理员建立完整语义索引后可用'} onClick={() => chooseMode('hybrid')}>情节描述</button>
            <button type="button" aria-pressed={mode === 'fuzzy'} onClick={() => chooseMode('fuzzy')}>近似文字</button>
          </div>
          <button type="button" className="text-button semantic-key-link" onClick={() => setShowVisitorKey(true)}>🔑 语义密钥{visitorKey && <span>已配置</span>}</button>
        </div>
        <div className="search-options">
          <div className="filter-row">
            <SelectField label="世界" value={filters.world} values={catalog.worlds} onChange={v => applyFilters({ ...filters, world: v })} />
            <SelectField label="版本" value={filters.version} values={catalog.versions} onChange={v => applyFilters({ ...filters, version: v })} />
            <SelectField label="任务类型" value={filters.type} values={catalog.types} onChange={v => applyFilters({ ...filters, type: v })} />
            <SelectField label="角色" value={filters.character} values={catalog.characters} searchable onChange={v => applyFilters({ ...filters, character: v })} />
            {Object.values(filters).some(Boolean) && <button type="button" className="text-button clear-filters" onClick={() => applyFilters(emptyFilters)}>重置筛选</button>}
          </div>
        </div>
      </div>
    </section>
    <main className="workspace"><section className="results-panel" aria-label="搜索结果"><div className="results-heading"><h2>{response ? response.search_mode === 'fuzzy' ? '近似文字结果' : response.search_mode === 'hybrid' ? '情节描述结果' : '关键词结果' : '剧情检索'}</h2>{response ? <span>{response.truncated ? '优先展示 ' : ''}{formatNumber(response.total)} 条相关结果{response.elapsed_ms != null && <small> · {(response.elapsed_ms / 1000).toFixed(2)} 秒</small>}</span> : <span>{formatNumber(status.data.stats?.narrative_lines ?? status.data.stats?.lines)} 条对白记录</span>}</div>
      <div className="results-scroll" aria-busy={searching} aria-live="polite">
        {searchError && <div className="inline-alert" role="alert">{searchError}</div>}
        {response?.notice && <div className="search-notice">{response.notice}</div>}
        {!status.data.ready ? <Empty title="准备你的第一份档案"><p>导入剧情数据后，就可以搜索台词和阅读上下文。</p>{status.admin && <button className="secondary" onClick={() => setShowSettings(true)}>导入数据 →</button>}</Empty> : !response ? <div className="search-welcome"><div className="welcome-orbit" aria-hidden="true"><span>✧</span></div><h2>从你记得的地方开始</h2><p>一句台词、一个名字，<br />或某次旅途里的模糊记忆。</p><div className="suggestions"><span>试着搜索</span>{['生命因何而沉睡', '三月七 星穹列车', '流萤', '老杨'].map(value => <button key={value} onClick={() => { setQuery(value); void runSearch(value); }}>{value}<span>↗</span></button>)}</div></div> : response.results.length === 0 ? <Empty title="还没有找到相关剧情"><p>尝试缩短台词、使用角色名，或减少筛选条件。</p></Empty> : <div className="result-list">{response.results.map((result, index) => {
          const title = cleanMissionName(result.mission_name, result.chapter, result.world);
          return <button className={`result-card ${selected?.line_id === result.line_id && selected?.script === result.script ? 'selected' : ''}`} key={`${result.chunk_id}:${result.line_id}:${index}`} onClick={() => void selectResult(result, response.snapshot)}>
          <div className="result-topline">
            <div className="result-badges">
              {result.type && <span className={`mission-badge type-${result.type.toLowerCase()}`}>{labels[result.type] || result.type}</span>}
              <span className="world-badge">{result.world || '剧情对白'}</span>
              {result.kind?.includes('option') && <span className="option-badge">❖ 玩家选项</span>}
            </div>
            <span className={!result.version ? 'pending-version' : 'version-tag'}>{versionText(result.version, result.version_status)}</span>
          </div>
          <h3>{title}{result.chapter && result.chapter !== title && <small className="result-chapter"> · {result.chapter}</small>}</h3>
          <p className="result-text"><strong>{result.kind?.includes("option") ? "❖ 开拓者选项" : speakerName(result.speaker)}</strong><span><Highlight text={result.text} query={searchedQuery} /></span></p>
          <div className="result-bottom"><span>{matchLabel(result)}</span><span className="result-open">阅读上下文 <span>→</span></span></div>
        </button>; })}{response.results.length < response.total && <button className="secondary load-results" disabled={searching} onClick={() => void runSearch(searchedQuery, response.results.length)}>{searching ? '加载中…' : '加载更多结果'}</button>}</div>}
      </div><footer className="results-footer"><a href="https://github.com/JahsehXL/hsr-story-search" target="_blank" rel="noreferrer">GitHub 仓库 ↗</a></footer>
    </section>
    <Reader selected={selected} detail={detail} busy={reading} query={searchedQuery} error={readerError} isAdmin={status.admin} bookmarked={bookmarked} onBookmark={toggleBookmark} onCopy={text => void copy(text)} onShare={share} onBack={clearReader} onVersionSave={saveVersion} onMore={() => void moreContext()} />
    </main>
    {showVisitorKey && <Modal title="配置语义搜索密钥" close={() => setShowVisitorKey(false)}><VisitorKeyForm initialKey={visitorKey} model={status.embedding.index_model || 'BAAI/bge-m3'} save={updateVisitorKey} clear={() => updateVisitorKey('')} /></Modal>}
    {showAdminLogin && <Modal title="管理员登录" close={() => setShowAdminLogin(false)}><Login admin onSuccess={() => { void refresh().then(() => { setShowAdminLogin(false); setShowSettings(true); }); }} /></Modal>}
    {showSettings && <Modal title="档案管理" close={() => setShowSettings(false)} wide><Admin status={status} onRefresh={() => refresh()} onToast={setToast} /></Modal>}
    {showBookmarks && <Modal title="收藏的剧情" close={() => setShowBookmarks(false)}><div className="bookmark-modal"><div className="bookmark-info"><p>保存在当前浏览器，可导出备份。</p><button className="text-button" disabled={!bookmarks.length} onClick={() => downloadJson('星轨档案-收藏.json', bookmarks)}>导出收藏 ↗</button></div>{bookmarks.length ? bookmarks.map(bookmark => <div className="bookmark-row" key={bookmark.key}><button onClick={() => { setSearchedQuery(bookmark.query); setShowBookmarks(false); void selectResult(bookmark.result, bookmark.snapshot); }}><h3>{cleanMissionName(bookmark.result.mission_name, bookmark.result.chapter, bookmark.result.world)}</h3><p>{bookmark.result.text || '打开收藏位置'}</p><small>{bookmark.result.chapter}</small></button><button className="icon-button" onClick={() => setBookmarks(old => old.filter(b => b.key !== bookmark.key))} aria-label="移除收藏" title="移除收藏">×</button></div>) : <Empty symbol="☆" title="还没有收藏"><p>阅读剧情时，点击标题旁的星标即可收藏。</p></Empty>}</div></Modal>}
    {toast && <div className="toast" role="status">{toast}</div>}
  </div>;
}

function Admin({ status, onRefresh, onToast }: { status: Status; onRefresh: () => Promise<Status>; onToast: (message: string) => void }) {
  const [tab, setTab] = useState<'api' | 'data' | 'jobs'>('api');
  const [settings, setSettings] = useState<Settings | null>(null);
  const [apiKey, setApiKey] = useState('');
  const [extra, setExtra] = useState('{}');
  const [source, setSource] = useState('');
  const [jobs, setJobs] = useState<Job[]>(status.jobs || []);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [testResult, setTestResult] = useState('');
  const [upload, setUpload] = useState<File | null>(null);
  const [packageFile, setPackageFile] = useState<File | null>(null);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [confirmIndex, setConfirmIndex] = useState(false);
  useEffect(() => {
    let alive = true;
    request<Settings>('admin/settings').then(data => { if (alive) { setSettings(data); setExtra(JSON.stringify(data.embedding.extra_body || {}, null, 2)); } }).catch(err => { if (alive) setError(errorText(err)); });
    async function poll() { try { const data = await request<{ jobs: Job[] }>('admin/jobs'); if (alive) setJobs(data.jobs || []); } catch {} }
    void poll(); const timer = setInterval(() => void poll(), 2500);
    return () => { alive = false; clearInterval(timer); };
  }, []);

  async function action(name: string, run: () => Promise<unknown>) {
    setBusy(name); setError('');
    try { await run(); await onRefresh(); } catch (err) { setError(errorText(err)); } finally { setBusy(''); }
  }
  async function saveSettings() {
    if (!settings) return;
    const extraBody: unknown = JSON.parse(extra);
    if (!extraBody || typeof extraBody !== 'object' || Array.isArray(extraBody)) throw new Error('额外请求参数应为 JSON 对象');
    const value = { ...settings, embedding: { ...settings.embedding, extra_body: extraBody, ...(apiKey ? { api_key: apiKey } : {}) } };
    await request('admin/settings', { method: 'PUT', body: JSON.stringify(value) });
    setApiKey('');
    setSettings(await request<Settings>('admin/settings'));
  }
  async function startJob(kind: string, args: Record<string, unknown> = {}) {
    await post('admin/jobs', { kind, ...args });
    const data = await request<{ jobs: Job[] }>('admin/jobs'); setJobs(data.jobs || []); setTab('jobs');
    onToast('任务已提交，可在任务列表查看进度');
  }
  async function uploadJob(file: File, kind: string) {
    setUploadProgress(0);
    type UploadState = { upload_id: string; offset: number; size: number; complete: boolean };
    let state = await post<UploadState>('admin/upload/start', { size: file.size });
    let failures = 0;
    while (!state.complete) {
      try {
        state = await request<UploadState>(`admin/upload/${state.upload_id}?offset=${state.offset}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' },
          body: file.slice(state.offset, state.offset + 8 * 1024 * 1024),
        });
        failures = 0;
        setUploadProgress(Math.round(state.offset / file.size * 100));
      } catch (error) {
        if (++failures >= 3) throw error;
        await new Promise(resolve => setTimeout(resolve, failures * 1000));
        state = await request<UploadState>(`admin/upload/${state.upload_id}`);
      }
    }
    await startJob(kind, { upload_id: state.upload_id });
  }
  function updateEmbedding(key: string, value: unknown) { setSettings(old => old ? { ...old, embedding: { ...old.embedding, [key]: value } } : old); }
  const running = jobs.some(job => ['queued', 'running', 'paused'].includes(job.status));
  return <div className="admin-shell"><nav className="admin-tabs" aria-label="管理分类">{([['api', '搜索 API'], ['data', '数据与更新'], ['jobs', '后台任务']] as const).map(([id, label]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{label}{id === 'jobs' && running && <span className="status-dot" />}</button>)}</nav>
    <div className="admin-content">{error && <div className="inline-alert" role="alert">{error}</div>}
      {tab === 'api' && (settings ? <>
        <div className="section-heading"><div><h3>嵌入模型接口</h3><p>用于理解情节描述，剧情索引与查询应使用配套模型。</p></div><span className={`state-pill ${status.embedding.ready && status.embedding.compatible ? 'ready' : ''}`}>{status.embedding.ready && status.embedding.compatible ? '索引可用' : '待建立索引'}</span></div>
        <p className="mode-description">推荐硅基流动 BAAI/bge-m3，原生 1024 维，每批 32 条。维度留空自动检测，查询和文档前缀均留空。填写自己的服务商密钥后保存。已有自定义配置会继续保留。</p>
        {settings.recommended_embedding && <div className="form-actions"><button className="secondary" disabled={!!busy} onClick={() => { setSettings({ ...settings, embedding: { ...settings.embedding, ...settings.recommended_embedding } }); setExtra(JSON.stringify(settings.recommended_embedding?.extra_body || {}, null, 2)); onToast('已填入国内推荐配置，请确认该服务的密钥并保存'); }}>使用国内推荐配置</button></div>}
        <form onSubmit={e => { e.preventDefault(); void action('save', async () => { await saveSettings(); onToast('API 设置已保存'); }); }}>
          <div className="form-grid"><label className="field span-two"><span>API 地址</span><input type="url" value={settings.embedding.base_url || ''} onChange={e => updateEmbedding('base_url', e.target.value)} placeholder="https://your-provider.example/v1" /></label>
            <label className="field span-two"><span>API 密钥{settings.embedding.api_key_set && <small>已配置，留空保留</small>}</span><input type="password" autoComplete="new-password" value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder={settings.embedding.api_key_set ? '•••••••• · 输入新密钥可替换' : '输入服务商提供的 API Key'} /></label>
            <label className="field"><span>模型名称</span><input value={settings.embedding.model || ''} onChange={e => updateEmbedding('model', e.target.value)} placeholder="服务商的 embedding 模型 ID" /></label>
            <label className="field"><span>向量维度<small>可留空自动检测</small></span><input type="number" min="1" max="32768" value={settings.embedding.dimensions || ''} onChange={e => updateEmbedding('dimensions', e.target.value ? Number(e.target.value) : 0)} placeholder="自动" /></label>
          </div>
          <details className="advanced-settings"><summary>请求与性能设置</summary><div className="form-grid">
            <label className="field span-two"><span>查询前缀或指令</span><textarea rows={2} value={settings.embedding.query_prefix || ''} onChange={e => updateEmbedding('query_prefix', e.target.value)} placeholder="按模型文档填写，可留空" /></label>
            <label className="field span-two"><span>剧情文本前缀或指令</span><textarea rows={2} value={settings.embedding.document_prefix || ''} onChange={e => updateEmbedding('document_prefix', e.target.value)} placeholder="按模型文档填写，可留空" /></label>
            <label className="field"><span>每批片段数</span><input type="number" min="1" max="128" value={settings.embedding.batch_size} onChange={e => updateEmbedding('batch_size', Number(e.target.value))} /></label>
            <label className="field"><span>并发请求数</span><input type="number" min="1" max="4" value={settings.embedding.concurrency} onChange={e => updateEmbedding('concurrency', Number(e.target.value))} /></label>
            <label className="field"><span>超时秒数</span><input type="number" min="5" max="300" value={settings.embedding.timeout} onChange={e => updateEmbedding('timeout', Number(e.target.value))} /></label>
            <label className="field"><span>搜索次数 / 分钟</span><input type="number" min="1" value={settings.limits.search_per_minute} onChange={e => setSettings({ ...settings, limits: { ...settings.limits, search_per_minute: Number(e.target.value) } })} /></label>
            <label className="field"><span>每日 API 调用上限<small>0 表示不限制</small></span><input type="number" min="0" value={settings.limits.daily_api_calls} onChange={e => setSettings({ ...settings, limits: { ...settings.limits, daily_api_calls: Number(e.target.value) } })} /></label>
            <label className="field span-two"><span>额外模型参数（JSON）</span><textarea className="code-input" rows={4} value={extra} onChange={e => setExtra(e.target.value)} spellCheck={false} /><small>模型参数会随索引共享，认证信息请填写在 API 密钥栏。</small></label>
          </div></details>
          <div className="form-actions"><button className="primary" disabled={!!busy}>{busy === 'save' ? '保存中…' : '保存设置'}</button><button type="button" className="secondary" disabled={!!busy || !settings.embedding.base_url || !settings.embedding.model} onClick={() => void action('test', async () => { await saveSettings(); const result = await post<{ ok: boolean; dimension: number; elapsed_ms: number; model: string }>('admin/embedding/test', { text: '沿着星轨，寻找故事的来处。' }); setTestResult(`连接成功 · ${result.model} · ${result.dimension} 维 · ${(result.elapsed_ms / 1000).toFixed(2)} 秒`); })}>{busy === 'test' ? '测试中…' : '保存并测试连接'}</button></div>
        </form>{testResult && <div className="success-message" role="status">{testResult}</div>}
        <section className="admin-section index-section"><h3>语义索引</h3><p>已有 {formatNumber(status.embedding.indexed)} / {formatNumber(status.embedding.total || status.data.stats.chunks)} 个片段。首次计算会将剧情片段发送至所配置的 API，并产生相应费用。</p>
          <p>共享数据包中的向量可直接复用。查询需使用相同模型、维度和前缀，API 地址与密钥可自行配置。</p>
          {status.embedding.index_identity?.model && <div className="form-actions"><button className="secondary" disabled={!!busy} onClick={() => {
            const identity = status.embedding.index_identity!;
            setSettings({ ...settings, embedding: { ...settings.embedding, model: identity.model, dimensions: status.embedding.index_request_dimensions ?? identity.dimension, query_prefix: identity.query_prefix || '', document_prefix: identity.document_prefix || '', extra_body: identity.extra_body || {} } });
            setExtra(JSON.stringify(identity.extra_body || {}, null, 2));
            onToast('已填入数据包模型参数，请确认 API 支持该模型并保存');
          }}>采用数据包的模型参数</button><span className="mode-description">{status.embedding.index_model}</span></div>}
          {!status.embedding.compatible && status.embedding.indexed > 0 && <p className="warning-text">当前参数与索引不匹配。可采用数据包模型参数，或为新模型建立索引。</p>}
          {confirmIndex ? <div className="confirm-box"><p>确认使用当前已保存的 API 设置，计算未缓存的剧情片段？</p><div className="form-actions"><button className="primary" disabled={!!busy} onClick={() => void action('index', async () => { await saveSettings(); await startJob('build_index'); setConfirmIndex(false); })}>确认开始计算</button><button className="secondary" onClick={() => setConfirmIndex(false)}>返回</button></div></div> : <button className="secondary" disabled={!!busy || !status.data.ready || running || !settings.embedding.model || !settings.embedding.base_url} onClick={() => setConfirmIndex(true)}>建立 / 更新语义索引 →</button>}
        </section>
      </> : <p className="loading-copy">正在读取设置…</p>)}
      {tab === 'data' && <>
        <div className="section-heading"><div><h3>剧情数据</h3><p>当前快照 {status.data.snapshot || '尚未导入'}</p></div><span className="state-pill">{status.mode === 'local' ? '本地存储' : '服务器存储'}</span></div>
        <div className="data-stats">{[['任务', status.data.stats.missions], ['对白记录', status.data.stats.lines], ['检索片段', status.data.stats.chunks]].map(([label, value]) => <div key={String(label)}><strong>{formatNumber(value)}</strong><span>{label}</span></div>)}</div>
        <section className="admin-section"><h3>导入原始数据</h3><p>支持包含 TextMap、ExcelOutput、Story 和 Config 的数据目录或 ZIP。</p>
          <form onSubmit={e => { e.preventDefault(); void action('directory', () => startJob('import_directory', { source })); }}><label className="field"><span>{status.mode === 'local' ? '本机数据目录' : '服务器数据目录'}</span><div className="input-action"><input value={source} onChange={e => setSource(e.target.value)} placeholder={status.mode === 'local' ? '例如 D:\\TurnBasedGameData-main' : '/data/source/TurnBasedGameData-main'} required /><button className="secondary" disabled={!!busy || running || !source.trim()}>导入目录</button></div></label></form>
          <div className="upload-row"><label className="file-input"><input type="file" accept=".zip" onChange={e => setUpload(e.target.files?.[0] || null)} /><span>{upload ? upload.name : '选择原始数据 ZIP'}</span></label><button className="secondary" disabled={!upload || !!busy || running} onClick={() => upload && void action('upload', () => uploadJob(upload, 'import_upload'))}>{busy === 'upload' ? `上传中 ${uploadProgress}%` : '上传并导入'}</button></div>
        </section>
        {settings && <section className="admin-section"><h3>从社区数据仓库更新</h3><p>下载固定提交的快照后解析，并复用已有的片段向量缓存。</p><label className="field"><span>GitHub 仓库</span><input value={settings.updates.repository} onChange={e => setSettings({ ...settings, updates: { ...settings.updates, repository: e.target.value } })} placeholder="DimbreathBot/TurnBasedGameData" /></label><label className="field"><span>GitHub 镜像加速地址<small>留空直连</small></span><input type="url" value={settings.updates.github_mirror || ''} onChange={e => setSettings({ ...settings, updates: { ...settings.updates, github_mirror: e.target.value } })} placeholder="https://your-github-mirror.example" /></label><p>支持 gh-proxy 完整 URL 前缀。优先从镜像下载固定提交 ZIP，失败后尝试 GitHub 直连。提交查询先直连，失败后尝试支持 API 转发的镜像。</p><button className="secondary" disabled={!!busy || running || !settings.updates.repository} onClick={() => void action('source', async () => { await saveSettings(); await startJob('download_source'); })}>{busy === 'source' ? '提交中…' : '拉取并导入最新快照 →'}</button></section>}
        <section className="admin-section"><h3>预处理数据包</h3><p>导入已整理的数据库与索引。模型配置匹配时可以直接使用语义搜索。</p><div className="upload-row"><label className="file-input"><input type="file" accept=".zip" onChange={e => setPackageFile(e.target.files?.[0] || null)} /><span>{packageFile ? packageFile.name : '选择剧情数据包 ZIP'}</span></label><button className="secondary" disabled={!packageFile || !!busy || running} onClick={() => packageFile && void action('package', () => uploadJob(packageFile, 'install_package'))}>{busy === 'package' ? `上传中 ${uploadProgress}%` : '安装数据包'}</button></div>
          {settings && <><label className="field"><span>数据包更新清单地址</span><input type="url" value={settings.updates.manifest_url || ''} onChange={e => setSettings({ ...settings, updates: { ...settings.updates, manifest_url: e.target.value } })} placeholder="https://your-host.example/latest.json" /></label><div className="form-actions"><button className="secondary" disabled={!!busy || running || !settings.updates.manifest_url} onClick={() => void action('check', async () => { await saveSettings(); await startJob('check_update'); })}>保存并检查更新</button><a className={`secondary button-link ${!status.data.ready ? 'disabled' : ''}`} href={apiUrl('admin/package')} aria-disabled={!status.data.ready} onClick={e => { if (!status.data.ready) e.preventDefault(); }}>导出当前数据包 ↗</a></div></>}
        </section>
        <section className="admin-section"><h3>恢复与诊断</h3><div className="form-actions"><button className="secondary" disabled={!!busy || running} onClick={() => { if (window.confirm('将活动数据恢复到上一份已保留快照，确认继续？')) void action('rollback', () => startJob('rollback')); }}>恢复上一份快照</button><button className="text-button" disabled={!!busy} onClick={() => void action('diagnostics', async () => downloadJson('星轨档案-诊断.json', await request('admin/diagnostics')))}>导出诊断信息 ↗</button></div></section>
      </>}
      {tab === 'jobs' && <><div className="section-heading"><div><h3>后台任务</h3><p>任务状态自动刷新，切换页面后仍可继续处理。</p></div></div>{jobs.length ? <div className="job-list">{jobs.map(job => <article key={job.id} className="job-card"><div className="job-title"><h4>{labels[job.kind] || job.kind}</h4><span className={`job-state state-${job.status}`}>{labels[job.status] || job.status}</span></div><p>{job.message || job.phase || '等待处理'}</p>{job.total > 0 && <><progress value={job.done || 0} max={job.total} /><div className="progress-caption"><span>{formatNumber(job.done)} / {formatNumber(job.total)}</span><span>{Math.min(100, Math.round(job.done / job.total * 100))}%</span></div></>}{job.error && <div className="error-message">{job.error}</div>}<div className="job-footer"><time>{job.created_at ? new Date(job.created_at).toLocaleString('zh-CN') : ''}</time><div>{job.status === 'running' && <button className="text-button" disabled={!!busy} onClick={() => void action(job.id, async () => { await post(`admin/jobs/${job.id}/pause`); })}>暂停</button>}{['paused', 'failed', 'interrupted', 'cancelled'].includes(job.status) && <button className="text-button" disabled={!!busy} onClick={() => void action(job.id, async () => { await post(`admin/jobs/${job.id}/resume`); })}>继续</button>}{['queued', 'running', 'paused'].includes(job.status) && <button className="text-button danger-text" disabled={!!busy} onClick={() => void action(job.id, async () => { await post(`admin/jobs/${job.id}/cancel`); })}>取消</button>}</div></div></article>)}</div> : <Empty title="暂无后台任务"><p>导入数据或建立索引后，可以在这里查看进度。</p></Empty>}</>}
    </div>
  </div>;
}
