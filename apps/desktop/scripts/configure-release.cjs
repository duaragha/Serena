'use strict';

const fs = require('node:fs');
const path = require('node:path');
const yaml = require('js-yaml');
const { desktopProfile } = require('../profile');

function configure(pkg, windows, tag) {
  const base = pkg.version.split('-')[0];
  const version = String(tag).replace(/^v/, '');
  if (version !== base && !new RegExp(`^${base.replaceAll('.', '\\.')}\\-dev\\.\\d+$`).test(version)) {
    throw new Error(`Release tag ${tag} does not match package version ${base}`);
  }
  const profile = desktopProfile(version);
  pkg = structuredClone(pkg);
  windows = structuredClone(windows);
  pkg.version = version;
  pkg.name = profile.channel === 'dev' ? 'serena-desktop-dev' : 'serena-desktop';
  const artifact = profile.channel === 'dev' ? 'Serena-Dev' : 'Serena';
  for (const build of [pkg.build, windows]) {
    build.appId = profile.channel === 'dev' ? 'ai.serena.desktop.dev' : 'ai.serena.desktop';
    build.productName = profile.name;
    // Custom channel names prevent development artifacts reaching stable.
    build.generateUpdatesFilesForAllChannels = false;
    build.publish = [{ provider: 'github', owner: 'duaragha', repo: 'Serena',
      channel: profile.updateChannel, releaseType: profile.channel === 'dev' ? 'prerelease' : 'release' }];
  }
  pkg.build.linux.executableName = profile.slug;
  pkg.build.linux.artifactName = `${artifact}-\${version}-\${arch}.\${ext}`;
  pkg.build.linux.desktop.entry.Name = profile.name;
  pkg.build.linux.desktop.entry.StartupWMClass = profile.name;
  windows.artifactName = `${artifact}-Setup-\${version}-\${arch}.\${ext}`;
  windows.win.executableName = profile.name;
  windows.nsis.shortcutName = profile.name;
  return { pkg, windows };
}

if (require.main === module) {
  const root = path.resolve(__dirname, '..');
  const packagePath = path.join(root, 'package.json');
  const windowsPath = path.join(root, 'windows', 'electron-builder.win.yml');
  const result = configure(JSON.parse(fs.readFileSync(packagePath, 'utf8')),
    yaml.load(fs.readFileSync(windowsPath, 'utf8')), process.argv[2]);
  fs.writeFileSync(packagePath, `${JSON.stringify(result.pkg, null, 2)}\n`);
  fs.writeFileSync(windowsPath, yaml.dump(result.windows));
  const lockPath = path.join(root, 'package-lock.json');
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  lock.name = lock.packages[''].name = result.pkg.name;
  lock.version = lock.packages[''].version = result.pkg.version;
  fs.writeFileSync(lockPath, `${JSON.stringify(lock, null, 2)}\n`);
  console.log(`Configured ${result.pkg.build.productName} ${result.pkg.version}`);
}

module.exports = { configure };
