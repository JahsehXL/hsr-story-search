import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_ANNOUNCEMENT,
  EMPTY_ANNOUNCEMENT_STATE,
  hasUnreadAnnouncement,
  markAnnouncementSeen,
  normalizeAnnouncement,
  parseAnnouncementState,
  parseChangelogBody,
  parseNoteParagraphs,
  shouldAutoShowAnnouncement,
  snoozeAnnouncementState,
} from '../src/announcement.ts';

test('announcement auto-show only triggers on unread homepage landings', () => {
  const day0 = Date.UTC(2026, 10, 22, 0, 0, 0);

  assert.equal(hasUnreadAnnouncement(EMPTY_ANNOUNCEMENT_STATE, DEFAULT_ANNOUNCEMENT.id), true);
  assert.equal(shouldAutoShowAnnouncement(EMPTY_ANNOUNCEMENT_STATE, DEFAULT_ANNOUNCEMENT.id, day0, true), true);
  assert.equal(shouldAutoShowAnnouncement(EMPTY_ANNOUNCEMENT_STATE, DEFAULT_ANNOUNCEMENT.id, day0, false), false);
});

test('markAnnouncementSeen suppresses repeat popups until a new update arrives', () => {
  const day0 = Date.UTC(2026, 10, 22, 0, 0, 0);
  const read = markAnnouncementSeen(EMPTY_ANNOUNCEMENT_STATE, '2026-11-22');

  assert.equal(hasUnreadAnnouncement(read, '2026-11-22'), false);
  assert.equal(shouldAutoShowAnnouncement(read, '2026-11-22', day0, true), false);

  assert.equal(hasUnreadAnnouncement(read, '2026-11-25'), true);
  assert.equal(shouldAutoShowAnnouncement(read, '2026-11-25', day0, true), true);
});

test('snoozeAnnouncementState quiets auto-popups for 7 days while keeping unread indicator for newer releases', () => {
  const day0 = Date.UTC(2026, 10, 22, 0, 0, 0);
  const day3 = day0 + 3 * 24 * 60 * 60 * 1000;
  const day8 = day0 + 8 * 24 * 60 * 60 * 1000;

  const snoozed = snoozeAnnouncementState(EMPTY_ANNOUNCEMENT_STATE, '2026-11-22', day0, 7);

  assert.equal(hasUnreadAnnouncement(snoozed, '2026-11-22'), false);
  assert.equal(shouldAutoShowAnnouncement(snoozed, '2026-11-22', day0, true), false);
  assert.equal(shouldAutoShowAnnouncement(snoozed, '2026-11-22', day3, true), false);

  // Even if a newer update ships inside the 7-day snooze window, avoid auto-popup while marking unread.
  assert.equal(hasUnreadAnnouncement(snoozed, '2026-11-25'), true);
  assert.equal(shouldAutoShowAnnouncement(snoozed, '2026-11-25', day3, true), false);

  // After 7 days expire: stay quiet if nothing newer shipped, auto-show if a newer update exists.
  assert.equal(shouldAutoShowAnnouncement(snoozed, '2026-11-22', day8, true), false);
  assert.equal(shouldAutoShowAnnouncement(snoozed, '2026-11-25', day8, true), true);
});

test('parseAnnouncementState handles malformed input safely', () => {
  assert.deepEqual(parseAnnouncementState(null), EMPTY_ANNOUNCEMENT_STATE);
  assert.deepEqual(parseAnnouncementState('{bad json'), EMPTY_ANNOUNCEMENT_STATE);
  assert.deepEqual(
    parseAnnouncementState(JSON.stringify({ seenId: 123, snoozeUntil: -5, snoozeId: null })),
    EMPTY_ANNOUNCEMENT_STATE,
  );
  assert.deepEqual(
    parseAnnouncementState(JSON.stringify({ seenId: '2026-11-22', snoozeUntil: 9999, snoozeId: '2026-11-22' })),
    { seenId: '2026-11-22', snoozeUntil: 9999, snoozeId: '2026-11-22' },
  );
});

test('normalizeAnnouncement and changelog/note parsers handle Markdown, bare dates, and plain lines', () => {
  const defaultSections = parseChangelogBody(DEFAULT_ANNOUNCEMENT.changelog_body);
  assert.equal(defaultSections.length, 3);
  assert.equal(defaultSections[0].heading, '2026-11-22');
  assert.equal(defaultSections[0].tag, '最新');
  assert.ok(defaultSections[0].items.length >= 4);

  assert.equal(parseNoteParagraphs(DEFAULT_ANNOUNCEMENT.note_body).length, 3);
  assert.deepEqual(parseNoteParagraphs('  '), []);

  const customSections = parseChangelogBody([
    '2026-11-25 【重要】',
    '• 补充 4.8 主线剧情。',
    '* 调整分支显示。',
    '',
    '单纯一行说明',
  ].join('\n'));
  assert.deepEqual(customSections, [
    {
      heading: '2026-11-25',
      tag: '重要',
      items: ['补充 4.8 主线剧情。', '调整分支显示。', '单纯一行说明'],
    },
  ]);

  assert.deepEqual(parseChangelogBody('只有纯文本条目\n第二条说明'), [
    { heading: '更新记录', items: ['只有纯文本条目', '第二条说明'] },
  ]);
  assert.deepEqual(parseChangelogBody('## 空章节\n\n'), []);

  const normalized = normalizeAnnouncement({ title: '新公告', snooze_days: 999, auto_popup: false, note_body: '第一行\r\n第二行' });
  assert.equal(normalized.title, '新公告');
  assert.equal(normalized.snooze_days, 30);
  assert.equal(normalized.auto_popup, false);
  assert.equal(normalized.note_body, '第一行\n第二行');
});
