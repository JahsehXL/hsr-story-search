import { createRequire } from 'node:module';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { build } = require('esbuild');
const postcss = require('postcss');
const root = fileURLToPath(new URL('.', import.meta.url));
const frontend = resolve(root, '../frontend');
const outdir = resolve(process.argv[2]);
await build({
  absWorkingDir: root,
  entryPoints: { app: 'main.jsx' },
  nodePaths: [fileURLToPath(new URL('../frontend/node_modules', import.meta.url))],
  outdir,
  assetNames: 'fonts/[name]',
  bundle: true,
  format: 'iife',
  platform: 'browser',
  target: ['es2017', 'chrome61'],
  jsx: 'automatic',
  loader: { '.woff2': 'file' },
  define: { 'process.env.NODE_ENV': '"production"' },
  minify: true,
  legalComments: 'inline',
});
const cssFile = resolve(outdir, 'app.css');
const css = postcss.parse(await readFile(cssFile, 'utf8'));
css.walkDecls(decl => {
  if (/\d+dvh\b/.test(decl.value)) {
    decl.cloneBefore({ value: decl.value.replace(/(\d+)dvh\b/g, '$1vh') });
  }
  if (decl.prop === 'inset' && decl.value === '0') {
    for (const side of ['top', 'right', 'bottom', 'left']) decl.cloneBefore({ prop: side });
  }
});
const fontLicense = await readFile(resolve(frontend, 'src/assets/fonts/OFL-Archivo.txt'), 'utf8');
await writeFile(cssFile, css.toString() + '\n/* Archivo font license\n' + fontLicense.trim() + '\n*/\n');
const reactLicense = await readFile(resolve(frontend, 'node_modules/react/LICENSE'), 'utf8');
const jsFile = resolve(outdir, 'app.js');
await writeFile(jsFile, '/*! React, React DOM and Scheduler\n' + reactLicense.trim() + '\n*/\n' + await readFile(jsFile, 'utf8'));
await writeFile(resolve(outdir, 'index.html'), await readFile(resolve(root, 'index.html')));
