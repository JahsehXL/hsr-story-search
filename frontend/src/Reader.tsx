import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { cleanMissionName, displayText, Empty, errorText, Highlight, missionTypeClass, speakerName, versionText } from './presentation';
import { buildStoryModel, formatAltLabel, resolveFullMissionStory, shareStoryAnchor, storyBlocks } from './story';
import StoryBlock from './StoryBlock';
import type { ChoiceSet } from './story';
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
  onExpand?: () => void;
  onMore: () => void;
  onEarlier: () => void;
}

const missionTypes: Record<string, string> = { Main: '开拓任务', Branch: '冒险任务', Companion: '同行任务', Daily: '日常任务', Interlude: '开拓续闻', Gap: '开拓续闻' };
const emptySelections: Record<string, string> = {};

function scriptDisplayName(script: string): string {
  const withoutTimeline = script.split('::timeline-')[0];
  const fileName = withoutTimeline.split('/').pop()?.replace(/\.json$/, '') || script;
  if (script.startsWith('Recovered/')) return `补录段落 ${fileName}`;
  if (script.startsWith('Story/BattlePerformance/')) return `战斗演出 ${fileName}`;
  if (script.includes('::timeline-')) return `${fileName}（过场对白）`;
  return fileName;
}

function Dialogue({ line, query, hit, onShare }: { line: DialogueLine; query: string; hit: boolean; onShare: (line: DialogueLine) => void }) {
  const name = speakerName(line.speaker);
  const isPlayer = name === '开拓者';
  return <article id={`line-${line.id}`} className={`dialogue-line ${hit ? 'line-hit' : ''} ${isPlayer ? 'is-player' : ''}`}>
    <div className="speaker"><span className="speaker-name">{name}</span></div>
    <div className="dialogue-body">
      <p><Highlight text={line.text} query={query} /></p>
      <div className="dialogue-meta">
        {hit && <span className="hit-label">✦ 命中原句</span>}
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
  const hasDistinctBranches = new Set(choice.options.map(option => option.trigger)).size > 1;
  return <section className={`choice-card ${anyHit ? 'choice-hit' : ''}`} aria-label="玩家回答">
    <div className="choice-card-header">
      <div className="choice-card-label">
        <span className="choice-icon" aria-hidden="true">❖</span>
        <span>玩家对话选项</span>
        <small>共 {choice.options.length} 项{hasDistinctBranches && ' · 点击切换下方分支回应'}</small>
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
            <span className="choice-option-state" aria-hidden="true">{isSelected ? '已选' : hasDistinctBranches ? '切换分支 →' : '选择'}</span>
          </span>
        </button>;
      })}
    </div>
    {current && !current.trigger && <div className="choice-note"><span>该选项无独立分支脚本，选择后直接衔接后续主线对白。</span></div>}
  </section>;
}

export default function Reader({ selected, detail, busy, query, error, isAdmin, bookmarked, onBookmark, onCopy, onShare, onBack, onVersionSave, onMore, onEarlier }: ReaderProps) {
  const [editing, setEditing] = useState(false);
  const [version, setVersion] = useState('');
  const [source, setSource] = useState('');
  const [versionState, setVersionState] = useState('verified');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [choiceOverrides, setChoiceOverrides] = useState<{ identity: string; selections: Record<string, string> }>({ identity: '', selections: {} });
  const [altOverrides, setAltOverrides] = useState<{ identity: string; selections: Record<string, string> }>({ identity: '', selections: {} });
  const [fullTextMode, setFullTextMode] = useState({ missionId: '', enabled: false });
  const scrollRef = useRef<HTMLDivElement>(null);
  const positionedIdentity = useRef('');
  const mission = detail?.mission;
  const identity = JSON.stringify([selected?.mission_id, selected?.line_id]);
  const hitId = detail?.anchor_line_id ?? selected?.line_id ?? '';
  const showFullText = fullTextMode.missionId === selected?.mission_id && fullTextMode.enabled;
  const model = useMemo(() => buildStoryModel(detail?.lines || [], detail?.edges || []), [detail?.lines, detail?.edges]);
  const customSelections = choiceOverrides.identity === identity ? choiceOverrides.selections : emptySelections;
  const customAltSelections = altOverrides.identity === identity ? altOverrides.selections : emptySelections;
  const resolved = useMemo(() => resolveFullMissionStory(model, hitId, customSelections, customAltSelections), [model, hitId, customSelections, customAltSelections]);
  const groups = useMemo(() => resolved.groups.map(group => ({ ...group, blocks: storyBlocks(group.segments) })), [resolved.groups]);
  const visibleLines = useMemo(
    () => {
      const selectedAnswers = new Set(Object.values(resolved.selections));
      return resolved.groups.flatMap(group => group.lines.filter(line => !line.kind.includes('option') || selectedAnswers.has(line.id)));
    },
    [resolved.groups, resolved.selections],
  );
  const shareAnchor = shareStoryAnchor(visibleLines, resolved.selections, hitId);
  const hasCustomBranch =
    Object.keys(customSelections).some(key => customSelections[key] !== resolved.initialSelections[key]) ||
    Object.keys(customAltSelections).some(key => customAltSelections[key] !== resolved.initialAltSelections[key]);
  const selectedTriggers = useMemo(() => {
    const triggers = new Set<string>();
    for (const group of resolved.groups) {
      for (const segment of group.segments) {
        if (segment.kind !== 'choice') continue;
        const chosen = segment.choice.options.find(opt => opt.id === resolved.selections[segment.choice.id]);
        if (chosen?.trigger && segment.choice.options.some(option => option.trigger !== chosen.trigger)) {
          triggers.add(JSON.stringify([chosen.script, chosen.trigger]));
        }
      }
    }
    return triggers;
  }, [resolved.groups, resolved.selections]);

  const scrollToHit = useCallback((behavior: ScrollBehavior = 'smooth') => {
    if (!hitId || !scrollRef.current) return;
    const target = scrollRef.current.querySelector<HTMLElement>(`#line-${CSS.escape(hitId)}`);
    if (target) {
      const scroller = scrollRef.current;
      const top = target.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
      scroller.scrollTo({ top: Math.max(0, top - (scroller.clientHeight - target.offsetHeight) / 2), behavior });
    }
  }, [hitId]);

  useEffect(() => {
    setFullTextMode({ missionId: selected?.mission_id || '', enabled: false });
  }, [selected?.mission_id]);

  useEffect(() => {
    setVersion(mission?.version || '');
    setSource(mission?.version_source || '');
    setVersionState(mission?.version_status === 'inferred' ? 'inferred' : 'verified');
    setEditing(false);
    setSaveError('');
  }, [mission?.id, mission?.version, mission?.version_source, mission?.version_status]);

  useEffect(() => {
    if (!detail) {
      positionedIdentity.current = '';
      return;
    }
    const scroller = scrollRef.current;
    const flow = scroller?.querySelector<HTMLElement>('.story-flow');
    if (!hitId || !scroller || !flow || positionedIdentity.current === identity) return;
    positionedIdentity.current = identity;
    // Nearby blocks replace estimates with their measured heights after the first jump.
    // Correct that initial layout only, and yield immediately when the reader interacts.
    let stopped = false;
    const align = () => { if (!stopped) scrollToHit('auto'); };
    const observer = 'ResizeObserver' in window ? new ResizeObserver(align) : null;
    observer?.observe(flow);
    const frame = requestAnimationFrame(align);
    const stop = () => { stopped = true; observer?.disconnect(); };
    const events = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const;
    for (const event of events) scroller.addEventListener(event, stop, { passive: true });
    const timer = setTimeout(stop, 1000);
    return () => {
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      stop();
      for (const event of events) scroller.removeEventListener(event, stop);
    };
  }, [detail, identity, hitId, scrollToHit]);

  function choose(choice: ChoiceSet, line: DialogueLine) {
    setChoiceOverrides({
      identity,
      selections: { ...customSelections, [choice.id]: line.id },
    });
  }

  function chooseAlt(slot: string, groupKey: string) {
    setAltOverrides({
      identity,
      selections: { ...customAltSelections, [slot]: groupKey },
    });
  }

  if (!selected) return <section className="reader reader-idle"><div><h2>选择左侧结果，回到故事里</h2><p>以剧本台词本形式查看完整任务上下文、开放版本与全部玩家分支选项。</p></div></section>;
  const chapter = mission?.chapter || selected.chapter;
  const world = mission?.world || selected.world;
  const name = cleanMissionName(mission?.name || selected.mission_name, chapter, world);
  const typeKey = mission?.type || selected.type || '';
  const verText = versionText(mission?.version || selected.version, mission?.version_status || selected.version_status);
  const copySource = [name, chapter && chapter !== name ? chapter : '', world, verText].filter(Boolean).join(' · ');
  const copyDialogue = () => onCopy(visibleLines.length ? visibleLines.map(line => `${speakerName(line.speaker)}：${displayText(line.text)}`).join('\n\n') : `${speakerName(selected.speaker)}：${selected.text}`);
  const hasMore = detail?.has_more ?? (detail ? detail.total > (detail.offset || 0) + detail.lines.length : false);
  const hasEarlier = (detail?.offset || 0) > 0;

  return <section className="reader reader-active">
    <header className="reader-heading">
      <div className="reader-top-row">
        <div className="reader-breadcrumb">
          <button type="button" className="reader-back-button" onClick={onBack} title="返回搜索结果">← 返回</button>
          {typeKey && <span className={`mission-badge type-${missionTypeClass(typeKey)}`}>{missionTypes[typeKey] || typeKey}</span>}
          <span className={`version-label ${!(mission?.version || selected.version) ? 'unverified' : ''}`}>{verText}</span>
          <span>{mission?.world || selected.world || '剧情对白'}</span>
          {chapter && <><span>/</span><span>{chapter}</span></>}
        </div>
        <div className="reader-meta">
          {detail && <button className="text-button" aria-pressed={showFullText} title={showFullText ? '按需显示屏幕附近的对白，减少内存占用' : '渲染全部已加载对白，支持浏览器查找和辅助技术遍历，会占用更多内存'} onClick={() => setFullTextMode({ missionId: selected.mission_id, enabled: !showFullText })}>{showFullText ? '按需显示' : '显示全文'}</button>}
          {hitId && detail && <button type="button" className="text-button locate-hit-button" onClick={() => scrollToHit('smooth')}>⌖ 定位命中原句</button>}
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
        <div className="reading-focusbar">
          <span>{hasEarlier || hasMore ? '当前剧情上下文' : '任务台词本'}（当前路径 {visibleLines.length} 句）</span>
          <div className="reading-focusbar-actions">
            {hasCustomBranch && <button className="text-button" onClick={() => { setChoiceOverrides({ identity, selections: {} }); setAltOverrides({ identity, selections: {} }); }}>↺ 回到命中分支</button>}
            {hitId && <button className="text-button" onClick={() => scrollToHit('smooth')}>⌖ 跳转到命中原句</button>}
          </div>
        </div>
        {hasEarlier && <button className="secondary load-context" onClick={onEarlier} disabled={busy}>加载更早上下文</button>}
        <div className="story-flow">
          {groups.map((group, groupIndex) => {
            const isBranchFollowup = Boolean(group.branch && selectedTriggers.has(JSON.stringify([group.script, group.branch])));
            const altSiblings = group.altSlot ? model.altGroups.get(group.altSlot) : undefined;
            const startsScene = groups[groupIndex - 1]?.script !== group.script;
            const endsScene = groups[groupIndex + 1]?.script !== group.script;
            const sceneName = scriptDisplayName(group.script);
            const sceneLabel = group.lines[0]?.scene_label || (
              group.script.startsWith('Story/BattlePerformance/') ? '战斗对白'
                : group.script.startsWith('Recovered/') ? '补录对白 · 场景位置待核实' : ''
            );
            return <div className={`story-segment ${isBranchFollowup ? 'branch-followup' : ''} ${altSiblings ? 'alt-branch-segment' : ''}`} key={group.key}>
              {startsScene && sceneLabel && <div className="branch-followup-header"><span>{sceneLabel}</span></div>}
              {isBranchFollowup && <div className="branch-followup-header"><span>↳ 当前所选回答的后续分支对白</span></div>}
              {altSiblings && group.altSlot && <div className="alt-branch-bar" role="group" aria-label={group.altKind === 'rand' ? '随机对白变体' : '条件分支'}>
                <span className="alt-branch-title">
                  {group.altKind === 'rand' ? `随机对白（共 ${altSiblings.length} 种变体）` : `条件分支（互斥，共 ${altSiblings.length} 种情况）`}
                </span>
                <div className="alt-branch-tabs">
                  {altSiblings.map(sibling => {
                    const active = sibling.key === group.key;
                    const containsHit = sibling.lines.some(line => line.id === hitId);
                    return <button
                      key={sibling.key}
                      type="button"
                      className={`alt-branch-tab ${active ? 'is-active' : ''}`}
                      aria-pressed={active}
                      onClick={() => chooseAlt(group.altSlot!, sibling.key)}
                    >
                      {containsHit && <span aria-hidden="true">✦ </span>}
                      {formatAltLabel(sibling.altTag || 'cond:1')}
                    </button>;
                  })}
                </div>
              </div>}
              {group.blocks.map((segment, index) => {
                const lines = segment.kind === 'dialogue' ? segment.lines : segment.choice.options;
                const key = `${identity}:${lines[0].id}`;
                return <StoryBlock key={key} scrollRef={scrollRef} estimatedHeight={segment.kind === 'dialogue' ? lines.length * 100 : 110 + lines.length * 70} containsHit={lines.some(line => line.id === hitId)} initiallyVisible={groupIndex === 0 && index < 2} renderAll={showFullText}>
                  {() => segment.kind === 'dialogue' ? <div className="dialogue-group">
                    {segment.lines.map(line => <Dialogue key={line.id} line={line} query={query} hit={line.id === hitId} onShare={onShare} />)}
                  </div> : <ChoiceCard choice={segment.choice} selectedId={resolved.selections[segment.choice.id]} hitId={hitId} query={query} onChoose={line => choose(segment.choice, line)} onShare={onShare} />}
                </StoryBlock>;
              })}
              {endsScene && (
                <div className="scene-end-divider" role="separator" aria-label={`演出脚本结束：${sceneName}`}>
                  <span className="scene-end-line" aria-hidden="true" />
                  <span className="scene-end-badge">
                    演出脚本结束 · <code>{sceneName}</code>
                    {groupIndex < groups.length - 1 ? ' · 以下为下一段独立脚本（跨脚本顺序以游戏内为准）' : ''}
                  </span>
                  <span className="scene-end-line" aria-hidden="true" />
                </div>
              )}
            </div>;
          })}
        </div>
        {detail.lines.length === 0 && <Empty title="这条记录暂无上下文"><p>可在搜索结果中查看命中的原文。</p></Empty>}
        {hasMore && <button className="secondary load-context" onClick={onMore} disabled={busy}>{busy ? '加载中…' : '继续加载更多上下文'}</button>}
      </> : <article className="dialogue-line standalone"><div className="speaker"><span className="speaker-name">{speakerName(selected.speaker)}</span></div><div className="dialogue-body"><p><Highlight text={selected.display_text || selected.text} query={query} /></p></div></article>}
      {!!detail?.recalls?.length && <details className="recall-note"><summary>剧情回顾线索</summary>{detail.recalls.map((recall, index) => <p key={index}>{recall.name || recall.title || '回顾段落'}{!recall.verified && <span>关联线索待游戏内核验</span>}{recall.path && <span>{recall.path}</span>}{recall.description && <span>{recall.description}</span>}</p>)}</details>}
    </div>
    <footer className="reader-actions"><button className="text-button" onClick={copyDialogue}>复制当前对白</button><button className="text-button" onClick={() => onCopy(copySource)}>复制出处</button><button className="text-button share-action" onClick={() => onShare(shareAnchor)}>分享定位链接 ↗</button></footer>
  </section>;
}
