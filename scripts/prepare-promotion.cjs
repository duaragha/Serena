'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const policy = require('../apps/desktop/promotion-policy.cjs');
const REPO = 'duaragha/Serena';
const receiptPath = 'config/stable-promotion.json';

function command(binary, args, options = {}) {
  return execFileSync(binary, args, { encoding: 'utf8', timeout: 120000, maxBuffer: 32 * 1024 * 1024, ...options });
}
function gh(args) { return command('gh', args); }
function git(cwd, args, input) { return command('git', args, { cwd, input }); }

function prepare({ root, destination, source, stable, selected, tested, request, mode, latest, artifacts = root }) {
  if (!policy.SHA.test(source) || !policy.STABLE.test(stable) || !policy.REQUEST.test(request)
      || !['verify', 'publish'].includes(mode)) throw new Error('Invalid promotion request');
  if (latest !== stable) throw new Error('Main changed since this selection; refresh and review again');
  if (git(root, ['rev-parse', 'HEAD']).trim() !== source) throw new Error('Dev source changed; refresh and review again');
  if (fs.existsSync(destination)) throw new Error('Candidate directory already exists');
  const catalog = JSON.parse(fs.readFileSync(path.join(root, 'config/promotion-features.json'), 'utf8'));
  policy.catalogFeatures(catalog);
  const base = git(root, ['rev-parse', `${stable}^{commit}`]).trim();
  const adopted = policy.adoptedBaseline(catalog, stable);
  let previous = null;
  if (stable !== catalog.initialStable && !adopted) {
    try { previous = JSON.parse(git(root, ['show', `${base}:${receiptPath}`])); }
    catch { throw new Error(`${stable} has no promotion receipt; review it as an adoptedStable baseline first`); }
  }
  const installed = policy.installedFeatures(catalog, stable, previous, base);
  // An adopted tree must really hold what its entry claims.
  if (adopted) for (const f of catalog.features.filter(f => installed.includes(f.id))) {
    git(root, ['merge-base', '--is-ancestor', f.commit, base]);
  }
  const plan = policy.selection(catalog, selected, tested, installed);
  const version = policy.nextVersion(stable);
  if (git(root, ['tag', '--list', version]).trim()) throw new Error(`${version} already exists; review the stable baseline`);

  git(root, ['clone', '--no-hardlinks', '--no-checkout', root, destination]);
  git(destination, ['checkout', '-b', 'candidate', base]);
  const patches = [];
  for (const f of plan.added) {
    // Exact commits and exact paths only. Three-way conflicts abort the entire candidate.
    git(root, ['merge-base', '--is-ancestor', f.commit, source]);
    git(root, ['merge-base', '--is-ancestor', f.commit, `${f.devTag}^{commit}`]);
    const baseCommit = f.base || `${f.commit}^`;
    git(root, ['merge-base', '--is-ancestor', baseCommit, f.commit]);
    const patch = git(root, ['diff', '--binary', baseCommit, f.commit, '--', ...f.paths]);
    if (!patch.trim()) throw new Error(`Empty feature patch: ${f.id}`);
    git(destination, ['apply', '--3way', '--index', '-'], patch);
    patches.push({ id: f.id, sha256: createHash('sha256').update(patch).digest('hex') });
  }
  const desktop = path.join(destination, 'apps/desktop');
  for (const name of ['package.json', 'package-lock.json']) {
    const file = path.join(desktop, name);
    const data = JSON.parse(fs.readFileSync(file, 'utf8'));
    data.version = version.slice(1);
    if (data.packages?.['']) data.packages[''].version = data.version;
    fs.writeFileSync(file, `${JSON.stringify(data, null, 2)}\n`);
  }
  const receipt = { schema: 1, version, baseTag: stable, baseCommit: base, source, request,
    features: plan.all.map(f => ({ id: f.id, commit: f.commit, devTag: f.devTag,
      ...(f.base ? { base: f.base } : {}) })), patches };
  fs.writeFileSync(path.join(destination, receiptPath), `${JSON.stringify(receipt, null, 2)}\n`);
  git(destination, ['add', 'apps/desktop/package.json', 'apps/desktop/package-lock.json', receiptPath]);
  git(destination, ['-c', 'user.name=Serena Release', '-c', 'user.email=release@serena.invalid',
    'commit', '-m', `release: selected features for ${version}`]);
  const commit = git(destination, ['rev-parse', 'HEAD']).trim();
  const result = { ...receipt, commit, mode, added: plan.added.map(f => f.id) };
  fs.writeFileSync(path.join(artifacts, 'promotion-plan.json'), `${JSON.stringify(result, null, 2)}\n`);
  git(destination, ['bundle', 'create', path.join(artifacts, 'candidate.bundle'), 'candidate']);
  return result;
}

if (require.main === module) {
  try {
    const root = process.cwd();
    const latest = JSON.parse(gh(['api', `repos/${REPO}/releases/latest`])).tag_name;
    const result = prepare({ root, destination: path.join(root, 'candidate'), latest,
      source: process.env.PROMOTION_SOURCE, stable: process.env.PROMOTION_STABLE,
      selected: JSON.parse(process.env.PROMOTION_SELECTED || '[]'), tested: JSON.parse(process.env.PROMOTION_TESTED || '[]'),
      request: process.env.PROMOTION_REQUEST, mode: process.env.PROMOTION_MODE });
    console.log(JSON.stringify(result, null, 2));
    if (process.env.GITHUB_STEP_SUMMARY) fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY,
      `## ${result.mode === 'verify' ? 'Verification only' : 'Stable candidate'}: ${result.version}\n\nBase: ${result.baseTag}\n\nSelected: ${result.added.join(', ')}\n\nSource: ${result.source}\n\nNo local app is installed or restarted.\n`);
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
module.exports = { prepare };
