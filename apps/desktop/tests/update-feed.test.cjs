'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { selectRelease, resolveDevRelease } = require('../update-feed.cjs');
const { desktopProfile } = require('../profile');
const { GenericProvider } = require('electron-updater/out/providers/GenericProvider');

const options = { profile: desktopProfile('0.3.8-dev.3'), platform: 'linux', arch: 'x64', owner: 'duaragha', repo: 'Serena' };
function release(version = '0.3.10-dev.2', platform = 'linux') {
  const tag_name = `v${version}`;
  const url = `https://github.com/duaragha/Serena/releases/download/${tag_name}/`;
  const names = platform === 'linux' ? ['dev-linux.yml', `Serena-Dev-${version}-x86_64.AppImage`] : ['dev.yml', `Serena-Dev-Setup-${version}-x64.exe`];
  return { tag_name, draft: false, prerelease: true, assets: names.map(name => ({ name, size: 100, state: 'uploaded', browser_download_url: url + name })) };
}

test('failed builds, drafts, partial uploads and bare tags cannot hide a complete release', () => {
  const good = release();
  const newer = release('0.3.10-dev.3');
  for (const broken of [
    { ...newer, assets: [] }, { ...newer, draft: true },
    { ...newer, assets: newer.assets.slice(0, 1) },
    { ...newer, assets: newer.assets.slice(1) },
    { ...newer, assets: newer.assets.map(asset => ({ ...asset, state: 'starter' })) },
    { ...newer, assets: newer.assets.map(asset => ({ ...asset, size: 0 })) },
    { ...newer, assets: newer.assets.map(asset => ({ ...asset, browser_download_url: 'https://example.com/fake' })) },
    { tag_name: 'v0.3.11-dev.1' },
  ]) assert.equal(selectRelease([broken, good], options).version, '0.3.10-dev.2');
});

test('sorts versions semantically rather than by publish time or lexically', () => {
  assert.equal(selectRelease([release('0.3.9-dev.9'), release('0.3.10-dev.2'), release('0.3.10-dev.10')], options).version, '0.3.10-dev.10');
});

test('never mixes editions, platforms, architectures or manifest-only uploads', () => {
  assert.equal(selectRelease([release('0.4.0'), release('0.4.0-beta.1'), release('0.3.10-dev.2', 'win32')], options), null);
  assert.equal(selectRelease([release()], { ...options, arch: 'arm64' }), null);
  assert.equal(selectRelease([release()], { ...options, platform: 'win32' }), null);
  assert.equal(selectRelease([release('0.3.9-dev.4', 'win32'), release()], { ...options, platform: 'win32' }).version, '0.3.9-dev.4');
  assert.equal(selectRelease([{ ...release(), prerelease: false }], options), null);
});

test('validates release list shape and reports unsupported platforms', () => {
  assert.throws(() => selectRelease({}, options), /release list/);
  assert.throws(() => selectRelease([], { ...options, platform: 'darwin' }), /Unsupported/);
});

test('fetches public releases without credentials and never consults Atom', async () => {
  const selected = await resolveDevRelease({ ...options, fetch: async (url, request) => {
    assert.equal(url, 'https://api.github.com/repos/duaragha/Serena/releases?per_page=100&page=1');
    assert.equal(request.headers.Authorization, undefined);
    assert.ok(request.signal);
    return { ok: true, json: async () => [release()] };
  } });
  assert.equal(selected.version, '0.3.10-dev.2');
});

test('paginates past incomplete releases and rejects API failures instead of reporting no updates', async () => {
  let calls = 0;
  const selected = await resolveDevRelease({ ...options, fetch: async () => ({ ok: true, json: async () => ++calls === 1 ? Array(100).fill({ tag_name: 'v0.3.11-dev.1' }) : [release()] }) });
  assert.equal(calls, 2);
  assert.equal(selected.version, '0.3.10-dev.2');
  for (const status of [403, 404, 429, 500]) {
    await assert.rejects(resolveDevRelease({ ...options, fetch: async () => ({ ok: false, status }) }), new RegExp(`HTTP ${status}`));
  }
  assert.equal(await resolveDevRelease({ ...options, fetch: async () => ({ ok: true, json: async () => [] }) }), null);
});

for (const platform of ['linux', 'win32']) {
  test(`real updater reads the selected ${platform} manifest and resolves the installer on its pinned tag`, async () => {
    const selected = selectRelease([release('0.3.10-dev.2', platform)], { ...options, platform });
    const artifact = release('0.3.10-dev.2', platform).assets[1].name;
    const filename = platform === 'linux' ? 'dev-linux.yml' : 'dev.yml';
    const provider = new GenericProvider({ url: selected.url, channel: 'dev' }, { channel: 'dev' }, {
      platform, executor: { request: async request => {
        assert.equal(request.hostname, 'github.com');
        assert.equal(request.path, `/duaragha/Serena/releases/download/v0.3.10-dev.2/${filename}`);
        return `version: 0.3.10-dev.2\nfiles:\n  - url: ${artifact}\n    sha512: proof\n`;
      } },
    });
    const info = await provider.getLatestVersion();
    assert.equal(info.version, selected.version);
    const resolved = provider.resolveFiles(info);
    assert.equal(resolved[0].url.href, selected.url + artifact);
    assert.equal(resolved[0].info.sha512, 'proof');
  });
}
