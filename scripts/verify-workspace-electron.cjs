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
  const windows=process.platform==='win32';
  if(!windows)assert.ok(xvfbPath, 'An isolated Xvfb executable is required');
  const display = windows?null:spawn(xvfbPath, ['-displayfd','3','-screen','0','1440x900x24','-nolisten','tcp','-ac'],
    {env,stdio:['ignore','ignore','pipe','pipe']});
  let app;
  let clipboardSnapshot;
  try {
    if(display){
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
    }
    app = await electron.launch({executablePath: electronPath,
      args: [appDir, '--dev', ...(!windows?['--ozone-platform=x11']:[]), '--disable-gpu'], env, timeout: 30000});
    if(windows)clipboardSnapshot=await app.evaluate(({clipboard})=>{
      const formats=clipboard.availableFormats();
      const allowed=['text/plain','text/html','text/rtf','image/png'];
      if(formats.some(format=>!allowed.includes(format)))throw Error('Clipboard has unsupported formats; refusing to overwrite it');
      return {text:clipboard.readText(),html:clipboard.readHTML(),rtf:clipboard.readRTF(),
        image:formats.includes('image/png')?clipboard.readImage().toDataURL():null};
    });
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
    await shell.getByRole('textbox', {name: 'Shell command'}).fill('echo SERENA_ELECTRON_NATIVE');
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
    let creations = 0;
    page.on('request', request => {
      if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/workspace/create') creations++;
    });
    await page.getByRole('button', {name:'New chat',exact:true}).click();
    await page.locator('#modalInput').fill('Electron native new chat');
    await page.locator('#modalAgentPicker [data-agent="codex"]').click();
    await page.locator('#modalConfirmBtn').click();
    const creation = page.frameLocator('iframe[src^="/workspace/new?"]');
    await creation.getByRole('button',{name:'Create Codex chat',exact:true}).waitFor();
    assert.equal(creations,0);
    await creation.getByRole('button',{name:'Create Codex chat',exact:true}).click();
    await creation.getByRole('button',{name:'Open conversation',exact:true}).waitFor();
    const newSid = (await creation.getByRole('status').innerText()).replace('Session ','');
    assert.match(newSid,/^[a-f0-9-]{36}$/);
    await creation.getByRole('button',{name:'Open conversation',exact:true}).click();
    const newPane = page.frameLocator(`iframe[src="/workspace/${newSid}"]`);
    await newPane.getByRole('button',{name:'Resume session',exact:true}).click();
    assert.equal(await page.locator('#convTitle').innerText(),'Electron native new chat');
    assert.equal(await page.locator('iframe[src^="/workspace/new?"]').count(),0);
    await newPane.getByRole('button',{name:'Run shell command',exact:true}).click();
    const newShell = newPane.getByRole('dialog',{name:'Run shell command'});
    await newShell.getByRole('textbox',{name:'Shell command'}).fill('echo SERENA_ELECTRON_CREATED');
    await newShell.getByRole('checkbox').check();
    await newShell.getByRole('button',{name:'Run command',exact:true}).click();
    await newPane.locator('summary').filter({hasText:'SERENA_ELECTRON_CREATED'}).first().click();
    await newPane.getByText('SERENA_ELECTRON_CREATED',{exact:true}).waitFor();
    await newPane.locator('.aw-state').filter({hasText:/^(ready|completed)$/}).waitFor();
    await page.waitForFunction(async sid => {
      const rows = await (await fetch('/api/sessions')).json();
      return rows.some(row => row.session_id === sid && !row.native_persistence_pending);
    }, newSid);
    const rows = await page.evaluate(async()=> (await fetch('/api/sessions')).json());
    const matching = rows.filter(row=>row.session_id===newSid);
    assert.equal(matching.length,1);
    assert.equal(matching[0].display_title,'Electron native new chat');
    assert.equal(creations,1);
    await page.screenshot({path:path.join(artifacts,'electron-native-created.png')});
    await page.getByRole('button',{name:'New chat',exact:true}).click();
    await page.locator('#modalInput').fill('Electron native Claude chat');
    await page.locator('#modalAgentPicker [data-agent="claude"]').click();
    await page.locator('#modalConfirmBtn').click();
    const claudeCreation = page.frameLocator('iframe[src^="/workspace/new?"]');
    await claudeCreation.getByRole('button',{name:'Create Claude chat',exact:true}).waitFor();
    assert.equal(creations,1);
    const creationFrame = page.frames().find(frame => frame.url().includes('/workspace/new?'));
    const storageKey = 'serena-workspace-create:' + new URL(creationFrame.url()).searchParams.get('source');
    await creationFrame.evaluate(key => sessionStorage.setItem(key, '{'), storageKey);
    await creationFrame.goto(creationFrame.url());
    await creationFrame.waitForFunction(() => document.querySelector('#creation-submit')?.disabled);
    await claudeCreation.locator('#creation-submit').dispatchEvent('click');
    assert.equal(creations,1);
    assert.equal(await creationFrame.evaluate(key => sessionStorage.getItem(key), storageKey), '{');
    await creationFrame.evaluate(key => sessionStorage.removeItem(key), storageKey);
    await creationFrame.goto(creationFrame.url());
    await claudeCreation.getByRole('button',{name:'Create Claude chat',exact:true}).click();
    await claudeCreation.getByRole('button',{name:'Open conversation',exact:true}).waitFor();
    const claudeSid = (await claudeCreation.getByRole('status').innerText()).replace('Session ','');
    assert.match(claudeSid,/^[a-f0-9-]{36}$/);
    assert.notEqual(claudeSid,newSid);
    await claudeCreation.getByRole('button',{name:'Open conversation',exact:true}).click();
    const claudePane = page.frameLocator(`iframe[src="/workspace/${claudeSid}"]`);
    await claudePane.getByRole('button',{name:'Resume session',exact:true}).click();
    assert.equal(await page.locator('#convTitle').innerText(),'Electron native Claude chat');
    assert.equal(await page.locator('iframe[src^="/workspace/new?"]').count(),0);
    await claudePane.getByRole('textbox',{name:'Message Claude',exact:true}).fill('/effort low');
    await claudePane.getByRole('button',{name:'Send message',exact:true}).click();
    await claudePane.getByText('/effort low',{exact:true}).waitFor();
    await claudePane.locator('.aw-state').filter({hasText:'completed'}).waitFor();
    await claudePane.getByText(/Set effort level to low/).waitFor();
    await page.waitForFunction(async sid => {
      const rows = await (await fetch('/api/sessions')).json();
      return rows.some(row => row.session_id === sid && !row.native_persistence_pending);
    },claudeSid);
    const claudeRows = (await page.evaluate(async()=> (await fetch('/api/sessions')).json())).filter(row=>row.session_id===claudeSid);
    assert.equal(claudeRows.length,1);
    assert.equal(claudeRows[0].display_title,'Electron native Claude chat');
    assert.equal(claudeRows[0].agent,'claude');
    assert.equal(creations,2);
    await page.screenshot({path:path.join(artifacts,'electron-native-claude-created.png')});
    assert.deepEqual(errors, []);
    console.log('PASS: real Electron main/preload, isolated frozen backend, native session input/output and skill catalog; sandbox/context isolation configured, Node integration off');
    console.log('PASS: real Electron clipboard copied native output and pasted multiline text without sending or losing the session');
    console.log('PASS: actual Electron New Chat button preserved its chosen title through exact native creation, iframe handoff and native input');
    console.log('PASS: actual Electron Claude New Chat used the selected provider, retained title through indexing, and rendered native local-command output');
    console.log('PASS: corrupt saved creation record disabled submission without replacing the record or launching another session');
  } catch (error) {
    if (app) {
      const page = await app.firstWindow();
      await page.screenshot({path:path.join(artifacts,'electron-native-failure.png')}).catch(()=>{});
      for (const frame of page.frames()) {
        if(frame.url().includes('/workspace/new?')) console.error('Creation failure state:',await frame.locator('#creation-status').innerText().catch(()=>''));
      }
    }
    throw error;
  } finally {
    try {
      if(clipboardSnapshot)await app.evaluate(({clipboard,nativeImage},snapshot)=>{
        clipboard.clear();
        const {image,...data}=snapshot;
        if(image)data.image=nativeImage.createFromDataURL(image);
        clipboard.write(data);
      },clipboardSnapshot);
    }
    finally {
      try{await app?.close();}finally{if(display?.pid && display.exitCode===null && display.signalCode===null){
        await new Promise(resolve=>{display.once('exit',resolve);display.kill('SIGTERM');});
      }}
    }
  }
}

main().catch(error => {console.error(error);process.exitCode = 1;});
