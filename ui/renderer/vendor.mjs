import { copyFile, mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const target = resolve(here, '..', 'static', 'vendor', 'xterm');

const assets = [
  ['@xterm/xterm/css/xterm.css', 'xterm.css'],
  ['@xterm/xterm/lib/xterm.js', 'xterm.js'],
  ['@xterm/addon-fit/lib/addon-fit.js', 'addon-fit.js'],
  ['@xterm/addon-web-links/lib/addon-web-links.js', 'addon-web-links.js'],
  ['@xterm/addon-webgl/lib/addon-webgl.js', 'addon-webgl.js'],
  ['@xterm/addon-canvas/lib/addon-canvas.js', 'addon-canvas.js'],
  ['@xterm/xterm/LICENSE', 'LICENSE.txt'],
];

await mkdir(target, { recursive: true });
for (const [source, name] of assets) {
  await copyFile(resolve(here, 'node_modules', source), resolve(target, name));
}

console.log(`vendored ${assets.length} pinned xterm assets into ${target}`);

const markdownTarget = resolve(here, '..', 'static', 'vendor', 'markdown-it');
await mkdir(markdownTarget, {recursive:true});
for (const [source, name] of [
  ['dist/browser/markdown-it.esm.min.mjs', 'markdown-it.mjs'],
  ['LICENSE', 'LICENSE.txt'],
]) {
  await copyFile(resolve(here, 'node_modules/markdown-it', source), resolve(markdownTarget, name));
}
console.log('vendored pinned markdown-it browser module and license');
