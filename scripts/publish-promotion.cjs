'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const yaml = require('../apps/desktop/node_modules/js-yaml');
const { STABLE, SHA, REQUEST } = require('../apps/desktop/promotion-policy.cjs');
const REPO = 'duaragha/Serena';
function run(binary, args, cwd) { return execFileSync(binary, args, { cwd, encoding: 'utf8', timeout: 300000, maxBuffer: 8 * 1024 * 1024 }); }

function verifyAssets(dir, plan) {
  if (!STABLE.test(plan.version) || !STABLE.test(plan.baseTag) || !SHA.test(plan.commit)
      || !REQUEST.test(plan.request) || !['verify', 'publish'].includes(plan.mode)) throw new Error('Invalid candidate plan');
  const expected = [
    ['latest-linux.yml', `Serena-${plan.version.slice(1)}-x86_64.AppImage`],
    ['latest.yml', `Serena-Setup-${plan.version.slice(1)}-x64.exe`],
  ];
  const names = [];
  for (const [feed, binary] of expected) {
    const manifest = yaml.load(fs.readFileSync(path.join(dir, feed), 'utf8'));
    if (manifest.version !== plan.version.slice(1) || manifest.files?.length !== 1
        || manifest.files[0].url !== binary || manifest.path !== binary) throw new Error(`Invalid ${feed}`);
    const bytes = fs.readFileSync(path.join(dir, binary));
    const digest = createHash('sha512').update(bytes).digest('base64');
    if (bytes.length !== manifest.files[0].size || digest !== manifest.files[0].sha512
        || digest !== manifest.sha512) throw new Error(`Checksum mismatch: ${binary}`);
    names.push(feed, binary);
  }
  const blockmap = `Serena-Setup-${plan.version.slice(1)}-x64.exe.blockmap`;
  if (fs.existsSync(path.join(dir, blockmap))) names.push(blockmap);
  return names;
}

function publish(root, dir, { command = run, authorization = {
  request: process.env.PROMOTION_REQUEST, mode: process.env.PROMOTION_MODE,
} } = {}) {
  const gh = args => command('gh', args, root);
  const git = args => command('git', args, root);
  const plan = JSON.parse(fs.readFileSync(path.join(dir, 'promotion-plan.json'), 'utf8'));
  const names = verifyAssets(dir, plan);
  if (plan.mode !== 'publish') return { published: false, version: plan.version };
  if (plan.request !== authorization.request || authorization.mode !== 'publish')
    throw new Error('Publish authorization does not match this candidate');
  const assertCurrent = () => {
    if (JSON.parse(gh(['api', `repos/${REPO}/releases/latest`])).tag_name !== plan.baseTag)
      throw new Error('Main changed during the build. Candidate was not published; refresh and rebuild.');
  };
  assertCurrent();
  git(['fetch', path.join(dir, 'candidate.bundle'), 'candidate']);
  if (git(['rev-parse', 'FETCH_HEAD']).trim() !== plan.commit) throw new Error('Candidate source mismatch');
  const receipt = git(['show', `${plan.commit}:config/stable-promotion.json`]);
  const parsed = JSON.parse(receipt);
  if (parsed.request !== plan.request || parsed.version !== plan.version || parsed.baseTag !== plan.baseTag)
    throw new Error('Candidate receipt mismatch');
  const receiptFile = path.join(dir, 'stable-promotion.json');
  fs.writeFileSync(receiptFile, receipt);
  // No force push and no overwrite: retries cannot silently replace shipped bits.
  git(['push', 'origin', `${plan.commit}:refs/tags/${plan.version}`]);
  gh(['release', 'create', plan.version, '--repo', REPO, '--verify-tag', '--draft',
    '--title', `Serena ${plan.version.slice(1)}`, '--notes',
    `Selected features: ${plan.features.map(f => f.id).join(', ')}\n\nBased on ${plan.baseTag}. Linux and Windows build gates passed. Original CLI terminals retained.`,
    ...names.map(name => path.join(dir, name)), receiptFile]);
  const release = JSON.parse(gh(['api', `repos/${REPO}/releases/tags/${plan.version}`]));
  for (const file of [...names, 'stable-promotion.json']) {
    const asset = release.assets.find(a => a.name === file);
    if (!asset || asset.state !== 'uploaded' || asset.size !== fs.statSync(path.join(dir, file)).size)
      throw new Error(`Incomplete upload: ${file}. Release remains a draft.`);
  }
  assertCurrent();
  gh(['release', 'edit', plan.version, '--repo', REPO, '--draft=false', '--latest']);
  return { published: true, version: plan.version };
}
if (require.main === module) {
  try { console.log(JSON.stringify(publish(process.cwd(), path.resolve('artifacts')))); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
module.exports = { verifyAssets, publish };
