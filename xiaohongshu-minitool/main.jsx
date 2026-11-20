import { createRoot } from 'react-dom/client';
import App from './App';
import { initHostLayout } from './host-layout.mjs';
import '../frontend/src/style.css';
import './minitool.css';

// The shared reader uses these two built-ins. Chrome 61 requires local fallbacks.
if (typeof Array.prototype.flatMap !== 'function') {
  Object.defineProperty(Array.prototype, 'flatMap', {
    configurable: true, writable: true,
    value(callback, receiver) {
      const flattened = [];
      this.forEach((item, index) => {
        const result = callback.call(receiver, item, index, this);
        if (Array.isArray(result)) result.forEach(value => flattened.push(value));
        else flattened.push(result);
      });
      return flattened;
    },
  });
}
if (typeof String.prototype.replaceAll !== 'function') {
  Object.defineProperty(String.prototype, 'replaceAll', {
    configurable: true, writable: true,
    value(search, replacement) { return String(this).split(search).join(replacement); },
  });
}

function detectFlexGap() {
  const probe = document.createElement('div');
  probe.style.cssText = 'display:flex;flex-direction:column;row-gap:1px;position:absolute;visibility:hidden';
  probe.appendChild(document.createElement('div'));
  probe.appendChild(document.createElement('div'));
  document.body.appendChild(probe);
  if (probe.scrollHeight !== 1) document.documentElement.classList.add('no-flex-gap');
  probe.remove();
}
detectFlexGap();
initHostLayout();
createRoot(document.getElementById('root')).render(<App />);
