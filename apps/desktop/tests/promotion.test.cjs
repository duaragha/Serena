'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const policy = require('../promotion-policy.cjs');
const { PromotionService } = require('../promotion-service.cjs');
const catalog = require('../../../config/promotion-features.json');
const source = 'a'.repeat(40);
const ids = catalog.features.map(f => f.id);

test('selection refuses unknown features, duplicates, missing dependencies and untested additions', () => {
  assert.throws(() => policy.selection(catalog, ['invented'], [], []), /Unknown/);
  assert.throws(() => policy.selection(catalog, [ids[0], ids[0]], [ids[0]]), /duplicate/);
  assert.throws(() => policy.selection(catalog, [ids[2]], [ids[2]]), /requires/);
  assert.throws(() => policy.selection(catalog, [ids[0]], []), /tested/);
  assert.deepEqual(policy.selection(catalog, ids, ids).all.map(f => f.id), ids);
});
test('already shipped features are retained and cannot be revised silently', () => {
  const receipt = { schema: 1, version: 'v0.3.5', features: [catalog.features[0]] };
  assert.deepEqual(policy.installedFeatures(catalog, 'v0.3.5', receipt), [ids[0]]);
  const result = policy.selection(catalog, [ids[1]], [ids[1]], [ids[0]]);
  assert.deepEqual(result.all.map(f => f.id), ids.slice(0, 2));
  assert.throws(() => policy.installedFeatures(catalog, 'v0.3.5', { ...receipt, features: [{ ...catalog.features[0], commit: source }] }), /revision changed/);
  assert.throws(() => policy.installedFeatures(catalog, 'v0.3.5', null), /receipt/);
});
test('catalog disallows paths outside source, duplicate IDs, cycles and mutable commits', () => {
  for (const change of [{ paths: ['../secret'] }, { paths: ['/tmp/x'] }, { commit: 'master' }, { requires: [ids[0]] }]) {
    const altered = structuredClone(catalog); Object.assign(altered.features[0], change);
    assert.throws(() => policy.catalogFeatures(altered), /catalog/);
  }
  assert.equal(policy.nextVersion('v0.3.4'), 'v0.3.5');
  assert.throws(() => policy.nextVersion('v0.3.4; echo bad'));
});

test('reviewed feature ranges require immutable endpoints and preserve their receipt identity', () => {
  const ranged = structuredClone(catalog);
  ranged.features[0].base = 'b'.repeat(40);
  assert.doesNotThrow(() => policy.catalogFeatures(ranged));
  const receipt = { schema: 1, version: 'v0.3.5', features: [ranged.features[0]] };
  assert.deepEqual(policy.installedFeatures(ranged, 'v0.3.5', receipt), [ids[0]]);
  const changed = structuredClone(ranged);
  changed.features[0].base = 'c'.repeat(40);
  assert.throws(() => policy.installedFeatures(changed, 'v0.3.5', receipt), /revision changed/);
  for (const base of ['master', ranged.features[0].commit, null]) {
    changed.features[0].base = base;
    assert.throws(() => policy.catalogFeatures(changed), /catalog/);
  }
});

async function fixture(t, options = {}) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'serena-promotion-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const calls = [];
  let revision = source;
  let runs = [];
  const service = new PromotionService({ directory, version: '0.3.9-dev.1', confirm: options.confirm || (async () => true),
    run: async args => {
      calls.push(args);
      if (args[0] === 'api') {
        if (args[1].endsWith('commits/master')) return JSON.stringify({ sha: revision });
        if (args[1].endsWith('releases/latest')) return JSON.stringify({ tag_name: 'v0.3.4' });
        if (args[1].includes('/contents/')) return JSON.stringify({ encoding: 'base64', content: Buffer.from(JSON.stringify(catalog)).toString('base64') });
      }
      if (args[0] === 'workflow') { if (options.failDispatch) throw new Error('Network lost'); return ''; }
      if (args[0] === 'run') return JSON.stringify(runs);
      throw new Error(`Unexpected command ${args}`);
    } });
  const request = { mode: 'verify', source, stable: 'v0.3.4', selected: [ids[0]], tested: [ids[0]] };
  return { service, calls, request, changeSource: () => { revision = 'b'.repeat(40); }, setRuns: value => { runs = value; } };
}
test('service sends only validated fixed workflow arguments and never invokes installer or backend', async t => {
  const { service, calls, request } = await fixture(t);
  await service.load();
  const result = await service.submit(request);
  assert.match(result.request, /^[a-f0-9]{32}$/);
  const dispatch = calls.find(a => a[0] === 'workflow');
  assert.deepEqual(dispatch.slice(0, 7), ['workflow', 'run', 'selective-promotion.yml', '--repo', 'duaragha/Serena', '--ref', 'master']);
  assert.ok(dispatch.includes('mode=verify'));
  assert.equal((await service.pending()).request, result.request);
  assert.ok(calls.every(a => ['api', 'workflow', 'run'].includes(a[0])));
  await assert.rejects(service.submit(request), /previous request/);
});
test('native cancellation sends no workflow; stale source is rejected before dispatch', async t => {
  const cancelled = await fixture(t, { confirm: async () => false });
  await cancelled.service.load();
  assert.deepEqual(await cancelled.service.submit(cancelled.request), { cancelled: true });
  assert.ok(!cancelled.calls.some(a => a[0] === 'workflow'));
  const stale = await fixture(t); await stale.service.load(); stale.changeSource();
  await assert.rejects(stale.service.submit(stale.request), /changed/);
  assert.ok(!stale.calls.some(a => a[0] === 'workflow'));
});
test('uncertain dispatch retains request for recovery rather than permitting duplicate publication', async t => {
  const { service, request } = await fixture(t, { failDispatch: true });
  await service.load();
  await assert.rejects(service.submit(request), /Network lost/);
  assert.ok((await service.pending()).request);
  await assert.rejects(service.submit(request), /previous request/);
});
test('concurrent clicks cannot submit twice; completed verification permits explicit publication', async t => {
  let releaseConfirm;
  const gate = new Promise(resolve => { releaseConfirm = resolve; });
  const f = await fixture(t, { confirm: () => gate }); await f.service.load();
  const first = f.service.submit(f.request);
  await assert.rejects(f.service.submit(f.request), /already being submitted/);
  releaseConfirm(true); const result = await first;
  f.setRuns([{ displayTitle: `Promotion verify ${result.request}`, status: 'completed', conclusion: 'success' }]);
  await f.service.submit({ ...f.request, mode: 'publish' });
  assert.equal(f.calls.filter(a => a[0] === 'workflow').length, 2);
});
test('invalid input and untested selections never reach GitHub dispatch', async t => {
  const f = await fixture(t); await f.service.load();
  for (const value of [null, { ...f.request, mode: 'install' }, { ...f.request, selected: ['--exec=rm'] }, { ...f.request, tested: [] }])
    await assert.rejects(f.service.submit(value));
  assert.ok(!f.calls.some(a => a[0] === 'workflow'));
});

test('an adopted out-of-band stable counts as installed only at its reviewed commit', () => {
  const [adopted] = catalog.adoptedStable;
  assert.deepEqual(policy.installedFeatures(catalog, adopted.tag, null, adopted.commit),
    ids.filter(id => adopted.features.includes(id)));
  assert.throws(() => policy.installedFeatures(catalog, adopted.tag, null, 'f'.repeat(40)), /reviewed baseline commit/);
  assert.throws(() => policy.installedFeatures(catalog, adopted.tag, null, null), /reviewed baseline commit/);
  // A later promotion's receipt, not the adoption, describes any release that has one.
  const receipt = { schema: 1, version: adopted.tag, features: [catalog.features[0]] };
  assert.deepEqual(policy.installedFeatures(catalog, adopted.tag, receipt), [ids[0]]);
  assert.equal(policy.nextVersion(adopted.tag), 'v0.3.11');
});

test('adopted baselines must be exact, known, unique and dependency-complete', () => {
  const [adopted] = catalog.adoptedStable;
  const needsDependency = catalog.features.find(f => f.requires.length);
  for (const change of [
    { commit: 'master' }, { tag: 'v0.3' }, { tag: catalog.initialStable }, { reason: '' },
    { features: ['invented'] }, { features: [ids[0], ids[0]] }, { features: [needsDependency.id] },
  ]) {
    const altered = structuredClone(catalog);
    Object.assign(altered.adoptedStable[0], change);
    assert.throws(() => policy.catalogFeatures(altered), /adopted/, JSON.stringify(change));
  }
  const duplicated = structuredClone(catalog);
  duplicated.adoptedStable.push(structuredClone(adopted));
  assert.throws(() => policy.catalogFeatures(duplicated), /adopted/);
});

function releaseService(t, { latest, tagCommit, receipt = null }) {
  const calls = [];
  return fs.mkdtemp(path.join(os.tmpdir(), 'serena-promotion-adopted-')).then(directory => {
    t.after(() => fs.rm(directory, { recursive: true, force: true }));
    const service = new PromotionService({ directory, version: '0.3.10-dev.14', run: async args => {
      calls.push(args.join(' '));
      if (args[1].endsWith('commits/master')) return JSON.stringify({ sha: source });
      if (args[1].endsWith('releases/latest')) return JSON.stringify({ tag_name: latest });
      if (args[1].endsWith(`commits/${latest}`)) return JSON.stringify({ sha: tagCommit });
      if (args[1].includes('contents/config/promotion-features.json'))
        return JSON.stringify({ encoding: 'base64', content: Buffer.from(JSON.stringify(catalog)).toString('base64') });
      if (args[1].includes('contents/config/stable-promotion.json')) {
        if (!receipt) throw new Error('GitHub request failed. Check your connection and GitHub CLI sign-in. gh: Not Found (HTTP 404)');
        return JSON.stringify({ encoding: 'base64', content: Buffer.from(JSON.stringify(receipt)).toString('base64') });
      }
      throw new Error(`Unexpected command ${args}`);
    } });
    return { service, calls };
  });
}

test('the release window loads an adopted main without asking GitHub for a receipt it never had', async t => {
  const [adopted] = catalog.adoptedStable;
  const { service, calls } = await releaseService(t, { latest: adopted.tag, tagCommit: adopted.commit });
  const loaded = await service.load();
  assert.equal(loaded.stable, adopted.tag);
  assert.deepEqual(loaded.installed, ids.filter(id => adopted.features.includes(id)));
  assert.ok(!calls.some(call => call.includes('stable-promotion.json')));
  const retagged = await releaseService(t, { latest: adopted.tag, tagCommit: 'e'.repeat(40) });
  await assert.rejects(retagged.service.load(), /reviewed baseline commit/);
});

test('an unreviewed out-of-band main is named as such, not as a sign-in failure', async t => {
  const { service } = await releaseService(t, { latest: 'v0.3.99', tagCommit: 'd'.repeat(40) });
  await assert.rejects(service.load(), error => {
    assert.match(error.message, /published outside Promote to Main/);
    assert.doesNotMatch(error.message, /sign-in/);
    return true;
  });
});
