/**
 * 网页版「站点公告与更新日志」数据结构、默认文案、解析与本地状态管理（小红书小工具不引入此模块）。
 *
 * 日常同步方式：
 * 1. 直接在管理后台（「星轨档案 → 站点公告与更新日志」或本地管理面板「站点公告」标签页）编辑并覆盖发布；
 * 2. 服务端保存后会生成新的版本标识 id（勾选“作为新公告覆盖发布”时），未静默的访客下次打开首页会再次看到提醒；
 *    明确点过「N 天内不再弹出」的访客，在静默期内不会被打扰，但公告按钮右上角仍保留未读红点。
 */

export interface AnnouncementData {
  id: string;
  title: string;
  note_title: string;
  note_body: string;
  changelog_title: string;
  changelog_body: string;
  auto_popup: boolean;
  snooze_days: number;
  updated_at: string;
}

export interface ChangelogSection {
  heading: string;
  tag?: string;
  items: string[];
}

export interface AnnouncementState {
  seenId: string;
  snoozeUntil: number;
  snoozeId: string;
}

export const ANNOUNCEMENT_STORAGE_KEY = 'hsr-announcement-state-v1';
export const ANNOUNCEMENT_SNOOZE_DAYS = 7;

export const DEFAULT_ANNOUNCEMENT: AnnouncementData = {
  id: '2026-11-22',
  title: '站点公告与更新日志',
  note_title: '写在前面',
  note_body: [
    '这个项目我一直在持续维护和更新，平时也在不断核对剧情、版本和搜索体验。',
    '',
    '可能你之前搜不到、或者显示不太对的某段剧情，今天再来看看就已经能搜到了；你想要的新功能也一直在陆续补上。',
    '',
    '平时可以多留意下面的更新日志。如果发现漏词、版本不对、分支显示奇怪，或者有什么好想法，都很欢迎随时点「意见反馈」告诉我。',
  ].join('\n'),
  changelog_title: '最近更新',
  changelog_body: [
    '## 2026-11-22 [最新]',
    '- 新增站点公告与更新日志，方便随时查看近期变动。',
    '- 修正了一批任务和场景的版本与地点归属（例如二相乐园、星穹列车相关对话）。',
    '- 区分「旁白」与「开拓者」台词，筛选开拓者时不再混入无署名旁白。',
    '- 搜索完整任务名（如「失控」）时，顶部新增「同名任务直达」入口。',
    '- 角色筛选支持搜索全库全部角色；刷新页面或返回时也会保留当前筛选。',
    '- 修复部分嵌套条件分支阅读时漏掉前一句对白的问题。',
    '',
    '## 2026-11-20',
    '- 核对并修正一批剧情的所属任务，同一段对白涉及多个出处时会一并列出。',
    '- 优化「描述找剧情」与「错字容错」的排序效果，更容易命中想找的片段。',
    '- 补录更多剧情配音台词，任务内多段演出的衔接更贴合游戏实际流程。',
    '',
    '## 2026-11-19',
    '- 阅读器标出演出分界并减少多余分隔线，连续读长剧情更清爽。',
    '- 调整任务内剧情排列顺序，更好还原游戏内推进顺序。',
  ].join('\n'),
  auto_popup: true,
  snooze_days: ANNOUNCEMENT_SNOOZE_DAYS,
  updated_at: '2026-11-22T00:00:00+00:00',
};

export const EMPTY_ANNOUNCEMENT_STATE: AnnouncementState = {
  seenId: '',
  snoozeUntil: 0,
  snoozeId: '',
};

const DAY_MS = 24 * 60 * 60 * 1000;

export function normalizeAnnouncement(raw: unknown): AnnouncementData {
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_ANNOUNCEMENT };
  const record = raw as Record<string, unknown>;
  const str = (key: keyof AnnouncementData, fallback: string) =>
    typeof record[key] === 'string' ? (record[key] as string) : fallback;
  const rawDays = record.snooze_days;
  const snoozeDays =
    typeof rawDays === 'number' && Number.isFinite(rawDays)
      ? Math.max(1, Math.min(30, Math.round(rawDays)))
      : DEFAULT_ANNOUNCEMENT.snooze_days;
  return {
    id: str('id', DEFAULT_ANNOUNCEMENT.id),
    title: str('title', DEFAULT_ANNOUNCEMENT.title),
    note_title: str('note_title', DEFAULT_ANNOUNCEMENT.note_title),
    note_body: str('note_body', DEFAULT_ANNOUNCEMENT.note_body).replace(/\r\n?/g, '\n'),
    changelog_title: str('changelog_title', DEFAULT_ANNOUNCEMENT.changelog_title),
    changelog_body: str('changelog_body', DEFAULT_ANNOUNCEMENT.changelog_body).replace(/\r\n?/g, '\n'),
    auto_popup: typeof record.auto_popup === 'boolean' ? record.auto_popup : DEFAULT_ANNOUNCEMENT.auto_popup,
    snooze_days: snoozeDays,
    updated_at: str('updated_at', DEFAULT_ANNOUNCEMENT.updated_at),
  };
}

export function parseNoteParagraphs(body: string): string[] {
  const normalized = body.replace(/\r\n?/g, '\n').trim();
  if (!normalized) return [];
  const blocks = normalized.split(/\n{2,}/).map(block => block.split('\n').map(line => line.trim()).filter(Boolean).join('\n')).filter(Boolean);
  return blocks;
}

function splitHeadingTag(rawHeading: string): { heading: string; tag?: string } {
  const trimmed = rawHeading.trim();
  const match = trimmed.match(/^(.*?)\s*(?:\[([^\]]+)\]|【([^】]+)】)\s*$/);
  if (match && match[1].trim()) {
    return { heading: match[1].trim(), tag: (match[2] || match[3] || '').trim() || undefined };
  }
  return { heading: trimmed };
}

export function parseChangelogBody(body: string): ChangelogSection[] {
  const normalized = body.replace(/\r\n?/g, '\n').trim();
  if (!normalized) return [];
  const sections: ChangelogSection[] = [];
  let current: ChangelogSection | null = null;

  for (const rawLine of normalized.split('\n')) {
    const line = rawLine.trim();
    if (!line) continue;

    const mdHeading = line.match(/^#{1,3}\s+(.+)$/);
    if (mdHeading) {
      const parsed = splitHeadingTag(mdHeading[1]);
      current = { ...parsed, items: [] };
      sections.push(current);
      continue;
    }

    const dateHeading = line.match(/^(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})(?:\s+(.*))?$/);
    if (dateHeading) {
      const parsed = splitHeadingTag(line);
      current = { ...parsed, items: [] };
      sections.push(current);
      continue;
    }

    const bullet = line.replace(/^[-*•·]\s+/, '').trim();
    if (!bullet) continue;
    if (!current) {
      current = { heading: '更新记录', items: [] };
      sections.push(current);
    }
    current.items.push(bullet);
  }

  return sections.filter(section => section.items.length > 0);
}

export function parseAnnouncementState(raw: string | null): AnnouncementState {
  if (!raw) return EMPTY_ANNOUNCEMENT_STATE;
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== 'object') return EMPTY_ANNOUNCEMENT_STATE;
    const record = parsed as Record<string, unknown>;
    const seenId = typeof record.seenId === 'string' ? record.seenId : '';
    const snoozeId = typeof record.snoozeId === 'string' ? record.snoozeId : '';
    const snoozeUntil =
      typeof record.snoozeUntil === 'number' && Number.isFinite(record.snoozeUntil) && record.snoozeUntil >= 0
        ? record.snoozeUntil
        : 0;
    return { seenId, snoozeUntil, snoozeId };
  } catch {
    return EMPTY_ANNOUNCEMENT_STATE;
  }
}

export function loadAnnouncementState(storage?: Pick<Storage, 'getItem'> | null): AnnouncementState {
  try {
    const target = storage ?? globalThis.localStorage;
    if (!target) return EMPTY_ANNOUNCEMENT_STATE;
    return parseAnnouncementState(target.getItem(ANNOUNCEMENT_STORAGE_KEY));
  } catch {
    return EMPTY_ANNOUNCEMENT_STATE;
  }
}

export function saveAnnouncementState(
  state: AnnouncementState,
  storage?: Pick<Storage, 'setItem'> | null,
): void {
  try {
    const target = storage ?? globalThis.localStorage;
    target?.setItem(ANNOUNCEMENT_STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Ignore quota or private-browsing storage errors.
  }
}

export function hasUnreadAnnouncement(state: AnnouncementState, currentId = DEFAULT_ANNOUNCEMENT.id): boolean {
  return Boolean(currentId) && state.seenId !== currentId;
}

export function shouldAutoShowAnnouncement(
  state: AnnouncementState,
  currentId = DEFAULT_ANNOUNCEMENT.id,
  now = Date.now(),
  isHomeLanding = true,
): boolean {
  if (!isHomeLanding || !currentId) return false;
  if (state.seenId === currentId) return false;
  if (state.snoozeUntil > now) return false;
  return true;
}

export function markAnnouncementSeen(
  state: AnnouncementState,
  currentId = DEFAULT_ANNOUNCEMENT.id,
): AnnouncementState {
  return { ...state, seenId: currentId };
}

export function snoozeAnnouncementState(
  state: AnnouncementState,
  currentId = DEFAULT_ANNOUNCEMENT.id,
  now = Date.now(),
  days = ANNOUNCEMENT_SNOOZE_DAYS,
): AnnouncementState {
  return {
    ...state,
    seenId: currentId,
    snoozeId: currentId,
    snoozeUntil: now + days * DAY_MS,
  };
}
