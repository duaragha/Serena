'use strict';
// Real desktop main/preload against the caller's isolated frozen backend.
const assert = require('node:assert/strict');
const path = require('node:path');
const {spawn} = require('node:child_process');

async function waitForOwnedPane(frame) {
  // A hidden-locator check alone also succeeds before the iframe has loaded.
  await frame.locator('.aw-state').filter({hasText:/^(ready|running|completed|failed|interrupted)$/}).waitFor();
  await frame.locator('#workspace-connect').waitFor({state:'hidden'});
}

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
    async function selectAgents(wanted) {
      for(const agent of wanted){
        const button=page.locator(`#modalAgentPicker [data-agent="${agent}"]`);
        if(await button.getAttribute('aria-pressed')!=='true')await button.click();
      }
      for(const agent of ['claude','codex','gemini']){
        const button=page.locator(`#modalAgentPicker [data-agent="${agent}"]`);
        if(!wanted.includes(agent) && await button.getAttribute('aria-pressed')==='true')await button.click();
      }
      assert.deepEqual(await page.locator('#modalAgentPicker [aria-pressed="true"]').evaluateAll(buttons=>buttons.map(button=>button.dataset.agent)),wanted);
    }
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
    await waitForOwnedPane(pane);
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
    let attentionPending=true,attentionClears=0;
    const attentionRoute=route=>route.fulfill({json:{sessions:attentionPending?{[sid]:Date.now()/1000}:{}}});
    const cleared=request=>{
      if(request.url().endsWith('/api/chat-attention/clear')){attentionPending=false;attentionClears++;}
    };
    await page.route('**/api/chat-attention',attentionRoute);
    page.on('request',cleared);
    await page.evaluate(id=>{_attentionSids.add(id);renderSessionList();setConvMode('read');},sid);
    const boot=await pane.locator('#workspace-boot').textContent();
    const background=await page.evaluate(async ({sid,token})=>{
      const response=await fetch(`/api/workspace/${sid}/commands`,{method:'POST',
        headers:{'Content-Type':'application/json','X-Serena-Workspace-Token':token},
        body:JSON.stringify({request_id:crypto.randomUUID(),action:'shell_command',
          payload:{command:'echo SERENA_BACKGROUND_ATTENTION',confirmed:true}})});
      return response.json();
    },{sid,token:JSON.parse(boot).token});
    assert.ok(background.ok,JSON.stringify(background));
    await pane.locator('summary').filter({hasText:'SERENA_BACKGROUND_ATTENTION'}).first().waitFor({state:'attached'});
    await page.waitForFunction(id=>termSessions.get(id)?.state==='completed',sid);
    assert.equal(attentionClears,0);
    assert.equal(await page.evaluate(id=>_attentionSids.has(id),sid),true);
    const acknowledgement=page.waitForRequest(request=>request.url().endsWith('/api/chat-attention/clear'));
    await page.locator(`.session-row[data-sid="${sid}"]`).first().click();
    await acknowledgement;
    await page.waitForFunction(id=>!_attentionSids.has(id),sid);
    assert.equal(attentionClears,1);
    await page.unroute('**/api/chat-attention',attentionRoute);
    page.off('request',cleared);
    await page.locator('#viewLiveBtn').click();
    await waitForOwnedPane(pane);
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
    await pane.getByRole('button',{name:'Codex account',exact:true}).click();
    const account=pane.getByRole('dialog',{name:'Codex account',exact:true});
    await account.getByRole('button',{name:'Sign in with ChatGPT',exact:true}).click();
    const authorize=account.getByRole('link',{name:'Continue browser sign-in',exact:true});
    await authorize.waitFor();
    assert.match(await authorize.getAttribute('href'),/^https:\/\/(auth\.openai\.com|auth0\.openai\.com|chatgpt\.com)\//);
    await account.getByRole('button',{name:'Close account',exact:true}).click();
    await pane.getByRole('button',{name:'Codex account',exact:true}).click();
    await account.getByRole('button',{name:'Cancel browser sign-in',exact:true}).click();
    await account.getByText('Sign-in: cancelled',{exact:true}).waitFor();
    await account.getByRole('button',{name:'Close account',exact:true}).click();
    assert.equal((await input.inputValue()).trim(),'SERENA_ELECTRON_NATIVE');
    assert.equal(page.url(), base + '/');
    await page.screenshot({path: path.join(artifacts, 'electron-native-workspace.png')});
    let creations = 0;
    page.on('request', request => {
      if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/workspace/create') creations++;
    });
    await page.getByRole('button', {name:'New chat',exact:true}).click();
    await page.locator('#modalInput').fill('Electron native new chat');
    await selectAgents(['codex']);
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
    await waitForOwnedPane(newPane);
    assert.equal(await page.locator('#convTitle').innerText(),'Electron native new chat');
    await page.waitForFunction(()=>document.querySelectorAll('iframe[src^="/workspace/new?"]').length===0);
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
    await selectAgents(['claude']);
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
    await waitForOwnedPane(claudePane);
    assert.equal(await page.locator('#convTitle').innerText(),'Electron native Claude chat');
    await page.waitForFunction(()=>document.querySelectorAll('iframe[src^="/workspace/new?"]').length===0);
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
    await page.getByRole('button',{name:'New chat',exact:true}).click();
    await page.locator('#modalInput').fill('Electron linked native pair');
    await selectAgents(['claude','codex']);
    await page.locator('#modalConfirmBtn').click();
    assert.equal(creations,2);
    const linkedIds=[];
    for(const provider of ['claude','codex']){
      const label=provider==='claude'?'Claude':'Codex';
      const pending=page.frameLocator(`iframe[src^="/workspace/new?"][src*="provider=${provider}"]`);
      await pending.getByRole('button',{name:`Create ${label} chat`,exact:true}).click();
      await pending.getByRole('button',{name:'Open conversation',exact:true}).waitFor();
      const target=(await pending.getByRole('status').innerText()).replace('Session ','');
      assert.match(target,/^[a-f0-9-]{36}$/);linkedIds.push(target);
      await pending.getByRole('button',{name:'Open conversation',exact:true}).click();
      await page.locator(`iframe[src="/workspace/${target}"]`).waitFor();
    }
    await page.waitForFunction(async ids=>{
      const rows=await(await fetch('/api/sessions')).json();
      const members=ids.map(id=>rows.find(row=>row.session_id===id));
      return members.every(Boolean) && members[0].group && members[0].group===members[1].group;
    },linkedIds);
    const linkedRows=await page.evaluate(async ids=>(await(await fetch('/api/sessions')).json()).filter(row=>ids.includes(row.session_id)),linkedIds);
    assert.equal(linkedRows.length,2);
    assert.ok(linkedRows.every(row=>row.display_title==='Electron linked native pair'));
    assert.equal(creations,4);
    await page.waitForFunction(()=>document.querySelectorAll('iframe[src^="/workspace/new?"]').length===0);
    await page.screenshot({path:path.join(artifacts,'electron-native-linked-created.png')});
    assert.ok(linkedRows.every(row=>row.workspace_runtime?.ok && row.workspace_runtime.session_id===row.session_id));
    const hiddenOwners=await page.evaluate(async ids=>{
      for(const id of ids)teardownLiveTerminal(id);
      await loadSessions(currentProject);
      return ids.map(id=>({id,mounted:termSessions.has(id),localActive:_activeTerms.has(id),
        listedActive:_sessionHasActiveRuntime(_findClientSession(id))}));
    },linkedIds);
    assert.ok(hiddenOwners.every(row=>!row.mounted && !row.localActive && row.listedActive),JSON.stringify(hiddenOwners));
    assert.ok(await page.locator('.session-row.active-terminal').filter({hasText:'Electron linked native pair'}).count());
    await page.evaluate(id=>openConv(id),linkedIds[0]);
    await waitForOwnedPane(page.frameLocator(`iframe[src="/workspace/${linkedIds[0]}"]`));
    assert.equal(creations,4);
    assert.deepEqual(errors, []);
    console.log(`PASS: real Electron main/preload, isolated ${process.env.SERENA_PROOF_BACKEND_MODE || 'frozen'} backend, native session input/output and skill catalog; sandbox/context isolation configured, Node integration off`);
    console.log('PASS: real native background output preserved a controlled attention flag; explicit row focus acknowledged it once');
    console.log('PASS: real Electron clipboard copied native output and pasted multiline text without sending or losing the session');
    console.log('PASS: native browser-login start, reopen and cancellation preserved the draft; no browser opened or credentials replaced');
    console.log('PASS: actual Electron New Chat button preserved its chosen title through exact native creation, iframe handoff and native input');
    console.log('PASS: actual Electron Claude New Chat used the selected provider, retained title through indexing, and rendered native local-command output');
    console.log('PASS: corrupt saved creation record disabled submission without replacing the record or launching another session');
    console.log('PASS: multi-agent picker created exact native Claude/Codex identities with one retained title and persisted group before any model input');
    console.log('PASS: closing both linked views kept their real owners in Active; reopening observed the same session without creating another');
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
