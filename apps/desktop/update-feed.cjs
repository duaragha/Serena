'use strict';

const semver = require('semver');
const { acceptsVersion } = require('./profile');

function selectRelease(releases, { profile, platform, arch, owner, repo }) {
  if (!Array.isArray(releases)) throw new Error('Release feed was not a release list');
  const channel = platform === 'linux' ? `dev-linux${arch === 'x64' ? '' : `-${arch}`}.yml` : 'dev.yml';
  if (!['linux', 'win32'].includes(platform)) throw new Error('Unsupported update platform');
  const candidates = [];
  for (const release of releases) {
    const version = String(release?.tag_name || '').replace(/^v/, '');
    if (release?.draft || !release?.prerelease || !acceptsVersion(profile, version) || !semver.valid(version)) continue;
    const tag = release.tag_name;
    const url = `https://github.com/${owner}/${repo}/releases/download/${tag}/`;
    const installer = platform === 'linux'
      ? `Serena-Dev-${version}-${arch === 'x64' ? 'x86_64' : arch}.AppImage`
      : `Serena-Dev-Setup-${version}-${arch}.exe`;
    const assets = Array.isArray(release.assets) ? release.assets : [];
    const complete = [channel, installer].every(name => assets.some(asset =>
      asset.name === name && asset.state === 'uploaded' && asset.size > 0
      && asset.browser_download_url === url + name));
    if (complete) candidates.push({ version, url });
  }
  return candidates.sort((a, b) => semver.rcompare(a.version, b.version))[0] || null;
}

async function resolveDevRelease({ fetch, profile, platform, arch, owner, repo }) {
  const releases = [];
  // GitHub's Atom feed includes bare tags. Only the release API distinguishes
  // a completed upload from a tag whose build failed or has not finished.
  for (let page = 1; page <= 3; page += 1) {
    const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/releases?per_page=100&page=${page}`, {
      headers: { Accept: 'application/vnd.github+json', 'User-Agent': `Serena/${profile.version}` },
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error(`Release feed returned HTTP ${response.status}; try again later`);
    const batch = await response.json();
    if (!Array.isArray(batch)) throw new Error('Release feed was not a release list');
    releases.push(...batch);
    if (batch.length < 100) return selectRelease(releases, { profile, platform, arch, owner, repo });
  }
  const selected = selectRelease(releases, { profile, platform, arch, owner, repo });
  if (!selected) throw new Error('No complete Dev build found in the recent releases');
  return selected;
}

module.exports = { selectRelease, resolveDevRelease };
