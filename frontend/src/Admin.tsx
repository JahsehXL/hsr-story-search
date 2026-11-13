import { useEffect, useState } from 'react';
import { apiUrl, downloadJson, post, request } from './api';
import { errorText, Empty } from './presentation';
import type { Job, Settings, Status } from './types';

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

export default function Admin({ status, onRefresh, onToast }: { status: Status; onRefresh: () => Promise<Status>; onToast: (message: string) => void }) {
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
