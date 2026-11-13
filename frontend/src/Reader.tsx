import { useEffect, useMemo, useRef, useState } from 'react';
import { cleanMissionName, displayText, Empty, errorText, Highlight, speakerName, versionText } from './presentation';
import { buildStoryModel, focusStory, resolveStory, shareStoryAnchor, windowStoryGroup, withDefaultChoices } from './story';
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
  const name = speakerName(line.speaker);
  const isPlayer = name === '开拓者';
  return <article id={`line-${line.id}`} className={`dialogue-line ${hit ? 'line-hit' : ''} ${isPlayer ? 'is-player' : ''}`}>
    <div className="speaker"><span className="speaker-name">{name}</span></div>
    <div className="dialogue-body">
      <p><Highlight text={line.text} query={query} /></p>
      <div className="dialogue-meta">
        {hit && <span className="hit-label">✦ 定位原句</span>}
        <button className="line-anchor" onClick={() => onShare(line)} title="复制这句话的位置" aria-label="复制这句话的位置">#</button>
      </div>
    </div>
  </article>;
}

function ChoiceCard({ choice, selectedId, hitId, query, onChoose, onShare }: {
  choice: ChoiceSet; selectedId?: string; hitId: string; query: string;
  onChoose: (option: DialogueLine) => void; onShare: (line: DialogueLine) => void;
}) {
  const current = choice.options.find(option => option.id === selectedId) || choice.options[0];
  const anyHit = choice.options.some(option => option.id === hitId);
  return <section className={`choice-card ${anyHit ? 'choice-hit' : ''}`} aria-label="玩家回答">
    <div className="choice-card-header">
      <div className="choice-card-label">
        <span className="choice-icon" aria-hidden="true">❖</span>
        <span>玩家对话选项</span>
        <small>共 {choice.options.length} 项 · 点击切换下方分支回应</small>
      </div>
      {current && <button className="text-button choice-share" onClick={() => onShare(current)}>复制选项位置 #</button>}
    </div>
    <div className="choice-options">
      {choice.options.map((option, idx) => {
        const isSelected = option.id === current?.id;
        const isHit = option.id === hitId;
        return <button
          id={`line-${option.id}`}
          className={`choice-option ${isSelected ? 'is-selected' : ''} ${isHit ? 'is-hit' : ''}`}
          key={option.id}
          type="button"
          aria-pressed={isSelected}
          onClick={() => onChoose(option)}
        >
          <span className="choice-option-badge" aria-hidden="true">{isSelected ? '❖' : '◇'} 选项 {idx + 1}</span>
          <span className="choice-option-text"><Highlight text={option.text} query={query} /></span>
          <span className="choice-option-meta">
            {isHit && <span className="hit-label">✦ 命中原句</span>}
            <span className="choice-option-state" aria-hidden="true">{isSelected ? '当前展示' : '切换分支 →'}</span>
          </span>
        </button>;
      })}
    </div>
    {current && !current.trigger && <div className="choice-note"><span>该选项无独立分支脚本，选择后直接衔接后续主线对白。</span></div>}
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
  const scrollRef = useRef<HTMLDivElement>(null);
  const mission = detail?.mission;
  const identity = JSON.stringify([selected?.mission_id, selected?.script, selected?.line_id]);
  const model = useMemo(() => buildStoryModel(detail?.lines || [], detail?.edges || []), [detail?.lines, detail?.edges]);
  const initialFocus = useMemo(() => withDefaultChoices(model, focusStory(model, selected?.line_id || '', selected?.script || '')), [model, selected?.line_id, selected?.script]);
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
  const selectedTriggers = useMemo(() => {
    const triggers = new Set<string>();
    for (const group of visible.groups) {
      for (const segment of group.segments) {
        if (segment.kind !== 'choice') continue;
        const chosen = segment.choice.options.find(opt => opt.id === focus.selections[segment.choice.id]);
        if (chosen?.trigger) triggers.add(chosen.trigger);
      }
    }
    return triggers;
  }, [visible.groups, focus.selections]);

  useEffect(() => {
    setVersion(mission?.version || '');
    setSource(mission?.version_source || '');
    setVersionState(mission?.version_status === 'inferred' ? 'inferred' : 'verified');
    setEditing(false);
    setSaveError('');
  }, [mission?.id, mission?.version, mission?.version_source, mission?.version_status]);

  useEffect(() => {
    if (!detail || !selected?.line_id || !scrollRef.current) return;
    const target = scrollRef.current.querySelector<HTMLElement>(`#line-${CSS.escape(selected.line_id)}`);
    if (target) {
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }, [detail, selected?.line_id]);

  function choose(choice: ChoiceSet, line: DialogueLine) {
    const nextFocus = withDefaultChoices(model, { rootKey: focus.rootKey, selections: { ...focus.selections, [choice.id]: line.id } });
    setNavigation({ identity, focus: nextFocus });
  }

  function openPassage(key: string) {
    setNavigation({ identity, focus: withDefaultChoices(model, { rootKey: key, selections: {} }) });
  }

  function togglePassage(key: string) {
    setExpandedPassages({ identity, keys: expandedKeys.includes(key) ? expandedKeys.filter(value => value !== key) : [...expandedKeys, key] });
  }

  if (!selected) return <section className="reader reader-idle"><div><h2>选择左侧结果，回到故事里</h2><p>以剧本台词本形式查看前后文、任务版本与全部玩家分支选项。</p></div></section>;
  const chapter = mission?.chapter || selected.chapter;
  const world = mission?.world || selected.world;
  const name = cleanMissionName(mission?.name || selected.mission_name, chapter, world);
  const typeKey = mission?.type || selected.type || '';
  const copySource = [name, chapter && chapter !== name ? chapter : '', world, versionText(mission?.version || selected.version, mission?.version_status || selected.version_status)].filter(Boolean).join(' · ');
  const copyText = visibleLines.length ? visibleLines.map(line => `${speakerName(line.speaker)}：${displayText(line.text)}`).join('\n\n') : `${speakerName(selected.speaker)}：${selected.text}`;
  const hasMore = detail?.has_more ?? (detail ? detail.total > (detail.offset || 0) + detail.lines.length : false);

  return <section className="reader reader-active">
    <header className="reader-heading">
      <button className="mobile-back text-button" onClick={onBack}>← 返回结果</button>
      <div className="reader-top-row">
        <div className="reader-breadcrumb">
          {typeKey && <span className={`mission-badge type-${typeKey.toLowerCase()}`}>{missionTypes[typeKey] || typeKey}</span>}
          <span>{mission?.world || selected.world || '剧情对白'}</span>
          {chapter && <><span>/</span><span>{chapter}</span></>}
        </div>
        <div className="reader-meta">
          <span className={`version-label ${!(mission?.version || selected.version) ? 'unverified' : ''}`}>{versionText(mission?.version || selected.version, mission?.version_status || selected.version_status)}</span>
          {mission?.version_source && /^https?:\/\//.test(mission.version_source) && <a href={mission.version_source} target="_blank" rel="noreferrer">BWIKI 依据 ↗</a>}
          {isAdmin && mission && <button className="text-button" onClick={() => setEditing(value => !value)}>补充版本</button>}
        </div>
      </div>
      <div className="reader-title-row">
        <h1>{name}</h1>
        <button className={`icon-button bookmark-button ${bookmarked ? 'is-saved' : ''}`} title={bookmarked ? '取消收藏命中结果' : '收藏命中结果'} aria-label={bookmarked ? '取消收藏命中结果' : '收藏命中结果'} onClick={onBookmark}>{bookmarked ? '★' : '☆'}</button>
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
    <div className="reader-scroll" ref={scrollRef} aria-busy={busy}>
      {busy && !detail ? <div className="reading-skeleton"><div /><div /><div /></div> : error ? <Empty symbol="!" title="暂时无法打开这段剧情"><p>{error}</p></Empty> : detail ? <>
        <div className="reading-focusbar"><span>{atInitialFocus ? '当前命中段落与上下文' : '正在浏览已切换的分支或段落'}</span>{!atInitialFocus && <button className="text-button" onClick={() => setNavigation({ identity, focus: initialFocus })}>↺ 回到初始命中</button>}</div>
        {!focusedHitVisible && selected.line_id && atInitialFocus && <p className="choice-note">当前页未包含命中原句，可继续加载上下文或重新打开搜索结果。</p>}
        <div className="story-flow">
          {passages.map(({ group, window }) => {
            const isBranchFollowup = Boolean(group.branch && selectedTriggers.has(group.branch));
            return <div className={`story-segment ${isBranchFollowup ? 'branch-followup' : ''}`} key={group.key}>
              {isBranchFollowup && <div className="branch-followup-header"><span>↳ 当前所选回答的后续分支对白</span></div>}
              {window.canExpand && <div className="passage-window-note"><span>{window.expanded ? '正在显示本段完整对白' : `已自动折叠前后较远台词（前隐 ${window.hiddenBefore} 条 / 后隐 ${window.hiddenAfter} 条）`}</span><button className="text-button" onClick={() => togglePassage(group.key)}>{window.expanded ? '收起较远台词' : `展开本段全部对白（共 ${group.lines.length} 条）`}</button></div>}
              {window.segments.map((segment, index) => segment.kind === 'dialogue' ? <div className="dialogue-group" key={`${group.key}-${index}`}>
                {segment.lines.map(line => <Dialogue key={line.id} line={line} query={query} hit={line.id === selected.line_id} onShare={onShare} />)}
              </div> : <ChoiceCard key={segment.choice.id} choice={segment.choice} selectedId={focus.selections[segment.choice.id]} hitId={selected.line_id} query={query} onChoose={line => choose(segment.choice, line)} onShare={onShare} />)}
            </div>;
          })}
        </div>
        {visible.missingTargets.length > 0 && <p className="choice-note">这条回答的后续未出现在当前已加载段落中。{hasMore ? '可继续加载上下文。' : '可查看任务其他段落。'}</p>}
        {detail.lines.length === 0 && <Empty title="这条记录暂无上下文"><p>可在搜索结果中查看命中的原文。</p></Empty>}
        {hiddenCount > 0 && <details className="story-alternatives"><summary>切换任务内其他段落或条件分支<span>其余 {hiddenCount} 个段落</span></summary>
          <div className="passage-picker"><label className="field"><span>选择段落</span><select aria-label="选择其他剧情段落" value={focus.rootKey} onChange={event => openPassage(event.target.value)}>{model.groups.map((group, index) => <option value={group.key} key={group.key}>{index + 1} · {speakerName(group.lines[0]?.speaker || '')} · {displayText(group.lines[0]?.text || '剧情段落').slice(0, 70)}</option>)}</select></label>
            <div className="passage-picker-actions"><button className="text-button" disabled={model.groups[0]?.key === focus.rootKey} onClick={() => openPassage(model.groups[Math.max(0, model.groups.findIndex(group => group.key === focus.rootKey) - 1)].key)}>← 上一段</button><button className="text-button" disabled={model.groups[model.groups.length - 1]?.key === focus.rootKey} onClick={() => openPassage(model.groups[Math.min(model.groups.length - 1, model.groups.findIndex(group => group.key === focus.rootKey) + 1)].key)}>下一段 →</button></div>
          </div>
        </details>}
        {hasMore && <button className="secondary load-context" onClick={onMore} disabled={busy}>{busy ? '加载中…' : '继续加载更多上下文'}</button>}
        <div className="reader-end">{selected.script && <button className="text-button" onClick={onExpand}>加载该任务全部脚本段落 →</button>}</div>
      </> : <article className="dialogue-line standalone"><div className="speaker"><span className="speaker-name">{speakerName(selected.speaker)}</span></div><div className="dialogue-body"><p><Highlight text={selected.text} query={query} /></p></div></article>}
      {mission?.version_source && !/^https?:\/\//.test(mission.version_source) && <div className="source-note"><span>版本依据</span><p>{mission.version_source}</p></div>}
      {!!detail?.recalls?.length && <details className="recall-note"><summary>剧情回顾线索</summary>{detail.recalls.map((recall, index) => <p key={index}>{recall.name || recall.title || '回顾段落'}{!recall.verified && <span>关联线索待游戏内核验</span>}{recall.path && <span>{recall.path}</span>}{recall.description && <span>{recall.description}</span>}</p>)}</details>}
    </div>
    <footer className="reader-actions"><button className="text-button" onClick={() => onCopy(copyText)}>复制当前对白</button><button className="text-button" onClick={() => onCopy(copySource)}>复制出处</button><button className="text-button share-action" onClick={() => onShare(shareAnchor)}>分享定位链接 ↗</button></footer>
  </section>;
}
