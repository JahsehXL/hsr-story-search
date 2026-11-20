const minimumStorageVersion = 9460;

function getVersion(options) {
  const value = Number(options?.miniToolEnv?.buildVersion);
  return Number.isSafeInteger(value) && value > 0 ? Math.floor(value / 1000) : 0;
}

export async function nativeStorage() {
  const xhs = window.xhs;
  const api = xhs?.miniTool;
  if (!api || typeof api.getStorage !== 'function' || typeof api.setStorage !== 'function') return null;
  let version = getVersion(xhs.launchOptions);
  if (!version && typeof api.getLaunchOptions === 'function') {
    try { version = getVersion(await api.getLaunchOptions()); }
    catch { return null; }
  }
  if (version < minimumStorageVersion) return null;
  return {
    async read(key) {
      const result = await api.getStorage({ key });
      if (result?.errMsg !== 'getStorage:ok') throw new Error('存储读取失败');
      return result.data === null ? null : JSON.parse(result.data);
    },
    async write(key, value) {
      const result = await api.setStorage({ key, data: JSON.stringify(value) });
      if (result?.errMsg !== 'setStorage:ok') throw new Error('存储写入未确认');
    },
  };
}
