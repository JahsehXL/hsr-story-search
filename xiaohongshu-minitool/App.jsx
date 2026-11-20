import { useCallback, useEffect, useRef, useState } from 'react';
import Reader from '../frontend/src/Reader';
import { cleanMissionName, displayText, Empty, errorText, Highlight, missionTypeClass, speakerName, versionText } from '../frontend/src/presentation';
import { openArchive, readCatalog } from './loader.mjs';
import { nativeStorage } from './storage.mjs';

const emptyFilters = { world: '', version: '', type: '', character: '' };
const labels = { world: '世界', version: '版本', type: '任务类型', character: '角色', unknown: '版本待核实',
  Gap: '开拓续闻', exact: '精准命中', keyword: '关键词匹配', mission: '任务名称匹配', speaker: '角色匹配', metadata: '剧情出处匹配' };
const pageSize = 30;
const contextSize = 1000;
const bookmarkStorageKey = 'hsr-bookmarks-v1';
const themeStorageKey = 'hsr-theme-v1';
const suggestions = ['生命因何而沉睡', '三月七 星穹列车', '流萤', '老杨'];
const routeStops = [
  ['空间站「黑塔」', '黑塔空间站'], ['雅利洛-Ⅵ', '雅利洛-Ⅵ'], ['仙舟「罗浮」', '仙舟罗浮'],
  ['匹诺康尼', '匹诺康尼'], ['翁法罗斯', '翁法罗斯'], ['二相乐园', '二相乐园'],
];
const unsupportedReaderAction = () => {};
const count = value => value.toLocaleString('zh-CN');
const bookmarkIdentity = result => result.line_id || JSON.stringify([result.mission_id, result.script]);
const resultTitle = result => result.dialogue_origin?.title || cleanMissionName(result.mission_name, result.chapter, result.world);

function Filter({ name, value, choices, change }) {
  const [open, setOpen] = useState(false);
  const [character, setCharacter] = useState('');
  const wrap = useRef(null);
  const isCharacter = name === 'character';
  const format = item => isCharacter ? speakerName(item) : labels[item] || item;
  const typed = character.trim();
  const filtered = isCharacter && typed
    ? choices.filter(item => format(item).toLowerCase().includes(typed.toLowerCase())) : choices;

  useEffect(() => {
    if (!open) return;
    function closeOnOutside(event) {
      if (!wrap.current?.contains(event.target)) setOpen(false);
    }
    function closeOnEscape(event) { if (event.key === 'Escape') setOpen(false); }
    document.addEventListener('pointerdown', closeOnOutside);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('pointerdown', closeOnOutside);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [open]);

  function select(item) {
    setOpen(false);
    change(item);
  }

  return <div ref={wrap} className={'filter-dropdown' + (open ? ' is-open' : '') + (value ? ' has-value' : '')}>
    <button type="button" className="filter" aria-label={labels[name] + '筛选'} aria-expanded={open}
      onClick={() => { setOpen(!open); setCharacter(''); }}>
      <span>{labels[name]}</span><strong className="filter-value">{value ? format(value) : '全部'}</strong><span aria-hidden="true">⌄</span>
    </button>
    {open && <div className="filter-menu">
      {isCharacter && <div className="filter-search-box">
        <input value={character} onChange={event => setCharacter(event.target.value)} aria-label="搜索角色" placeholder="搜索或输入角色"
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.nativeEvent.isComposing && typed) {
              event.preventDefault();
              select(filtered[0] || typed);
            }
          }} />
      </div>}
      <div className="filter-options" role="listbox" aria-label={labels[name] + '选项'}>
        <button type="button" role="option" aria-selected={!value} className="filter-option" onClick={() => select('')}>全部</button>
        {isCharacter && typed && !filtered.includes(typed) && <button type="button" className="filter-option" onClick={() => select(typed)}>查找「{typed}」</button>}
        {filtered.map(item => <button key={item} type="button" role="option" aria-selected={item === value}
          className={'filter-option' + (item === value ? ' is-selected' : '')} onClick={() => select(item)}>{format(item)}</button>)}
      </div>
    </div>}
  </div>;
}

function ResultCard({ result, selected, query, onOpen }) {
  const origin = result.dialogue_origin;
  const title = resultTitle(result);
  const label = result.matched_field !== 'text' && labels[result.matched_field] || labels[result.match_type] || '相关对白';
  return <button className={'result-card' + (selected ? ' selected' : '')} onClick={onOpen}>
    <span className="result-topline">
      <span className="result-badges">
        {!origin && result.type && <span className={'mission-badge type-' + missionTypeClass(result.type)}>{labels[result.type] || result.type}</span>}
        <span className="world-badge">{result.world || '剧情对白'}</span>
        {result.kind?.includes('option') && <span className="option-badge">❖ 玩家选项</span>}
      </span>
      <span className={origin || !result.version ? 'pending-version' : 'version-tag'}>
        {origin ? '对白版本待核实' : versionText(result.version, result.version_status)}
      </span>
    </span>
    <h3>{title}{!origin && result.chapter && result.chapter !== title && <small className="result-chapter"> · {result.chapter}</small>}</h3>
    <p className="result-text"><strong>{result.kind?.includes('option') ? '❖ 开拓者选项' : speakerName(result.speaker)}</strong>
      <span><Highlight text={result.display_text || result.text} query={query} /></span></p>
    {origin && <p className="result-origins">多个出处 · {origin.origins.map(item => item.name).join(' / ')}</p>}
    <span className="result-bottom"><span>{label}</span><span className="result-open">阅读上下文 →</span></span>
  </button>;
}

function Bookmarks({ bookmarks, close, open, remove, canSave }) {
  const dialog = useRef(null);
  const closeButton = useRef(null);
  useEffect(() => {
    const previous = document.activeElement;
    closeButton.current?.focus();
    function key(event) {
      if (event.key === 'Escape') close();
      if (event.key !== 'Tab') return;
      const buttons = dialog.current.querySelectorAll('button');
      const first = buttons[0], last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener('keydown', key);
    return () => { document.removeEventListener('keydown', key); previous?.focus(); };
  }, [close]);
  return <div className="modal-backdrop" onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <section ref={dialog} className="modal" role="dialog" aria-modal="true" aria-label="收藏的剧情">
      <header className="modal-header"><h2>收藏的剧情</h2><button ref={closeButton} className="icon-button" onClick={close} aria-label="关闭">×</button></header>
      <div className="bookmark-modal">
        <div className="bookmark-info"><p>{canSave ? '收藏保存在此设备的小工具中。' : '当前环境无法读取本地收藏，本次新增暂存于会话。'}</p></div>
        {bookmarks.length ? bookmarks.map(item => <div className="bookmark-row" key={item.key}>
          <button onClick={() => { close(); open(item.result, item.query); }}>
            <h3>{resultTitle(item.result)}</h3>
            <p>{displayText(item.result.display_text || item.result.text) || '打开收藏位置'}</p>
            {!item.result.dialogue_origin && <small>{item.result.chapter}</small>}
          </button>
          <button className="icon-button" onClick={() => remove(item.key)} aria-label="移除收藏">×</button>
        </div>) : <Empty symbol="☆" title="还没有收藏"><p>阅读剧情时，点击标题旁的星标收藏。</p></Empty>}
      </div>
    </section>
  </div>;
}

export default function App() {
  const [catalog, setCatalog] = useState(null);
  const [catalogError, setCatalogError] = useState('');
  const [query, setQuery] = useState('');
  const [filters, setFilters] = useState(emptyFilters);
  const [criteria, setCriteria] = useState(null);
  const [searching, setSearching] = useState(false);
  const [response, setResponse] = useState(null);
  const [searchError, setSearchError] = useState('');
  const [selected, setSelected] = useState(null);
  const [readerQuery, setReaderQuery] = useState('');
  const [detail, setDetail] = useState(null);
  const [reading, setReading] = useState(false);
  const [readerError, setReaderError] = useState('');
  const [showBookmarks, setShowBookmarks] = useState(false);
  const [bookmarks, setBookmarks] = useState([]);
  const [canSave, setCanSave] = useState(false);
  const [theme, setTheme] = useState(() => window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const [toast, setToast] = useState('');
  const searchGeneration = useRef(0);
  const readerGeneration = useRef(0);
  const scrollBeforeReader = useRef(0);
  const resultsScroll = useRef(null);
  const input = useRef(null);
  const storage = useRef(null);
  const readKeys = useRef(new Set());
  const latestBookmarks = useRef([]);
  const latestThemeChanged = useRef(false);
  const writes = useRef(Promise.resolve());

  async function loadCatalog() {
    setCatalogError('');
    try { setCatalog(await readCatalog()); }
    catch (error) { setCatalogError(errorText(error)); }
  }
  useEffect(() => { void loadCatalog(); }, []);
  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  useEffect(() => {
    let active = true;
    async function loadStorage() {
      storage.current = await nativeStorage();
      if (!storage.current || !active) return;
      const api = storage.current;
      try {
        const saved = await api.read(themeStorageKey);
        if (active && (saved === null || saved === 'light' || saved === 'dark')) {
          readKeys.current.add(themeStorageKey);
          if (saved && !latestThemeChanged.current) setTheme(saved);
        }
      } catch { /* Leave saved settings on the device unchanged. */ }
      try {
        const saved = await api.read(bookmarkStorageKey);
        if (active && (saved === null || Array.isArray(saved))) {
          readKeys.current.add(bookmarkStorageKey);
          const merged = new Map((saved || []).filter(item => item?.result?.mission_id)
            .map(item => {
              const key = bookmarkIdentity(item.result);
              return [key, { ...item, key }];
            }));
          for (const item of latestBookmarks.current) merged.set(item.key, item);
          latestBookmarks.current = Array.from(merged.values());
          setBookmarks(latestBookmarks.current);
          setCanSave(true);
        }
      } catch { /* A failed read does not authorize a write. */ }
    }
    void loadStorage();
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(''), 3200);
    return () => clearTimeout(timer);
  }, [toast]);

  function saveSetting(key, value, confirmation, sessionMessage) {
    if (!storage.current || !readKeys.current.has(key)) {
      setToast(sessionMessage);
      return;
    }
    const save = writes.current.then(() => storage.current.write(key, value));
    writes.current = save.catch(() => {});
    void save.then(() => setToast(confirmation), () => setToast(sessionMessage));
  }

  function updateBookmarks(next, confirmation) {
    latestBookmarks.current = next;
    setBookmarks(next);
    saveSetting(bookmarkStorageKey, next, confirmation, '收藏调整已应用于本次使用，设备保存未确认');
  }
  const bookmarkKey = selected ? bookmarkIdentity(selected) : '';
  const bookmarked = bookmarks.some(item => item.key === bookmarkKey);
  function toggleBookmark() {
    if (!selected) return;
    const items = latestBookmarks.current;
    const hasBookmark = items.some(item => item.key === bookmarkKey);
    updateBookmarks(hasBookmark ? items.filter(item => item.key !== bookmarkKey)
      : [{ key: bookmarkKey, result: selected, query: readerQuery }, ...items],
    hasBookmark ? '已取消收藏' : '已收藏到此设备');
  }

  function closeReader(restoreScroll = true) {
    readerGeneration.current++;
    setSelected(null);
    setDetail(null);
    setReading(false);
    setReaderError('');
    setReaderQuery('');
    if (restoreScroll) requestAnimationFrame(() => window.scrollTo(0, scrollBeforeReader.current));
  }

  function reset() {
    searchGeneration.current++;
    closeReader(false);
    setSearching(false);
    setQuery('');
    setFilters(emptyFilters);
    setResponse(null);
    setCriteria(null);
    setSearchError('');
  }

  async function search(value, nextFilters, more = false) {
    const searchCriteria = more ? criteria : { query: value.trim(), filters: nextFilters };
    if (!searchCriteria || (!searchCriteria.query && !Object.values(searchCriteria.filters).some(Boolean))) {
      input.current?.focus();
      return;
    }
    const generation = ++searchGeneration.current;
    const offset = more ? response.results.length : 0;
    setSearching(true);
    setSearchError('');
    try {
      const engine = await openArchive();
      if (generation !== searchGeneration.current) return;
      const result = await engine.search(searchCriteria.query, searchCriteria.filters, offset, pageSize,
        () => generation !== searchGeneration.current);
      if (generation !== searchGeneration.current) return;
      setResponse(previous => more && previous ? { ...result, results: [...previous.results, ...result.results] } : result);
      if (!more) {
        setCriteria(searchCriteria);
        closeReader(false);
        if (resultsScroll.current) resultsScroll.current.scrollTop = 0;
      }
    } catch (error) {
      if (generation === searchGeneration.current) setSearchError(errorText(error));
    } finally {
      if (generation === searchGeneration.current) setSearching(false);
    }
  }

  function applyFilters(next) {
    setFilters(next);
    if (query.trim() || Object.values(next).some(Boolean)) void search(query, next);
    else {
      searchGeneration.current++;
      closeReader(false);
      setResponse(null);
      setCriteria(null);
      setSearching(false);
      setSearchError('');
    }
  }

  async function select(result, highlight = criteria?.query || '', saved = false) {
    if (!selected) scrollBeforeReader.current = window.scrollY || 0;
    const generation = ++readerGeneration.current;
    setSelected(result);
    setReaderQuery(highlight);
    setDetail(null);
    setReaderError('');
    setReading(true);
    try {
      const engine = await openArchive();
      const value = saved
        ? await engine.bookmarkDetails(result.mission_id, result.line_id, result.script)
        : await engine.missionDetails(result.mission_id, result.line_id);
      if (generation !== readerGeneration.current) return;
      setDetail(value);
      const anchor = saved && value.lines.find(line => line.id === value.anchor_line_id);
      if (anchor) {
        const { mission } = value;
        const corrected = { ...result, chunk_id: anchor.chunk_id, mission_id: mission.id, mission_name: mission.name,
          chapter: mission.chapter, world: mission.world, type: mission.type, version: mission.version,
          version_status: mission.version_status, version_source: mission.version_source, dialogue_origin: anchor.dialogue_origin };
        setSelected(corrected);
        if (JSON.stringify(corrected) !== JSON.stringify(result)) {
          const key = bookmarkIdentity(result);
          const items = latestBookmarks.current;
          if (items.some(item => item.key === key)) {
            updateBookmarks(items.map(item => item.key === key ? { ...item, result: corrected } : item), '');
          }
        }
      }
    } catch (error) {
      if (generation === readerGeneration.current) setReaderError(errorText(error));
    } finally {
      if (generation === readerGeneration.current) setReading(false);
    }
  }

  async function moreContext(before = false) {
    if (!selected || !detail || reading) return;
    const generation = readerGeneration.current;
    const previousOffset = Math.max(0, detail.offset - contextSize);
    const offset = before ? previousOffset : detail.offset + detail.lines.length;
    const limit = before ? detail.offset - previousOffset : contextSize;
    if (limit <= 0) return;
    setReading(true);
    try {
      const page = await (await openArchive()).missionDetails(selected.mission_id, null, offset, limit);
      if (generation !== readerGeneration.current) return;
      const edgeMap = new Map([...detail.edges, ...page.edges].map(edge => [JSON.stringify(edge), edge]));
      setDetail({
        ...detail, offset: before ? page.offset : detail.offset, has_more: before ? detail.has_more : page.has_more,
        lines: before ? [...page.lines, ...detail.lines] : [...detail.lines, ...page.lines], edges: Array.from(edgeMap.values()),
      });
    } catch (error) {
      if (generation === readerGeneration.current) setToast(errorText(error));
    } finally {
      if (generation === readerGeneration.current) setReading(false);
    }
  }

  const closeBookmarks = useCallback(() => setShowBookmarks(false), []);
  const ready = !!catalog;
  const canSearch = ready && (query.trim() || Object.values(filters).some(Boolean));
  const chooseSuggestion = value => { setQuery(value); void search(value, filters); };
  const controls = catalog?.catalog;

  return <div className={'app-shell minitool' + (selected ? ' has-selection' : '')}>
    <header className="app-header">
      <button type="button" className="brand minitool-brand" onClick={reset}>
        <span className="brand-mark archive-mark" aria-hidden="true">✦</span><span>星轨档案</span>
      </button>
      <div className="header-actions">
        <button className="header-button" onClick={() => setShowBookmarks(true)} aria-label="打开收藏" title="打开收藏"><span aria-hidden="true">☆</span><span>收藏</span>{bookmarks.length > 0 && <small>{bookmarks.length}</small>}</button>
        <button className="header-button theme-toggle" aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
          onClick={() => {
            const value = theme === 'dark' ? 'light' : 'dark';
            latestThemeChanged.current = true;
            setTheme(value);
            if (readKeys.current.has(themeStorageKey)) saveSetting(themeStorageKey, value, '', '主题已切换，设备保存未确认');
          }}><span aria-hidden="true">{theme === 'dark' ? '☀' : '☾'}</span></button>
      </div>
    </header>
    <section className="search-section" aria-label="搜索剧情">
      <form className="search-form" onSubmit={event => { event.preventDefault(); if (canSearch && !searching) void search(query, filters); }}>
        <span className="search-symbol" aria-hidden="true">⌕</span>
        <input ref={input} value={query} onChange={event => setQuery(event.target.value)} maxLength={1000}
          aria-label="搜索剧情" placeholder="搜台词、任务或角色" enterKeyHint="search" />
        {query && <button className="icon-button clear-query" type="button" aria-label="清空输入" onClick={() => { setQuery(''); input.current?.focus(); }}>×</button>}
        <button className="primary search-submit" disabled={!canSearch || searching}>{searching ? '搜索中' : '搜索'}<span aria-hidden="true">↗</span></button>
      </form>
      {ready && <div className="search-toolbar">
        <div className="filter-row">
          <Filter name="world" value={filters.world} choices={controls.worlds} change={value => applyFilters({ ...filters, world: value })} />
          <Filter name="version" value={filters.version} choices={controls.versions} change={value => applyFilters({ ...filters, version: value })} />
          <Filter name="type" value={filters.type} choices={controls.types} change={value => applyFilters({ ...filters, type: value })} />
          <Filter name="character" value={filters.character} choices={controls.characters} change={value => applyFilters({ ...filters, character: value })} />
          {Object.values(filters).some(Boolean) && <button className="text-button clear-filters" onClick={() => applyFilters(emptyFilters)}>重置筛选</button>}
        </div>
      </div>}
    </section>
    <main className="workspace">
      <section className="results-panel" aria-label="搜索结果" aria-busy={searching}>
        <div className="results-heading"><h2>{response ? '精准关键词结果' : '剧情检索'}</h2><span>
          {response ? (response.truncated ? '优先展示 ' : '') + count(response.total) + ' 条相关结果'
            : ready ? count(catalog.lines) + ' 条对白记录' : ''}
        </span></div>
        <div className="results-scroll" ref={resultsScroll}>
          {catalogError ? <Empty title="档案暂时无法打开"><p>{catalogError}</p><button className="secondary" onClick={loadCatalog}>重试</button></Empty>
            : searchError ? <Empty title="搜索失败"><p>{searchError}</p><button className="secondary" onClick={() => void search(query, filters)}>重试</button></Empty>
            : !ready || (searching && !response) ? <Empty title={searching ? '正在检索剧情' : '正在读取档案目录'}><p role="status">{searching ? '首次搜索需要加载本地台词。' : '读取完成后即可开始搜索。'}</p></Empty>
            : !response ? <div className="search-welcome">
              <div className="archive-welcome">
                <div className="archive-welcome-copy">
                  <h2>列车智库</h2><p>查阅《崩坏：星穹铁道》的台词与任务。</p>
                </div>
                <span className="archive-ticket" aria-hidden="true"><i /></span>
              </div>
              <section className="archive-route" aria-label="开拓航线">
                <h3>开拓航线</h3>
                <div className="archive-stops">
                  {routeStops.filter(([world]) => controls.worlds.includes(world)).map(([world, name]) =>
                    <button key={world} type="button" aria-label={'筛选剧情：' + name}
                      onClick={() => applyFilters({ ...filters, world })}>{name}</button>)}
                </div>
              </section>
              <div className="suggestions"><span>试着搜索</span>{suggestions.map(value => <button key={value} onClick={() => chooseSuggestion(value)}>{value}<span aria-hidden="true">↗</span></button>)}</div>
            </div> : !response.results.length ? <Empty title="还没有找到相关剧情"><p>试试缩短台词、使用角色名，或减少筛选条件。</p></Empty>
            : <div className="result-list">
              {response.results.map(result => <ResultCard key={result.chunk_id} result={result}
                selected={selected?.chunk_id === result.chunk_id} query={criteria?.query || ''} onOpen={() => void select(result)} />)}
              {response.results.length < response.total && <button className="secondary load-results" disabled={searching}
                onClick={() => void search(query, filters, true)}>{searching ? '加载中…' : '加载更多结果'}</button>}
            </div>}
        </div>
      </section>
      <Reader selected={selected} detail={detail} busy={reading} query={readerQuery} error={readerError} isAdmin={false}
        bookmarked={bookmarked} onBookmark={toggleBookmark} onCopy={unsupportedReaderAction} onShare={unsupportedReaderAction}
        onVersionSave={unsupportedReaderAction} onBack={() => closeReader()} onMore={() => void moreContext()}
        onEarlier={() => void moreContext(true)} />
    </main>
    {showBookmarks && <Bookmarks bookmarks={bookmarks} canSave={canSave} close={closeBookmarks}
      open={(result, highlight) => void select(result, highlight, true)}
      remove={key => updateBookmarks(latestBookmarks.current.filter(item => item.key !== key), '已移除收藏')} />}
    {toast && <div className="toast" role="status">{toast}</div>}
  </div>;
}
