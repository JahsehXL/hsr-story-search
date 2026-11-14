import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './style.css';

let theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
try {
  const saved = localStorage.getItem('hsr-theme');
  if (saved === 'dark' || saved === 'light') theme = saved;
} catch { /* System preference remains available when storage is blocked. */ }
document.documentElement.dataset.theme = theme;

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
