import type { ReactNode } from 'react';

export function speakerName(value: string) { return !value || value.includes('{NICKNAME}') || value === 'PlayerName' ? '开拓者' : value; }
export function displayText(value: string) {
  return value.replace(/\{RUBY_B#([^}]*)\}([\s\S]*?)\{RUBY_E#\}/g, '$2（$1）')
    .replace(/<[^>]*>/g, '').replace(/\{(?:F|M)#([^}]*)\}/g, '$1')
    .replaceAll('{NICKNAME}', '开拓者').replaceAll('\\n', '\n');
}
export function errorText(error: unknown) { return error instanceof Error ? error.message : '操作未完成，请稍后重试'; }
export function versionText(version?: string, state?: string) { return version ? `${version}${state === 'inferred' ? ' · 推定' : ''}` : '版本待核实'; }
export function Highlight({ text, query }: { text: string; query: string }) {
  text = displayText(text);
  const cleanQuery = query.trim();
  if (!cleanQuery) return <>{text}</>;
  const rawTokens = [...new Set([cleanQuery, ...cleanQuery.split(/[\s，。？！、；：“”‘’「」『』（）《》—…·,.;:?!()[\]]+/).filter(Boolean)])];
  let words = rawTokens.filter(w => text.toLowerCase().includes(w.toLowerCase()));
  if (!words.length) {
    const cjkRuns = cleanQuery.match(/[\u3400-\u9fff]{2,}/g) || [];
    const subterms: string[] = [];
    for (const run of cjkRuns) {
      for (let len = Math.min(6, run.length); len >= 2; len--) {
        for (let i = 0; i <= run.length - len; i++) {
          const sub = run.slice(i, i + len);
          if (text.includes(sub)) subterms.push(sub);
        }
      }
    }
    words = [...new Set(subterms)];
  }
  words.sort((a, b) => b.length - a.length);
  if (!words.length) return <>{text}</>;
  const pattern = words.map(word => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  const pieces = text.split(new RegExp(`(${pattern})`, 'gi'));
  return <>{pieces.map((piece, i) => words.some(word => word.toLowerCase() === piece.toLowerCase()) ? <mark key={i}>{piece}</mark> : piece)}</>;
}

export function Empty({ symbol = '✧', title, children }: { symbol?: string; title: string; children?: ReactNode }) {
  return <div className="empty"><div className="empty-symbol" aria-hidden="true">{symbol}</div><h2>{title}</h2>{children && <div className="empty-copy">{children}</div>}</div>;
}
