const assert = require('node:assert/strict');
const test = require('node:test');
const { SemVer } = require('semver');
const { GitHubProvider } = require('electron-updater/out/providers/GitHubProvider');

for (const platform of ['linux', 'win32']) {
  for (const channel of ['latest', 'dev']) {
    test(`real updater selects only ${channel} releases on ${platform}`, async () => {
      const versions = ['0.4.0', '0.3.1-dev.2', '0.3.1-dev.1', '0.3.1'];
      const feed = `<feed>${versions.map(version => `<entry><title>${version}</title><link href="https://github.com/duaragha/Serena/releases/tag/v${version}"/><content>Release</content></entry>`).join('')}</feed>`;
      const expected = channel === 'dev' ? '0.3.1-dev.2' : '0.4.0';
      const filename = `${channel}${platform === 'linux' ? '-linux' : ''}.yml`;
      const paths = [];
      const provider = new GitHubProvider({ owner: 'duaragha', repo: 'Serena' }, {
        channel, allowPrerelease: channel === 'dev',
        currentVersion: new SemVer(channel === 'dev' ? '0.3.0-dev.1' : '0.3.0'),
      }, { platform, executor: { request: async options => {
        paths.push(options.path);
        if (options.path.endsWith('.atom')) return feed;
        if (options.path.endsWith('/latest')) return JSON.stringify({ tag_name: 'v0.4.0' });
        assert.equal(options.path, `/duaragha/Serena/releases/download/v${expected}/${filename}`);
        return `version: ${expected}\nfiles:\n  - url: package.AppImage\n    sha512: proof\n`;
      } } });
      const info = await provider.getLatestVersion();
      assert.equal(info.version, expected);
      assert.ok(paths.at(-1).endsWith(filename));
    });
  }
}
