const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const verify = require('../scripts/verify-packed-workspace.cjs');

test('finished package requires the SDK files, pinned identity and importable dependencies', async () => {
  const appOutDir = fs.mkdtempSync(path.join(os.tmpdir(), 'packed-workspace-'));
  try {
    const root = path.join(appOutDir, 'resources/runtimes/claude-sdk');
    fs.mkdirSync(root, {recursive:true});
    fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({dependencies:{'@anthropic-ai/claude-agent-sdk':'1.0.0'}}));
    await assert.rejects(verify({appOutDir}), /ENOENT/);
    const sdk = path.join(root, 'node_modules/@anthropic-ai/claude-agent-sdk');
    fs.mkdirSync(sdk, {recursive:true});
    fs.writeFileSync(path.join(sdk, 'package.json'), JSON.stringify({name:'@anthropic-ai/claude-agent-sdk',version:'wrong'}));
    await assert.rejects(verify({appOutDir}), /identity/);
    fs.writeFileSync(path.join(sdk, 'package.json'), JSON.stringify({name:'@anthropic-ai/claude-agent-sdk',version:'1.0.0'}));
    fs.writeFileSync(path.join(sdk, 'sdk.mjs'), 'import "missing-sdk-dependency";');
    await assert.rejects(verify({appOutDir}));
    fs.writeFileSync(path.join(sdk, 'sdk.mjs'), 'export const query = () => {};');
    await verify({appOutDir});
  } finally {
    fs.rmSync(appOutDir, {recursive:true,force:true});
  }
});
