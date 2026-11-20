import { createArchive } from './archive.mjs';

const corpusBase = './corpus/';
const shardData = new Map();
let catalog;
let indexData = {};
let archiveTask;

// Classic package-local scripts are usable in the offline container without fetch.
self.hsrRegisterCatalog = value => { catalog = value; };
self.hsrRegisterIndexPart = (key, offset, values) => {
  const records = indexData[key] || (indexData[key] = []);
  for (let i = 0; i < values.length; i++) records[offset + i] = values[i];
};
self.hsrRegisterShard = (index, rows) => { shardData.set(index, rows); };

function loadScript(name) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    const timer = setTimeout(() => finish(new Error('读取档案超时，请重试')), 15000);
    function finish(error) {
      clearTimeout(timer);
      script.onload = null;
      script.onerror = null;
      script.remove();
      if (error) reject(error);
      else resolve();
    }
    script.onload = () => finish();
    script.onerror = () => finish(new Error('档案资源读取失败（' + name + '），请重试'));
    script.src = corpusBase + name;
    document.head.appendChild(script);
  });
}

export async function readCatalog() {
  if (!catalog) await loadScript('catalog.js');
  if (!catalog) throw new Error('档案目录读取失败，请重试');
  return catalog;
}

export function openArchive() {
  if (!archiveTask) archiveTask = prepareArchive().catch(error => {
    archiveTask = undefined;
    throw error;
  });
  return archiveTask;
}

async function prepareArchive() {
  const header = await readCatalog();
  for (let start = 0; start < header.indexFiles.length; start += 3) {
    await Promise.all(header.indexFiles.slice(start, start + 3).map(loadScript));
  }
  if (!indexData.missions || !indexData.nodes || !indexData.texts) throw new Error('档案索引读取失败，请重试');
  const manifest = Object.assign({}, header, indexData);
  indexData = {};
  return createArchive(manifest, async index => {
    if (!shardData.has(index)) await loadScript(manifest.shards[index]);
    const rows = shardData.get(index);
    shardData.delete(index);
    return rows;
  });
}
