import { useEffect, useMemo, useState } from 'react';
import { displayText, Empty, errorText, Highlight, speakerName, versionText } from './presentation';
import { buildStoryModel, focusStory, resolveStory, shareStoryAnchor, windowStoryGroup } from './story';
import type { ChoiceSet, StoryFocus } from './story';
import type { DialogueLine, MissionResponse, Result } from './types';

interface ReaderProps {
  selected: Result | null;
  detail: MissionResponse | null;
  busy: boolean;
  query: string;
  error: string;
  isAdmin: boolean;
  bookmarked: boolean;
  onBookmark: () => void;
  onCopy: (text: string) => void;
  onShare: (line?: DialogueLine) => void;
  onBack: () => void;
  onVersionSave: (version: string, source: string, status: string) => Promise<void>;
  onExpand: () => void;
  onMore: () => void;
}

const missionTypes: Record<string, string> = { Main: '开拓任务', Branch: '冒险任务', Companion: '同行任务', Daily: '日常任务', Interlude: '开拓续闻', Gap: '开拓续闻' };

function Dialogue({ line, query, hit, onShare }: { line: DialogueLine; query: string; hit: boolean; onShare: (line: DialogueLine) => void }) {
  return <article id={`line-${line.id}`} className={`dialogue-line ${hit ? 'line-hit' : ''}`}>
    <div className="speaker">{speakerName(line.speaker)}{hit && <span className="hit-label">定位原句</span>}<button className="line-anchor" onClick={() => onShare(line)} title="复制这句话的位置" aria-label="复制这句话的位置">#</button></div>
    <p><Highlight text={line.text} query={query} /></p>
  </article>;
}

function ChoiceCard({ choice, selectedId, hitId, query, onChoose, onShare }: {
  choice: ChoiceSet; selectedId?: string; hitId: string; query: string;
  onChoose: (option: DialogueLine) => void; onShare: (line: DialogueLine) => void;
}) {
  const [open, setOpen] = useState(false);
  const current = choice.options.find(option => option.id === selectedId);
  return <section className={`choice-card ${current?.id === hitId ? 'choice-hit' : ''}`} aria-label="玩家回答">
    <div className="choice-card-label">玩家回答{current?.id === hitId && <span className="hit-label">定位原句</span>}</div>
    <div className="choice-current" id={current ? `line-${current.id}` : undefined}>
      <div>{current ? <Highlight text={current.text} query={query} /> : '选择一条回答，查看后续对白'}</div>
      <button className="choice-switch" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? '收起回答' : current ? '切换回答' : `查看 ${choice.options.length} 条回答`}</button>
    </div>
    {open && <div className="choice-options">
      {choice.options.map(option => <button className="choice-option" key={option.id} aria-pressed={option.id === current?.id} onClick={() => { onChoose(option); setOpen(false); }}><Highlight text={option.text} query={query} /><span aria-hidden="true">{option.id === current?.id ? '✓' : '→'}</span></button>)}
    </div>}
    {current && <div className="choice-note">{!current.trigger && <span>当前数据未提供这条回答的后续关联。</span>}<button className="text-button" onClick={() => onShare(current)}>复制回答位置</button></div>}
  </section>;
}

export default function Reader({ selected, detail, busy, query, error, isAdmin, bookmarked, onBookmark, onCopy, onShare, onBack, onVersionSave, onExpand, onMore }: ReaderProps) {
  const [editing, setEditing] = useState(false);
  const [version, setVersion] = useState('');
  const [source, setSource] = useState('');
  const [versionState, setVersionState] = useState('verified');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [navigation, setNavigation] = useState<{ identity: string; focus: StoryFocus } | null>(null);
  const [expandedPassages, setExpandedPassages] = useState<{ identity: string; keys: string[] }>({ identity: '', keys: [] });
  const mission = detail?.mission;
  const identity = JSON.stringify([selected?.mission_id, selected?.script, selected?.line_id]);
  const model = useMemo(() => buildStoryModel(detail?.lines || [], detail?.edges || []), [detail?.lines, detail?.edges]);
  const initialFocus = useMemo(() => focusStory(model, selected?.line_id || '', selected?.script || ''), [model, selected?.line_id, selected?.script]);
  const focus = navigation?.identity === identity && model.byKey.has(navigation.focus.rootKey) ? navigation.focus : initialFocus;
  const visible = useMemo(() => resolveStory(model, focus), [model, focus]);
  const visibleKeys = new Set(visible.groups.map(group => group.key));
  const hiddenCount = model.groups.filter(group => !visibleKeys.has(group.key)).length;
  const expandedKeys = expandedPassages.identity === identity ? expandedPassages.keys : [];
  const passages = visible.groups.map(group => ({ group, window: windowStoryGroup(group, selected?.line_id || '', expandedKeys.includes(group.key), focus.selections) }));
  const visibleLines = passages.flatMap(passage => passage.window.lines.filter(line => !line.kind.includes('option') || Object.values(focus.selections).includes(line.id)));
  const shareAnchor = shareStoryAnchor(visibleLines, focus.selections, selected?.line_id || '');
  const focusedHitVisible = visibleLines.some(line => line.id === selected?.line_id);
  const atInitialFocus = focus.rootKey === initialFocus.rootKey && JSON.stringify(focus.selections) === JSON.stringify(initialFocus.selections);
  useEffect(() => {
    setVersion(mission?.version || '');
    setSource(mission?.version_source || '');
    setVersionState(mission?.version_status === 'inferred' ? 'inferred' : 'verified');
    setEditing(false);
    setSaveError('');
  }, [mission?.id, mission?.version, mission?.version_source, mission?.version_status]);

  function choose(choice: ChoiceSet, line: DialogueLine) {
    setNavigation({ identity, focus: { rootKey: focus.rootKey, selections: { ...focus.selections, [choice.id]: line.id } } });
  }

  function openPassage(key: string) {
    setNavigation({ identity, focus: { rootKey: key, selections: {} } });
  }

  function togglePassage(key: string) {
    setExpandedPassages({ identity, keys: expandedKeys.includes(key) ? expandedKeys.filter(value => value !== key) : [...expandedKeys, key] });
  }

  if (!selected) return <section className="reader reader-idle"><div><h2>选择结果，回到故事里</h2><p>查看台词上下文、任务与开放版本。</p></div></section>;
  const name = mission?.name || selected.mission_name || '剧情对白';
  const chapter = mission?.chapter || selected.chapter;
  const copySource = [name, chapter, mission?.world || selected.world, versionText(mission?.version || selected.version, mission?.version_status || selected.version_status)].filter(Boolean).join(' · ');
  const copyText = visibleLines.length ? visibleLines.map(line => `${speakerName(line.speaker)}：${displayText(line.text)}`).join('\n\n') : `${speakerName(selected.speaker)}：${selected.text}`;
  const hasMore = detail?.has_more ?? (detail ? detail.total > (detail.offset || 0) + detail.lines.length : false);

  return <section className="reader reader-active">
    <header className="reader-heading">
      <button className="mobile-back text-button" onClick={onBack}>← 返回结果</button>
      <div className="reader-breadcrumb">{mission?.world || selected.world || '剧情对白'}{chapter && <><span>/</span>{chapter}</>}</div>
      <div className="reader-title-row"><h1>{name}</h1><button className={`icon-button bookmark-button ${bookmarked ? 'is-saved' : ''}`} title={bookmarked ? '取消收藏命中结果' : '收藏命中结果'} aria-label={bookmarked ? '取消收藏命中结果' : '收藏命中结果'} onClick={onBookmark}>{bookmarked ? '★' : '☆'}</button></div>
      <div className="reader-meta"><span className={`version-label ${!(mission?.version || selected.version) ? 'unverified' : ''}`}>{versionText(mission?.version || selected.version, mission?.version_status || selected.version_status)}</span>{mission?.type && <span>{missionTypes[mission.type] || mission.type}</span>}
        {mission?.version_source && /^https?:\/\//.test(mission.version_source) && <a href={mission.version_source} target="_blank" rel="noreferrer">版本依据 ↗</a>}
        {isAdmin && mission && <button className="text-button" onClick={() => setEditing(value => !value)}>补充版本</button>}
      </div>
      {editing && <form className="version-editor" onSubmit={async event => {
        event.preventDefault(); setSaving(true); setSaveError('');
        try { await onVersionSave(version, source, versionState); setEditing(false); }
        catch (failure) { setSaveError(errorText(failure)); }
        finally { setSaving(false); }
      }}>
        <label className="field"><span>开放版本</span><input value={version} onChange={event => setVersion(event.target.value)} placeholder="例如 2.0" required={versionState === 'verified'} /></label>
        <label className="field"><span>依据链接或说明</span><input value={source} onChange={event => setSource(event.target.value)} placeholder="官方公告链接或核验说明" required={versionState === 'verified'} /></label>
        <label className="field"><span>核验状态</span><select value={versionState} onChange={event => setVersionState(event.target.value)}><option value="verified">已核实</option><option value="inferred">推定</option><option value="unknown">待核实</option></select></label>
        <button className="primary" disabled={saving}>{saving ? '保存中…' : '保存标注'}</button>{saveError && <p className="error-message" role="alert">{saveError}</p>}
      </form>}
    </header>
    <div className="reader-scroll" aria-busy={busy}>
      {busy && !detail ? <div className="reading-skeleton"><div /><div /><div /></div> : error ? <Empty symbol="!" title="暂时无法打开这段剧情"><p>{error}</p></Empty> : detail ? <>
        <div className="reading-focusbar"><span>{atInitialFocus ? '当前命中段落' : '正在浏览其他剧情'}</span>{!atInitialFocus && <button className="text-button" onClick={() => setNavigation({ identity, focus: initialFocus })}>回到命中</button>}</div>
        {!focusedHitVisible && selected.line_id && atInitialFocus && <p className="choice-note">当前页未包含命中原句，可继续加载上下文或重新打开搜索结果。</p>}
        <div className="story-flow">
          {passages.map(({ group, window }) => <div className="story-segment" key={group.key}>
            {window.canExpand && <div className="passage-window-note"><span>{window.expanded ? '本段完整对白' : '显示命中附近的对白'}</span><button className="text-button" onClick={() => togglePassage(group.key)}>{window.expanded ? '收起完整对白' : `展开本段完整对白（共 ${group.lines.length} 条）`}</button></div>}
            {window.segments.map((segment, index) => segment.kind === 'dialogue' ? <div key={`${group.key}-${index}`}>
              {segment.lines.map(line => <Dialogue key={line.id} line={line} query={query} hit={line.id === selected.line_id} onShare={onShare} />)}
            </div> : <ChoiceCard key={segment.choice.id} choice={segment.choice} selectedId={focus.selections[segment.choice.id]} hitId={selected.line_id} query={query} onChoose={line => choose(segment.choice, line)} onShare={onShare} />)}
          </div>)}
        </div>
        {visible.missingTargets.length > 0 && <p className="choice-note">这条回答的后续未出现在当前已加载段落中。{hasMore ? '可继续加载上下文。' : '可查看任务其他段落。'}</p>}
        {detail.lines.length === 0 && <Empty title="这条记录暂无上下文"><p>可在搜索结果中查看命中的原文。</p></Empty>}
        {hiddenCount > 0 && <details className="story-alternatives"><summary>其他剧情<span>{hiddenCount} 个段落</span></summary>
          <div className="passage-picker"><label className="field"><span>选择段落</span><select aria-label="选择其他剧情段落" value={focus.rootKey} onChange={event => openPassage(event.target.value)}>{model.groups.map((group, index) => <option value={group.key} key={group.key}>{index + 1} · {speakerName(group.lines[0]?.speaker || '')} · {displayText(group.lines[0]?.text || '剧情段落').slice(0, 70)}</option>)}</select></label>
            <div className="passage-picker-actions"><button className="text-button" disabled={model.groups[0]?.key === focus.rootKey} onClick={() => openPassage(model.groups[Math.max(0, model.groups.findIndex(group => group.key === focus.rootKey) - 1)].key)}>上一段</button><button className="text-button" disabled={model.groups[model.groups.length - 1]?.key === focus.rootKey} onClick={() => openPassage(model.groups[Math.min(model.groups.length - 1, model.groups.findIndex(group => group.key === focus.rootKey) + 1)].key)}>下一段</button></div>
          </div>
        </details>}
        {hasMore && <button className="secondary load-context" onClick={onMore} disabled={busy}>{busy ? '加载中…' : '继续加载上下文'}</button>}
        <div className="reader-end">{selected.script && <button className="text-button" onClick={onExpand}>查看任务其他段落 →</button>}</div>
      </> : <article className="dialogue-line standalone"><div className="speaker">{speakerName(selected.speaker)}</div><p><Highlight text={selected.text} query={query} /></p></article>}
      {mission?.version_source && !/^https?:\/\//.test(mission.version_source) && <div className="source-note"><span>版本依据</span><p>{mission.version_source}</p></div>}
      {!!detail?.recalls?.length && <details className="recall-note"><summary>剧情回顾线索</summary>{detail.recalls.map((recall, index) => <p key={index}>{recall.name || recall.title || '回顾段落'}{!recall.verified && <span>关联线索待游戏内核验</span>}{recall.path && <span>{recall.path}</span>}{recall.description && <span>{recall.description}</span>}</p>)}</details>}
    </div>
    <footer className="reader-actions"><button className="text-button" onClick={() => onCopy(copyText)}>复制当前对白</button><button className="text-button" onClick={() => onCopy(copySource)}>复制出处</button><button className="text-button share-action" onClick={() => onShare(shareAnchor)}>分享位置 ↗</button></footer>
  </section>;
}
