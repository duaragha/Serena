'use strict';
// Reuse an immutable candidate and its successful Windows gate after a Linux
// validation failure. This never assembles a different application tree.
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');
const { SHA, REQUEST, STABLE, DEV } = require('../apps/desktop/promotion-policy.cjs');
const REPO = 'duaragha/Serena';
function run(binary, args, cwd) {
  return execFileSync(binary, args, { cwd, encoding: 'utf8', timeout: 300000, maxBuffer: 16 * 1024 * 1024 });
}
function validate(runInfo, jobs, plan, inputs) {
  if (!/^[1-9][0-9]{0,15}$/.test(inputs.runId) || !SHA.test(inputs.source) || !REQUEST.test(inputs.request))
    throw Error('Invalid promotion resume request');
  if (runInfo.status !== 'completed' || runInfo.conclusion !== 'failure'
      || runInfo.event !== 'workflow_dispatch' || runInfo.head_branch !== 'master'
      || runInfo.path !== '.github/workflows/selective-promotion.yml'
      || runInfo.repository?.full_name !== REPO || runInfo.head_sha !== plan.source)
    throw Error('Only a failed, reviewed master promotion can be resumed');
  if (!jobs.some(j => j.name === 'windows' && j.status === 'completed' && j.conclusion === 'success')
      || !jobs.some(j => j.name === 'linux' && j.status === 'completed' && j.conclusion === 'failure'))
    throw Error('Resume requires a successful Windows gate and failed Linux gate');
  if (!STABLE.test(plan.version) || !STABLE.test(plan.baseTag) || !SHA.test(plan.source)
      || !DEV.test(plan.fullDev?.tag) || !SHA.test(plan.fullDev?.commit)
      || plan.mode !== 'publish' || inputs.mode !== 'publish' || plan.request !== inputs.request
      || plan.baseTag !== inputs.stable || plan.fullDev?.tag !== inputs.devTag
      || plan.fullDev?.commit !== inputs.devCommit || !SHA.test(plan.commit))
    throw Error('Resume inputs do not authorize this exact candidate');
  assert.deepEqual([...inputs.selected].sort(), plan.features.map(f => f.id).sort());
  for (const id of plan.added) if (!inputs.tested.includes(id)) throw Error('Untested resumed feature');
}
function resume(root, inputs, command = run) {
  const gh = args => command('gh', args, root);
  const git = args => command('git', args, root);
  if (!/^[1-9][0-9]{0,15}$/.test(inputs.runId)) throw Error('Invalid run ID');
  const info = JSON.parse(gh(['api', `repos/${REPO}/actions/runs/${inputs.runId}`]));
  const jobs = JSON.parse(gh(['api', `repos/${REPO}/actions/runs/${inputs.runId}/jobs?filter=latest&per_page=100`])).jobs;
  gh(['run', 'download', inputs.runId, '--repo', REPO, '--name', 'candidate-source', '--dir', root]);
  const plan = JSON.parse(fs.readFileSync(path.join(root, 'promotion-plan.json'), 'utf8'));
  validate(info, jobs, plan, inputs);
  if (git(['rev-parse', 'HEAD']).trim() !== inputs.source) throw Error('Reviewed source changed');
  git(['merge-base', '--is-ancestor', plan.source, inputs.source]);
  if (JSON.parse(gh(['api', `repos/${REPO}/releases/latest`])).tag_name !== plan.baseTag)
    throw Error('Stable changed since this candidate was prepared');
  if (git(['tag', '--list', plan.version]).trim()) throw Error('Candidate version already exists');
  git(['fetch', path.join(root, 'candidate.bundle'), 'candidate']);
  if (git(['rev-parse', 'FETCH_HEAD']).trim() !== plan.commit) throw Error('Resumed bundle commit mismatch');
  const receipt = JSON.parse(git(['show', `${plan.commit}:config/stable-promotion.json`]));
  for (const field of ['schema', 'version', 'baseTag', 'baseCommit', 'source', 'request', 'fullDev', 'features', 'patches'])
    assert.deepEqual(receipt[field], plan[field], `Resumed receipt differs: ${field}`);
  gh(['run', 'download', inputs.runId, '--repo', REPO, '--name', 'candidate-windows', '--dir', path.join(root, 'resumed-windows')]);
  fs.writeFileSync(path.join(root, 'resumed-from.json'), JSON.stringify({run: inputs.runId,
    reviewedSource: inputs.source, candidate: plan.commit, windowsGate: 'success'}, null, 2) + '\n');
  return plan;
}
if (require.main === module) {
  try {
    const e = process.env;
    const plan = resume(process.cwd(), { runId: e.PROMOTION_RESUME_RUN, source: e.PROMOTION_SOURCE,
      request: e.PROMOTION_REQUEST, mode: e.PROMOTION_MODE, stable: e.PROMOTION_STABLE,
      devTag: e.PROMOTION_DEV_TAG, devCommit: e.PROMOTION_DEV_COMMIT,
      selected: JSON.parse(e.PROMOTION_SELECTED || '[]'), tested: JSON.parse(e.PROMOTION_TESTED || '[]') });
    console.log(`Resumed verified Windows candidate ${plan.commit}`);
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
module.exports = { validate, resume };
