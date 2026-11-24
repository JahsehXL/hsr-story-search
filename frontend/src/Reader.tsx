import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { cleanMissionName, displayText, Empty, errorText, Highlight, missionTypeClass, speakerName, versionText } from './presentation';
import { altTagMatches, buildStoryModel, formatAltLabel, resolveFullMissionStory, sceneStatusLabel, shareStoryAnchor, shouldShowTrailingExtraDivider, storyBlocks } from './story';
import StoryBlock from './StoryBlock';
import type { ChoiceSet } from './story';
import type { DialogueLine, DialogueOrigin, MissionResponse, Result } from './types';

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
  onRetry?: () => void;
  onVersionSave: (version: string, source: string, status: string) => Promise<void>;
  onOpenCurrentForVersion?: () => void;
  onExpand?: () => void;
  onMore: () => void;
  onEarlier: () => void;
}

const missionTypes: Record<string, string> = { Main: '开拓任务', Branch: '冒险任务', Companion: '同行任务', Daily: '日常任务', Interlude: '开拓续闻', Gap: '开拓续闻' };
const emptySelections: Record<string, string> = {};

const originKindLabels = { trigger: '游戏触发点', shared_config: '共用配置', workflow: '任务引用' };

function DialogueOrigins({ origin }: { origin: DialogueOrigin }) {
  return <div className="dialogue-origins">
    <strong>多个出处</strong>
    <ul>{origin.origins.map(item => <li key={`${item.kind}:${item.name}`}>
      <span>{originKindLabels[item.kind]}</span> {item.name}{item.task_version && `（任务版本 ${item.task_version}）`}
    </li>)}</ul>
  </div>;
}

function Dialogue({ line, query, hit, anchorLabel, onShare }: { line: DialogueLine; query: string; hit: boolean; anchorLabel: string;
  onShare: (line: DialogueLine) => void }) {
  const isNarration = !line.speaker && !line.kind.includes('option');
  const name = isNarration ? '旁白' : speakerName(line.speaker);
  const isPlayer = !isNarration && name === '开拓者';
  return <article id={`line-${line.id}`} className={`dialogue-line ${hit ? 'line-hit' : ''} ${isPlayer ? 'is-player' : ''} ${isNarration ? 'is-narration' : ''}`}>
    <div className="speaker"><span className="speaker-name">{name}</span></div>
    <div className="dialogue-body">
      <p><Highlight text={line.text} query={query} /></p>
      <div className="dialogue-meta">
        {hit && <span className="hit-label">✦ {anchorLabel}</span>}
        <button className="line-anchor" onClick={() => onShare(line)} title="复制这句话的位置" aria-label="复制这句话的位置">#</button>
      </div>
    </div>
  </article>;
}

function ChoiceCard({ choice, selectedId, hitId, query, anchorLabel, onChoose, onShare }: {
  choice: ChoiceSet; selectedId?: string; hitId: string; query: string; anchorLabel: string;
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
            {isHit && <span className="hit-label">✦ {anchorLabel}</span>}
            <span className="choice-option-state" aria-hidden="true">{isSelected ? '已选' : hasDistinctBranches ? '切换分支 →' : '选择'}</span>
          </span>
        </button>;
      })}
    </div>
    {current && !current.trigger && <div className="choice-note"><span>该选项无独立分支脚本，选择后直接衔接后续主线对白。</span></div>}
  </section>;
}

export default function Reader({ selected, detail, busy, query, isAdmin, bookmarked, onBookmark, onCopy, onShare, onBack, onRetry, onVersionSave, onOpenCurrentForVersion, error, onMore, onEarlier }: ReaderProps) {
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
  const locateWhenVisible = useRef('');
  const mission = detail?.mission;
  const identity = JSON.stringify([selected?.mission_id, selected?.line_id]);
  const hitId = detail?.anchor_missing ? '' : detail?.anchor_line_id ?? selected?.line_id ?? '';
  const showFullText = fullTextMode.missionId === selected?.mission_id && fullTextMode.enabled;
  const model = useMemo(() => buildStoryModel(detail?.lines || [], detail?.edges || []), [detail?.lines, detail?.edges]);
  const customSelections = choiceOverrides.identity === identity ? choiceOverrides.selections : emptySelections;
  const customAltSelections = altOverrides.identity === identity ? altOverrides.selections : emptySelections;
  const resolved = useMemo(() => resolveFullMissionStory(model, hitId, customSelections, customAltSelections), [model, hitId, customSelections, customAltSelections]);
  const groups = useMemo(() => resolved.groups.map(group => ({ ...group, blocks: storyBlocks(group.segments) })), [resolved.groups]);
  const orderedScripts = useMemo(() => new Set(detail?.ordered_scripts || []), [detail?.ordered_scripts]);
  const optionalScripts = useMemo(() => new Set(detail?.optional_scripts || []), [detail?.optional_scripts]);
  const hitGroup = model.byLine.get(hitId);
  const hitVisible = Boolean(hitGroup && resolved.groups.includes(hitGroup));
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
    if (!hitId || !scrollRef.current) return false;
    const target = scrollRef.current.querySelector<HTMLElement>(`#line-${CSS.escape(hitId)}`);
    if (target) {
      const scroller = scrollRef.current;
      const top = target.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
      scroller.scrollTo({ top: Math.max(0, top - (scroller.clientHeight - target.offsetHeight) / 2), behavior });
      return true;
    }
    return false;
  }, [hitId]);

  function locateHit() {
    if (scrollToHit() || !hitGroup) return;
    locateWhenVisible.current = identity;
    setChoiceOverrides({ identity, selections: {} });
    setAltOverrides({ identity, selections: {} });
  }

  useEffect(() => {
    if (locateWhenVisible.current !== identity || !hitVisible) return;
    locateWhenVisible.current = '';
    scrollToHit();
  }, [identity, hitVisible, scrollToHit]);

  useEffect(() => {
    setFullTextMode({ missionId: selected?.mission_id || '', enabled: false });
  }, [selected?.mission_id]);

  useEffect(() => {
    setVersion(mission?.version || '');
    setSource(mission?.version_source || '');
    setVersionState(mission?.version_status === 'inferred' ? 'inferred' : 'verified');
    setEditing(false);
    setSaveError('');
  }, [detail?.snapshot, mission?.id, mission?.version, mission?.version_source, mission?.version_status]);

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
  const isRandom = selected.match_type === 'random';
  const anchorLabel = isRandom ? '随机原句' : '命中原句';
  const bookmarkLabel = `${bookmarked ? '取消收藏' : '收藏'}${isRandom ? '随机剧情' : '命中结果'}`;
  const chapter = mission?.chapter || selected.chapter;
  const world = mission?.world || selected.world;
  const hitLine = detail?.lines.find(line => line.id === hitId);
  const origin = hitLine ? hitLine.dialogue_origin : selected.dialogue_origin;
  const originScript = hitLine?.script || selected.script;
  const name = origin?.title || cleanMissionName(mission?.name || selected.mission_name, chapter, world);
  const typeKey = origin ? '' : mission?.type || selected.type || '';
  const verText = origin ? '对白版本待核实' : versionText(mission?.version || selected.version, mission?.version_status || selected.version_status);
  const copySource = [name, !origin && chapter !== name ? chapter : '', world, verText].filter(Boolean).join(' · ');
  const copyDialogue = () => onCopy(visibleLines.length ? visibleLines.map(line => (!line.speaker && !line.kind.includes('option') ? displayText(line.text) : `${speakerName(line.speaker)}：${displayText(line.text)}`)).join('\n\n') : (!selected.speaker && !selected.kind?.includes('option') ? displayText(selected.text) : `${speakerName(selected.speaker)}：${selected.text}`));
  const hasMore = detail?.has_more ?? (detail ? detail.total > (detail.offset || 0) + detail.lines.length : false);
  const hasEarlier = (detail?.offset || 0) > 0;

  return <section className="reader reader-active">
    <header className="reader-heading">
      <div className="reader-top-row">
        <div className="reader-breadcrumb">
          <button type="button" className="reader-back-button" onClick={onBack} title={isRandom ? '返回随机结果' : '返回搜索结果'}>← 返回</button>
          {typeKey && <span className={`mission-badge type-${missionTypeClass(typeKey)}`}>{missionTypes[typeKey] || typeKey}</span>}
          <span className={`version-label ${origin || !(mission?.version || selected.version) ? 'unverified' : ''}`}>{verText}</span>
          <span>{mission?.world || selected.world || '剧情对白'}</span>
          {!origin && chapter && <><span>/</span><span>{chapter}</span></>}
        </div>
        <div className="reader-meta">
          {detail && <button className="text-button" aria-pressed={showFullText} title={showFullText ? '按需显示屏幕附近的对白，减少内存占用' : '渲染全部已加载对白，支持浏览器查找和辅助技术遍历，会占用更多内存'} onClick={() => setFullTextMode({ missionId: selected.mission_id, enabled: !showFullText })}>{showFullText ? '按需显示' : '显示全文'}</button>}
          {hitGroup && <button type="button" className="text-button locate-hit-button" onClick={locateHit}>⌖ 定位{anchorLabel}</button>}
          {isAdmin && mission && (onOpenCurrentForVersion
            ? <button className="text-button" onClick={onOpenCurrentForVersion}>打开当前快照编辑</button>
            : <button className="text-button" onClick={() => setEditing(value => !value)}>补充版本</button>)}
        </div>
      </div>
      <div className="reader-title-row">
        <h1>{name}</h1>
        <button className={`icon-button bookmark-button ${bookmarked ? 'is-saved' : ''}`} title={bookmarkLabel} aria-label={bookmarkLabel} onClick={onBookmark}>{bookmarked ? '★' : '☆'}</button>
      </div>
      {origin && <DialogueOrigins origin={origin} />}
      {editing && !onOpenCurrentForVersion && <form className="version-editor" onSubmit={async event => {
        event.preventDefault(); setSaving(true); setSaveError('');
        try { await onVersionSave(version, source, versionState); setEditing(false); }
        catch (failure) { setSaveError(errorText(failure)); }
        finally { setSaving(false); }
      }}>
        <label className="field"><span>开放版本</span><input value={version} onChange={event => setVersion(event.target.value)} placeholder="例如 2.0" required={versionState === 'verified'} /></label>
        <label className="field"><span>依据链接或说明</span><input value={source} onChange={event => setSource(event.target.value)} placeholder="官方公告链接或核验说明" required={versionState === 'verified'} /></label>
        <label className="field"><span>核验状态</span><select value={versionState} onChange={event => setVersionState(event.target.value)}><option value="verified">已核实</option><option value="inferred">推定</option><option value="unknown">待核实</option></select></label>
        <button className="primary" disabled={saving}>{saving ? '保存中…' : '保存标注'}</button>
      </form>}
      {saveError && <p className="error-message" role="alert">{saveError}</p>}
    </header>
    <div className="reader-scroll" ref={scrollRef} aria-busy={busy}>
      {busy && !detail ? <div className="reading-skeleton"><div /><div /><div /></div> : error ? <Empty symbol="!" title="暂时无法打开这段剧情"><p>{error}</p>{onRetry && <button type="button" className="secondary" onClick={onRetry}>重试打开</button>}</Empty> : detail ? <>
        <div className="reading-focusbar">
          <span>{hasEarlier || hasMore ? '当前剧情上下文' : '任务台词本'}（当前路径 {visibleLines.length} 句）</span>
          <div className="reading-focusbar-actions">
            {hasCustomBranch && <button className="text-button" onClick={() => { setChoiceOverrides({ identity, selections: {} }); setAltOverrides({ identity, selections: {} }); }}>↺ 回到{isRandom ? '随机对白所在' : '命中'}分支</button>}
          </div>
        </div>
        {detail.anchor_missing && <p className="inline-alert" role="status">此快照中找不到链接指向的台词，已打开任务上下文。</p>}
        {hasEarlier && <button className="secondary load-context" onClick={onEarlier} disabled={busy}>加载更早上下文</button>}
        <div className="story-flow">
          {groups.map((group, groupIndex) => {
            const isBranchFollowup = Boolean(group.branch && selectedTriggers.has(JSON.stringify([group.script, group.branch])));
            const altSiblings = group.altSlot ? model.altGroups.get(group.altSlot) : undefined;
            const showAltBar = Boolean(altSiblings && group.altSlot && groups.findIndex(item => item.altSlot === group.altSlot) === groupIndex);
            const startsScene = groups[groupIndex - 1]?.script !== group.script;
            const showExtraDivider = shouldShowTrailingExtraDivider(groups[groupIndex - 1]?.script, group.script, orderedScripts, optionalScripts);
            const sceneLabel = sceneStatusLabel(group.script, group.lines[0]?.scene_label);
            const sceneOrigin = startsScene && (!origin || originScript !== group.script)
              ? group.lines.find(line => line.dialogue_origin)?.dialogue_origin
              : undefined;
            return <div className={`story-segment ${isBranchFollowup ? 'branch-followup' : ''} ${altSiblings ? 'alt-branch-segment' : ''}`} key={group.key}>
              {showExtraDivider && (
                <div className="scene-end-divider" role="separator" aria-label="任务内补充对白">
                  <span className="scene-end-line" aria-hidden="true" />
                  <span className="scene-end-badge">任务内补充对白</span>
                  <span className="scene-end-line" aria-hidden="true" />
                </div>
              )}
              {startsScene && sceneLabel && <div className="branch-followup-header"><span>{sceneLabel}</span></div>}
              {sceneOrigin && <DialogueOrigins origin={sceneOrigin} />}
              {isBranchFollowup && <div className="branch-followup-header"><span>↳ 当前所选回答的后续分支对白</span></div>}
              {showAltBar && altSiblings && group.altSlot && <div className="alt-branch-bar" role="group" aria-label={group.altKind === 'rand' ? '随机对白变体' : '条件分支'}>
                <span className="alt-branch-title">
                  {group.altKind === 'rand' ? `随机对白（共 ${altSiblings.length} 种变体）` : `条件分支（互斥，共 ${altSiblings.length} 种情况）`}
                </span>
                <div className="alt-branch-tabs">
                  {altSiblings.map(sibling => {
                    const active = sibling.key === resolved.altSelections[group.altSlot!];
                    const containsHit = Boolean(hitGroup && hitGroup.altSlot === group.altSlot && hitGroup.altTag && sibling.altTag && altTagMatches(hitGroup.altTag, sibling.altTag));
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
                    {segment.lines.map(line => <Dialogue key={line.id} line={line} query={query} hit={line.id === hitId} anchorLabel={anchorLabel} onShare={onShare} />)}
                  </div> : <ChoiceCard choice={segment.choice} selectedId={resolved.selections[segment.choice.id]} hitId={hitId} query={query} anchorLabel={anchorLabel} onChoose={line => choose(segment.choice, line)} onShare={onShare} />}
                </StoryBlock>;
              })}
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
