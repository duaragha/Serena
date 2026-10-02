'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { prepare } = require('../scripts/prepare-promotion.cjs');
const catalog = require('../config/promotion-features.json');
const root = path.resolve(__dirname, '..');
const git = (cwd, args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }).trim();

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'full-dev-promotion-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const repo = path.join(directory, 'source');
  git(root, ['clone', '--no-hardlinks', root, repo]);
  // Include the catalog under review and an unregistered feature. A full
  // promotion must preserve the entire tree, not just catalog-selected paths.
  fs.copyFileSync(path.join(root, 'config/promotion-features.json'), path.join(repo, 'config/promotion-features.json'));
  fs.writeFileSync(path.join(repo, 'full-dev-probe.txt'), 'released but unregistered\n');
  git(repo, ['add', '.']);
  git(repo, ['-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'full release fixture']);
  const source = git(repo, ['rev-parse', 'HEAD']);
  git(repo, ['tag', 'v99.0.0-dev.1']);
  // All mutation is confined to this disposable clone.
  if (git(repo, ['tag', '--list', 'v0.3.14'])) git(repo, ['tag', '-d', 'v0.3.14']);
  const options = { root: repo, source, artifacts: directory, destination: path.join(directory, 'candidate'),
    stable: 'v0.3.13', latest: 'v0.3.13', request: 'f'.repeat(32), mode: 'verify',
    selected: catalog.features.map(f => f.id), tested: catalog.features.map(f => f.id),
    devTag: 'v99.0.0-dev.1', devCommit: source };
  return { directory, repo, options };
}

test('full promotion preserves every released file and a normal selective-promotion receipt', t => {
  const { repo, options } = fixture(t);
  const plan = prepare(options);
  assert.equal(plan.version, 'v0.3.14');
  assert.deepEqual(plan.fullDev, { tag: options.devTag, commit: options.devCommit,
    tree: git(repo, ['rev-parse', `${options.devCommit}^{tree}`]) });
  assert.deepEqual(plan.features.map(f => f.id), catalog.features.map(f => f.id));
  assert.equal(fs.readFileSync(path.join(options.destination, 'full-dev-probe.txt'), 'utf8'), 'released but unregistered\n');
  assert.deepEqual(git(options.destination, ['diff', '--name-only', options.devCommit, 'HEAD']).split('\n').sort(),
    ['apps/desktop/package-lock.json', 'apps/desktop/package.json', 'config/stable-promotion.json']);
  assert.equal(git(options.destination, ['rev-parse', 'HEAD^']), git(repo, ['rev-parse', options.stable]));
  assert.equal(require('../apps/desktop/promotion-policy.cjs').installedFeatures(catalog, plan.version, plan).length, catalog.features.length);
});

test('full promotion rejects moved tags, partial selections, missing testing and non-Dev references', t => {
  const { options } = fixture(t);
  assert.throws(() => prepare({ ...options, devCommit: 'a'.repeat(40) }), /Dev tag changed/);
  assert.throws(() => prepare({ ...options, devTag: options.stable }), /exact Dev tag/);
  assert.throws(() => prepare({ ...options, devCommit: '' }), /exact Dev tag/);
  assert.throws(() => prepare({ ...options, selected: ['windows-chat-index-refresh'] }), /every registered feature/);
  assert.throws(() => prepare({ ...options, tested: [] }), /Mark as tested/);
  assert.equal(fs.existsSync(options.destination), false);
});
