import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './style.css';

const systemDark = window.matchMedia('(prefers-color-scheme: dark)');
const THEME_COLORS: Record<string, string> = { light: '#eef0ed', dark: '#0f0e0c' };

function savedTheme(): string | null {
  try {
    const saved = localStorage.getItem('hsr-theme');
    return saved === 'dark' || saved === 'light' ? saved : null;
  } catch { return null; /* System preference remains available when storage is blocked. */ }
}

function syncThemeColor() {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', THEME_COLORS[document.documentElement.dataset.theme || 'light'] || THEME_COLORS.light);
}

document.documentElement.dataset.theme = savedTheme() ?? (systemDark.matches ? 'dark' : 'light');
syncThemeColor();
// Keep the mobile browser bar in step with manual toggles made inside the app.
new MutationObserver(syncThemeColor).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
// Follow live system changes until the visitor picks a theme explicitly.
systemDark.addEventListener('change', event => {
  if (!savedTheme()) document.documentElement.dataset.theme = event.matches ? 'dark' : 'light';
});

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
