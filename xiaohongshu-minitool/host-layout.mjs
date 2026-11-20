const sdkMinimum = 9490;

export function hostHeaderInsets(info) {
  const width = info?.windowWidth;
  const buttons = info?.menuButtons;
  if (!buttons || !Number.isFinite(width) || width <= 0) return null;
  const safeTop = Number.isFinite(info.safeArea?.top) ? info.safeArea.top : 0;
  const visible = rect => rect?.width > 0 && rect.height > 0
    && [rect.left, rect.right, rect.top, rect.bottom].every(Number.isFinite)
    && rect.right > 0 && rect.left < width && rect.bottom > safeTop && rect.top < safeTop + 58;
  const rects = visible(buttons.capsule)
    ? [buttons.capsule]
    : [buttons.back, buttons.me, buttons.share].filter(visible);
  if (!rects.length) return null;
  let left = 0;
  let right = 0;
  for (const rect of rects) {
    if (rect.left + rect.right < width) left = Math.max(left, rect.right);
    else right = Math.max(right, width - rect.left);
  }
  return { left: Math.ceil(left), right: Math.ceil(right) };
}

export function initHostLayout() {
  const xhs = window.xhs;
  const api = xhs?.miniTool;
  const root = document.documentElement;
  function apply(info) {
    const insets = hostHeaderInsets(info);
    if (!insets) return;
    root.style.setProperty('--host-header-left', insets.left + 'px');
    root.style.setProperty('--host-header-right', insets.right + 'px');
    root.classList.add('host-header-positioned');
  }
  apply(xhs?.launchOptions?.miniToolEnv?.windowInfo);
  if (typeof api?.getWindowInfo !== 'function') return;

  const versionOf = options => Math.floor(Number(options?.miniToolEnv?.buildVersion) / 1000);
  let generation = 0;
  let refreshTimer;
  async function refresh() {
    const request = ++generation;
    try {
      let version = versionOf(xhs.launchOptions);
      if (!version && typeof api.getLaunchOptions === 'function') version = versionOf(await api.getLaunchOptions());
      if (!(version >= sdkMinimum)) return;
      const info = await api.getWindowInfo();
      if (generation === request) apply(info);
    } catch {
      // The two-row mobile header also works without host measurements.
    }
  }
  function schedule() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 80);
  }
  window.addEventListener('resize', schedule);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) schedule(); });
  schedule();
}
