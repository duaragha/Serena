'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { prepare } = require('../scripts/prepare-promotion.cjs');
const { selection } = require('../apps/desktop/promotion-policy.cjs');
const catalog = require('../config/promotion-features.json');
const root = path.resolve(__dirname, '..');
const git = (args, cwd = root) => execFileSync('git', args, { cwd, encoding: 'utf8' }).trim();

test('every valid feature subset composes onto stable without importing unselected backend changes', t => {
  const source = git(['rev-parse', 'HEAD']);
  let count = 0;
  for (let bits = 1; bits < 2 ** catalog.features.length; bits++) {
    const selected = catalog.features.filter((_, i) => bits & (1 << i)).map(f => f.id);
    try { selection(catalog, selected, selected); } catch { continue; }
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'serena-selection-'));
    try {
      const destination = path.join(dir, 'candidate');
      const result = prepare({ root, destination, artifacts: dir, source, stable: catalog.initialStable,
        latest: catalog.initialStable, selected, tested: selected, request: 'a'.repeat(32), mode: 'verify' });
      assert.deepEqual(result.features.map(f => f.id), selected);
      const changed = git(['diff', '--name-only', `${catalog.initialStable}..HEAD`], destination).split('\n');
      const allowed = new Set(['apps/desktop/package.json', 'apps/desktop/package-lock.json', 'config/stable-promotion.json',
        ...catalog.features.filter(f => selected.includes(f.id)).flatMap(f => f.paths)]);
      assert.ok(changed.every(file => allowed.has(file)), changed.join('\n'));
      for (const file of ['apps/desktop/main.js', 'apps/desktop/profile.js', 'core/workspace_host.py'])
        assert.equal(git(['rev-parse', `HEAD:${file}`], destination), git(['rev-parse', `${catalog.initialStable}:${file}`]));
      const restored = path.join(dir, 'restored');
      git(['clone', '--branch', 'candidate', path.join(dir, 'candidate.bundle'), restored]);
      assert.equal(git(['rev-parse', 'HEAD'], restored), result.commit);
      count++;
    } finally { fs.rmSync(dir, { recursive: true, force: true }); }
  }
  assert.ok(count >= catalog.features.length, `too few valid selections: ${count}`);
  t.diagnostic(`${count} valid selections verified`);
});
