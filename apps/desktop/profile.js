'use strict';

const path = require('node:path');

function desktopProfile(version, { packaged = true, argv = [] } = {}) {
  const dev = /^\d+\.\d+\.\d+-dev\.\d+$/.test(version)
    || (!packaged && argv.includes('--dev'));
  return Object.freeze({
    channel: dev ? 'dev' : 'stable',
    updateChannel: dev ? 'dev' : 'latest',
    name: dev ? 'Serena Dev' : 'Serena',
    slug: dev ? 'serena-dev' : 'serena',
    structured: dev,
    version,
  });
}

function backendEnvironment(profile, home) {
  const config = path.join(home, '.config', profile.slug);
  return {
    SERENA_DESKTOP_CHANNEL: profile.channel,
    SERENA_DESKTOP_VERSION: profile.version,
    SERENA_STRUCTURED_WORKSPACE: profile.structured ? '1' : '0',
    CHATS_DATA_DIR: path.join(home, '.local', 'share', profile.structured ? 'chats-dev' : 'chats'),
    SERENA_CONFIG_DIR: config,
    SERENA_CODING_MODEL_PATH: path.join(home, '.local', 'state', profile.slug, 'coding-model.json'),
    // Deliberately shared: editions must not become concurrent native writers.
    SERENA_RUNTIME_LEASE_DIR: path.join(home, '.config', 'serena', 'runtime-leases'),
    SERENA_CALL_RUNTIME: 'lazy',
  };
}

function acceptsVersion(profile, version) {
  return profile.channel === 'dev'
    ? /^\d+\.\d+\.\d+-dev\.\d+$/.test(version)
    : /^\d+\.\d+\.\d+$/.test(version);
}

module.exports = { desktopProfile, backendEnvironment, acceptsVersion };
