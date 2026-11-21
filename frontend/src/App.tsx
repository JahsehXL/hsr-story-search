import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { downloadJson, post, request, searchRequest } from './api';
import { createSemanticSearchClient, indexReady, loadVisitorEndpoint, loadVisitorKey, saveVisitorEndpoint, saveVisitorKey, serverKeyReady, VISITOR_ENDPOINT_STORAGE, VISITOR_KEY_STORAGE } from './semantic';
import type { SemanticContext } from './semantic';
import Reader from './Reader';
import { cleanMissionName, displayText, speakerName, errorText, missionTypeClass, versionText, Highlight, Empty } from './presentation';
import {
  DEFAULT_ANNOUNCEMENT, hasUnreadAnnouncement, loadAnnouncementState, markAnnouncementSeen,
  normalizeAnnouncement, parseChangelogBody, parseNoteParagraphs, saveAnnouncementState,
  shouldAutoShowAnnouncement, snoozeAnnouncementState,
} from './announcement';
import type { AnnouncementData, AnnouncementState } from './announcement';
import { buildMissionHash, buildSearchHash, hasActiveFilters, parseRoute } from './route';
import type { Bookmark, Catalog, DialogueLine, FeedbackContext, Filters, MatchedMission, MissionResponse, Result, SearchMode, SearchResponse, Status } from './types';

const Admin = lazy(() => import('./Admin'));

const emptyFilters: Filters = { world: '', version: '', type: '', character: '' };
const emptyCatalog: Catalog = { worlds: [], versions: [], types: [], characters: [] };
let bootstrapPromise: Promise<void> | undefined;

function storedVisitorKey(): string {
  try { return loadVisitorKey(localStorage); } catch { return ''; }
}
function storedVisitorEndpoint(): string {
  try { return loadVisitorEndpoint(localStorage); } catch { return ''; }
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
  Interlude: '开拓续闻', Gap: '开拓续闻', exact: '原文命中', keyword: '关键词匹配', fuzzy: '错字容错',
  semantic: '剧情相关', hybrid: '描述找剧情', text: '精准关键词', unknown: '版本待核实',
  queued: '等待中', running: '进行中', paused: '已暂停', completed: '已完成',
  failed: '失败', cancelled: '已取消', interrupted: '已中断', succeeded: '已完成',
  import_directory: '导入目录', import_upload: '导入文件', download_source: '更新原始数据',
  build_index: '建立语义索引', install_package: '安装数据包', check_update: '检查数据更新', rollback: '恢复上一版',
};

function formatNumber(value: number | string | undefined) { return typeof value === 'number' ? value.toLocaleString('zh-CN') : value || '—'; }

function matchLabel(result: Result) {
  if (result.matched_field === 'mission') return '任务名称匹配';
  if (result.matched_field === 'speaker') return '角色匹配';
  if (result.matched_field === 'metadata') return '剧情出处匹配';
  return labels[result.match_type] || '相关对白';
}

function SelectField({ label, value, values, onChange, searchable = false, format = (v: string) => labels[v] || v, onSearch }: {
  label: string; value: string; values: string[]; onChange: (v: string) => void; searchable?: boolean; format?: (value: string) => string;
  onSearch?: (query: string, signal: AbortSignal) => Promise<string[]>;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [remoteValues, setRemoteValues] = useState<string[] | null>(null);
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

  useEffect(() => {
    if (!open || !searchable || !onSearch) {
      setRemoteValues(null);
      return;
    }
    const q = search.trim();
    if (!q) {
      setRemoteValues(null);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void onSearch(q, controller.signal)
        .then(items => { if (!controller.signal.aborted) setRemoteValues(items); })
        .catch(() => { if (!controller.signal.aborted) setRemoteValues(null); });
    }, 150);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [open, searchable, search, onSearch]);

  const displayValue = value ? format(value) : '全部';
  const trimmedSearch = search.trim();
  const localFiltered = searchable && trimmedSearch
    ? values.filter(v => format(v).toLowerCase().includes(trimmedSearch.toLowerCase()))
    : values;
  const filtered = searchable && trimmedSearch && remoteValues
    ? [...new Set([...remoteValues, ...localFiltered])]
    : localFiltered;

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
            if (e.key === 'Enter' && trimmedSearch) {
              e.preventDefault();
              onChange(filtered[0] || trimmedSearch);
              setOpen(false);
            }
          }}
        />
      </div>}
      <div className="filter-options">
        <button type="button" role="option" aria-selected={!value} className={`filter-option ${!value ? 'is-selected' : ''}`} onClick={() => { onChange(''); setOpen(false); }}>
          <span>全部</span>{!value && <span aria-hidden="true">✓</span>}
        </button>
        {searchable && trimmedSearch && !filtered.includes(trimmedSearch) && <button type="button" role="option" aria-selected={value === trimmedSearch} className="filter-option" onClick={() => { onChange(trimmedSearch); setOpen(false); }}>
          <span>查找「{trimmedSearch}」</span>
        </button>}
        {filtered.map(v => <button key={v} type="button" role="option" aria-selected={value === v} className={`filter-option ${value === v ? 'is-selected' : ''}`} onClick={() => { onChange(v); setOpen(false); }}>
          <span>{format(v)}</span>{value === v && <span aria-hidden="true">✓</span>}
        </button>)}
      </div>
    </div>}
  </div>;
}

function Modal({ title, close, children, wide = false, className = '' }: { title: string; close: () => void; children: ReactNode; wide?: boolean; className?: string }) {
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
  return <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) close(); }}><section ref={dialogRef} className={`modal ${wide ? 'modal-wide' : ''} ${className}`.trim()} role="dialog" aria-modal="true" aria-label={title}>
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

function VisitorKeyForm({ initialKey, initialEndpoint, model, save, clear }: { initialKey: string; initialEndpoint: string; model: string; save: (key: string, endpoint: string) => void; clear: () => void }) {
  const [key, setKey] = useState(initialKey);
  const [endpoint, setEndpoint] = useState(initialEndpoint);
  const [error, setError] = useState('');
  const targetModel = model || 'BAAI/bge-m3';
  return <form className="visitor-key-form" onSubmit={event => {
    event.preventDefault(); setError('');
    try { save(key, endpoint); } catch (failure) { setError(errorText(failure)); }
  }}>
    <div className="visitor-key-steps">
      <strong>获取 API Key 步骤（以硅基流动为例）：</strong><br />
      打开<a href="https://cloud.siliconflow.cn" target="_blank" rel="noreferrer">硅基流动控制台 ↗</a> → 注册 / 登录账号 → 完成实名认证 → 在「API 密钥」页面新建并复制 Key。<br />
      <a href="https://jahsehxx.dpdns.org/posts/siliconflow-api-key-guide/" target="_blank" rel="noreferrer">不会申请？查看 API Key 图文教程 ↗</a>
    </div>
    <p>请选择提供 <strong><code>{targetModel}</code></strong> 的 OpenAI Embeddings 兼容服务，模型与参数需匹配当前索引。本站的 BGE-M3 索引由硅基流动生成，仅作配置示例，不构成推广。</p>
    {targetModel === 'BAAI/bge-m3' && <p>硅基流动的标准版 BGE-M3 目前免费，Cloudflare Workers AI 也提供每日免费额度，具体以平台政策为准。<a href="https://www.siliconflow.cn/pricing" target="_blank" rel="noreferrer">硅基流动价格 ↗</a> · <a href="https://developers.cloudflare.com/workers-ai/platform/pricing/" target="_blank" rel="noreferrer">CF 免费额度 ↗</a></p>}
    <p>使用其他模型需自行重建剧情索引。<a href="https://github.com/JahsehXL/hsr-story-search/blob/main/docs/build-guide.md" target="_blank" rel="noreferrer">构建与部署指南 ↗</a></p>
    <label className="field"><span>API Key</span><input type="password" autoComplete="new-password" value={key} onChange={event => setKey(event.target.value)} placeholder={`粘贴支持 ${targetModel} 的 API Key`} aria-label="语义搜索 API Key" maxLength={4096} /></label>
    <details className="custom-endpoint-box" open={Boolean(initialEndpoint)}>
      <summary>其他兼容平台接口地址（可选，留空默认使用硅基流动）</summary>
      <label className="field"><span>Embeddings 接口地址</span><input type="url" value={endpoint} onChange={event => setEndpoint(event.target.value)} placeholder="留空默认 https://api.siliconflow.cn/v1/embeddings" aria-label="自定义 Embeddings 接口地址" maxLength={1024} /></label>
    </details>
    <p className="key-storage-note">密钥仅保存在当前浏览器，可随时修改或清除。默认（硅基流动）优先由浏览器直连，直连失败时才由本站服务器在内存中代发；填写其他接口地址时仅限浏览器直连。</p>
    <div className="form-actions"><button className="primary">保存密钥</button><button className="secondary" type="button" onClick={() => { try { clear(); } catch (failure) { setError(errorText(failure)); } }}>清除密钥</button></div>
    {error && <p className="error-message" role="alert">{error}</p>}
  </form>;
}

const feedbackCategories = [
  { id: 'missing', label: '剧情或台词遗漏' },
  { id: 'display', label: '显示或分支顺序错误' },
  { id: 'feature', label: '功能建议' },
  { id: 'other', label: '随便聊聊 / 其他' },
] as const;

const MAX_FEEDBACK_ATTACHMENT_BYTES = 5 * 1024 * 1024;
const MAX_FEEDBACK_FILES = 5;

function formatBytes(bytes: number) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

function FeedbackModal({
  full,
  context,
  onSuccess,
  onClose,
}: {
  full: boolean;
  context: FeedbackContext | null;
  onSuccess: () => void;
  onClose: () => void;
}) {
  const [category, setCategory] = useState<string>('missing');
  const [content, setContent] = useState('');
  const [includeContext, setIncludeContext] = useState(Boolean(context));
  const [files, setFiles] = useState<File[]>([]);
  const [previews, setPreviews] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [isFull, setIsFull] = useState(full);

  useEffect(() => {
    const urls = files.map(file => URL.createObjectURL(file));
    setPreviews(urls);
    return () => {
      for (const url of urls) URL.revokeObjectURL(url);
    };
  }, [files]);

  const totalBytes = files.reduce((sum, item) => sum + item.size, 0);

  function addFiles(incoming: FileList | File[]) {
    setError('');
    const list = Array.from(incoming).filter(file => /^image\/(png|jpeg|webp|gif)$/i.test(file.type));
    if (!list.length) {
      setError('请选择 PNG、JPG、WebP 或 GIF 格式的图片');
      return;
    }
    const next = [...files, ...list];
    if (next.length > MAX_FEEDBACK_FILES) {
      setError(`单次最多附带 ${MAX_FEEDBACK_FILES} 张图片`);
      return;
    }
    const nextBytes = next.reduce((sum, item) => sum + item.size, 0);
    if (nextBytes > MAX_FEEDBACK_ATTACHMENT_BYTES) {
      setError(`图片附件总大小不能超过 5 MB（当前已选 ${formatBytes(nextBytes)}）`);
      return;
    }
    setFiles(next);
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting || isFull) return;
    const trimmed = content.trim();
    if (!trimmed) {
      setError('请输入反馈内容');
      return;
    }
    if (totalBytes > MAX_FEEDBACK_ATTACHMENT_BYTES) {
      setError('图片附件总大小不能超过 5 MB');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      const form = new FormData();
      form.append('category', category);
      form.append('content', trimmed);
      if (includeContext && context) {
        form.append('context', JSON.stringify(context));
      }
      for (const file of files) {
        form.append('files', file, file.name || 'image.png');
      }
      await request('feedback', { method: 'POST', body: form });
      onSuccess();
    } catch (failure) {
      const msg = errorText(failure);
      if (msg.includes('存储空间已满') || msg.includes('反馈已满')) setIsFull(true);
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  }

  return <form
    className="feedback-form"
    onSubmit={e => void handleSubmit(e)}
    onPaste={e => {
      const pasted: File[] = [];
      for (const item of Array.from(e.clipboardData?.items || [])) {
        if (item.type.startsWith('image/')) {
          const f = item.getAsFile();
          if (f) pasted.push(f);
        }
      }
      if (pasted.length) {
        e.preventDefault();
        addFiles(pasted);
      }
    }}
  >
    {isFull && <div className="inline-alert feedback-full-alert" role="alert">
      <strong>目前反馈存储空间已满</strong>
      <p>请稍后再试，或前往 <a href="https://github.com/JahsehXL/hsr-story-search/issues" target="_blank" rel="noreferrer">GitHub 仓库 ↗</a> / <a href="https://jahsehxx.dpdns.org/blog/" target="_blank" rel="noreferrer">个人博客 ↗</a> 联系作者处理。</p>
    </div>}
    <div className="feedback-intro">
      <p>发现剧情遗漏、顺序错乱，或者有任何建议与想说的话，都可以在这里直接发送，支持选择或直接粘贴截图。</p>
      <p>如果方便，也可以在正文结尾留下你的称呼或联系方式；也可以去抖音私信 / 评论，或者前往 GitHub 仓库反馈。最新改动与详细记录一切以 <a href="https://github.com/JahsehXL/hsr-story-search" target="_blank" rel="noreferrer">GitHub 仓库 ↗</a> 和公告栏中为准。</p>
    </div>
    <div className="field">
      <span className="feedback-field-title">反馈类型</span>
      <div className="feedback-category-pills" role="radiogroup" aria-label="反馈类型">
        {feedbackCategories.map(item => <button
          key={item.id}
          type="button"
          className={`feedback-category-pill ${category === item.id ? 'is-selected' : ''}`}
          aria-pressed={category === item.id}
          onClick={() => setCategory(item.id)}
          disabled={isFull || submitting}
        >{item.label}</button>)}
      </div>
    </div>
    <label className="field">
      <span className="feedback-field-title">反馈内容 <small>{content.length} / 4000</small></span>
      <textarea
        rows={5}
        value={content}
        onChange={e => setContent(e.target.value)}
        placeholder="请描述遇到的问题、遗漏的剧情任务或想对作者说的话（可直接 Ctrl+V 粘贴截图，方便的话可在结尾留下称呼或联系方式）…"
        maxLength={4000}
        required
        disabled={isFull || submitting}
      />
    </label>
    {context && (context.mission_id || context.query) && <label className="feedback-context-toggle">
      <input
        type="checkbox"
        checked={includeContext}
        onChange={e => setIncludeContext(e.target.checked)}
        disabled={isFull || submitting}
      />
      <div className="feedback-context-preview">
        <strong>附带当前查看的剧情上下文</strong>
        <span>
          {[
            context.mission_name && context.mission_id ? `${context.mission_name}（${context.mission_id}）` : (context.mission_name || context.mission_id),
            context.speaker && context.text ? `${speakerName(context.speaker)}：${displayText(context.text).slice(0, 48)}` : '',
            context.query ? `搜索词：「${context.query}」` : '',
          ].filter(Boolean).join(' · ')}
        </span>
      </div>
    </label>}
    <div className="field">
      <span className="feedback-field-title">附带图片 <small>{files.length > 0 ? `已选 ${files.length} 张 · ${formatBytes(totalBytes)}` : '可选，支持粘贴或选择截图'}</small></span>
      <div className="feedback-upload-row">
        <label className={`feedback-dropzone ${isFull || submitting || files.length >= MAX_FEEDBACK_FILES ? 'disabled' : ''}`}>
          <input
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            multiple
            disabled={isFull || submitting || files.length >= MAX_FEEDBACK_FILES}
            onChange={e => {
              if (e.target.files?.length) addFiles(e.target.files);
              e.target.value = '';
            }}
          />
          <span className="feedback-dropzone-icon" aria-hidden="true">🖼</span>
          <span className="feedback-dropzone-text">点击选择图片，或直接按 Ctrl + V 粘贴截图</span>
          <small className="feedback-dropzone-hint">支持 PNG / JPG / WebP / GIF</small>
        </label>
      </div>
      {files.length > 0 && <div className="feedback-attachment-list">
        {files.map((file, idx) => <div className="feedback-attachment-chip" key={`${file.name}-${idx}`}>
          {previews[idx] && <img src={previews[idx]} alt={file.name} />}
          <div className="feedback-attachment-meta">
            <span title={file.name}>{file.name}</span>
            <small>{formatBytes(file.size)}</small>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label={`移除 ${file.name}`}
            onClick={() => setFiles(old => old.filter((_, i) => i !== idx))}
            disabled={submitting}
          >×</button>
        </div>)}
      </div>}
    </div>
    {error && !isFull && <p className="error-message" role="alert">{error}</p>}
    <div className="form-actions">
      <button className="primary" type="submit" disabled={isFull || submitting || !content.trim()}>
        {submitting ? '正在发送…' : '提交反馈'}
      </button>
      <button className="secondary" type="button" onClick={onClose} disabled={submitting}>取消</button>
    </div>
  </form>;
}

export default function App() {
  const initial = useRef(parseRoute(location.hash));
  const [status, setStatus] = useState<Status | null>(null);
  const readable = Boolean(status?.authenticated || (status?.mode === 'server' && status.public_readonly));
  const [bootError, setBootError] = useState('');
  const [catalog, setCatalog] = useState<Catalog>(emptyCatalog);
  const [query, setQuery] = useState(initial.current.query);
  const [searchedQuery, setSearchedQuery] = useState(initial.current.query);
  const [filters, setFilters] = useState<Filters>(initial.current.filters);
  const [theme, setTheme] = useState(document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
  function toggleTheme() {
    const next = theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    setTheme(next);
    try { localStorage.setItem('hsr-theme', next); } catch { /* The current session still switches theme. */ }
  }
  const [mode, setMode] = useState<SearchMode>(initial.current.mode);
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState('');
  const [selected, setSelected] = useState<Result | null>(null);
  const [detail, setDetail] = useState<MissionResponse | null>(null);
  const [reading, setReading] = useState(false);
  const [readerError, setReaderError] = useState('');
  const [readerQuery, setReaderQuery] = useState('');
  const [showSettings, setShowSettings] = useState(false);
  const [showAdminLogin, setShowAdminLogin] = useState(false);
  const [showBookmarks, setShowBookmarks] = useState(false);
  const [showVisitorKey, setShowVisitorKey] = useState(false);
  const [showModeHelp, setShowModeHelp] = useState(false);
  const [showFeedback, setShowFeedback] = useState(false);
  const [showAnnouncement, setShowAnnouncement] = useState(false);
  const [announcement, setAnnouncement] = useState<AnnouncementData>(DEFAULT_ANNOUNCEMENT);
  const [announcementLoaded, setAnnouncementLoaded] = useState(false);
  const [announcementState, setAnnouncementState] = useState<AnnouncementState>(() => loadAnnouncementState());
  const announcementAutoChecked = useRef(false);
  const unreadAnnouncement = announcementLoaded && hasUnreadAnnouncement(announcementState, announcement.id);
  const [visitorKey, setVisitorKey] = useState(storedVisitorKey);
  const [visitorEndpoint, setVisitorEndpoint] = useState(storedVisitorEndpoint);
  const [bookmarks, setBookmarks] = useState<Bookmark[]>(() => { try { return JSON.parse(localStorage.getItem('hsr-bookmarks') || '[]'); } catch { return []; } });
  const [toast, setToast] = useState('');
  const searchId = useRef(0);
  const readerId = useRef(0);
  const searchController = useRef<AbortController | null>(null);
  const readerController = useRef<AbortController | null>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const booted = useRef(false);
  const activeSearch = useRef({ filters: initial.current.filters, mode: initial.current.mode, query: initial.current.query });
  const activeSemantic = useRef<SemanticContext | null>(null);
  const semanticClient = useRef(createSemanticSearchClient());

  function updateVisitorKey(value: string, endpointValue = '') {
    const endpoint = saveVisitorEndpoint(localStorage, endpointValue);
    const key = saveVisitorKey(localStorage, value);
    semanticClient.current.clear();
    searchId.current++;
    setSearching(false);
    setVisitorKey(key);
    setVisitorEndpoint(endpoint);
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
    setReaderQuery('');
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

  useEffect(() => {
    if (!readable) {
      setAnnouncementLoaded(false);
      return;
    }
    let alive = true;
    request<AnnouncementData>('announcement')
      .then(data => { if (alive) setAnnouncement(normalizeAnnouncement(data)); })
      .catch(() => {})
      .finally(() => { if (alive) setAnnouncementLoaded(true); });
    return () => { alive = false; };
  }, [readable]);

  const selectResult = useCallback(async (result: Result, snapshot?: string, updateRoute = true, routeQuery = searchedQuery) => {
    const generation = ++readerId.current;
    readerController.current?.abort();
    const controller = new AbortController();
    readerController.current = controller;
    setSelected(result); setDetail(null); setReaderError(''); setReaderQuery(routeQuery);
    if (updateRoute) {
      history.replaceState(null, '', buildMissionHash({ ...activeSearch.current, query: routeQuery }, {
        mission_id: result.mission_id, line_id: result.line_id, script: result.script, snapshot,
      }));
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

  const runSearch = useCallback(async (
    value: string,
    offset = 0,
    overrideFilters?: Filters,
    overrideMode?: SearchMode,
    options?: { keepReader?: boolean; updateRoute?: boolean },
  ) => {
    const activeFilters = overrideFilters ?? filters;
    const activeMode = overrideMode ?? mode;
    const hasFilter = hasActiveFilters(activeFilters);
    if (!value.trim() && !hasFilter) { searchInput.current?.focus(); return; }
    const criteria = offset > 0 ? activeSearch.current : { query: value.trim(), filters: { ...activeFilters }, mode: activeMode };
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
      const prepared = criteria.mode === 'hybrid' && criteria.query && semanticContext ? await semanticClient.current.prepare(criteria.query, visitorKey, semanticContext, visitorEndpoint) : {};
      if (generation !== searchId.current) return;
      const data = await searchRequest<SearchResponse>({ ...criteria, limit: 30, offset, snapshot: offset > 0 ? response?.snapshot : semanticContext?.snapshot || undefined }, prepared, controller.signal);
      if (generation !== searchId.current) return;
      setSearchedQuery(criteria.query);
      setResponse(old => offset > 0 && old ? { ...data, matched_missions: old.matched_missions, results: [...old.results, ...data.results] } : data);
      if (offset === 0) {
        activeSearch.current = criteria;
        activeSemantic.current = semanticContext;
        if (!options?.keepReader) clearReader();
        if (options?.updateRoute !== false) {
          history.replaceState(null, '', buildSearchHash(criteria));
        }
      }
    } catch (err) { if (generation === searchId.current) setSearchError(errorText(err)); }
    finally { if (generation === searchId.current) setSearching(false); }
  }, [filters, mode, response?.snapshot, status, visitorKey, visitorEndpoint, clearReader]);

  const closeReader = useCallback(() => {
    clearReader();
    const criteria = activeSearch.current;
    if (criteria.query.trim() || hasActiveFilters(criteria.filters)) {
      history.replaceState(null, '', buildSearchHash(criteria));
      if (!response) {
        void runSearch(criteria.query, 0, criteria.filters, criteria.mode, { updateRoute: false });
      }
    } else {
      history.replaceState(null, '', '#/');
    }
  }, [clearReader, response, runSearch]);

  const applyFilters = useCallback((nextFilters: Filters) => {
    setFilters(nextFilters);
    if (query.trim() || hasActiveFilters(nextFilters)) {
      void runSearch(query, 0, nextFilters);
    } else {
      setResponse(null);
      searchId.current++;
      searchController.current?.abort();
      setSearching(false);
      activeSearch.current = { query: '', filters: emptyFilters, mode };
      clearReader();
      history.replaceState(null, '', '#/');
    }
  }, [query, mode, runSearch, clearReader]);

  useEffect(() => {
    const synchronizeKey = (event: StorageEvent) => {
      if (event.key !== VISITOR_KEY_STORAGE && event.key !== VISITOR_ENDPOINT_STORAGE && event.key !== null) return;
      semanticClient.current.clear();
      searchId.current++;
      setSearching(false);
      setVisitorKey(storedVisitorKey());
      setVisitorEndpoint(storedVisitorEndpoint());
    };
    window.addEventListener('storage', synchronizeKey);
    return () => window.removeEventListener('storage', synchronizeKey);
  }, []);

  useEffect(() => {
    if (!readable) return;
    function openRoute() {
      const route = parseRoute(location.hash);
      searchId.current++;
      searchController.current?.abort();
      setSearching(false);
      setSearchError('');
      setResponse(null);
      setQuery(route.query);
      setSearchedQuery(route.query);
      setFilters(route.filters);
      setMode(route.mode);
      activeSearch.current = { query: route.query.trim(), filters: route.filters, mode: route.mode };
      clearReader();
      const hasCriteria = Boolean(route.query.trim() || hasActiveFilters(route.filters));
      if (route.mission) {
        const result: Result = { chunk_id: 0, mission_id: route.mission, mission_name: '', chapter: '', world: '', version: '', version_status: 'unknown', speaker: '', text: '', line_id: route.line, script: route.script, branch: '', match_type: '', score: 0 };
        void selectResult(result, route.snapshot, false, route.query);
        if (hasCriteria) {
          void runSearch(route.query, 0, route.filters, route.mode, { keepReader: true, updateRoute: false });
        }
      } else if (hasCriteria) {
        void runSearch(route.query, 0, route.filters, route.mode, { updateRoute: false });
      }
    }
    if (!booted.current) {
      booted.current = true;
      openRoute();
    }
    window.addEventListener('hashchange', openRoute);
    return () => window.removeEventListener('hashchange', openRoute);
  }, [readable, runSearch, selectResult, clearReader]);

  useEffect(() => {
    if (!readable || !announcementLoaded || announcementAutoChecked.current) return;
    announcementAutoChecked.current = true;
    if (!announcement.auto_popup) return;
    const route = parseRoute(location.hash);
    const isHomeLanding = !route.mission && !route.query.trim() && !hasActiveFilters(route.filters);
    if (shouldAutoShowAnnouncement(announcementState, announcement.id, Date.now(), isHomeLanding)) {
      setShowAnnouncement(true);
    }
  }, [readable, announcementLoaded, announcement, announcementState]);

  const openAnnouncement = useCallback(() => {
    setAnnouncementState(old => {
      const next = markAnnouncementSeen(old, announcement.id);
      saveAnnouncementState(next);
      return next;
    });
    setShowAnnouncement(true);
  }, [announcement.id]);

  const closeAnnouncement = useCallback(() => {
    setAnnouncementState(old => {
      const next = markAnnouncementSeen(old, announcement.id);
      saveAnnouncementState(next);
      return next;
    });
    setShowAnnouncement(false);
  }, [announcement.id]);

  const snoozeAnnouncement = useCallback(() => {
    setAnnouncementState(old => {
      const next = snoozeAnnouncementState(old, announcement.id, Date.now(), announcement.snooze_days);
      saveAnnouncementState(next);
      return next;
    });
    setShowAnnouncement(false);
    setToast(`已设为 ${announcement.snooze_days} 天内不再自动弹出，随时可点「公告」查看`);
  }, [announcement.id, announcement.snooze_days]);

  const openFeedbackFromAnnouncement = useCallback(() => {
    closeAnnouncement();
    setShowFeedback(true);
  }, [closeAnnouncement]);

  useEffect(() => { try { localStorage.setItem('hsr-bookmarks', JSON.stringify(bookmarks)); } catch { setToast('浏览器存储空间不足，请导出收藏备份'); } }, [bookmarks]);
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(''), 3500); return () => clearTimeout(timer); }, [toast]);
  useEffect(() => { const key = (event: KeyboardEvent) => { if ((event.ctrlKey || event.metaKey) && event.key === 'k') { event.preventDefault(); searchInput.current?.focus(); } }; document.addEventListener('keydown', key); return () => document.removeEventListener('keydown', key); }, []);

  async function copy(text: string) { try { await navigator.clipboard.writeText(text); setToast('已复制到剪贴板'); } catch { setToast('无法访问剪贴板，请手动选择文字复制'); } }
  function share(line?: DialogueLine) {
    if (!selected) return;
    const params = new URLSearchParams({ mission: selected.mission_id, line: line?.id || selected.line_id, script: line?.script || selected.script });
    if (readerQuery) params.set('q', readerQuery);
    if (detail?.snapshot || response?.snapshot) params.set('snapshot', detail?.snapshot || response!.snapshot);
    void copy(`${location.origin}${location.pathname}#/search?${params}`);
  }
  const bookmarkKey = selected ? `${selected.mission_id}:${selected.line_id}:${selected.script}` : '';
  const bookmarked = bookmarks.some(b => b.key === bookmarkKey);
  function toggleBookmark() {
    if (!selected) return;
    const result = { ...selected, mission_name: detail?.mission.name || selected.mission_name, chapter: detail?.mission.chapter || selected.chapter };
    setBookmarks(old => bookmarked ? old.filter(b => b.key !== bookmarkKey) : [{ key: bookmarkKey, result, query: readerQuery, saved_at: new Date().toISOString(), snapshot: detail?.snapshot || response?.snapshot }, ...old]);
    setToast(bookmarked ? '已取消收藏' : '已收藏到当前浏览器');
  }
  async function saveVersion(version: string, source: string, state: string) {
    if (!selected || !detail?.snapshot) return;
    const generation = readerId.current;
    const previousSnapshot = detail.snapshot;
    let saved: { snapshot: string };
    try {
      saved = await request<{ snapshot: string }>(`admin/missions/${encodeURIComponent(selected.mission_id)}`, {
        method: 'PUT', body: JSON.stringify({ version, source, status: state, snapshot: previousSnapshot }),
      });
    } catch (error) {
      void refresh().catch(() => {});
      throw error;
    }
    setResponse(old => old && old.snapshot === previousSnapshot
      ? { ...old, snapshot: saved.snapshot, results: old.results.map(r => r.mission_id === selected.mission_id ? { ...r, version, version_status: state } : r) }
      : old);
    if (generation === readerId.current) {
      await selectResult({ ...selected, version, version_status: state }, saved.snapshot, true, readerQuery);
    }
    setToast('版本标注已保存');
    void refresh().catch(() => {});
  }
  async function moreContext(before = false) {
    if (!selected || !detail || reading) return;
    const generation = readerId.current;
    const currentOffset = detail.offset || 0;
    const previousOffset = Math.max(0, currentOffset - 1000);
    if (before && currentOffset === 0) return;
    setReading(true);
    const p = new URLSearchParams(before
      ? { offset: String(previousOffset), limit: String(currentOffset - previousOffset) }
      : { offset: String(currentOffset + detail.lines.length), limit: '1000' });
    if (detail.snapshot || response?.snapshot) p.set('snapshot', detail.snapshot || response!.snapshot);
    const controller = new AbortController();
    readerController.current = controller;
    try { const extra = await request<MissionResponse>(`missions/${encodeURIComponent(selected.mission_id)}?${p}`, { signal: controller.signal }); if (generation === readerId.current) setDetail(old => {
        if (!old) return extra;
        const edges = new Map([...old.edges, ...extra.edges].map(edge => [JSON.stringify(edge), edge]));
        return {
          ...old,
          offset: before ? extra.offset : old.offset,
          has_more: before ? old.has_more : extra.has_more,
          lines: before ? [...extra.lines, ...old.lines] : [...old.lines, ...extra.lines],
          edges: [...edges.values()],
        };
      }); }
    catch (err) { if (generation === readerId.current) setToast(errorText(err)); } finally { if (generation === readerId.current) setReading(false); }
  }

  const searchCharacters = useCallback(
    (q: string, signal: AbortSignal) =>
      request<{ characters: string[] }>(`characters?q=${encodeURIComponent(q)}&limit=80`, { signal }).then(data => data.characters),
    [],
  );

  if (!status) return <div className="boot-screen"><div className="brand-mark">✦</div><h1>星轨档案</h1><p>{bootError || '正在打开档案…'}</p>{bootError && <button className="secondary" onClick={() => location.reload()}>重新连接</button>}</div>;
  if (!readable) return <div className="login-screen"><div className="login-stars" aria-hidden="true" /><Login local={status.mode === 'local'} onSuccess={() => { void refresh(); }} /><div className="login-footer">星轨档案 · 崩坏：星穹铁道剧情检索</div></div>;

  const openMissionShortcut = (item: MatchedMission) => {
    const result: Result = {
      chunk_id: 0, mission_id: item.mission_id, mission_name: item.mission_name, chapter: item.chapter,
      world: item.world, type: item.type, version: item.version, version_status: item.version_status,
      speaker: '', text: '', line_id: '', script: '', branch: '', match_type: 'exact', matched_field: 'mission', score: 1,
    };
    void selectResult(result, response?.snapshot);
  };

  return <div className={`app-shell ${selected ? 'has-selection' : ''}`}>
    <header className="app-header"><a className="brand" href="#/" onClick={e => { e.preventDefault(); searchId.current++; searchController.current?.abort(); setSearching(false); clearReader(); setResponse(null); setQuery(''); setSearchedQuery(''); setFilters(emptyFilters); setMode('text'); activeSearch.current = { filters: emptyFilters, mode: 'text', query: '' }; history.replaceState(null, '', '#/'); }}><span className="brand-mark" aria-hidden="true">✦</span><span>星轨档案<small>STARRAIL STORY ARCHIVE</small></span></a>
      <div className="header-actions">{status.mode === 'server' && <a className="header-button" href="/" aria-label="返回首页" title="返回首页"><span aria-hidden="true">←</span><span>返回首页</span></a>}<span className="connection-label"><span className="status-dot" />{status.mode === 'local' ? '本地档案' : '在线档案'}</span><button className="header-button" aria-label="打开收藏" title="收藏" onClick={() => setShowBookmarks(true)}><span aria-hidden="true">☆</span><span>收藏</span>{bookmarks.length > 0 && <small>{bookmarks.length}</small>}</button>{status.mode === 'local' && <button className="header-button" aria-label="管理档案" title="管理" onClick={() => setShowSettings(true)}><span aria-hidden="true">⚙</span><span>管理</span></button>}{status.mode === 'server' && !status.public_readonly && <button className="header-button logout-button" title="退出登录" onClick={() => { void post('auth/logout').then(() => refresh()).catch(err => setToast(errorText(err))); }}><span aria-hidden="true">↪</span><span>退出</span></button>}<button className="header-button theme-toggle" onClick={toggleTheme} aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'} title={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}><span aria-hidden="true">{theme === 'dark' ? '☀' : '☾'}</span></button></div>
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
          <span className="mode-label" id="search-mode-label">搜索模式</span>
          <div className="mode-tabs" role="group" aria-labelledby="search-mode-label">
            <button type="button" className="mode-recommended" aria-pressed={mode === 'hybrid'} disabled={!indexReady(status.embedding)} title={!indexReady(status.embedding) ? '完整语义索引就绪后可用' : '推荐：只记得大概情节也能搜'} onClick={() => chooseMode('hybrid')}>描述找剧情<span className="mode-recommend">推荐</span></button>
            <button type="button" aria-pressed={mode === 'text'} onClick={() => chooseMode('text')}>精准关键词</button>
            <button type="button" aria-pressed={mode === 'fuzzy'} onClick={() => chooseMode('fuzzy')}>错字容错</button>
          </div>
          <div className="mode-help-wrap">
            <button type="button" className="mode-help-button" aria-label="查看搜索模式说明" aria-haspopup="dialog" aria-describedby="mode-help-tip" onClick={() => setShowModeHelp(true)}>!</button>
            <div id="mode-help-tip" className="mode-help-tooltip" role="tooltip">
              <p><strong>描述找剧情：</strong>忘了台词，只记得大概情节</p>
              <p><strong>精准关键词：</strong>记得原话、角色名、外号或任务名</p>
              <p><strong>错字容错：</strong>记得大致台词，但怕记错或漏字</p>
              <span>点击查看详细用法与举例</span>
            </div>
          </div>
          <button type="button" className="text-button semantic-key-link" onClick={() => setShowVisitorKey(true)}>🔑 语义密钥{visitorKey && <span>已配置</span>}</button>
          <button type="button" className="text-button feedback-open-link" onClick={() => setShowFeedback(true)}>💬 意见反馈</button>
          <button type="button" className="text-button announcement-open-link" onClick={openAnnouncement}>
            📢 公告{unreadAnnouncement && <span className="announcement-dot" aria-label="有未读更新" />}
          </button>
        </div>
        <div className="search-options">
          <div className="filter-row">
            <SelectField label="世界" value={filters.world} values={catalog.worlds} onChange={v => applyFilters({ ...filters, world: v })} />
            <SelectField label="版本" value={filters.version} values={catalog.versions} onChange={v => applyFilters({ ...filters, version: v })} />
            <SelectField label="任务类型" value={filters.type} values={catalog.types} onChange={v => applyFilters({ ...filters, type: v })} />
            <SelectField label="角色" value={filters.character} values={catalog.characters} searchable format={speakerName} onSearch={searchCharacters} onChange={v => applyFilters({ ...filters, character: v })} />
            {Object.values(filters).some(Boolean) && <button type="button" className="text-button clear-filters" onClick={() => applyFilters(emptyFilters)}>重置筛选</button>}
          </div>
        </div>
      </div>
    </section>
    <main className="workspace"><section className="results-panel" aria-label="搜索结果"><div className="results-heading"><h2>{response ? response.search_mode === 'fuzzy' ? '错字容错结果' : response.search_mode === 'hybrid' ? '描述找剧情结果' : '精准关键词结果' : '剧情检索'}</h2>{response ? <span>{response.truncated ? '优先展示 ' : ''}{formatNumber(response.total)} 条相关结果{response.elapsed_ms != null && <small> · {(response.elapsed_ms / 1000).toFixed(2)} 秒</small>}</span> : <span>{formatNumber(status.data.stats?.narrative_lines ?? status.data.stats?.lines)} 条对白记录</span>}</div>
      <div className="results-scroll" aria-busy={searching} aria-live="polite">
        {searchError && <div className="inline-alert" role="alert">{searchError}</div>}
        {response?.notice && <div className="search-notice">{response.notice}</div>}
        {!status.data.ready ? <Empty title="准备你的第一份档案"><p>导入剧情数据后，就可以搜索台词和阅读上下文。</p>{status.admin && <button className="secondary" onClick={() => setShowSettings(true)}>导入数据 →</button>}</Empty> : !response ? <div className="search-welcome"><div className="welcome-orbit" aria-hidden="true"><span>✧</span></div><h2>从你记得的地方开始</h2><p>一句台词、一个名字，<br />或某次旅途里的模糊记忆。</p><div className="suggestions"><span>试着搜索</span>{(mode === 'hybrid' ? ['流萤带开拓者逛游乐园', '丹恒与景元在幽囚狱交谈', '卡芙卡在空间站唤醒开拓者'] : ['生命因何而沉睡', '三月七 星穹列车', '流萤', '老杨']).map(value => <button key={value} onClick={() => { setQuery(value); void runSearch(value); }}>{value}<span>↗</span></button>)}</div></div> : (response.results.length === 0 && !response.matched_missions?.length) ? <Empty title="还没有找到相关剧情"><p>尝试缩短台词、使用角色名，或减少筛选条件。</p></Empty> : <div className="result-list">{Boolean(response.matched_missions?.length) && <div className="matched-missions" aria-label="同名任务直达"><span className="matched-missions-title">同名任务直达</span>{response.matched_missions!.map(item => {
          const title = cleanMissionName(item.mission_name, item.chapter, item.world);
          const isSelected = selected?.mission_id === item.mission_id && !selected?.line_id;
          return <button key={`mm:${item.mission_id}`} type="button" className={`mission-shortcut-card ${isSelected ? 'selected' : ''}`} onClick={() => openMissionShortcut(item)}>
            <div className="result-topline">
              <div className="result-badges">
                {item.type && <span className={`mission-badge type-${missionTypeClass(item.type)}`}>{labels[item.type] || item.type}</span>}
                <span className="world-badge">{item.world || '剧情对白'}</span>
              </div>
              <span className={!item.version ? 'pending-version' : 'version-tag'}>{versionText(item.version, item.version_status)}</span>
            </div>
            <h3>{title}{item.chapter && item.chapter !== title && <small className="result-chapter"> · {item.chapter}</small>}</h3>
            <div className="result-bottom"><span>{item.line_count ? `共 ${formatNumber(item.line_count)} 条记录` : '任务名称精确匹配'}</span><span className="result-open">直达任务 <span>→</span></span></div>
          </button>;
        })}</div>}{response.results.map((result, index) => {
          const origin = result.dialogue_origin;
          const title = origin?.title || cleanMissionName(result.mission_name, result.chapter, result.world);
          return <button className={`result-card ${selected?.line_id === result.line_id && selected?.script === result.script ? 'selected' : ''}`} key={`${result.chunk_id}:${result.line_id}:${index}`} onClick={() => void selectResult(result, response.snapshot)}>
          <div className="result-topline">
            <div className="result-badges">
              {!origin && result.type && <span className={`mission-badge type-${missionTypeClass(result.type)}`}>{labels[result.type] || result.type}</span>}
              <span className="world-badge">{result.world || '剧情对白'}</span>
              {result.kind?.includes('option') && <span className="option-badge">❖ 玩家选项</span>}
            </div>
            <span className={origin || !result.version ? 'pending-version' : 'version-tag'}>{origin ? '对白版本待核实' : versionText(result.version, result.version_status)}</span>
          </div>
          <h3>{title}{!origin && result.chapter && result.chapter !== title && <small className="result-chapter"> · {result.chapter}</small>}</h3>
          <p className="result-text"><strong>{result.kind?.includes("option") ? "❖ 开拓者选项" : speakerName(result.speaker)}</strong><span><Highlight text={result.display_text || result.text} query={searchedQuery} /></span></p>
          {origin && <p className="result-origins">多个出处 · {origin.origins.map(item => item.name).join(' / ')}</p>}
          <div className="result-bottom"><span>{matchLabel(result)}</span><span className="result-open">阅读上下文 <span>→</span></span></div>
        </button>; })}{response.results.length < response.total && <button className="secondary load-results" disabled={searching} onClick={() => void runSearch(searchedQuery, response.results.length)}>{searching ? '加载中…' : '加载更多结果'}</button>}</div>}
      </div><footer className="results-footer"><a href="https://github.com/JahsehXL/hsr-story-search" target="_blank" rel="noreferrer">GitHub 仓库 ↗</a></footer>
    </section>
    <Reader selected={selected} detail={detail} busy={reading} query={readerQuery} error={readerError} isAdmin={status.admin} bookmarked={bookmarked} onBookmark={toggleBookmark} onCopy={text => void copy(text)} onShare={share} onBack={closeReader} onVersionSave={saveVersion}
      onOpenCurrentForVersion={selected && status.data.snapshot && detail?.snapshot && detail.snapshot !== status.data.snapshot
        ? () => void selectResult(selected, status.data.snapshot, true, readerQuery) : undefined}
      onMore={() => void moreContext()} onEarlier={() => void moreContext(true)} />
    </main>
    {showModeHelp && <Modal title="搜索模式说明" close={() => setShowModeHelp(false)}>
      <div className="mode-help-modal">
        <section className="mode-help-card">
          <div className="mode-help-card-head">
            <h4>描述找剧情</h4>
            <span className="mode-help-tag recommended">推荐 · 需先配置密钥</span>
          </div>
          <p><strong>适用场景：</strong>完全想不起原台词，只记得人物、地点或大概发生过什么事。</p>
          <p><strong>怎么输入：</strong>像平时说话一样把画面或情节写出来，例如 <code>流萤带开拓者逛游乐园</code>、<code>星期日和知更鸟在剧场争论</code>。</p>
          <p className="mode-help-note">首次使用点旁边的「🔑 语义密钥」按引导配置支持当前模型的服务，密钥只保存在当前浏览器。</p>
        </section>
        <section className="mode-help-card">
          <div className="mode-help-card-head">
            <h4>精准关键词</h4>
            <span className="mode-help-tag">无需密钥</span>
          </div>
          <p><strong>适用场景：</strong>记得一两句原台词、任务名字、版本号、角色名或玩家常用外号。</p>
          <p><strong>怎么输入：</strong>直接输入台词或词组，多个词可用<strong>空格隔开</strong>一起搜，例如 <code>生命因何而沉睡</code>、<code>三月七 星穹列车</code>；也支持常见外号（如搜 <code>老杨</code> 会同时搜 <code>瓦尔特</code>、搜 <code>萨姆</code> 会同时搜 <code>流萤</code>）。</p>
        </section>
        <section className="mode-help-card">
          <div className="mode-help-card-head">
            <h4>错字容错</h4>
            <span className="mode-help-tag">无需密钥</span>
          </div>
          <p><strong>适用场景：</strong>大致记得某句台词怎么说，但记混了个别字、中间漏了字或有错别字，用「精准关键词」搜不到时。</p>
          <p><strong>怎么输入：</strong>至少输入 3 个字，例如把“生命因何而沉睡”记成 <code>生命因啥而沉睡</code> 也能找回原剧情。</p>
        </section>
        <p className="mode-help-summary">默认使用免密钥的「精准关键词」。日常使用建议：推荐「描述找剧情」，配置好「🔑 语义密钥」后描述你记得的情节；记得原话或人名时可切换到免密钥的「精准关键词」；记错字时切换「错字容错」。</p>
      </div>
    </Modal>}
    {showVisitorKey && <Modal title="配置语义搜索密钥" close={() => setShowVisitorKey(false)}><VisitorKeyForm initialKey={visitorKey} initialEndpoint={visitorEndpoint} model={status.embedding.index_model || 'BAAI/bge-m3'} save={updateVisitorKey} clear={() => updateVisitorKey('', '')} /></Modal>}
    {showFeedback && <Modal title="意见反馈" close={() => setShowFeedback(false)}>
      <FeedbackModal
        full={Boolean(status.feedback?.full)}
        context={(selected || searchedQuery) ? {
          mission_id: detail?.mission?.id || selected?.mission_id || '',
          mission_name: (detail?.mission?.name || selected?.mission_name) ? cleanMissionName(detail?.mission?.name || selected?.mission_name, detail?.mission?.chapter || selected?.chapter, detail?.mission?.world || selected?.world) : '',
          chapter: detail?.mission?.chapter || selected?.chapter || '',
          world: detail?.mission?.world || selected?.world || '',
          version: detail?.mission?.version || selected?.version || '',
          line_id: detail?.anchor_line_id || selected?.line_id || '',
          speaker: selected?.speaker || '',
          text: selected?.display_text || selected?.text || '',
          script: selected?.script || '',
          branch: selected?.branch || '',
          query: (selected ? readerQuery : searchedQuery) || query.trim(),
          mode,
        } : null}
        onSuccess={() => {
          setShowFeedback(false);
          setToast('感谢你的反馈！已保存到服务器');
          void refresh().catch(() => {});
        }}
        onClose={() => setShowFeedback(false)}
      />
    </Modal>}
    {showAnnouncement && (() => {
      const noteParagraphs = parseNoteParagraphs(announcement.note_body);
      const changelogSections = parseChangelogBody(announcement.changelog_body);
      return <Modal title={announcement.title || '站点公告与更新日志'} close={closeAnnouncement} className="modal-announcement">
        <div className="announcement-body">
          {noteParagraphs.length > 0 && <section className="announcement-note" aria-label={announcement.note_title || '写在前面'}>
            <div className="announcement-note-head">
              {announcement.note_title ? <span className="announcement-note-badge">✦ {announcement.note_title}</span> : <span />}
              <button type="button" className="text-button announcement-feedback-inline" onClick={openFeedbackFromAnnouncement}>💬 去提反馈 →</button>
            </div>
            {noteParagraphs.map((text, index) => <p key={index}>{text}</p>)}
          </section>}
          {changelogSections.length > 0 && <section className="announcement-changelog" aria-label={announcement.changelog_title || '最近更新'}>
            <div className="announcement-changelog-head">
              <h3 className="announcement-section-title">{announcement.changelog_title || '最近更新'}</h3>
              <span className="announcement-scroll-hint">上下滑动查看</span>
            </div>
            <div className="announcement-changelog-window">
              <div className="announcement-releases">
                {changelogSections.map((release, idx) => <article className="announcement-release" key={`${release.heading}-${idx}`}>
                  <div className="announcement-release-head">
                    <time className="announcement-date">{release.heading}</time>
                    {release.tag && <span className="announcement-tag">{release.tag}</span>}
                  </div>
                  <ul>{release.items.map((item, itemIdx) => <li key={itemIdx}>{item}</li>)}</ul>
                </article>)}
              </div>
            </div>
          </section>}
        </div>
        <footer className="announcement-footer">
          <button type="button" className="text-button announcement-snooze" onClick={snoozeAnnouncement}>{announcement.snooze_days} 天内不再弹出</button>
          <button type="button" className="primary" onClick={closeAnnouncement}>我知道了</button>
        </footer>
      </Modal>;
    })()}
    {showAdminLogin && <Modal title="管理员登录" close={() => setShowAdminLogin(false)}><Login admin onSuccess={() => { void refresh().then(() => { setShowAdminLogin(false); setShowSettings(true); }); }} /></Modal>}
    {showSettings && <Modal title="档案管理" close={() => setShowSettings(false)} wide><Suspense fallback={<p role="status">正在加载管理工具…</p>}><Admin status={status} onRefresh={() => refresh()} onToast={setToast} onAnnouncementSaved={next => setAnnouncement(normalizeAnnouncement(next))} /></Suspense></Modal>}
    {showBookmarks && <Modal title="收藏的剧情" close={() => setShowBookmarks(false)}><div className="bookmark-modal"><div className="bookmark-info"><p>保存在当前浏览器，可导出备份。</p><button className="text-button" disabled={!bookmarks.length} onClick={() => downloadJson('星轨档案-收藏.json', bookmarks)}>导出收藏 ↗</button></div>{bookmarks.length ? bookmarks.map(bookmark => <div className="bookmark-row" key={bookmark.key}><button onClick={() => { setShowBookmarks(false); void selectResult(bookmark.result, bookmark.snapshot, true, bookmark.query); }}><h3>{cleanMissionName(bookmark.result.mission_name, bookmark.result.chapter, bookmark.result.world)}</h3><p>{displayText(bookmark.result.display_text || bookmark.result.text) || '打开收藏位置'}</p><small>{bookmark.result.chapter}</small></button><button className="icon-button" onClick={() => setBookmarks(old => old.filter(b => b.key !== bookmark.key))} aria-label="移除收藏" title="移除收藏">×</button></div>) : <Empty symbol="☆" title="还没有收藏"><p>阅读剧情时，点击标题旁的星标即可收藏。</p></Empty>}</div></Modal>}
    {toast && <div className="toast" role="status">{toast}</div>}
  </div>;
}
