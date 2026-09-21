'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { prepare } = require('../scripts/prepare-promotion.cjs');
const { selection, nextVersion } = require('../apps/desktop/promotion-policy.cjs');
const catalog = require('../config/promotion-features.json');
const root = path.resolve(__dirname, '..');
const git = (args, cwd = root) => execFileSync('git', args, { cwd, encoding: 'utf8' }).trim();

function historyFixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'serena-promotion-history-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const fixture = path.join(directory, 'repo');
  git(['clone', '--no-hardlinks', root, fixture]);
  const source = git(['rev-parse', 'HEAD'], fixture);
  // Historical composition tests must still work after these versions ship.
  // Only remove tags inside this disposable clone, never from the source or remote.
  for (const version of [nextVersion(catalog.initialStable), nextVersion(nextVersion(catalog.initialStable))]) {
    if (git(['tag', '--list', version], fixture)) git(['tag', '-d', version], fixture);
  }
  return { directory, root: fixture, source };
}

test('every valid feature subset composes onto stable without importing unselected backend changes', t => {
  const fixture = historyFixture(t);
  const source = fixture.source;
  let count = 0;
  for (let bits = 1; bits < 2 ** catalog.features.length; bits++) {
    const selected = catalog.features.filter((_, i) => bits & (1 << i)).map(f => f.id);
    try { selection(catalog, selected, selected); } catch { continue; }
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'serena-selection-'));
    try {
      const destination = path.join(dir, 'candidate');
      const result = prepare({ root: fixture.root, destination, artifacts: dir, source, stable: catalog.initialStable,
        latest: catalog.initialStable, selected, tested: selected, request: 'a'.repeat(32), mode: 'verify' });
      assert.deepEqual(result.features.map(f => f.id), selected);
      for (const feature of catalog.features.filter(f => selected.includes(f.id)))
        assert.equal(result.features.find(f => f.id === feature.id).base, feature.base);
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

test('a second promotion retains the first release and refuses collisions or stale baselines', t => {
  const fixture = historyFixture(t);
  const [first, second] = catalog.features;
  const options = { root: fixture.root, source: fixture.source, artifacts: fixture.directory,
    request: 'b'.repeat(32), mode: 'verify' };
  const one = prepare({ ...options, destination: path.join(fixture.directory, 'one'), stable: catalog.initialStable,
    latest: catalog.initialStable, selected: [first.id], tested: [first.id] });
  git(['fetch', path.join(fixture.directory, 'one'), 'candidate'], fixture.root);
  git(['tag', one.version, one.commit], fixture.root);
  const two = prepare({ ...options, destination: path.join(fixture.directory, 'two'), stable: one.version,
    latest: one.version, selected: [second.id], tested: [second.id] });
  assert.equal(two.version, nextVersion(one.version));
  assert.deepEqual(two.features.map(f => f.id), [first.id, second.id]);
  assert.deepEqual(two.added, [second.id]);
  const attempted = { ...options, destination: path.join(fixture.directory, 'refused'), stable: catalog.initialStable,
    latest: one.version, selected: [second.id], tested: [second.id] };
  assert.throws(() => prepare(attempted), /Main changed/);
  git(['fetch', path.join(fixture.directory, 'two'), 'candidate'], fixture.root);
  git(['tag', two.version, two.commit], fixture.root);
  assert.throws(() => prepare({ ...attempted, stable: one.version }), /already exists/);
});
