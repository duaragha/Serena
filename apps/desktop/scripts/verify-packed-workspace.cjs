'use strict';

const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {execFileSync} = require('node:child_process');

module.exports = async ({appOutDir}) => {
  const root = path.join(appOutDir, 'resources', 'runtimes', 'claude-sdk');
  const sdk = path.join(root, 'node_modules', '@anthropic-ai', 'claude-agent-sdk');
  const expected = JSON.parse(fs.readFileSync(path.join(root, 'package.json'))).dependencies['@anthropic-ai/claude-agent-sdk'];
  const installed = JSON.parse(fs.readFileSync(path.join(sdk, 'package.json')));
  if (installed.name !== '@anthropic-ai/claude-agent-sdk' || installed.version !== expected) {
    throw Error('Packaged Claude SDK identity does not match its runtime manifest');
  }
  const url = pathToFileURL(path.join(sdk, 'sdk.mjs')).href;
  execFileSync(process.execPath, ['--input-type=module', '-e', `await import(${JSON.stringify(url)})`], {timeout:30000, stdio:'pipe'});
};
