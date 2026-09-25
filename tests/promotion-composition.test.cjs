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

// Stable only moves forward. Features shipped in the newest adopted baseline
// compose onto the original stable, as they did before it; features registered
// after it compose onto that adopted tag, which is where a promotion applies them.
const adoptedStable = (catalog.adoptedStable || []).at(-1) || null;
const shippedInAdopted = new Set(adoptedStable ? adoptedStable.features : []);

function composeEverySubset(t, { features, stable, installed }) {
  const fixture = historyFixture(t);
  const source = fixture.source;
  if (stable !== catalog.initialStable) {
    const next = nextVersion(stable);
    if (git(['tag', '--list', next], fixture.root)) git(['tag', '-d', next], fixture.root);
  }
  let count = 0;
  for (let bits = 1; bits < 2 ** features.length; bits++) {
    const selected = features.filter((_, i) => bits & (1 << i)).map(f => f.id);
    try { selection(catalog, selected, selected, installed); } catch { continue; }
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'serena-selection-'));
    try {
      const destination = path.join(dir, 'candidate');
      const result = prepare({ root: fixture.root, destination, artifacts: dir, source, stable,
        latest: stable, selected, tested: selected, request: 'a'.repeat(32), mode: 'verify' });
      assert.deepEqual(result.features.map(f => f.id),
        catalog.features.map(f => f.id).filter(id => installed.includes(id) || selected.includes(id)));
      for (const feature of catalog.features.filter(f => selected.includes(f.id)))
        assert.equal(result.features.find(f => f.id === feature.id).base, feature.base);
      const changed = git(['diff', '--name-only', `${stable}..HEAD`], destination).split('\n');
      const allowed = new Set(['apps/desktop/package.json', 'apps/desktop/package-lock.json', 'config/stable-promotion.json',
        ...catalog.features.filter(f => selected.includes(f.id)).flatMap(f => f.paths)]);
      assert.ok(changed.every(file => allowed.has(file)), changed.join('\n'));
      for (const file of ['apps/desktop/main.js', 'apps/desktop/profile.js', 'core/workspace_host.py'].filter(f => !allowed.has(f)))
        assert.equal(git(['rev-parse', `HEAD:${file}`], destination), git(['rev-parse', `${stable}:${file}`]));
      const restored = path.join(dir, 'restored');
      git(['clone', '--branch', 'candidate', path.join(dir, 'candidate.bundle'), restored]);
      assert.equal(git(['rev-parse', 'HEAD'], restored), result.commit);
      count++;
    } finally { fs.rmSync(dir, { recursive: true, force: true }); }
  }
  assert.ok(count >= features.length, `too few valid selections: ${count}`);
  t.diagnostic(`${count} valid selections verified onto ${stable}`);
}

test('every valid feature subset composes onto stable without importing unselected backend changes', t => {
  const features = adoptedStable ? catalog.features.filter(f => shippedInAdopted.has(f.id)) : catalog.features;
  composeEverySubset(t, { features, stable: catalog.initialStable, installed: [] });
});

test('every feature registered after the adopted stable composes onto it', t => {
  if (!adoptedStable) return t.skip('no adopted stable baseline');
  const features = catalog.features.filter(f => !shippedInAdopted.has(f.id));
  if (!features.length) return t.skip('nothing registered after the adopted stable');
  try { git(['rev-parse', '--verify', `${adoptedStable.tag}^{commit}`]); }
  catch { return t.skip(`${adoptedStable.tag} is not in this checkout`); }
  composeEverySubset(t, { features, stable: adoptedStable.tag, installed: adoptedStable.features });
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

test('promotion continues from an adopted out-of-band stable and names the next version above it', t => {
  const [adopted] = catalog.adoptedStable || [];
  if (!adopted) return t.skip('no adopted stable baseline');
  const fixture = historyFixture(t);
  try { git(['rev-parse', '--verify', `${adopted.tag}^{commit}`], fixture.root); }
  catch { return t.skip(`${adopted.tag} is not in this checkout`); }
  const next = nextVersion(adopted.tag);
  if (git(['tag', '--list', next], fixture.root)) git(['tag', '-d', next], fixture.root);
  // A throwaway feature committed on top of the source, registered only in this clone.
  const probe = 'docs/promotion-adoption-probe.md';
  fs.writeFileSync(path.join(fixture.root, probe), 'adoption probe\n');
  git(['add', probe], fixture.root);
  git(['-c', 'user.name=t', '-c', 'user.email=t@t.invalid', 'commit', '-q', '-m', 'probe'], fixture.root);
  const commit = git(['rev-parse', 'HEAD'], fixture.root);
  git(['tag', 'v9.9.9-dev.1', commit], fixture.root);
  const registered = structuredClone(catalog);
  registered.features.push({ id: 'adoption-probe', title: 'Adoption probe', devTag: 'v9.9.9-dev.1', commit,
    requires: [], paths: [probe] });
  fs.writeFileSync(path.join(fixture.root, 'config/promotion-features.json'), JSON.stringify(registered));
  const options = { root: fixture.root, source: commit, stable: adopted.tag, latest: adopted.tag,
    selected: ['adoption-probe'], tested: ['adoption-probe'], request: 'c'.repeat(32), mode: 'verify' };
  const result = prepare({ ...options, destination: path.join(fixture.directory, 'adopted'), artifacts: fixture.directory });
  assert.equal(result.version, next);
  assert.equal(result.baseTag, adopted.tag);
  assert.equal(result.baseCommit, adopted.commit);
  assert.deepEqual(result.added, ['adoption-probe']);
  assert.deepEqual(result.features.map(f => f.id), [...adopted.features, 'adoption-probe']);
  const changed = git(['diff', '--name-only', `${adopted.tag}..HEAD`], path.join(fixture.directory, 'adopted')).split('\n');
  assert.deepEqual(changed.sort(), ['apps/desktop/package-lock.json', 'apps/desktop/package.json',
    'config/stable-promotion.json', probe].sort());
  // Already-shipped features cannot be reapplied onto the adopted tree.
  assert.throws(() => prepare({ ...options, destination: path.join(fixture.directory, 'again'),
    selected: [adopted.features[0]], tested: [adopted.features[0]] }), /not already in main/);
  registered.adoptedStable[0].commit = 'f'.repeat(40);
  fs.writeFileSync(path.join(fixture.root, 'config/promotion-features.json'), JSON.stringify(registered));
  assert.throws(() => prepare({ ...options, destination: path.join(fixture.directory, 'retagged') }), /reviewed baseline/);
});
