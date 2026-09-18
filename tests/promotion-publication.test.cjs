'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const yaml = require('../apps/desktop/node_modules/js-yaml');
const { verifyAssets, publish } = require('../scripts/publish-promotion.cjs');

function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'promotion-artifacts-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const plan = { version: 'v0.3.5', baseTag: 'v0.3.4', commit: 'a'.repeat(40), request: 'b'.repeat(32), mode: 'verify' };
  fs.writeFileSync(path.join(dir, 'promotion-plan.json'), JSON.stringify(plan));
  const names = ['Serena-0.3.5-x86_64.AppImage', 'Serena-Setup-0.3.5-x64.exe'];
  names.forEach((name, i) => {
    const bytes = Buffer.from(`fixture ${name}`);
    const sha512 = createHash('sha512').update(bytes).digest('base64');
    fs.writeFileSync(path.join(dir, name), bytes);
    fs.writeFileSync(path.join(dir, i ? 'latest.yml' : 'latest-linux.yml'), yaml.dump({
      version: '0.3.5', path: name, sha512, files: [{ url: name, sha512, size: bytes.length }] }));
  });
  return { dir, plan, names };
}
test('verify mode validates both platforms without invoking git or GitHub publication', t => {
  const { dir, plan } = fixture(t);
  assert.equal(verifyAssets(dir, plan).length, 4);
  assert.deepEqual(publish('/does/not/exist', dir), { published: false, version: 'v0.3.5' });
});
test('incomplete, tampered and cross-version artifacts fail closed', t => {
  const { dir, plan, names } = fixture(t);
  fs.appendFileSync(path.join(dir, names[0]), 'tampered');
  assert.throws(() => verifyAssets(dir, plan), /Checksum/);
  fs.unlinkSync(path.join(dir, 'latest.yml'));
  assert.throws(() => verifyAssets(dir, plan));
  assert.throws(() => verifyAssets(dir, { ...plan, version: 'v0.3.5-dev.1' }), /Invalid/);
});
test('workflow never publishes a platform before both builds pass', () => {
  const workflow = yaml.load(fs.readFileSync(path.resolve(__dirname, '../.github/workflows/selective-promotion.yml'), 'utf8'));
  assert.deepEqual(workflow.jobs.finish.needs, ['linux', 'windows']);
  assert.equal(workflow.concurrency['cancel-in-progress'], false);
  for (const platform of ['linux', 'windows']) {
    assert.equal(workflow.jobs[platform].permissions, undefined);
    assert.ok(!JSON.stringify(workflow.jobs[platform]).includes('--publish always'));
  }
  assert.equal(workflow.permissions.contents, 'read');
  assert.equal(workflow.jobs.finish.permissions.contents, 'write');
});
