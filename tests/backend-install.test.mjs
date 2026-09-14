import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const install = require('../apps/desktop/backend-install.js');

// A stand-in for the AppImage's resources/sidecar directory.
async function build(root, {version='1.0.0', bytes='#!/bin/sh\necho v1\n'}={}) {
  const home = path.join(root, 'home');
  const resources = path.join(root, `res-${version}`);
  await fsp.mkdir(path.join(resources, 'sidecar', '_internal'), {recursive:true});
  await fsp.writeFile(path.join(resources, 'sidecar', install.BINARY), bytes);
  await fsp.writeFile(path.join(resources, 'sidecar', '_internal', 'lib.so'), version);
  await fsp.mkdir(home, {recursive:true});
  return {home, resources};
}

const calls = () => { const seen=[]; return {seen, runner:async(cmd,args)=>{seen.push([cmd,...args].join(' '));}}; };

test('a platform with no shared unit is left to the app it ships with', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(),'bi-'));
  t.after(()=>fsp.rm(root,{recursive:true,force:true}));
  const {home, resources} = await build(root, {version:'1.0.0'});
  const {seen, runner} = calls();
  const result = await install.syncBackend({isPackaged:true, resourcesPath:resources, version:'1.0.0',
                                            home, runner, platform:'win32'});
  assert.equal(result.installed, false);
  assert.deepEqual(seen, [], 'systemctl does not exist there');
});

test('a checkout run installs nothing and leaves the unit alone', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(),'bi-'));
  t.after(()=>fsp.rm(root,{recursive:true,force:true}));
  const {home} = await build(root);
  const {seen, runner} = calls();
  const result = await install.syncBackend({isPackaged:false, resourcesPath:'/nowhere', version:'1.0.0', home, runner, platform:'linux'});
  assert.equal(result.installed, false);
  assert.equal(result.restarted, false);
  assert.deepEqual(seen, [], 'a development run must not restart the shared server');
});

test('the first packaged launch installs the backend and restarts the unit', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(),'bi-'));
  t.after(()=>fsp.rm(root,{recursive:true,force:true}));
  const {home, resources} = await build(root, {version:'1.0.0'});
  const {seen, runner} = calls();
  const result = await install.syncBackend({isPackaged:true, resourcesPath:resources, version:'1.0.0', home, runner, platform:'linux'});

  assert.equal(result.installed, true);
  assert.equal(result.restarted, true);
  const binary = path.join(install.installRoot(home), install.BINARY);
  assert.ok(fs.existsSync(binary), 'the backend is where the unit can run it');
  assert.ok(fs.existsSync(path.join(install.installRoot(home),'_internal','lib.so')), 'its libraries came too');
  assert.equal(fs.statSync(binary).mode & 0o111, 0o111, 'the backend must be executable');
  assert.ok(seen.some(c=>c.includes('restart serena-mobile-host.service')));
});

test('a relaunch on the same version changes nothing', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(),'bi-'));
  t.after(()=>fsp.rm(root,{recursive:true,force:true}));
  const {home, resources} = await build(root, {version:'1.0.0'});
  await install.syncBackend({isPackaged:true, resourcesPath:resources, version:'1.0.0', home, runner:calls().runner, platform:'linux'});

  const {seen, runner} = calls();
  const again = await install.syncBackend({isPackaged:true, resourcesPath:resources, version:'1.0.0', home, runner, platform:'linux'});
  assert.equal(again.installed, false);
  assert.equal(again.restarted, false);
  assert.deepEqual(seen, [], 'an ordinary launch must not bounce the server and every open pane with it');
});

test('a new version replaces the backend and restarts once', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(),'bi-'));
  t.after(()=>fsp.rm(root,{recursive:true,force:true}));
  const {home, resources} = await build(root, {version:'1.0.0', bytes:'old'});
  await install.syncBackend({isPackaged:true, resourcesPath:resources, version:'1.0.0', home, runner:calls().runner, platform:'linux'});
  const next = await build(root, {version:'2.0.0', bytes:'new'});

  const {seen, runner} = calls();
  const result = await install.syncBackend({isPackaged:true, resourcesPath:next.resources, version:'2.0.0', home, runner, platform:'linux'});

  assert.equal(result.installed, true);
  assert.equal(fs.readFileSync(path.join(install.installRoot(home), install.BINARY),'utf8'), 'new');
  assert.equal(install.installedVersion(install.installRoot(home)), '2.0.0');
  assert.equal(seen.filter(c=>c.includes('restart')).length, 1, 'exactly one restart per update');
});

test('the swap leaves no half-copied directory for systemd to execute', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(),'bi-'));
  t.after(()=>fsp.rm(root,{recursive:true,force:true}));
  const {home, resources} = await build(root, {version:'1.0.0'});
  await install.syncBackend({isPackaged:true, resourcesPath:resources, version:'1.0.0', home, runner:calls().runner, platform:'linux'});
  const entries = await fsp.readdir(path.dirname(install.installRoot(home)));
  assert.deepEqual(entries, ['backend'], 'staging and retired copies are cleaned up');
});

test('a version is only stamped once the copy finished', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(),'bi-'));
  t.after(()=>fsp.rm(root,{recursive:true,force:true}));
  const {home} = await build(root);
  // A directory that exists but was never stamped must not be trusted.
  await fsp.mkdir(install.installRoot(home), {recursive:true});
  await fsp.writeFile(path.join(install.installRoot(home), install.BINARY), 'truncated');
  assert.equal(install.installedVersion(install.installRoot(home)), '');
});

test('the unit runs the installed backend, not a checkout', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(),'bi-'));
  t.after(()=>fsp.rm(root,{recursive:true,force:true}));
  const {home, resources} = await build(root, {version:'1.0.0'});
  const {seen, runner} = calls();
  await install.syncBackend({isPackaged:true, resourcesPath:resources, version:'1.0.0', home, runner, platform:'linux'});

  const unit = fs.readFileSync(path.join(home,'.config','systemd','user',install.UNIT),'utf8');
  assert.match(unit, new RegExp(`ExecStart=${install.installRoot(home)}/${install.BINARY} --host 127\\.0\\.0\\.1 --port 8767`));
  assert.doesNotMatch(unit, /\.venv|core\.mobile_host/, 'the unit must not run the git checkout any more');
  assert.match(unit, /Restart=always/, 'the shared server has to come back on its own');
  assert.ok(seen.some(c=>c.includes('daemon-reload')), 'systemd must be told the unit changed');
});

test('an unchanged unit is not rewritten', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(),'bi-'));
  t.after(()=>fsp.rm(root,{recursive:true,force:true}));
  const {home, resources} = await build(root, {version:'1.0.0'});
  await install.syncBackend({isPackaged:true, resourcesPath:resources, version:'1.0.0', home, runner:calls().runner, platform:'linux'});
  const unitPath = path.join(home,'.config','systemd','user',install.UNIT);
  await fsp.appendFile(unitPath, '# a hand edit\n');

  const next = await build(root, {version:'2.0.0'});
  await install.syncBackend({isPackaged:true, resourcesPath:next.resources, version:'2.0.0', home, runner:calls().runner, platform:'linux'});

  assert.match(fs.readFileSync(unitPath,'utf8'), /# a hand edit/,
    'ExecStart already pointed at the install root, so the file was left as the user had it');
});

test('a failed swap keeps the running server instead of breaking it', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(),'bi-'));
  t.after(()=>fsp.rm(root,{recursive:true,force:true}));
  const {home, resources} = await build(root, {version:'1.0.0'});
  const result = await install.syncBackend({
    isPackaged:true, resourcesPath:resources, version:'1.0.0', home,
    runner:async()=>{throw new Error('systemctl is unavailable');}, platform:'linux',
  });
  assert.equal(result.restarted, false);
  assert.match(result.error, /systemctl is unavailable/);
});

test('a build with no sidecar is reported, not guessed at', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(),'bi-'));
  t.after(()=>fsp.rm(root,{recursive:true,force:true}));
  const {home} = await build(root);
  const empty = path.join(root,'empty');
  await fsp.mkdir(empty,{recursive:true});
  const result = await install.syncBackend({isPackaged:true, resourcesPath:empty, version:'1.0.0', home, runner:calls().runner, platform:'linux'});
  assert.equal(result.installed, false);
  assert.equal(result.reason, 'not a packaged build');
});

test('startup installs the backend before it goes looking for one', () => {
  const main = fs.readFileSync(new URL('../apps/desktop/main.js', import.meta.url),'utf8');
  const sync = main.indexOf('backendInstall.syncBackend(');
  const start = main.indexOf('startBackend()', sync);
  assert.ok(sync > 0 && start > sync, 'the server it finds must already be this build');
});
