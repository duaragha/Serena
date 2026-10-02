'use strict';

const { execFile } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const policy = require('./promotion-policy.cjs');
const REPO = 'duaragha/Serena';
const WORKFLOW = 'selective-promotion.yml';
const WORKFLOW_URL = `https://github.com/${REPO}/actions/workflows/${WORKFLOW}`;

function github(args) {
  return new Promise((resolve, reject) => {
    execFile('gh', args, { timeout: 20000, maxBuffer: 2 * 1024 * 1024, windowsHide: true,
      env: { ...process.env, GH_HOST: 'github.com', GH_PROMPT_DISABLED: '1', GH_PAGER: 'cat' } }, (error, stdout, stderr) => {
      if (error) {
        const message = error.code === 'ENOENT' ? 'GitHub CLI is unavailable. Open GitHub to manage releases.'
          : `GitHub request failed. Check your connection and GitHub CLI sign-in. ${String(stderr || '').slice(0, 300)}`;
        reject(new Error(message));
      } else resolve(stdout);
    });
  });
}

class PromotionService {
  constructor({ directory, version, run = github, confirm = async () => false }) {
    this.file = path.join(directory, 'promotion-request.json');
    this.version = version;
    this.run = run;
    this.confirm = confirm;
    this.snapshot = null;
    this.busy = false;
  }
  async api(endpoint) { return JSON.parse(await this.run(['api', `repos/${REPO}/${endpoint}`])); }
  async content(file, ref) {
    const result = await this.api(`contents/${file}?ref=${ref}`);
    if (result.encoding !== 'base64' || typeof result.content !== 'string') throw new Error('Invalid GitHub file response');
    return JSON.parse(Buffer.from(result.content, 'base64').toString('utf8'));
  }
  async receipt(tag) {
    try { return await this.content('config/stable-promotion.json', tag); }
    catch (error) {
      // A 404 here is not a connection or sign-in problem: main was published
      // some other way and nobody has reviewed what it contains yet.
      if (/HTTP 404|Not Found/.test(error.message)) {
        throw new Error(`Main ${tag} was published outside Promote to Main and has no reviewed baseline. `
          + 'Add it to adoptedStable in config/promotion-features.json before promoting.');
      }
      throw error;
    }
  }
  async pending() {
    try { return JSON.parse(await fs.readFile(this.file, 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') return null; throw new Error('Saved release request is unreadable'); }
  }
  async save(value) {
    await fs.mkdir(path.dirname(this.file), { recursive: true });
    await fs.writeFile(`${this.file}.tmp`, JSON.stringify(value), { mode: 0o600 });
    await fs.rename(`${this.file}.tmp`, this.file);
  }
  async status() {
    const pending = await this.pending();
    if (!pending) return null;
    const runs = JSON.parse(await this.run(['run', 'list', '--repo', REPO, '--workflow', WORKFLOW,
      '--limit', '50', '--json', 'databaseId,displayTitle,status,conclusion,url']));
    const run = runs.find(r => r.displayTitle === `Promotion ${pending.mode} ${pending.request}`);
    return { ...pending, run: run || null };
  }
  async load() {
    // Pin the catalog and receipt used in the confirmation to immutable commits.
    const source = (await this.api('commits/master')).sha;
    if (!policy.SHA.test(source)) throw new Error('Invalid catalog revision');
    const release = await this.api('releases/latest');
    if (!policy.STABLE.test(release.tag_name)) throw new Error('Invalid main release');
    const catalog = await this.content('config/promotion-features.json', source);
    const adopted = policy.adoptedBaseline(catalog, release.tag_name);
    const receipt = release.tag_name === catalog.initialStable || adopted ? null
      : await this.receipt(release.tag_name);
    const tagCommit = adopted ? (await this.api(`commits/${release.tag_name}`)).sha : null;
    const installed = policy.installedFeatures(catalog, release.tag_name, receipt, tagCommit);
    const status = await this.status();
    this.snapshot = { source, stable: release.tag_name, catalog, installed, version: this.version };
    return { ...this.snapshot, status };
  }
  async submit(value) {
    if (this.busy) throw new Error('A release request is already being submitted');
    this.busy = true;
    try {
      if (!value || !['verify', 'publish'].includes(value.mode) || !this.snapshot
          || value.source !== this.snapshot.source || value.stable !== this.snapshot.stable)
        throw new Error('Refresh and review the release selection');
      const snapshot = this.snapshot;
      const plan = policy.selection(snapshot.catalog, value.selected, value.tested, snapshot.installed);
      const status = await this.status();
      if (status && status.run?.status !== 'completed') throw new Error('A previous request is pending. Check its GitHub status before submitting another.');
      if (!await this.confirm({ mode: value.mode, stable: snapshot.stable, titles: plan.added.map(f => f.title) })) return { cancelled: true };
      // Recheck after the native confirmation; no stale approval can publish over newer main.
      if ((await this.api('commits/master')).sha !== snapshot.source
          || (await this.api('releases/latest')).tag_name !== snapshot.stable) throw new Error('Releases changed. Refresh and review again.');
      const request = randomBytes(16).toString('hex');
      const pending = { request, mode: value.mode, source: snapshot.source, stable: snapshot.stable,
        selected: plan.added.map(f => f.id), startedAt: new Date().toISOString() };
      await this.save(pending);
      // If the network drops after acceptance, retain this ID instead of blindly sending twice.
      await this.run(['workflow', 'run', WORKFLOW, '--repo', REPO, '--ref', 'master',
        '-f', `mode=${value.mode}`, '-f', `source=${snapshot.source}`, '-f', `stable=${snapshot.stable}`,
        '-f', `selected=${JSON.stringify(value.selected)}`, '-f', `tested=${JSON.stringify(value.tested)}`, '-f', `request=${request}`]);
      return { ...pending, run: null };
    } finally { this.busy = false; }
  }
  async dismiss() {
    if (this.busy) throw new Error('Wait for the current request');
    this.busy = true;
    try {
      const pending = await this.pending();
      if (!pending) return false;
      if (!await this.confirm({ mode: 'dismiss', titles: [] })) return false;
      await fs.unlink(this.file);
      return true;
    } finally { this.busy = false; }
  }
}
module.exports = { PromotionService, WORKFLOW_URL };
