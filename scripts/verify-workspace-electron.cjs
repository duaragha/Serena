'use strict';
// Real desktop main/preload against the caller's isolated frozen backend.
const assert = require('node:assert/strict');
const path = require('node:path');
const {spawn} = require('node:child_process');

async function main() {
  const [electronPath, playwrightPath, appDir, base, sid, artifacts, xvfbPath] = process.argv.slice(2);
  const {_electron: electron} = require(playwrightPath);
  assert.equal(new URL(base).hostname, '127.0.0.1');
  const env = {...process.env, SERENA_DESKTOP_SHARED_PORT: new URL(base).port,
    SERENA_DESKTOP_SHARE_BACKEND: '1'};
  delete env.ELECTRON_RUN_AS_NODE;
  assert.ok(xvfbPath, 'An isolated Xvfb executable is required');
  const display = spawn(xvfbPath, ['-displayfd','3','-screen','0','1440x900x24','-nolisten','tcp','-ac'],
    {env,stdio:['ignore','ignore','pipe','pipe']});
  let app;
  try {
    const number = await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error('Virtual display did not start')),10000);
      let value='',errors='';
      display.stderr.on('data',chunk=>{errors+=chunk;});
      display.on('error',error=>{clearTimeout(timer);reject(error);});
      display.on('exit',()=>{clearTimeout(timer);reject(Error(errors || 'Virtual display exited'));});
      display.stdio[3].on('data',chunk=>{
        value+=chunk;
        if(/^\d+\n$/.test(value)){clearTimeout(timer);resolve(value.trim());}
      });
    });
    env.DISPLAY=`:${number}`;
    delete env.XAUTHORITY;
    app = await electron.launch({executablePath: electronPath,
      args: [appDir, '--dev', '--ozone-platform=x11', '--disable-gpu'], env, timeout: 30000});
    const page = await app.firstWindow();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.waitForURL(base + '/');
    page.on('console', message => {if(message.type()==='error')errors.push(message.text());});
    page.on('response', response => {if(response.status()>=400 && !response.url().endsWith('favicon.ico'))errors.push(`HTTP ${response.status()}: ${response.url()}`);});
    const version = await page.evaluate(() => window.serenaDesktop.getVersion());
    assert.equal(version, require(path.join(appDir, 'package.json')).version);
    const preferences = await app.evaluate(({BrowserWindow}) => {
      const settings = BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences();
      return {sandbox: settings.sandbox, contextIsolation: settings.contextIsolation, nodeIntegration: settings.nodeIntegration};
    });
    assert.deepEqual(preferences, {sandbox: true, contextIsolation: true, nodeIntegration: false});
    const catalog = await page.evaluate(async () => (await fetch('/api/sessions')).json());
    assert.ok(catalog.some(row => row.session_id === sid), 'Exact native session must be visible in the real sidebar catalog');
    await page.locator(`.session-row[data-sid="${sid}"]`).first().click();
    await page.locator('#viewLiveBtn').click();
    const pane = page.frameLocator(`iframe[src="/workspace/${sid}"]`);
    await pane.getByRole('button', {name: 'Resume session', exact: true}).click();
    await pane.getByRole('button', {name: 'Commands and skills', exact: true}).click();
    const dialog = pane.getByRole('dialog', {name: 'Commands and skills'});
    await dialog.getByRole('searchbox', {name: 'Search commands'}).fill('workspace-setting-proof');
    const skill = dialog.getByRole('checkbox', {name: 'Enable skill workspace-setting-proof'});
    await skill.waitFor();
    assert.equal(await skill.isChecked(), true);
    await dialog.getByRole('button', {name: 'Close commands'}).click();
    await pane.getByRole('button', {name: 'Run shell command', exact: true}).click();
    const shell = pane.getByRole('dialog', {name: 'Run shell command'});
    await shell.getByRole('textbox', {name: 'Shell command'}).fill('printf SERENA_ELECTRON_NATIVE');
    await shell.getByRole('checkbox').check();
    await shell.getByRole('button', {name: 'Run command', exact: true}).click();
    await pane.locator('summary').filter({hasText: 'SERENA_ELECTRON_NATIVE'}).first().click();
    await pane.getByText('SERENA_ELECTRON_NATIVE', {exact: true}).waitFor();
    await pane.locator('.aw-state').filter({hasText:/^(ready|completed)$/}).waitFor();
    const input = pane.getByRole('textbox', {name: 'Message Codex', exact: true});
    await app.evaluate(({clipboard}) => clipboard.writeText('first line\nsecond line'));
    await input.focus();
    await input.press('Control+V');
    assert.equal(await input.inputValue(), 'first line\nsecond line');
    await input.fill('');
    await pane.getByText('SERENA_ELECTRON_NATIVE', {exact: true}).click({clickCount:3});
    await page.keyboard.press('Control+C');
    const copied = await app.evaluate(({clipboard}) => clipboard.readText());
    assert.equal(copied.trim(), 'SERENA_ELECTRON_NATIVE');
    await input.focus();
    await input.press('Control+V');
    assert.equal((await input.inputValue()).trim(), 'SERENA_ELECTRON_NATIVE');
    assert.match(await pane.locator('.aw-state').innerText(), /^(ready|completed)$/);
    assert.equal(page.url(), base + '/');
    await page.screenshot({path: path.join(artifacts, 'electron-native-workspace.png')});
    assert.deepEqual(errors, []);
    console.log('PASS: real Electron main/preload, isolated frozen backend, native session input/output and skill catalog; sandbox/context isolation configured, Node integration off');
    console.log('PASS: real virtual-display clipboard copied native output and pasted multiline text without sending or losing the session');
  } finally {
    try {await app?.close();}
    finally {
      if(display.pid && display.exitCode===null && display.signalCode===null){
        await new Promise(resolve=>{display.once('exit',resolve);display.kill('SIGTERM');});
      }
    }
  }
}

main().catch(error => {console.error(error);process.exitCode = 1;});
