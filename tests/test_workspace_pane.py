"""Browser contract for the real rich-event pane, without starting agents."""

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

playwright = pytest.importorskip("playwright.sync_api")
STATIC = Path(__file__).resolve().parents[1] / "ui" / "static"


@pytest.mark.parametrize('width', [390, 1400])
def test_session_actions_keep_headers_aligned_and_support_keyboard(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      const Pane=pane.constructor;right.dispose();
      const all={...controls,listSessions:async()=>[],openSession:()=>{},accountStatus:async()=>({}),
        hooks:async()=>[],apps:async()=>({data:[]}),renameSession:async()=>{},projectDiff:async()=>({}),
        events:async()=>[],forkSession:async()=>{},openFork:()=>{},disconnectSession:async()=>{},shellCommand:async()=>{}};
      window.right=new Pane(document.querySelector('#right'),{sessionId:'other',provider:'Codex',model:'A very long model name that must not resize the header',controls:all});
    }""")
    headers = page.locator('.aw-head').evaluate_all('els=>els.map(el=>el.getBoundingClientRect().height)')
    assert headers[0] == 48
    if width > 600:
        assert headers == [48, 48]
    assert page.locator('body').evaluate('el=>el.scrollWidth<=innerWidth')
    left = page.locator('#left')
    trigger = left.get_by_role('button', name='Session actions', exact=True)
    assert left.get_by_role('button', name='Prompt color', exact=True).is_hidden()
    assert page.evaluate('calls') == []
    trigger.focus()
    page.keyboard.press('ArrowDown')
    playwright.expect(left.get_by_role('button', name='Prompt color', exact=True)).to_be_focused()
    page.keyboard.press('Escape')
    playwright.expect(trigger).to_be_focused()
    trigger.click()
    left.get_by_role('button', name='Prompt color', exact=True).click()
    dialog = page.get_by_role('dialog', name='Prompt color')
    playwright.expect(dialog).to_be_visible()
    assert not page.locator('.aw-session-actions:popover-open').count()
    page.keyboard.press('Escape')
    trigger.click()
    left.locator('textarea').click()
    assert not page.locator('.aw-session-actions:popover-open').count()
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'actions-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    scope = page.locator('#right' if width > 600 else '#left')
    scope.get_by_role('button', name='Session actions', exact=True).focus()
    page.keyboard.press('ArrowDown')
    menu = scope.get_by_role('group', name='Session actions', exact=True)
    assert menu.evaluate('el=>el.scrollWidth<=el.clientWidth')
    page.keyboard.press('End')
    assert menu.evaluate('el=>[...el.querySelectorAll("button")].filter(b=>!b.hidden&&!b.disabled).at(-1)===document.activeElement')
    page.keyboard.press('Home')
    assert menu.evaluate('el=>[...el.querySelectorAll("button")].filter(b=>!b.hidden&&!b.disabled)[0]===document.activeElement')
    page.screenshot(path=str(shot.with_name(f'actions-menu-{width}.png')))
    page.keyboard.press('Escape')
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
@pytest.mark.parametrize('state', ['unavailable', 'reconciling'])
def test_codex_readonly_commands_work_without_enabling_message_submission(pane, width, state):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""state=>{
      controls.accountTokenUsage=async()=>{calls.push('usage');throw Error('Session unavailable');};
      const Pane=pane.constructor;pane.dispose();window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
      pane.conversation.status=state;pane.render();
    }""", state)
    composer = page.locator('#left textarea')
    send = page.locator('#left').get_by_role('button', name='Send message', exact=True)
    assert send.is_disabled()
    composer.fill('/status')
    composer.press('Enter')
    dialog = page.get_by_role('dialog', name='Session status', exact=True)
    dialog.wait_for()
    assert page.evaluate('calls') == []
    page.keyboard.press('Escape')
    assert composer.input_value() == '/status'
    composer.fill('/usage')
    composer.press('Enter')
    dialog = page.get_by_role('dialog', name='Account token usage', exact=True)
    dialog.get_by_text('Token usage unavailable: Session unavailable', exact=True).wait_for()
    assert page.evaluate('calls') == ['usage']
    page.keyboard.press('Escape')
    assert composer.input_value() == '/usage' and send.is_disabled()
    composer.fill('do not submit while unavailable')
    composer.press('Enter')
    assert page.evaluate('calls') == ['usage']
    assert composer.input_value() == 'do not submit while unavailable'
    assert page.evaluate('pane.conversation.status') == state
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
@pytest.mark.parametrize('command', ['exit', 'quit'])
def test_codex_exit_requires_confirmation_and_preserves_failed_draft(pane, width, command):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.disconnectSession=async()=>{calls.push('disconnect');throw Error('Session still has background work');};
      const Pane=pane.constructor;pane.dispose();window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});pane.render();
    }""")
    composer = page.locator('#left textarea')
    composer.fill('/' + command)
    composer.press('Enter')
    dialog = page.get_by_role('dialog', name='Disconnect session', exact=True)
    dialog.wait_for()
    assert page.evaluate('calls') == []
    dialog.get_by_role('button', name='Cancel', exact=True).click()
    assert composer.input_value() == '/' + command
    composer.press('Enter')
    dialog.get_by_role('button', name='Disconnect', exact=True).click()
    dialog.get_by_text('Session still has background work', exact=True).wait_for()
    assert page.evaluate('calls') == ['disconnect']
    assert composer.input_value() == '/' + command
    page.evaluate("()=>{controls.disconnectSession=async()=>{calls.push('confirmed');};}")
    dialog.get_by_role('button', name='Disconnect', exact=True).click()
    dialog.wait_for(state='hidden')
    assert page.evaluate('calls') == ['disconnect', 'confirmed']
    composer.fill('/' + command + ' unexpected')
    composer.press('Enter')
    assert page.evaluate('calls') == ['disconnect', 'confirmed']
    assert dialog.count() == 0
    page.evaluate("()=>{pane.conversation.status='running';pane.render();}")
    composer.fill('/' + command)
    composer.press('Enter')
    assert dialog.count() == 0
    assert page.evaluate('calls') == ['disconnect', 'confirmed']
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_session_usage_estimates_keep_scope_precision_and_missing_values(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.accountTokenUsage=async scope=>{calls.push(scope);return scope==='session' ? {threadUsage:{threadId:'exact',
        estimatedUsageCreditsMicros:'1234567',estimatedUsageUsdMicros:null,groups:[{model:'Native model',reasoningEffort:'high',speed:null,
        estimatedUsageCreditsMicros:'1',inputTokens:'0',totalTokens:'9223372036854775807'}]},observedAt:'2026-09-10T12:00:00Z'} :
        {summary:{},dailyUsageBuckets:null,observedAt:'2026-09-10T12:00:00Z'};};
      const Pane=pane.constructor;pane.dispose();window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});pane.input.value='/usage';pane.render();
    }""")
    page.locator('#left textarea').press('Enter')
    dialog = page.get_by_role('dialog', name='Account token usage', exact=True)
    dialog.get_by_role('radio', name='This session', exact=True).check()
    dialog.get_by_role('heading', name='Native model').wait_for()
    assert '1.234567' in dialog.inner_text() and '0.000001' in dialog.inner_text()
    assert '9,223,372,036,854,775,807' in dialog.inner_text()
    assert 'Estimated USD' in dialog.inner_text() and 'Unavailable' in dialog.inner_text()
    assert page.evaluate('calls') == ['account', 'session']
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'session-usage-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    page.evaluate("()=>{controls.accountTokenUsage=async()=>({threadUsage:{threadId:'foreign'}});}")
    dialog.get_by_role('button', name='Refresh token usage').click()
    dialog.get_by_text('Token usage unavailable: Usage belongs to a different session', exact=True).wait_for()
    assert dialog.get_by_role('heading', name='Native model').count() == 0
    page.evaluate("()=>{controls.accountTokenUsage=async()=>({threadUsage:null});}")
    dialog.get_by_role('button', name='Refresh token usage').click()
    dialog.get_by_text('Session usage estimate unavailable', exact=True).wait_for()
    page.keyboard.press('Escape')
    page.wait_for_function('!pane.accountUsageDialog.open')
    assert page.evaluate('pane.input.value') == '/usage'
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_native_token_usage_has_explicit_refresh_and_preserves_draft(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.accountTokenUsage=async()=>{calls.push('usage');throw Error('Not authenticated');};
      const Pane=pane.constructor;pane.dispose();window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});pane.input.value='/usage';pane.render();
    }""")
    assert page.evaluate('calls') == []
    page.locator('#left textarea').press('Enter')
    dialog = page.get_by_role('dialog', name='Account token usage', exact=True)
    dialog.get_by_text('Token usage unavailable: Not authenticated', exact=True).wait_for()
    assert page.evaluate('pane.input.value') == '/usage'
    page.evaluate("""()=>{controls.accountTokenUsage=async()=>{calls.push('usage');return {
      summary:{lifetimeTokens:'9223372036854775807',peakDailyTokens:null,currentStreakDays:'0'},observedAt:'2026-09-10T12:00:00Z',
      dailyUsageBuckets:Array.from({length:32},(_,i)=>({startDate:new Date(Date.UTC(2026,8,10-i)).toISOString().slice(0,10),tokens:'0'}))};};}""")
    dialog.get_by_role('button', name='Refresh token usage').click()
    table = dialog.get_by_role('table', name='Daily token activity')
    table.wait_for()
    assert table.locator('tbody tr').count() == 31
    assert '9,223,372,036,854,775,807' in dialog.inner_text()
    assert 'Unavailable' in dialog.inner_text()
    dialog.get_by_role('button', name='Load more daily activity').click()
    assert table.locator('tbody tr').count() == 32
    assert page.evaluate('calls') == ['usage', 'usage']
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    dialog.evaluate('el=>el.scrollTop=0')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'account-usage-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    page.evaluate("()=>{controls.accountTokenUsage=async()=>({summary:{},dailyUsageBuckets:null,observedAt:'2026-09-10T12:00:00Z'});}")
    dialog.get_by_role('button', name='Refresh token usage').click()
    dialog.get_by_text('Daily activity unavailable', exact=True).wait_for()
    assert table.count() == 0
    page.evaluate("()=>{controls.accountTokenUsage=async()=>({summary:{},dailyUsageBuckets:[],observedAt:'2026-09-10T12:00:00Z'});}")
    dialog.get_by_role('button', name='Refresh token usage').click()
    dialog.get_by_text('No daily activity returned', exact=True).wait_for()
    page.keyboard.press('Escape')
    page.wait_for_function('!pane.accountUsageDialog.open')
    assert page.evaluate('pane.input.value') == '/usage'
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_saved_setting_recovery_requires_confirmation_without_reconnect(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.resetSavedSetting=async id=>{calls.push(id);throw Error('Saved setting changed');};
      const Pane=pane.constructor;pane.dispose();window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
      pane.input.value='keep my draft';
      const error=Error('Unsupported personality');error.settingRecovery={setting:'personality',failure_id:'exact-failure'};pane.error(error);
    }""")
    page.get_by_role('button', name='Recover saved setting', exact=True).click()
    dialog = page.get_by_role('dialog', name='Recover saved setting')
    reset = dialog.get_by_role('button', name='Reset saved preference', exact=True)
    assert reset.is_disabled() and page.evaluate('calls') == []
    dialog.get_by_role('checkbox', name='Confirm saved preference reset').check()
    reset.click()
    dialog.get_by_text('Saved setting changed', exact=True).wait_for()
    assert page.evaluate('pane.input.value') == 'keep my draft'
    page.evaluate("()=>{controls.resetSavedSetting=async id=>{calls.push(id);return {setting:'personality',reset:true,reconnectRequired:true};};}")
    reset.click()
    dialog.get_by_text('Saved preference cleared. Retry connection to resume this chat.', exact=True).wait_for()
    assert reset.is_disabled()
    assert page.evaluate('calls') == ['exact-failure', 'exact-failure']
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'setting-recovery-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    page.keyboard.press('Escape')
    page.wait_for_function('!pane.settingRecoveryDialog.open')
    assert page.evaluate('pane.input.value') == 'keep my draft'
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_session_speed_changes_without_sending_and_keeps_failed_selection(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.speedTiers=async()=>({model:'current',currentValue:null,options:[{id:'priority',name:'Fast'}]});
      controls.setSpeedTier=async(...args)=>{calls.push(args);throw Error('Speed unavailable');};
      const Pane=pane.constructor;pane.dispose();window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});pane.input.value='/fast';pane.render();
    }""")
    page.locator('#left textarea').press('Enter')
    dialog = page.get_by_role('dialog', name='Session speed', exact=True)
    select = dialog.get_by_role('combobox', name='Session speed tier')
    select.select_option('priority')
    assert page.evaluate('calls') == []
    dialog.get_by_role('button', name='Apply', exact=True).click()
    dialog.get_by_text('Speed unavailable', exact=True).wait_for()
    assert select.input_value() == 'priority'
    assert page.evaluate('pane.input.value') == '/fast'
    page.evaluate("()=>{controls.setSpeedTier=async(value,model)=>{calls.push([value,model]);return {model,currentValue:value,options:[{id:'priority',name:'Fast'}]};};}")
    dialog.get_by_role('button', name='Apply', exact=True).click()
    dialog.get_by_text('current / priority', exact=True).wait_for()
    select.select_option('')
    dialog.get_by_role('button', name='Apply', exact=True).click()
    dialog.get_by_text('current / Default', exact=True).wait_for()
    assert page.evaluate('calls') == [['priority', 'current'], ['priority', 'current'], [None, 'current']]
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'session-speed-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_native_rename_requires_confirmation_and_preserves_failed_draft(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.renameSession=async name=>{calls.push(['rename',name]);throw Error('Native rename unavailable');};
      const Pane=pane.constructor;pane.dispose();window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});pane.input.value='/rename New title';pane.render();
    }""")
    assert page.evaluate('calls') == []
    page.get_by_role('button', name='Send message', exact=True).first.click()
    dialog=page.get_by_role('dialog', name='Rename conversation', exact=True)
    assert dialog.get_by_role('textbox', name='Conversation title').input_value() == 'New title'
    assert page.evaluate('calls') == []
    dialog.get_by_role('button', name='Rename', exact=True).click()
    dialog.get_by_text('Native rename unavailable', exact=True).wait_for()
    assert page.evaluate('pane.input.value') == '/rename New title'
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'rename-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    page.evaluate("()=>{controls.renameSession=async name=>{calls.push(['rename',name]);return {name};};}")
    dialog.get_by_role('button', name='Rename', exact=True).click()
    dialog.wait_for(state='hidden')
    assert page.evaluate('pane.input.value') == ''
    assert page.evaluate('calls') == [['rename', 'New title'], ['rename', 'New title']]
    assert not errors


@pytest.mark.parametrize('available', [False, True])
def test_codex_new_chat_keeps_running_owner_and_draft(pane, available):
    page, errors = pane
    page.evaluate("""available=>{
      if(available)controls.newConversation=title=>calls.push(['new',title]);
      const Pane=pane.constructor;pane.dispose();
      window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'turn/started',params:{turn:{id:'running',status:'inProgress'}}});
      pane.input.value='/new Next work';pane.render();
    }""", available)
    page.locator('#left textarea').press('Enter')
    if available:
        page.wait_for_function('calls.length===1')
        assert page.evaluate('calls') == [['new','Next work']]
    else:
        page.get_by_text('New conversation is unavailable outside the app', exact=True).wait_for()
        assert page.evaluate('calls') == []
    assert page.evaluate('pane.input.value') == '/new Next work'
    assert page.evaluate('pane.conversation.status') == 'running'
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
@pytest.mark.parametrize('supported', [False, True])
def test_personality_is_explicit_native_selection_and_keeps_failed_draft(pane, width, supported):
    page, errors = pane
    page.set_viewport_size({'width':width,'height':900})
    page.evaluate("""supported=>{
      controls.personality=async()=>({currentValue:null,options:supported?['none','friendly','pragmatic']:[]});
      controls.setPersonality=async value=>{calls.push(value);throw Error('Native update failed');};
      const Pane=pane.constructor;pane.dispose();
      window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});pane.input.value='/personality';pane.render();
    }""", supported)
    assert page.evaluate('calls') == []
    page.locator('#left textarea').press('Enter')
    dialog=page.get_by_role('dialog',name='Codex personality',exact=True)
    select=dialog.get_by_role('combobox',name='Personality',exact=True)
    if not supported:
        dialog.get_by_text('This model does not support personality',exact=True).wait_for()
        assert select.is_disabled()
        assert dialog.get_by_role('button',name='Apply',exact=True).is_disabled()
        assert page.evaluate('calls') == []
        assert page.evaluate('pane.input.value') == '/personality'
        assert not errors
        return
    select.select_option('friendly')
    assert page.evaluate('calls') == []
    dialog.get_by_role('button',name='Apply',exact=True).click()
    dialog.get_by_text('Native update failed',exact=True).wait_for()
    assert page.evaluate('calls') == ['friendly']
    assert page.evaluate('pane.input.value') == '/personality'
    assert select.is_enabled()
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot=STATIC.parents[1]/'apps/desktop/build/workspace-proof'/f'personality-{width}.png'
    shot.parent.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(shot))
    page.evaluate("()=>{controls.setPersonality=async value=>({currentValue:value,options:['none','friendly','pragmatic']});}")
    dialog.get_by_role('button',name='Apply',exact=True).click()
    dialog.get_by_text('Last confirmed: friendly',exact=True).wait_for()
    dialog.get_by_role('button',name='Close personality',exact=True).click()
    assert page.evaluate('pane.input.value') == '/personality'
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_goal_requires_confirmation_preserves_draft_and_recovers_failure(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      window.goal={threadId:'exact',objective:'Existing objective',status:'active',tokenBudget:1000,tokensUsed:10,timeUsedSeconds:3};
      controls.goal=async()=>({goal});
      controls.updateGoal=async(changes,expected)=>{calls.push(['update',changes,expected]);throw Error('Goal changed; refresh before applying changes');};
      controls.clearGoal=async expected=>{calls.push(['clear',expected]);return {goal:null};};
      const Pane=pane.constructor;pane.dispose();
      window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});pane.input.value='/goal';pane.render();
    }""")
    assert page.evaluate('calls') == []
    page.locator('#left textarea').press('Enter')
    dialog = page.get_by_role('dialog', name='Session goal', exact=True)
    mode = dialog.get_by_role('combobox', name='Goal status')
    playwright.expect(mode).to_have_value('active')
    mode.select_option('paused')
    apply = dialog.get_by_role('button', name='Apply', exact=True)
    assert apply.is_disabled()
    assert page.evaluate('calls') == []
    dialog.get_by_role('checkbox').check()
    apply.click()
    dialog.get_by_text('Goal changed; refresh before applying changes', exact=True).wait_for()
    assert page.evaluate('calls[0][1]') == {'status': 'paused'}
    assert mode.input_value() == 'paused'
    assert page.evaluate('pane.input.value') == '/goal'
    assert apply.is_disabled()
    dialog.get_by_role('button', name='Refresh goal', exact=True).click()
    playwright.expect(mode).to_have_value('active')
    page.evaluate("()=>{controls.updateGoal=async changes=>({goal:{...goal,...changes}});}")
    mode.select_option('paused')
    dialog.get_by_role('checkbox').check()
    apply.click()
    dialog.get_by_text('paused / 10 tokens / 3s', exact=True).wait_for()
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'goal-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    clear = dialog.get_by_role('button', name='Clear goal', exact=True)
    assert clear.is_disabled()
    dialog.get_by_role('checkbox').check()
    clear.click()
    dialog.get_by_text('No goal', exact=True).wait_for()
    assert page.evaluate('calls.at(-1)[1].status') == 'paused'
    dialog.get_by_role('button', name='Close goal', exact=True).click()
    assert page.evaluate('pane.input.value') == '/goal'
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_agent_switcher_inspects_without_launching_and_keeps_parent_draft(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.agents=async cursor=>{calls.push(['list',cursor]);return {data:[{id:'child-exact',agentNickname:'Research <img onerror=alert(1)>',status:{type:'active'}}],nextCursor:null};};
      controls.inspectAgent=async(id,cursor)=>{calls.push(['inspect',id,cursor]);return {thread:{id,status:{type:'active'},turns:[{id:cursor?'older':'latest',status:'completed',items:[{id:cursor?'old-answer':'answer',type:'agentMessage',text:cursor?'Earlier answer':'Actual child answer'},{id:'input',type:'userMessage',content:[{type:'text',text:'Delegated input'},{type:'image',url:'https://example.invalid/private.png'}]}]}]},historyCursor:cursor?null:'older-cursor'};};
      const Pane=pane.constructor;pane.dispose();
      window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'turn/started',params:{turn:{id:'parent-running',status:'inProgress'}}});pane.input.value='/agent';pane.render();
    }""")
    assert page.evaluate('calls') == []
    page.locator('#left textarea').press('Enter')
    dialog = page.get_by_role('dialog', name='Delegated agents', exact=True)
    row = dialog.get_by_role('button', name='Research <img onerror=alert(1)> child-exact active', exact=True)
    row.click()
    dialog.get_by_text('Actual child answer', exact=True).wait_for()
    assert dialog.get_by_text('Agent input', exact=True).count() == 1
    dialog.get_by_text('Attached image unavailable', exact=True).wait_for()
    assert dialog.locator('img').count() == 0
    assert page.evaluate('calls') == [['list', None], ['inspect', 'child-exact', None]]
    dialog.get_by_role('button', name='Earlier agent turns', exact=True).click()
    dialog.get_by_text('Earlier answer', exact=True).wait_for()
    assert dialog.get_by_text('Actual child answer', exact=True).count() == 1
    assert dialog.get_by_role('button', name='Earlier agent turns', exact=True).is_hidden()
    page.evaluate("()=>emit({method:'workspace/agentEvent',params:{threadId:'exact',agentThreadId:'child-exact',activeAgentCount:1,event:{method:'turn/started',params:{threadId:'child-exact',turn:{id:'child-live'}}}}})")
    dialog.get_by_text('Agent changed; refresh snapshot', exact=True).wait_for()
    assert page.evaluate('pane.conversation.turns.has("child-live")') is False
    assert page.evaluate('calls.length') == 3
    page.evaluate("()=>{controls.inspectAgent=async()=>{throw Error('Agent read unavailable');};}")
    dialog.get_by_role('button', name='Refresh selected agent', exact=True).click()
    dialog.get_by_text('Agent read unavailable', exact=True).wait_for()
    assert dialog.get_by_text('Actual child answer', exact=True).count() == 1
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'agents-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    dialog.get_by_role('button', name='Close agents', exact=True).click()
    assert page.evaluate('pane.input.value') == '/agent'
    assert page.evaluate('pane.conversation.status') == 'running'
    page.evaluate("()=>emit({id:77,method:'item/commandExecution/requestApproval',params:{threadId:'exact',agentThreadId:'child-exact',command:'echo child'}})")
    page.get_by_text('Agent: child-exact', exact=True).wait_for()
    assert page.evaluate('calls.length') == 3
    page.get_by_role('button', name='Decline', exact=True).click()
    page.wait_for_function('calls.length===4')
    assert page.evaluate('calls.at(-1)') == ['answer',77,{'decision':'decline'}]
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_agent_image_preview_zoom_refresh_and_close_release_urls(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      window.revoked=[];const revoke=URL.revokeObjectURL.bind(URL);
      URL.revokeObjectURL=url=>{revoked.push(url);revoke(url);};
      controls.image=async token=>{
        calls.push(['image',token]);
        const canvas=document.createElement('canvas');canvas.width=8;canvas.height=8;
        const ctx=canvas.getContext('2d');ctx.fillStyle='#00ff00';ctx.fillRect(0,0,8,8);
        return await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
      };
      controls.agents=async()=>({data:[{id:'child',name:'Research',status:{type:'idle'}}],nextCursor:null});
      controls.inspectAgent=async id=>({thread:{id,turns:[{id:'turn',status:'completed',items:[
        {type:'userMessage',id:'image',content:[{type:'localImage',previewToken:'managed',name:'photo.png'}]}
      ]}]},historyCursor:null});
      const Pane=pane.constructor;pane.dispose();
      window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
      pane.input.value='parent draft';pane.openAgents();
    }""")
    dialog = page.get_by_role('dialog', name='Delegated agents', exact=True)
    dialog.get_by_role('button', name='Research child idle', exact=True).click()
    page.wait_for_function("document.querySelector('.aw-agent-history img')?.naturalWidth===8")
    assert page.evaluate("""()=>{const c=document.createElement('canvas');c.width=c.height=8;
      const x=c.getContext('2d');x.drawImage(document.querySelector('.aw-agent-history img'),0,0);
      return Array.from(x.getImageData(0,0,1,1).data);}""") == [0, 255, 0, 255]
    dialog.get_by_role('button', name='Open image: photo.png', exact=True).click()
    viewer = page.get_by_role('dialog', name='Image viewer', exact=True)
    viewer.get_by_role('button', name='Actual size', exact=True).click()
    viewer.get_by_role('button', name='Close image', exact=True).click()
    dialog.get_by_role('button', name='Refresh selected agent', exact=True).click()
    page.wait_for_function("revoked.length===1 && document.querySelector('.aw-agent-history img')?.naturalWidth===8")
    assert page.evaluate('pane.historyImageUrls.size') == 1
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'agent-image-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    dialog.get_by_role('button', name='Close agents', exact=True).click()
    page.wait_for_function('revoked.length===2 && pane.historyImageUrls.size===0')
    assert page.evaluate('pane.input.value') == 'parent draft'
    assert page.evaluate('calls') == [['image', 'managed'], ['image', 'managed']]
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_agent_files_reopen_retry_and_do_not_reach_parent(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.agents=async()=>({data:[{id:'child',name:'Research',status:{type:'active'}}],nextCursor:null});
      controls.inspectAgent=async id=>({thread:{id,turns:[{id:'turn',status:'inProgress',items:[]}]},historyCursor:null});
      controls.steerAgent=async(id,turn,text,files)=>{calls.push([id,turn,text,files.map(f=>f.name)]);throw Error('Send failed');};
      const Pane=pane.constructor;pane.dispose();
      window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
      pane.input.value='parent draft';pane.openAgents();
    }""")
    dialog = page.get_by_role('dialog', name='Delegated agents', exact=True)
    dialog.get_by_role('button', name='Research child active', exact=True).click()
    dialog.locator('input[type=file]').set_input_files({'name': 'notes.txt', 'mimeType': 'text/plain', 'buffer': b'notes'})
    dialog.get_by_role('textbox', name='Message active agent').fill('Read this')
    dialog.get_by_role('button', name='Send to active agent', exact=True).click()
    dialog.get_by_text('Send failed', exact=True).wait_for()
    dialog.get_by_role('button', name='Close agents', exact=True).click()
    page.evaluate('()=>pane.openAgents()')
    dialog.get_by_role('button', name='Research child active', exact=True).click()
    dialog.get_by_role('button', name='Remove agent attachment: notes.txt', exact=True).wait_for()
    assert dialog.get_by_role('textbox', name='Message active agent').input_value() == 'Read this'
    assert page.evaluate('pane.files.length') == 0
    page.evaluate("()=>{controls.steerAgent=async(id,turn,text,files)=>{calls.push([id,turn,text,files.map(f=>f.name)]);return {accepted:true,threadId:id,turnId:turn};};}")
    dialog.get_by_role('button', name='Send to active agent', exact=True).click()
    dialog.get_by_text('Message accepted by active agent', exact=True).wait_for()
    assert dialog.get_by_role('button', name='Remove agent attachment: notes.txt', exact=True).count() == 0
    assert page.evaluate('calls') == [['child', 'turn', 'Read this', ['notes.txt']]] * 2
    assert page.evaluate('pane.input.value') == 'parent draft'
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_agent_drop_paste_and_pending_receipt_recovery(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.agents=async()=>({data:[{id:'child',name:'Research',status:{type:'active'}}],nextCursor:null});
      controls.inspectAgent=async id=>({thread:{id,turns:[{id:'turn',status:'inProgress',items:[]}]},historyCursor:null});
      controls.steerAgent=async()=>{throw Error('Not expected');};
      window.pendingAgent=[{requestId:'original',payload:{thread_id:'child',expected_turn_id:'old-turn',inputs:[{type:'upload',token:'managed'}]}}];
      controls.pendingAgentMessages=()=>pendingAgent;
      controls.retryAgentMessage=async id=>{calls.push(['retry',id]);pendingAgent=[];return {accepted:true,threadId:'child',turnId:'old-turn'};};
      const Pane=pane.constructor;pane.dispose();
      window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
      pane.input.value='parent draft';pane.openAgents();
    }""")
    dialog = page.get_by_role('dialog', name='Delegated agents', exact=True)
    dialog.get_by_role('button', name='Research child active', exact=True).click()
    message = dialog.get_by_role('textbox', name='Message active agent')
    message.fill('new unsent draft')
    page.evaluate("""()=>{
      const data=new DataTransfer();data.items.add(new File(['photo'],'very-long-'.repeat(15)+'.png',{type:'image/png'}));
      document.querySelector('.aw-agent-composer').dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:data}));
      const pasted=new DataTransfer();pasted.items.add(new File(['paste'],'clipboard.png',{type:'image/png'}));
      document.querySelector('.aw-agent-composer textarea').dispatchEvent(new ClipboardEvent('paste',{bubbles:true,clipboardData:pasted}));
    }""")
    assert dialog.get_by_role('button', name='Send to active agent', exact=True).is_disabled()
    assert page.evaluate('pane.files.length') == 0
    assert page.evaluate('pane.agentFiles.get("child").length') == 2
    assert dialog.get_by_role('button', name='Retry unconfirmed agent message', exact=True).locator('svg').count() == 1
    assert dialog.get_by_role('button', name='Remove agent attachment: clipboard.png', exact=True).locator('svg').count() == 1
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'agent-files-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    dialog.get_by_role('button', name='Retry unconfirmed agent message', exact=True).click()
    dialog.get_by_text('Previous agent message accepted; current draft retained', exact=True).wait_for()
    assert message.input_value() == 'new unsent draft'
    assert page.evaluate('pane.agentFiles.get("child").length') == 2
    assert page.evaluate('calls') == [['retry', 'original']]
    assert dialog.get_by_role('button', name='Send to active agent', exact=True).is_enabled()
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_idle_agent_continuation_is_explicit_and_preserves_failed_draft(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.agents=async()=>({data:[{id:'child',name:'Review',status:{type:'idle'}}],nextCursor:null});
      controls.inspectAgent=async id=>({thread:{id,status:{type:'idle'},turns:[{id:'latest',status:'completed',items:[]}]},historyCursor:null});
      controls.continueAgent=async(...args)=>{calls.push(args);throw Error('Agent changed; refresh');};
      controls.steerAgent=async()=>{throw Error('Must not steer idle child');};
      const Pane=pane.constructor;pane.dispose();
      window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});
      pane.input.value='parent draft';pane.openAgents();
    }""")
    dialog = page.get_by_role('dialog', name='Delegated agents', exact=True)
    dialog.get_by_role('button', name='Review child idle', exact=True).click()
    message = dialog.get_by_role('textbox', name='Message idle agent', exact=True)
    message.fill('Continue the review')
    button = dialog.get_by_role('button', name='Continue idle agent', exact=True)
    assert button.is_disabled()
    assert dialog.get_by_role('button', name='Attach agent files', exact=True).is_disabled()
    assert page.evaluate('calls') == []
    dialog.get_by_role('checkbox', name='Continue this idle agent').check()
    button.click()
    dialog.get_by_text('Agent changed; refresh', exact=True).wait_for()
    assert message.input_value() == 'Continue the review'
    assert button.is_disabled()
    page.evaluate("()=>{controls.continueAgent=async(...args)=>{calls.push(args);return {accepted:true,threadId:'child',turnId:'next'};};}")
    dialog.get_by_role('checkbox', name='Continue this idle agent').check()
    button.click()
    dialog.get_by_text('Agent continuation accepted; refresh snapshot', exact=True).wait_for()
    assert message.input_value() == ''
    message.fill('another message')
    dialog.get_by_role('checkbox', name='Continue this idle agent').check()
    assert button.is_disabled()
    assert page.evaluate('calls') == [['child', 'latest', 'Continue the review']] * 2
    assert page.evaluate('pane.input.value') == 'parent draft'
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'agent-continue-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_agent_stop_requires_exact_confirmation_and_waits_for_native_completion(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.agents=async()=>({data:[{id:'child',agentNickname:'Research',status:{type:'active'}}],nextCursor:null});
      window.childTurn='child-turn';
      controls.inspectAgent=async id=>({thread:{id,status:{type:'active'},turns:[{id:childTurn,status:'inProgress',items:[]}]},historyCursor:null});
      controls.interruptAgent=async(id,turn)=>{calls.push(['stop',id,turn]);throw Error('Stop not confirmed');};
      const Pane=pane.constructor;pane.dispose();
      window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'turn/started',params:{turn:{id:'parent-running',status:'inProgress'}}});pane.input.value='/agent';pane.render();
    }""")
    page.locator('#left textarea').press('Enter')
    dialog = page.get_by_role('dialog', name='Delegated agents', exact=True)
    dialog.get_by_role('button', name='Research child active', exact=True).click()
    stop = dialog.get_by_role('button', name='Stop agent turn', exact=True)
    stop.wait_for()
    assert stop.is_disabled()
    assert page.evaluate('calls') == []
    dialog.get_by_role('checkbox').check()
    stop.click()
    dialog.get_by_text('Stop not confirmed', exact=True).wait_for()
    assert stop.is_disabled()
    assert page.evaluate('calls') == [['stop', 'child', 'child-turn']]
    page.evaluate("()=>{controls.interruptAgent=async(threadId,turnId)=>{calls.push(['stop',threadId,turnId]);return {requested:true,threadId,turnId};};}")
    dialog.get_by_role('checkbox').check()
    stop.click()
    dialog.get_by_text('Stop requested; refresh agent status', exact=True).wait_for()
    dialog.get_by_text('Turn child-turn / inProgress', exact=True).wait_for()
    assert page.evaluate('pane.conversation.status') == 'running'
    dialog.get_by_role('button', name='Refresh selected agent', exact=True).click()
    dialog.get_by_text('Snapshot / active', exact=True).wait_for()
    dialog.get_by_role('checkbox').check()
    assert stop.is_disabled()
    page.evaluate("()=>{window.childTurn='new-child-turn';}")
    dialog.get_by_role('button', name='Refresh selected agent', exact=True).click()
    dialog.get_by_text('Turn new-child-turn / inProgress', exact=True).wait_for()
    assert stop.is_disabled()
    assert not dialog.get_by_role('checkbox').is_checked()
    assert len(page.evaluate('calls')) == 2
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'agent-stop-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    dialog.get_by_role('button', name='Close agents', exact=True).click()
    assert page.evaluate('pane.input.value') == '/agent'
    assert len(page.evaluate('calls')) == 2
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_active_agent_message_keeps_failed_draft_and_never_starts_idle_turn(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.agents=async()=>({data:[{id:'child',agentNickname:'Research',status:{type:'active'}}],nextCursor:null});
      window.childStatus='inProgress';
      controls.inspectAgent=async id=>({thread:{id,status:{type:'active'},turns:[{id:'child-turn',status:childStatus,items:[]}]},historyCursor:null});
      controls.steerAgent=async(id,turn,text)=>{calls.push(['steer',id,turn,text]);throw Error('Delivery unconfirmed');};
      const Pane=pane.constructor;pane.dispose();
      window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'turn/started',params:{turn:{id:'parent-running',status:'inProgress'}}});pane.input.value='/agent';pane.render();
    }""")
    page.locator('#left textarea').press('Enter')
    dialog = page.get_by_role('dialog', name='Delegated agents', exact=True)
    dialog.get_by_role('button', name='Research child active', exact=True).click()
    message = dialog.get_by_role('textbox', name='Message active agent', exact=True)
    message.fill('Focus on tests\nKeep the scope')
    assert page.evaluate('calls') == []
    dialog.get_by_role('button', name='Send to active agent', exact=True).click()
    dialog.get_by_text('Delivery unconfirmed', exact=True).wait_for()
    assert message.input_value() == 'Focus on tests\nKeep the scope'
    assert page.evaluate('pane.input.value') == '/agent'
    dialog.get_by_role('button', name='Close agents', exact=True).click()
    playwright.expect(dialog).to_have_count(0)
    page.locator('#left').get_by_role('textbox', name='Message Codex', exact=True).press('Enter')
    dialog.get_by_role('button', name='Research child active', exact=True).click()
    playwright.expect(message).to_have_value('Focus on tests\nKeep the scope')
    assert len(page.evaluate('calls')) == 1
    page.evaluate("()=>{controls.steerAgent=async(threadId,turnId,text)=>{calls.push(['steer',threadId,turnId,text]);return {accepted:true,threadId,turnId};};}")
    dialog.get_by_role('button', name='Send to active agent', exact=True).click()
    dialog.get_by_text('Message accepted by active agent', exact=True).wait_for()
    assert message.input_value() == ''
    assert page.evaluate('calls') == [['steer', 'child', 'child-turn', 'Focus on tests\nKeep the scope']] * 2
    message.fill('Next instruction')
    page.evaluate("()=>{window.childStatus='completed';}")
    dialog.get_by_role('button', name='Refresh selected agent', exact=True).click()
    dialog.get_by_text('Turn child-turn / completed', exact=True).wait_for()
    assert message.is_disabled()
    assert dialog.get_by_role('button', name='Send to active agent', exact=True).is_disabled()
    assert message.input_value() == 'Next instruction'
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'agent-message-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    dialog.get_by_role('button', name='Close agents', exact=True).click()
    assert page.evaluate('pane.conversation.status') == 'running'
    assert len(page.evaluate('calls')) == 2
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_app_picker_selects_exact_ids_preserves_failed_draft_and_never_auto_loads(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.apps=async()=>{calls.push('apps');return {data:[
        {id:'demo',name:'Demo App',description:'<b>literal metadata</b>',accessible:true,enabled:true,callable:true},
        {id:'disabled',name:'Disabled App',description:'Not callable',accessible:true,enabled:false,callable:false}]};};
      controls.submit=async value=>{window.sent=value;throw Error('Send failed');};
      const Pane=pane.constructor;pane.dispose();window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});pane.input.value='/apps';pane.render();
    }""")
    assert page.evaluate('calls') == []
    page.get_by_role('button', name='Send message', exact=True).first.click()
    dialog = page.get_by_role('dialog', name='Apps and connectors', exact=True)
    dialog.get_by_text('<b>literal metadata</b>', exact=True).wait_for()
    assert dialog.locator('b').count() == 0
    assert dialog.get_by_role('button', name='Select app Disabled App').is_disabled()
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'apps-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    dialog.get_by_role('button', name='Select app Demo App', exact=True).click()
    assert page.evaluate('pane.selectedApps') == [{'id': 'demo', 'name': 'Demo App'}]
    assert page.evaluate('pane.input.value') == ''
    assert page.evaluate("JSON.parse(sessionStorage.getItem(pane.draftKey+':apps'))") == [{'id': 'demo', 'name': 'Demo App'}]
    page.locator('#left textarea').fill('Read selected app')
    page.get_by_role('button', name='Send message', exact=True).first.click()
    page.get_by_text('Send failed', exact=True).wait_for()
    assert page.evaluate('sent.options.apps') == ['demo']
    assert page.evaluate('pane.input.value') == 'Read selected app'
    assert page.evaluate('pane.selectedApps.length') == 1
    page.evaluate("()=>{controls.apps=async()=>{throw Error('Apps unavailable');};}")
    if not page.get_by_role('button', name="Apps and connectors", exact=True).first.is_visible():
        page.get_by_role('button', name='Session actions', exact=True).first.click()
    page.get_by_role('button', name='Apps and connectors', exact=True).click()
    dialog.get_by_text('Apps unavailable', exact=True).wait_for()
    assert dialog.get_by_role('button', name='Select app Demo App').count() == 0
    dialog.get_by_role('button', name='Close apps').click()
    page.get_by_role('button', name='Remove app Demo App', exact=True).click()
    assert page.evaluate('pane.selectedApps') == []
    assert page.evaluate('calls') == ['apps']
    page.evaluate("""()=>{
      emit({method:'turn/started',params:{turn:{id:'app-turn',status:'inProgress'}}});
      emit({method:'item/started',params:{turnId:'app-turn',item:{id:'app-message',type:'userMessage',content:[{type:'mention',name:'<b>Demo App</b>',path:'app://demo'}]}}});
    }""")
    mention=page.locator('#left .aw-user-message [title="app://demo"]')
    playwright.expect(mention).to_have_text('<b>Demo App</b>')
    assert mention.locator('b').count() == 0
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_project_diff_is_explicit_read_only_and_text_safe(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.projectDiff=async()=>{calls.push('diff');return {staged:'+staged',unstaged:'-old\\n+new',untracked:'+<b>new file</b>',omitted:['outside-link']};};
      const Pane=pane.constructor;pane.dispose();window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});pane.input.value='/diff';pane.render();
    }""")
    assert page.evaluate('calls') == []
    page.get_by_role('button', name='Send message', exact=True).first.click()
    dialog = page.get_by_role('dialog', name='Project diff', exact=True)
    dialog.get_by_text('+<b>new file</b>', exact=True).wait_for()
    assert dialog.locator('b').count() == 0
    assert 'outside-link' in dialog.locator('[role=status]').inner_text()
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'diff-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    page.evaluate("()=>{controls.projectDiff=async()=>{throw Error('Git unavailable');};}")
    dialog.get_by_role('button', name='Refresh project diff').click()
    dialog.get_by_text('Git unavailable', exact=True).wait_for()
    dialog.get_by_role('button', name='Close project diff').click()
    assert page.evaluate('pane.input.value') == '/diff'
    assert page.evaluate('calls') == ['diff']
    assert not errors


@pytest.mark.parametrize('command', ['/plugins', '/delete', '/debug-config', '/prompts:custom', '/unknown arg'])
def test_unknown_codex_command_stays_in_draft_without_model_call(pane, command):
    page, errors = pane
    page.evaluate("command=>{pane.provider='Codex';pane.input.value=command;pane.render();}", command)
    page.get_by_role('button',name='Send message',exact=True).first.click()
    assert 'nothing was sent' in page.locator('#left [role=alert]').inner_text()
    assert page.evaluate('calls') == []
    assert page.evaluate('pane.input.value') == command
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_hook_inspector_reads_only_and_preserves_draft(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.hooks=async()=>{calls.push('hooks');return {data:[{eventName:'preToolUse',enabled:false,isManaged:false,trustStatus:'modified',command:'<b>do not execute</b>',source:'project',sourcePath:'/project/hooks.json'}],warnings:['Needs review'],errors:[]};};
      const Pane=pane.constructor;pane.dispose();
      window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});
      pane.input.value='/hooks';pane.render();
    }""")
    assert page.evaluate('calls') == []
    page.get_by_role('button',name='Send message',exact=True).first.click()
    dialog=page.get_by_role('dialog',name='Lifecycle hooks')
    dialog.get_by_text('Disabled · modified',exact=True).wait_for()
    assert dialog.get_by_text('Disabled · modified',exact=True).evaluate('el=>el.getBoundingClientRect().height<50')
    assert dialog.locator('pre').inner_text() == '<b>do not execute</b>'
    assert dialog.locator('pre b').count() == 0
    assert page.evaluate('calls') == ['hooks']
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'hooks-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    page.evaluate("()=>{controls.hooks=async()=>{throw Error('Hook catalog unavailable');};}")
    dialog.get_by_role('button',name='Refresh hooks').click()
    dialog.get_by_text('Hook catalog unavailable',exact=True).wait_for()
    dialog.get_by_role('button',name='Close hooks').click()
    assert page.evaluate('pane.input.value') == '/hooks'
    assert not errors


@pytest.mark.parametrize("action", ["button", "slash", "shortcut"])
def test_copy_completed_output_ignores_running_turn_and_preserves_draft(pane, action):
    page, errors = pane
    page.evaluate("""()=>{
      pane.provider='Codex';pane.copyOutputButton.hidden=false;
      Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copied=text;}}});
      emit({method:'turn/started',params:{turn:{id:'running',status:'inProgress'}}});
      emit({method:'item/agentMessage/delta',params:{turnId:'running',itemId:'partial',delta:'DO NOT COPY PARTIAL'}});
      pane.input.value='Keep this draft';
    }""")
    if action == 'button':
        if not page.get_by_role('button', name="Copy latest completed output", exact=True).first.is_visible():
            page.get_by_role('button', name='Session actions', exact=True).first.click()
        page.get_by_role('button', name='Copy latest completed output', exact=True).first.click()
    elif action == 'slash':
        page.evaluate("pane.input.value='/copy';pane.submit()")
    else:
        page.locator('#left textarea').press('Control+o')
    page.wait_for_function("window.copied!==undefined")
    assert page.evaluate('window.copied') == 'The change is ready for review. <img src=x onerror=alert(1)>'
    assert page.evaluate('pane.input.value') == ('/copy' if action == 'slash' else 'Keep this draft')
    assert page.evaluate('window.calls') == []
    page.evaluate("""()=>{navigator.clipboard.writeText=async()=>{throw Error('Clipboard denied');};pane.copyLatestOutput();}""")
    page.get_by_text('Clipboard denied', exact=True).wait_for()
    page.evaluate("""()=>{window.copied=null;pane.conversation.turns.clear();pane.copyLatestOutput();}""")
    assert page.evaluate('window.copied') is None
    assert 'No completed output' in page.locator('#left [role=alert]').inner_text()
    assert not errors


def test_copy_after_revert_waits_for_new_completed_output(pane):
    page, errors = pane
    page.evaluate("""()=>{
      pane.provider='Codex';
      Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copied=text;}}});
      window.copied=null;pane.input.value='Keep draft';
      emit({method:'thread/reverted',params:{threadId:'exact'}});
      emit({method:'workspace/history',params:{historyRevision:1,copyUnavailableAfterRevert:true,thread:{id:'exact',turns:[
        {id:'retained',status:'completed',items:[{id:'old',type:'agentMessage',text:'Old retained answer'}]}]}}});
    }""")
    page.locator('#left textarea').press('Control+o')
    assert page.evaluate('copied') is None
    copy_button = page.locator('#left').get_by_role('button', name='Copy latest completed output', exact=True, include_hidden=True)
    playwright.expect(copy_button).to_be_disabled()
    assert 'Copy is unavailable after a history revert' in page.locator('#left [role=alert]').inner_text()
    page.evaluate("""()=>emit({method:'turn/completed',params:{turn:{id:'fresh',status:'completed',items:[
      {id:'new',type:'agentMessage',text:'New completed answer'}]}}})""")
    page.locator('#left textarea').press('Control+o')
    page.wait_for_function("copied==='New completed answer'")
    playwright.expect(copy_button).to_be_enabled()
    assert page.evaluate('pane.input.value') == 'Keep draft'
    assert page.evaluate('calls') == []
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_rewind_confirmation_routes_exact_turn_and_preserves_draft(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      controls.revertHistory=async payload=>{calls.push(payload);throw Error('History changed; reopen the rewind dialog');};
      const Pane=pane.constructor;pane.dispose();
      window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[{id:'selected',status:'completed',items:[
        {id:'user',type:'userMessage',content:[{type:'text',text:'A previous request'}]}]}]}}});
      pane.input.value='Keep my draft';
    }""")
    left=page.locator('#left')
    left.get_by_role('button',name='Session actions',exact=True).click()
    left.get_by_role('button',name='Rewind conversation',exact=True).click()
    dialog=page.get_by_role('dialog',name='Rewind conversation',exact=True)
    assert page.evaluate('calls') == []
    dialog.get_by_role('button',name='Rewind',exact=True).click()
    assert page.evaluate('calls') == []
    dialog.get_by_role('checkbox').check()
    dialog.get_by_role('button',name='Rewind',exact=True).click()
    dialog.get_by_text('History changed; reopen the rewind dialog',exact=True).wait_for()
    assert page.evaluate('calls') == [{'before_turn_id':'selected','expected_latest_turn_id':'selected','confirmed':True}]
    assert page.evaluate('pane.input.value') == 'Keep my draft'
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot=STATIC.parents[1]/'apps/desktop/build/workspace-proof'/f'rewind-{width}.png'
    shot.parent.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(shot))
    dialog.get_by_role('button',name='Cancel rewind',exact=True).click()
    assert len(page.evaluate('calls')) == 1
    page.evaluate("()=>{controls.revertHistory=async payload=>{calls.push(payload);return {files_changed:false};};}")
    left.get_by_role('button',name='Session actions',exact=True).click()
    left.get_by_role('button',name='Rewind conversation',exact=True).click()
    dialog.get_by_role('checkbox').check()
    dialog.get_by_role('button',name='Rewind',exact=True).click()
    dialog.wait_for(state='hidden')
    assert page.evaluate('pane.input.value') == 'Keep my draft'
    assert not errors


@pytest.fixture(scope="module")
def pane_browser():
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel=os.environ.get("SERENA_PROOF_BROWSER_CHANNEL"))
        yield browser
        browser.close()


@pytest.fixture
def pane(pane_browser):
    with pane_browser.new_context(viewport={"width": 1600, "height": 1000}) as context:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(r):
            path = urlparse(r.request.url).path
            if path == "/":
                return r.fulfill(
                    content_type="text/html",
                    body=r"""
<!doctype html><html><head><link rel="stylesheet" href="/workspace-pane.css">
<style>body{margin:0;background:#000}.panes{display:grid;grid-template-columns:1fr 1fr;height:100vh}#left{border-right:1px solid #302730}section{min-width:0}@media(max-width:600px){.panes{grid-template-columns:1fr}#right{display:none}}</style>
</head><body><main class="panes"><section id="left"></section><section id="right"></section></main>
<script src="/vendor/lucide.min.js"></script><script type="module">
import {WorkspacePane} from '/workspace-pane.mjs';
window.calls=[];window.seq=0;
window.controls={submit:async v=>{window.calls.push(['submit',{text:v.text,files:v.files.map(f=>f.name)}]);if(window.failSubmit)throw Error('Submission unconfirmed');},interrupt:async()=>window.calls.push(['interrupt']),answer:async(id,a)=>window.calls.push(['answer',id,a])};
window.pane=new WorkspacePane(document.querySelector('#left'),{sessionId:'exact',provider:'Claude',model:'Configured model',controls});
window.right=new WorkspacePane(document.querySelector('#right'),{sessionId:'other',provider:'Codex',controls});
window.emit=event=>pane.receive({sequence:++window.seq,event});
emit({method:'workspace/history',params:{thread:{id:'exact',turns:[{id:'t',status:'completed',items:[
{id:'u',type:'userMessage',content:[{type:'text',text:'Review the change and show the result.'}]},
{id:'a',type:'agentMessage',text:'The change is ready for review. <img src=x onerror=alert(1)>'},
{id:'cmd',type:'commandExecution',command:'pytest tests/test_contract.py -q',status:'completed',exitCode:0,aggregatedOutput:'3 passed'},
{id:'diff',type:'fileChange',status:'completed',changes:[{path:'core/example.py',diff:'-old_value\n+new_value'}]}
]}]}}});
</script></body></html>""",
                )
            asset = STATIC / path.lstrip("/")
            if asset.is_file():
                return r.fulfill(
                    path=str(asset),
                    content_type="text/javascript"
                    if asset.suffix in {".js", ".mjs"}
                    else "text/css",
                )
            return r.fulfill(status=404)

        page.route("**/*", route)
        page.goto("http://workspace-pane.test/")
        assert not errors
        page.wait_for_function("window.pane && pane.conversation.sequence === 1", timeout=5000)
        page.locator(".aw-message").wait_for()
        yield page, errors


@pytest.mark.parametrize("width", [390, 1600])
def test_account_status_is_explicit_honest_and_preserves_draft(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      controls.accountStatus=async()=>{calls.push('account');return {account:{type:'chatgpt',email:'person@example.test',planType:'pro'},credentialsVerified:false};};
      pane.accountButton.hidden=false;pane.input.value='keep my draft';
    }""")
    assert page.evaluate('calls') == []
    if not page.locator('#left').get_by_role('button', name="Codex account", exact=True).first.is_visible():
        page.locator('#left').get_by_role('button', name='Session actions', exact=True).first.click()
    page.locator('#left').get_by_role('button', name='Codex account', exact=True).click()
    dialog = page.get_by_role('dialog', name='Codex account')
    assert dialog.get_by_role('status').inner_text() == 'ChatGPT account saved'
    assert 'Credential validity has not been verified.' in dialog.inner_text()
    assert 'person@example.test' in dialog.inner_text()
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    assert page.evaluate('calls') == ['account']
    page.keyboard.press('Escape')
    assert page.evaluate('pane.input.value') == 'keep my draft'
    assert page.evaluate('document.activeElement===pane.input')
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_account_connection_check_is_explicit_and_recovers_from_expired_login(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""()=>{
      controls.accountStatus=async()=>({account:{type:'chatgpt',email:'person@example.test'},login:null});
      controls.accountRateLimits=async()=>{calls.push('limits');throw Error('Refresh token already used. Sign in again.');};
      controls.accountLogin=async()=>{calls.push('login');return {status:'pending'};};
      const Pane=pane.constructor;pane.dispose();window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});pane.input.value='keep my draft';pane.render();
    }""")
    page.locator('#left').get_by_role('button', name='Session actions', exact=True).click()
    page.locator('#left').get_by_role('button', name='Codex account', exact=True).click()
    dialog = page.get_by_role('dialog', name='Codex account')
    check = dialog.get_by_role('button', name='Check account connection', exact=True)
    assert page.evaluate('calls') == []
    check.click()
    dialog.get_by_text('Account connection failed: Refresh token already used. Sign in again.', exact=True).wait_for()
    assert page.evaluate('pane.input.value') == 'keep my draft'
    page.evaluate("""()=>{controls.accountRateLimits=()=>{calls.push('limits');return new Promise(resolve=>window.resolveLimits=resolve);};}""")
    check.click()
    assert check.is_disabled()
    assert dialog.get_by_role('button', name='Refresh account status', exact=True).is_disabled()
    page.evaluate("""()=>resolveLimits({observedAt:'2026-09-10T12:00:00Z',limits:[{id:'codex',name:'Codex',primary:null,secondary:null}]})""")
    dialog.get_by_text('No model request was sent.', exact=False).wait_for()
    assert 'failed' not in dialog.inner_text()
    assert page.evaluate('calls') == ['limits', 'limits']
    assert page.evaluate('pane.conversation.metadata.accountLimits.limits[0].id') == 'codex'
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'account-connection-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    dialog.get_by_role('button', name='Refresh account status', exact=True).click()
    playwright.expect(dialog.get_by_text('No model request was sent.', exact=False)).to_have_count(0)
    check.click()
    page.keyboard.press('Escape')
    page.wait_for_function('!pane.accountDialog.open')
    page.evaluate("""()=>resolveLimits({observedAt:'2026-09-10T13:00:00Z',limits:[{id:'late'}]})""")
    assert page.evaluate('pane.conversation.metadata.accountLimits.limits[0].id') == 'codex'
    assert page.evaluate('pane.input.value') == 'keep my draft'
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_browser_login_requires_click_and_closing_does_not_cancel(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      window.login=null;
      controls.accountStatus=async()=>({account:null,login});
      controls.accountLogin=async()=>{calls.push('login');return window.login={status:'pending',loginId:'native-one',authUrl:'https://auth.openai.com/authorize?state=proof'};};
      controls.cancelAccountLogin=async id=>{calls.push(['cancel',id]);return window.login={status:'cancelled'};};
      controls.accountRateLimits=async()=>{throw Error('Must not check during pending login');};
      pane.accountButton.hidden=false;pane.input.value='draft';
    }""")
    button = page.locator('#left').get_by_role('button', name='Codex account', exact=True)
    page.locator('#left').get_by_role('button', name='Session actions', exact=True).click()
    button.click()
    dialog = page.get_by_role('dialog', name='Codex account')
    assert page.evaluate('calls') == []
    dialog.get_by_role('button', name='Sign in with ChatGPT', exact=True).click()
    page.wait_for_function("calls.length===1")
    assert dialog.get_by_role('button', name='Sign in with ChatGPT', exact=True).is_disabled()
    assert dialog.get_by_role('button', name='Check account connection', exact=True).is_disabled()
    assert dialog.get_by_role('link', name='Continue browser sign-in').get_attribute('href').startswith('https://auth.openai.com/')
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    page.keyboard.press('Escape')
    assert page.evaluate('calls') == ['login']
    assert page.evaluate('pane.input.value') == 'draft'
    page.locator('#left').get_by_role('button', name='Session actions', exact=True).click()
    button.click()
    dialog.get_by_role('button', name='Cancel browser sign-in', exact=True).click()
    assert page.evaluate('calls') == ['login', ['cancel', 'native-one']]
    assert not errors


def test_provider_badges_distinguish_linked_panes(pane):
    page, errors = pane
    assert page.locator("#left .aw-badge").inner_text() == "C"
    assert page.locator("#right .aw-badge").inner_text() == "X"
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_installation_diagnostics_are_explicit_and_show_native_exit_without_sending(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate(r"""() => {
      controls.diagnostics=async()=>{calls.push('doctor');return {command:'claude doctor',exitCode:4,output:'Native diagnostic warning\nNo changes applied'};};
      pane.diagnosticsButton.hidden=false;pane.input.value='keep draft';pane.diagnosticsButton.click();
    }""")
    dialog = page.get_by_role('dialog', name='Installation diagnostics')
    dialog.wait_for()
    assert page.evaluate('calls') == []
    dialog.get_by_role('button', name='Run installation diagnostics', exact=True).click()
    page.wait_for_function("calls.length===1")
    assert dialog.get_by_role('status').inner_text() == 'claude doctor exited 4'
    assert 'Native diagnostic warning' in dialog.locator('pre').inner_text()
    assert page.evaluate('calls') == ['doctor']
    assert page.evaluate('pane.input.value') == 'keep draft'
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    assert not errors


@pytest.mark.parametrize('command', ['/doctor', '/doctor check configuration', '/checkup'])
def test_claude_doctor_skill_is_not_replaced_with_installation_diagnostics(pane, command):
    page, errors = pane
    page.evaluate("""command=>{
      controls.diagnostics=async()=>{throw Error('Wrong diagnostics path');};
      pane.input.value=command;pane.render();
    }""", command)
    page.locator('#left').get_by_role('button', name='Send message', exact=True).click()
    page.wait_for_function('calls.length===1')
    assert page.evaluate('calls') == [['submit', {'text': command, 'files': []}]]
    assert page.get_by_role('dialog', name='Installation diagnostics').count() == 0
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_prompt_color_is_session_scoped_persistent_and_never_sent(pane, width, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    if not page.locator('#left').get_by_role('button', name="Prompt color", exact=True).first.is_visible():
        page.locator('#left').get_by_role('button', name='Session actions', exact=True).first.click()
    page.locator('#left').get_by_role('button', name='Prompt color', exact=True).click()
    dialog = page.get_by_role('dialog', name='Prompt color')
    dialog.get_by_role('button', name='cyan prompt color', exact=True).click()
    assert dialog.get_by_role('button', name='cyan prompt color').get_attribute('aria-pressed') == 'true'
    assert page.locator('#left .aw-composer').evaluate('el=>getComputedStyle(el).borderColor') == 'rgb(112, 219, 225)'
    assert page.locator('#right .aw-composer').evaluate('el=>getComputedStyle(el).borderColor') == 'rgb(80, 53, 74)'
    assert page.locator('#left').evaluate('el=>getComputedStyle(el).backgroundColor') == 'rgb(0, 0, 0)'
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    page.screenshot(path=str(tmp_path / f'prompt-color-{width}.png'))
    page.keyboard.press('Escape')
    assert page.evaluate('calls') == []
    page.reload()
    page.wait_for_function('window.pane && pane.conversation.sequence===1')
    assert page.locator('#left .aw-composer').evaluate('el=>getComputedStyle(el).borderColor') == 'rgb(112, 219, 225)'
    page.evaluate("() => {pane.input.value='/color default';pane.submit();}")
    assert page.locator('#left .aw-composer').evaluate('el=>getComputedStyle(el).borderColor') == 'rgb(80, 53, 74)'
    page.evaluate("() => {pane.input.value='/color url(evil)';pane.submit();}")
    assert page.evaluate('calls') == []
    assert page.evaluate("sessionStorage.getItem(pane.draftKey+':color')") is None
    assert not errors


@pytest.mark.parametrize("provider", ["Claude", "Codex"])
@pytest.mark.parametrize("width", [390, 1600])
def test_saved_session_picker_does_not_submit_or_stop_running_work(pane, provider, width, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("() => {pane.dispose();window.calls=[];}")
    page.evaluate("""async provider => {
      const {WorkspacePane}=await import('/workspace-pane.mjs');
      pane=new WorkspacePane(document.querySelector('#left'),{sessionId:'exact',provider,controls:{...controls,
        listSessions:async(q,offset)=>{calls.push(['list',q,offset]);return {data:[{session_id:offset?'second':'target',title:q||'Saved project',cwd:'/project'}],nextOffset:offset?null:50};},
        openSession:async sid=>calls.push(['open',sid])}});
      pane.receive({sequence:1,event:{method:'workspace/history',params:{thread:{id:'exact',turns:[{id:'working',status:'inProgress',items:[]}]}}}});
      pane.input.value='Keep my draft';
    }""", provider)
    assert page.evaluate("calls") == []
    if not page.get_by_role('button', name="Open saved conversation", exact=True).first.is_visible():
        page.get_by_role('button', name='Session actions', exact=True).first.click()
    page.get_by_role("button", name="Open saved conversation", exact=True).click()
    dialog = page.get_by_role("dialog", name="Saved conversations")
    dialog.get_by_role("button", name="Load more conversations", exact=True).click()
    page.wait_for_function("document.querySelector('.aw-command-list').childElementCount===2")
    search = dialog.get_by_role("searchbox", name="Search saved conversations")
    search.fill("Custom title")
    search.press("Enter")
    dialog.get_by_role("button", name="Custom title target /project").wait_for()
    page.screenshot(path=str(tmp_path / f"resume-{provider}-{width}.png"))
    dialog.get_by_role("button", name="Custom title target /project").click()
    assert page.evaluate("calls.filter(c=>!['list','open'].includes(c[0]))") == []
    assert page.evaluate("calls.at(-1)") == ["open", "target"]
    assert page.evaluate("pane.input.value") == "Keep my draft"
    assert page.evaluate("pane.conversation.turns.get('working').status") == "inProgress"
    page.evaluate("() => {pane.input.value='/resume';pane.send.disabled=false;}")
    page.evaluate("pane.submit()")
    dialog.wait_for()
    dialog.get_by_role("button", name="Close saved conversations", exact=True).click()
    assert page.evaluate("pane.input.value") == "/resume"
    assert page.evaluate("calls.filter(c=>!['list','open'].includes(c[0]))") == []
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_archived_picker_restores_only_on_confirmation_and_opens_separately(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("""()=>{
      const Pane=pane.constructor;pane.dispose();window.calls=[];window.restored=false;window.failRestore=true;
      pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls:{...controls,
        listSessions:async(q,offset,archived)=>{calls.push(['list',archived]);return {data:archived&&!restored?[{session_id:'019de3ff-997e-76e3-9e51-2eeff93b0318',title:'My archived chat',cwd:'/project'}]:[],nextOffset:null};},
        restoreArchive:async sid=>{calls.push(['restore',sid]);if(failRestore)throw Error('Restoration unconfirmed');restored=true;return {session_id:sid,archived:false};},
        openSession:async sid=>calls.push(['open',sid])}});
      pane.input.value='Retain this draft';
    }""")
    assert page.evaluate('calls') == []
    page.evaluate('pane.openSessions()')
    saved = page.get_by_role('dialog', name='Saved conversations', exact=True)
    saved.get_by_role('radio', name='Archived', exact=True).check()
    saved.locator('.aw-command').filter(has_text='My archived chat').click()
    dialog = page.get_by_role('dialog', name='Restore archived conversation', exact=True)
    assert page.evaluate("calls.filter(c=>c[0]!=='list')") == []
    dialog.get_by_role('button', name='Close archive restore', exact=True).click()
    saved.locator('.aw-command').filter(has_text='My archived chat').click()
    dialog.get_by_role('button', name='Confirm restore conversation', exact=True).click()
    playwright.expect(dialog.get_by_role('status')).to_have_text('Restoration unconfirmed')
    page.evaluate('window.failRestore=false')
    dialog.get_by_role('button', name='Confirm restore conversation', exact=True).click()
    playwright.expect(dialog.get_by_role('status')).to_have_text('Conversation restored')
    assert page.evaluate("calls.filter(c=>c[0]==='open')") == []
    assert not saved.locator('.aw-command').filter(has_text='My archived chat').count()
    assert page.evaluate('pane.input.value') == 'Retain this draft'
    assert page.locator('body').evaluate('el=>el.scrollWidth<=innerWidth')
    shot = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'archive-restore-{width}.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    dialog.get_by_role('button', name='Open restored conversation', exact=True).click()
    assert page.evaluate('calls.at(-1)') == ['open', '019de3ff-997e-76e3-9e51-2eeff93b0318']
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_grouped_reply_marks_queued_input_without_duplicate_duration(pane, width, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("""() => {
      emit({method:'turn/started',params:{turn:{id:'queued'}}});
      emit({method:'item/completed',params:{turnId:'queued',item:{id:'q',type:'userMessage',content:[{type:'text',text:'Also check the tests.'}]}}});
      emit({method:'turn/completed',params:{turn:{id:'t',status:'completed',durationMs:12000}}});
      emit({method:'turn/completed',params:{turn:{id:'queued',status:'completed',combinedWithTurnId:'t'}}});
    }""")
    playwright.expect(page.locator("#left .aw-turn-summary")).to_have_text([
        "Worked for 12s", "Included in combined reply"])
    assert page.get_by_text("Also check the tests.", exact=True).count() == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors and page.evaluate("calls") == []
    page.screenshot(path=str(tmp_path / f"combined-reply-{width}.png"))


@pytest.mark.parametrize("width", [390, 1600])
def test_acp_context_usage_is_labeled_and_invalid_updates_clear_it(pane, width):
    from core.workspace_acp_events import AcpEvents

    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    events = AcpEvents("exact")
    def usage(used, size):
        page.evaluate("event => emit(event)", events.update({"sessionId": "exact", "update": {
            "sessionUpdate": "usage_update", "used": used, "size": size}}))
    usage(53000, 200000)
    page.wait_for_function("pane.usageLabel.textContent === 'Context: 53,000 / 200,000 tokens (27%)'")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    usage(0, 200000)
    page.wait_for_function("pane.usageLabel.textContent.includes('(0%)')")
    usage(None, 0)
    page.wait_for_function("pane.usageLabel.textContent === ''")
    assert not errors and page.evaluate("calls") == []


@pytest.mark.parametrize("width", [390, 1600])
@pytest.mark.parametrize("reason,label", [("max_tokens", "Stopped: token limit reached"),
    ("max_turn_requests", "Stopped: model request limit reached"), ("refusal", "Provider declined to continue")])
def test_acp_stop_reason_visible_without_duration_or_auto_continuation(pane, width, reason, label):
    from core.workspace_acp_events import AcpEvents

    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    events = AcpEvents("exact")
    events.begin("stopped-turn")
    events.submitted([{"type": "text", "text": "my request"}])
    page.evaluate("event => emit(event)", events.complete(reason))
    page.get_by_text(label, exact=True).wait_for()
    assert page.locator("#left").get_by_role("button", name="Send message", exact=True).is_enabled()
    assert page.evaluate("calls") == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_acp_plan_updates_in_place_without_inventing_completion(pane, width, tmp_path):
    from core.workspace_acp_events import AcpEvents

    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    events = AcpEvents("exact")
    events.begin("t")
    def plan(entries):
        event = events.update({"sessionId": "exact", "update": {"sessionUpdate": "plan", "entries": entries}})
        page.evaluate("event => emit(event)", event)
    plan([{"content": "Old step", "status": "pending", "priority": "high"}])
    page.get_by_text("Old step", exact=True).wait_for()
    plan([{"content": "Inspect <script>unsafe()</script>", "status": "completed", "priority": "high"},
          {"content": "Verify " + "longfilename" * 20, "status": "in_progress", "priority": "medium"}])
    page.get_by_text("Inspect <script>unsafe()</script>", exact=True).wait_for()
    assert page.get_by_text("Old step", exact=True).count() == 0
    assert page.get_by_role("list", name="Agent plan").count() == 1
    assert page.locator('.aw-plan-step[data-status="completed"]').count() == 1
    assert page.locator('.aw-plan-step[data-status="in_progress"]').count() == 1
    assert page.locator(".aw-plan script").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"acp-plan-{width}.png"))
    plan([])
    page.wait_for_function("document.querySelectorAll('.aw-plan-step').length === 0")
    assert not errors and page.evaluate("calls") == []


@pytest.mark.parametrize("width", [390, 1600])
@pytest.mark.parametrize("failure", [False, True])
def test_gemini_native_mode_requires_explicit_apply_and_confirmation(pane, width, failure, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("""async failure => {
      pane.dispose();
      window.seq=0;
      const {WorkspacePane}=await import('/workspace-pane.mjs');
      const modes={currentValue:'plan',options:[{value:'plan',name:'Plan'},{value:'execute',name:'Execute',description:'Native mode description'}]};
      controls.sessionModes=async()=>structuredClone(modes);
      controls.setSessionMode=async mode=>{calls.push(['mode',mode]);if(failure)throw Error('Native mode change unconfirmed');return {...modes,currentValue:mode};};
      window.pane=new WorkspacePane(document.querySelector('#left'),{sessionId:'exact',provider:'Gemini',controls});
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});
    }""", failure)
    page.get_by_role("textbox", name="Message Gemini").fill("preserve draft")
    page.get_by_role("button", name="Session mode", exact=True).click()
    dialog = page.get_by_role("dialog", name="Session mode")
    dialog.get_by_role("combobox", name="Session mode").select_option("execute")
    assert dialog.get_by_text("Native mode description").is_visible()
    assert page.evaluate("calls") == []
    dialog.get_by_role("button", name="Apply", exact=True).click()
    expected = "Native mode change unconfirmed" if failure else "Last confirmed: Execute"
    playwright.expect(dialog.get_by_role("status")).to_have_text(expected)
    assert page.evaluate("calls") == [["mode", "execute"]]
    assert page.get_by_role("textbox", name="Message Gemini").input_value() == "preserve draft"
    if failure:
        assert dialog.get_by_role("button", name="Apply", exact=True).is_disabled()
    assert dialog.bounding_box()["width"] <= width
    if not failure:
        page.screenshot(path=str(tmp_path / f"gemini-mode-{width}.png"))
    dialog.get_by_role("button", name="Close session mode").click()
    assert not page.get_by_role("dialog", name="Session mode").count()
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_gemini_command_picker_preserves_draft_until_send(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("""async () => {
      pane.dispose();
      const {WorkspacePane}=await import('/workspace-pane.mjs');
      controls.commands=async()=>({data:[{name:'plan',description:'Plan work',argumentHint:'task',kind:'command'}]});
      window.pane=new WorkspacePane(document.querySelector('#left'),{sessionId:'exact',provider:'Gemini',controls});
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});
    }""")
    page.get_by_role("textbox", name="Message Gemini").fill("keep this task")
    page.get_by_role("button", name="Commands and skills", exact=True).click()
    page.get_by_role("button", name="/plan task Plan work").click()
    assert page.get_by_role("textbox", name="Message Gemini").input_value() == "/plan keep this task"
    assert page.evaluate("calls") == []
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_model_catalog_update_clears_unavailable_header_without_submission(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("emit({method:'workspace/models',params:{data:[{id:'native-a',model:'native-a',displayName:'Native A'}],settings:{model:'native-a'}}})")
    page.wait_for_function("pane.modelLabel.textContent === 'native-a'")
    page.evaluate("emit({method:'workspace/models',params:{data:[{id:'native-b',model:'native-b',displayName:'Native B'}],settings:{model:'native-b'}}})")
    page.wait_for_function("pane.modelLabel.textContent === 'native-b'")
    page.evaluate("emit({method:'workspace/models',params:{data:[],settings:{model:null}}})")
    page.wait_for_function("pane.modelLabel.textContent === 'Model unavailable'")
    assert page.evaluate("pane.modelSelect.hidden")
    assert page.evaluate("calls") == [] and not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_acp_tool_content_is_readable_and_diff_markup_is_inert(pane, width, tmp_path):
    from core.workspace_acp_events import AcpEvents

    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    events = AcpEvents("exact")
    events.begin("t")
    event = events.update({"sessionId": "exact", "update": {
        "sessionUpdate": "tool_call", "toolCallId": "edit", "title": "Update settings",
        "rawOutput": {"internal": "raw result"}, "status": "completed", "content": [
            {"type": "content", "content": {"type": "text", "text": "Configuration updated."}},
            {"type": "diff", "path": "/project/settings.txt", "oldText": "old setting",
             "newText": "<img src=x onerror=alert(1)>"},
            {"type": "diff", "path": "/project/new.txt", "oldText": None, "newText": "new file"}]}})
    page.evaluate("event => emit(event)", event)
    detail = page.locator('[data-item-id="t:tool:edit"] > details')
    detail.locator(":scope > summary").click()
    assert detail.locator(".aw-tool-output").inner_text() == "Configuration updated."
    assert detail.locator(".aw-remove").inner_text().strip() == "-old setting"
    assert detail.locator(".aw-add").all_inner_texts() == ["+<img src=x onerror=alert(1)>\n", "+new file\n"]
    assert detail.locator("img").count() == 0
    assert "raw result" not in detail.inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"acp-tool-{width}.png"))
    detail.get_by_text("Native details", exact=True).click()
    assert "raw result" in detail.inner_text()
    assert not errors and page.evaluate("calls") == []


@pytest.mark.parametrize("width", [390, 1600])
@pytest.mark.parametrize("choice", ["Allow once", "Cancel"])
def test_acp_permission_options_are_explicit_and_exact(pane, width, choice):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      emit({method:'item/completed',params:{threadId:'exact',turnId:'t',item:{
        id:'acp-tool',type:'acpToolCall',tool:'Inspect config',input:{path:'settings.json'},
        output:'<script>not executable</script>',status:'completed'}}});
      emit({id:'acp-permission',method:'session/request_permission',params:{threadId:'exact',sessionId:'exact',
        toolCall:{title:'Change config',rawInput:{path:'settings.json'}},
        options:[{optionId:'native-once',name:'Allow once',kind:'allow_once'}]}});
    }""")
    page.get_by_text("Change config", exact=True).wait_for()
    page.locator("summary").filter(has_text="Inspect config").click()
    page.get_by_text("<script>not executable</script>", exact=True).wait_for()
    assert page.evaluate("calls") == []
    page.get_by_role("button", name=choice, exact=True).click()
    outcome = {"outcome": "cancelled"} if choice == "Cancel" else {"outcome": "selected", "optionId": "native-once"}
    assert page.evaluate("calls") == [["answer", "acp-permission", {"outcome": outcome}]]
    assert page.locator("body").evaluate("el=>el.scrollWidth<=innerWidth")
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_codex_skill_toggle_waits_for_confirmation_and_preserves_draft(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      pane.dispose();window.enabled=true;
      controls.commands=async()=>({data:[{kind:'skill',name:'proof',path:'/skills/proof/SKILL.md',enabled,unavailableReason:enabled?'':'Skill is disabled'}]});
      controls.setSkillEnabled=(path,value)=>new Promise(resolve=>{calls.push([path,value]);window.finish=async()=>{enabled=value;resolve({...await controls.commands(),effectiveEnabled:value});};});
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
    }""")
    composer = page.get_by_role('textbox', name='Message Codex').first
    composer.fill('keep this draft')
    page.get_by_role('button', name='Commands and skills', exact=True).click()
    toggle = page.get_by_role('checkbox', name='Enable skill proof')
    toggle.wait_for()
    assert page.evaluate('calls') == []
    toggle.click()
    assert toggle.is_disabled() and toggle.is_checked()
    page.evaluate('finish()')
    page.get_by_text('Skill disabled', exact=True).wait_for()
    assert not toggle.is_checked() and not toggle.is_disabled()
    assert page.get_by_role('dialog').get_by_role('button').filter(has_text='$proof').is_disabled()
    assert page.evaluate('calls') == [['/skills/proof/SKILL.md', False]]
    page.get_by_role('button', name='Close commands').click()
    assert composer.input_value() == 'keep this draft'
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
@pytest.mark.parametrize("apply_rules", [False, True])
def test_claude_permission_suggestions_require_explicit_selection(pane, width, apply_rules):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""emit({id:'permission',method:'workspace/claudeApproval',params:{tool:'Bash',input:{command:'pwd'},suggestions:[{type:'addRules',rules:[{toolName:'Bash',ruleContent:'pwd'}],behavior:'allow',destination:'localSettings'}]}})""")
    button = page.get_by_role('button', name='Allow and apply selected changes')
    assert button.is_disabled()
    assert page.evaluate('calls') == []
    page.get_by_role('checkbox', name='addRules - Project local settings (persistent)').check()
    if apply_rules:
        button.click()
    else:
        page.get_by_role('button', name='Allow once', exact=True).click()
    expected = {'decision': 'allow', 'suggestions': [0]} if apply_rules else {'decision': 'allow'}
    assert page.evaluate('calls') == [['answer', 'permission', expected]]
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_inline_file_completion_selects_without_sending(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {controls.searchFiles=async query=>{
      calls.push(['search',query]);return {paths:['src/first.py','src/with space.py']};
    };}""")
    composer=page.get_by_role('textbox',name='Message Claude')
    composer.fill('review @src')
    suggestions=page.get_by_role('listbox',name='Project file suggestions')
    suggestions.get_by_role('option',name='src/first.py').wait_for()
    composer.press('ArrowDown')
    composer.press('Enter')
    assert composer.input_value() == 'review @"src/with space.py" '
    assert page.evaluate('calls') == [['search','src']]
    assert suggestions.is_hidden()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors


def test_inline_completion_discards_stale_results_and_escape_does_not_interrupt(pane):
    page, errors = pane
    page.evaluate("""() => {
      window.pending={};controls.searchFiles=query=>new Promise(resolve=>{pending[query]=resolve;});
      emit({method:'turn/started',params:{turn:{id:'working',status:'inProgress'}}});
    }""")
    composer=page.get_by_role('textbox',name='Message Claude')
    composer.fill('@old')
    page.wait_for_function('!!pending.old')
    composer.fill('@new')
    page.wait_for_function('!!pending.new')
    composer.press('Enter')
    assert composer.input_value() == '@new'
    assert page.evaluate('calls') == []
    page.evaluate("pending.new({paths:['new.py']})")
    page.get_by_role('option',name='new.py',exact=True).wait_for()
    page.evaluate("pending.old({paths:['old.py']})")
    assert page.get_by_role('option',name='old.py',exact=True).count() == 0
    composer.press('Escape')
    assert page.get_by_role('listbox',name='Project file suggestions').is_hidden()
    assert page.evaluate('calls') == []
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_project_file_picker_preserves_draft_and_never_sends(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      controls.searchFiles=async query=>{calls.push(query);return {paths:['src/main.py','src/with space.py']};};
      pane.mentionButton.hidden=false;
    }""")
    composer=page.get_by_role('textbox',name='Message Claude')
    composer.fill('review this')
    composer.evaluate('el=>el.setSelectionRange(7,11)')
    page.get_by_role('button',name='Mention project file',exact=True).click()
    assert page.evaluate('calls') == []
    dialog=page.get_by_role('dialog',name='Mention project file')
    search=dialog.get_by_role('searchbox',name='Find project file')
    search.fill('src')
    search.press('Enter')
    dialog.get_by_role('button',name='src/with space.py',exact=True).wait_for()
    search.press('ArrowDown')
    page.keyboard.press('ArrowDown')
    page.keyboard.press('Enter')
    assert composer.input_value() == 'review @"src/with space.py" '
    assert page.evaluate('calls') == ['src']
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_plugin_reload_is_explicit_refreshes_commands_and_reports_native_errors(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      controls.commands=async()=>({data:[{name:'old'}]});
      controls.reloadPlugins=async()=>{calls.push('plugins');return {data:[{name:'fresh'}],plugins:[{name:'one'}],error_count:2};};
      pane.commandsButton.hidden=false;
    }""")
    page.get_by_role('button', name='Commands and skills', exact=True).click()
    dialog = page.get_by_role('dialog', name='Commands and skills')
    dialog.get_by_role('button', name='/old', exact=True).wait_for()
    assert page.evaluate('calls') == []
    dialog.get_by_role('button', name='Reload plugins from disk').click()
    dialog.get_by_role('button', name='/fresh', exact=True).wait_for()
    assert '2 plugin errors' in dialog.get_by_role('status').inner_text()
    assert page.evaluate('calls') == ['plugins']
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    assert not errors


@pytest.mark.parametrize("command", ["reload-plugins", "reload-skills"])
@pytest.mark.parametrize("entry", ["typed", "picker"])
def test_reload_commands_use_native_controls_without_model_input(pane, command, entry):
    page, errors = pane
    page.evaluate("""command => {
      const data=[{name:command,workspaceAction:command}];
      controls.commands=async()=>({data});
      controls.reloadPlugins=async()=>{calls.push('reload-plugins');return {data,plugins:[],error_count:0};};
      controls.reloadSkills=async()=>{calls.push('reload-skills');return {data};};
      pane.commandsButton.hidden=false;pane.input.value='/'+command;
    }""", command)
    if entry == "typed":
        page.evaluate("pane.submit()")
    else:
        page.get_by_role('button', name='Commands and skills', exact=True).click()
        assert page.evaluate('calls') == []
        page.get_by_role('dialog', name='Commands and skills').get_by_role('button', name=f'/{command}', exact=True).click()
    page.wait_for_function('calls.length===1')
    assert page.evaluate('calls') == [command]
    assert page.evaluate('pane.input.value') == f'/{command}'
    assert not errors


def test_pending_command_streams_output_without_raw_event_json(pane):
    page, errors = pane
    page.evaluate("""() => emit({method:'item/started',params:{turnId:'t',item:{
      id:'pending-command',type:'commandExecution',command:'printf hello',
      status:'inProgress',aggregatedOutput:null,exitCode:null
    }}})""")
    tool = page.locator('[data-item-id="pending-command"]')
    tool.locator('summary').click()
    assert 'printf hello' in tool.inner_text()
    assert 'inProgress' in tool.inner_text()
    assert tool.locator('pre').count() == 0
    page.evaluate("""() => emit({method:'item/commandExecution/outputDelta',params:{
      turnId:'t',itemId:'pending-command',delta:'hello'
    }})""")
    tool.get_by_text('hello', exact=True).wait_for()
    assert tool.locator('details').evaluate('el=>el.open')
    page.evaluate("""() => emit({method:'item/completed',params:{turnId:'t',item:{
      id:'pending-command',type:'commandExecution',command:'printf hello',
      status:'completed',aggregatedOutput:'hello',exitCode:0
    }}})""")
    tool.get_by_text('Exit 0', exact=True).wait_for()
    assert tool.locator('.aw-tool-output').inner_text() == 'hello'
    assert tool.count() == 1
    assert not errors


def test_subagent_provenance_is_visible_for_messages_and_tools(pane, tmp_path):
    page, errors = pane
    page.evaluate("""() => {
      emit({method:'workspace/settings',params:{model:'parent-model'}});
      emit({method:'item/agentMessage/delta',params:{turnId:'t',itemId:'child-message',delta:'Child output',parentToolUseId:'agent-parent'}});
      emit({method:'item/started',params:{turnId:'t',item:{id:'child-tool',type:'claudeToolCall',tool:'Bash',input:{command:'pwd'},parentToolUseId:'agent-parent',status:'inProgress'}}});
    }""")
    child = page.locator('[data-item-id="child-message"]')
    child.get_by_text("Subagent response", exact=True).wait_for()
    child.locator(".aw-agent-origin summary").click()
    assert child.get_by_text("Parent tool: agent-parent", exact=True).is_visible()
    tool = page.locator('[data-item-id="child-tool"]')
    tool.locator(".aw-tool > summary").first.click()
    page.evaluate("""() => {
      emit({method:'item/completed',params:{turnId:'t',item:{id:'child-message',type:'agentMessage',text:'Complete child output',parentToolUseId:'agent-parent',sourceModel:'child-model'}}});
      emit({method:'item/completed',params:{turnId:'t',item:{id:'child-tool',type:'claudeToolCall',tool:'Bash',input:{command:'pwd'},output:'project',parentToolUseId:'agent-parent',status:'completed'}}});
    }""")
    child.get_by_text("Subagent - child-model", exact=True).wait_for()
    assert child.get_by_text("Parent tool: agent-parent", exact=True).is_visible()
    assert tool.locator(".aw-tool-output").inner_text() == "project"
    assert tool.locator(".aw-tool").first.evaluate("el=>el.open")
    assert page.locator("#left .aw-head small").inner_text() == "parent-model"
    page.set_viewport_size({"width": 390, "height": 900})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / "subagent-provenance-mobile.png"))
    assert not errors


def test_escape_interrupts_only_focused_running_turn_not_dialog_or_draft(pane):
    page, errors = pane
    draft = page.get_by_role("textbox", name="Message Claude")
    draft.fill("Keep my draft")
    draft.press("Escape")
    assert page.evaluate("calls") == []
    page.evaluate("""() => {
      controls.interrupt=id=>{calls.push(['interrupt',id]);return new Promise(resolve=>window.finishInterrupt=resolve);};
      controls.cancelQueuedBridge=async()=>{};
      emit({method:'turn/started',params:{turn:{id:'running-exact',status:'inProgress'}}});
      emit({method:'workspace/bridgeQueue',params:{count:1,requests:[{id:'q',prompt:'queued'}]}});
    }""")
    page.get_by_role("button", name="Queued sibling messages").click()
    page.keyboard.press("Escape")
    page.get_by_role("dialog", name="Queued sibling messages").wait_for(state="hidden")
    assert page.evaluate("calls") == []
    draft.press("Escape")
    draft.press("Escape")
    assert page.evaluate("calls") == [["interrupt", "running-exact"]]
    assert page.get_by_role("button", name="Interrupt turn").is_disabled()
    assert draft.input_value() == "Keep my draft"
    page.evaluate("finishInterrupt();emit({method:'turn/completed',params:{turn:{id:'running-exact',status:'interrupted'}}})")
    page.get_by_role("button", name="Interrupt turn").wait_for(state="hidden")
    draft.press("Escape")
    assert page.evaluate("calls") == [["interrupt", "running-exact"]]
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_queue_edit_keeps_draft_and_targets_original_message(pane, tmp_path, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      controls.cancelQueuedBridge=async id=>calls.push(['cancel',id]);
      controls.editQueuedBridge=async(id,text,expected)=>{calls.push(['edit',id,text,expected]);if(window.failEdit)throw Error('Edit unconfirmed');};
      emit({method:'workspace/bridgeQueue',params:{count:1,requests:[{id:'q',prompt:'original'}]}});
    }""")
    draft = page.get_by_role("textbox", name="Message Claude")
    draft.fill("Main draft")
    page.get_by_role("button", name="Queued sibling messages").click()
    page.get_by_role("button", name="Edit queued message q", exact=True).click()
    dialog = page.get_by_role("dialog", name="Edit queued message", exact=True)
    editor = dialog.get_by_role("textbox", name="Queued message text")
    editor.fill("Corrected message")
    page.evaluate("emit({method:'workspace/bridgeQueue',params:{count:2,requests:[{id:'q',prompt:'original'},{id:'other',prompt:'second'}]}})")
    assert editor.input_value() == "Corrected message"
    page.evaluate("window.failEdit=true")
    dialog.get_by_role("button", name="Save", exact=True).click()
    dialog.get_by_text("Edit unconfirmed", exact=True).wait_for()
    assert editor.input_value() == "Corrected message"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"queue-edit-{width}.png"))
    page.evaluate("window.failEdit=false")
    dialog.get_by_role("button", name="Save", exact=True).click()
    dialog.wait_for(state="hidden")
    assert draft.input_value() == "Main draft"
    assert page.evaluate("calls") == [["edit", "q", "Corrected message", "original"]] * 2
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_event_inspector_pages_lazily_without_session_actions(pane, tmp_path, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      const root=pane.root;const Constructor=pane.constructor;pane.dispose();root.replaceChildren();window.eventReads=[];
      controls.events=async after=>{eventReads.push(after);if(window.failEvents)throw Error('Journal unavailable');return {events:[{sequence:after+1,event:{method:'native-event',params:{text:'<img src=x onerror=window.bad=true>'}}}],cursor:after+1,has_more:after===0};};
      window.pane=new Constructor(root,{sessionId:'exact',provider:'Claude',controls});
    }""")
    assert page.evaluate("eventReads") == []
    if not page.get_by_role('button', name="Session events", exact=True).first.is_visible():
        page.get_by_role('button', name='Session actions', exact=True).first.click()
    page.get_by_role("button", name="Session events", exact=True).click()
    dialog = page.get_by_role("dialog", name="Session events")
    dialog.get_by_text("1 native-event", exact=True).wait_for()
    assert dialog.locator("pre").count() == 0
    dialog.get_by_text("1 native-event", exact=True).click()
    assert "<img" in dialog.locator("pre").inner_text()
    assert dialog.locator("img").count() == 0
    dialog.get_by_role("button", name="Next event page").click()
    dialog.get_by_text("2 native-event", exact=True).wait_for()
    assert dialog.get_by_role("button", name="Next event page").is_disabled()
    assert dialog.locator("pre").count() == 0
    dialog.get_by_role("button", name="Previous event page").click()
    dialog.get_by_text("1 native-event", exact=True).wait_for()
    page.evaluate("window.failEvents=true")
    dialog.get_by_role("button", name="Refresh event page").click()
    dialog.get_by_text("Journal unavailable").wait_for()
    assert dialog.locator("details").count() == 0
    page.evaluate("window.failEvents=false")
    dialog.get_by_role("button", name="Refresh event page").click()
    dialog.get_by_text("1 native-event", exact=True).wait_for()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"event-inspector-{width}.png"))
    dialog.get_by_role("button", name="Close session events").click()
    assert page.evaluate("calls") == []
    assert page.evaluate("eventReads") == [0, 1, 0, 0, 0]
    assert not errors


def test_streaming_tool_input_keeps_one_expanded_call(pane):
    page, errors = pane
    page.evaluate("emit({method:'item/started',params:{turnId:'t',item:{id:'streamed',type:'claudeToolCall',tool:'Bash',input:{},inputStreaming:true,inputJson:'{\"command\":',status:'inProgress'}}})")
    item = page.locator('[data-item-id="streamed"]')
    item.locator("details > summary").first.click()
    assert item.get_by_text("Receiving tool input", exact=True).is_visible()
    assert item.locator(".aw-tool-input").inner_text() == '{"command":'
    assert item.locator(".aw-command").count() == 0
    page.evaluate("emit({method:'item/started',params:{turnId:'t',item:{id:'streamed',type:'claudeToolCall',tool:'Bash',input:{command:'pwd'},status:'inProgress'}}})")
    page.wait_for_function("document.querySelector('[data-item-id=streamed] .aw-command')?.textContent === 'pwd'")
    assert item.locator("details").first.evaluate("el=>el.open")
    assert item.locator(".aw-tool-input").count() == 0
    assert page.locator('[data-item-id="streamed"]').count() == 1
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_claude_tools_show_readable_native_output_and_requested_edits(pane, tmp_path, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("""() => {
      for(const item of [
        {id:'bash-native',type:'claudeToolCall',tool:'Bash',input:{command:'pytest tests/test_example.py -q',description:'Run focused tests'},output:[{type:'text',text:'3 passed in 0.4s'}],status:'completed'},
        {id:'edit-native',type:'claudeToolCall',tool:'Edit',input:{file_path:'core/example.py',old_string:'old_value',new_string:'new_value'},output:'Edit rejected by tool',status:'failed'},
        {id:'write-native',type:'claudeToolCall',tool:'Write',input:{file_path:'index.html',content:'<img src=x onerror=window.compromised=true>'},status:'inProgress'},
        {id:'unknown-native',type:'claudeToolCall',tool:'CustomTool',input:{custom:'retained'},output:{structured:'also retained'},status:'completed'}
      ])emit({method:'item/completed',params:{turnId:'t',item}});
    }""")
    for identifier in ["bash-native", "edit-native", "write-native", "unknown-native"]:
        page.locator(f'[data-item-id="{identifier}"] > details > summary').click()
    command = page.locator('[data-item-id="bash-native"]')
    assert command.locator(".aw-command").inner_text() == "pytest tests/test_example.py -q"
    assert command.locator(".aw-tool-output").inner_text() == "3 passed in 0.4s"
    assert command.locator(".aw-exit").count() == 0
    edit = page.locator('[data-item-id="edit-native"]')
    assert edit.get_by_text("Requested edit", exact=True).is_visible()
    assert edit.locator(".aw-diff").inner_text() == "-old_value\n+new_value\n"
    assert edit.get_by_text("failed", exact=True).is_visible()
    assert edit.locator(".aw-tool-output").inner_text() == "Edit rejected by tool"
    assert page.locator('[data-item-id="write-native"] img').count() == 0
    assert page.evaluate("window.compromised === undefined")
    assert "also retained" in page.locator('[data-item-id="unknown-native"] .aw-tool-output').inner_text()
    command.get_by_text("Native details", exact=True).click()
    assert '"description": "Run focused tests"' in command.locator("details details pre").inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"claude-tools-{width}.png"))
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_claude_effort_uses_native_command_without_consuming_draft(pane, tmp_path, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      const root=pane.root;const Constructor=pane.constructor;pane.dispose();root.replaceChildren();
      controls.commands=async()=>({data:[{name:'effort'}]});
      controls.models=async()=>({data:[{model:'default',claudeCapabilities:{resolvedModel:'claude-proof',supportsEffort:true,supportedEffortLevels:['low','high','xhigh']}}]});
      window.pane=new Constructor(root,{sessionId:'exact',provider:'Claude',controls});window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',model:'claude-proof',turns:[]}}});
      emit({method:'workspace/settings',params:{model:'claude-proof'}});
    }""")
    draft = page.get_by_role("textbox", name="Message Claude")
    draft.fill("Keep this draft")
    page.get_by_role("button", name="Claude reasoning effort", exact=True).click()
    dialog = page.get_by_role("dialog", name="Claude reasoning effort")
    select = dialog.get_by_role("combobox", name="Claude effort level")
    select.select_option("xhigh")
    assert page.evaluate("calls") == []
    page.evaluate("emit({method:'turn/started',params:{turn:{id:'busy',status:'inProgress'}}})")
    dialog.get_by_role("button", name="Apply", exact=True).click()
    dialog.get_by_text("Finish the current turn before changing effort").wait_for()
    assert page.evaluate("calls") == []
    page.evaluate("emit({method:'turn/completed',params:{turn:{id:'busy',status:'completed'}}})")
    page.evaluate("window.failSubmit=true")
    dialog.get_by_role("button", name="Apply", exact=True).click()
    dialog.get_by_text("Submission unconfirmed").wait_for()
    assert draft.input_value() == "Keep this draft"
    assert dialog.is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"claude-effort-{width}.png"))
    page.evaluate("window.failSubmit=false")
    dialog.get_by_role("button", name="Apply", exact=True).click()
    dialog.wait_for(state="hidden")
    assert draft.input_value() == "Keep this draft"
    assert page.evaluate("calls") == [["submit", {"text": "/effort xhigh", "files": []}]] * 2
    assert not errors


@pytest.mark.parametrize("provider", ["Codex", "Claude"])
@pytest.mark.parametrize("width", [390, 1600])
def test_complete_control_surface_fits_without_auto_actions(pane, tmp_path, provider, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 844})
    page.evaluate("""provider => {
      pane.dispose();
      for(const name of ['commands','permissions','contextUsage','mcpServers','review','compact','backgroundTasks','cancelQueuedBridge']) controls[name]=async()=>{calls.push([name]);return {};};
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider,controls});
      window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});
      emit({method:'workspace/models',params:{data:[{model:'native',displayName:'Native model with a long display name',supportedReasoningEfforts:provider==='Codex'?[{reasoningEffort:'high'}]:[],serviceTiers:provider==='Codex'?[{id:'fast',name:'Fast'}]:[]}]}});
      emit({method:'workspace/bridgeQueue',params:{threadId:'exact',count:3,requests:[]}});
    }""", provider)
    page.wait_for_function("!pane.send.disabled")
    page.locator('#left').get_by_role('combobox', name='Model', exact=True).select_option('native')
    assert page.evaluate("calls") == []
    assert page.locator("#left").evaluate("el=>el.scrollWidth<=el.clientWidth")
    boxes = page.locator("#left .aw-composer-tools").evaluate("""el=>[...el.children].filter(c=>c.getClientRects().length).map(c=>{const r=c.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom};})""")
    root = page.locator("#left").bounding_box()
    for index, box in enumerate(boxes):
        assert root["x"] <= box["left"] < box["right"] <= root["x"] + root["width"]
        assert 0 <= box["top"] < box["bottom"] <= 844
        for other in boxes[index + 1:]:
            assert box["right"] <= other["left"] or other["right"] <= box["left"] or box["bottom"] <= other["top"] or other["bottom"] <= box["top"]
    page.screenshot(path=str(tmp_path / f"controls-{provider}-{width}.png"))
    assert not errors


def test_codex_permission_profile_picker_disables_managed_denials(pane):
    page, errors = pane
    page.evaluate("""() => {
      pane.dispose();
      controls.permissions=async()=>({mode:null,modes:[':read-only','blocked'],profiles:[{id:':read-only',allowed:true},{id:'blocked',allowed:false}]});
      controls.setPermissions=async(mode,confirmed)=>{calls.push([mode,confirmed]);return {mode};};
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
    }""")
    page.get_by_role("button", name="Permission mode", exact=True).click()
    dialog = page.get_by_role("dialog", name="Permission mode", exact=True)
    select = dialog.get_by_role("combobox", name="Permission mode")
    select.select_option(":read-only")
    assert select.locator("option[value=blocked]").evaluate("el=>el.disabled"), select.evaluate("el=>el.outerHTML")
    select.focus()
    select.press("ArrowDown")
    assert select.input_value() == ":read-only"
    dialog.get_by_role("button", name="Apply", exact=True).click()
    assert page.evaluate("calls") == []
    dialog.get_by_role("checkbox", name="Apply this permission profile to subsequent turns").check()
    dialog.get_by_role("button", name="Apply", exact=True).click()
    page.wait_for_function("calls.length===1")
    assert page.evaluate("calls") == [[":read-only", True]]
    assert not errors


def test_permission_mode_requires_explicit_apply_and_bypass_confirmation(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""() => {
      pane.dispose();
      controls.permissions=async()=>({mode:'default',modes:['default','plan','bypassPermissions']});
      controls.setPermissions=async(mode,confirmed)=>{calls.push([mode,confirmed]);return {mode};};
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider:'Claude',controls});
    }""")
    assert page.evaluate("calls") == []
    page.get_by_role("button", name="Permission mode", exact=True).click()
    dialog = page.get_by_role("dialog", name="Permission mode", exact=True)
    select = dialog.get_by_role("combobox", name="Permission mode")
    select.select_option("bypassPermissions")
    dialog.get_by_role("button", name="Apply", exact=True).click()
    dialog.get_by_text("Confirm bypassing permission prompts first").wait_for()
    assert page.evaluate("calls") == []
    page.screenshot(path=str(tmp_path / "permission-mode-mobile.png"))
    dialog.get_by_role("checkbox", name="Allow tools without permission prompts").check()
    dialog.get_by_role("button", name="Apply", exact=True).click()
    page.wait_for_function("calls.length===1")
    assert page.evaluate("calls") == [["bypassPermissions", True]]
    dialog.get_by_role("button", name="Close permission mode").click()
    assert not errors


def test_context_breakdown_is_explicit_and_clears_stale_data_on_failure(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""() => {
      pane.dispose();
      controls.contextUsage=async()=>{calls.push(['context']);if(calls.length>1)throw Error('Context unavailable');return {model:'Native model',totalTokens:1500,maxTokens:10000,percentage:15,categories:[{name:'Messages',tokens:1400},{name:'Deferred tools',tokens:100,isDeferred:true}]};};
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider:'Claude',controls});
    }""")
    assert page.evaluate("calls") == []
    page.get_by_role("button", name="Context breakdown", exact=True).click()
    dialog = page.get_by_role("dialog", name="Context breakdown", exact=True)
    dialog.get_by_text("15.0% used", exact=True).wait_for()
    assert dialog.get_by_role("button", name="Close context breakdown").locator("svg").count() == 1
    assert dialog.get_by_text("1,500 / 10,000 tokens", exact=True).is_visible()
    assert dialog.get_by_text("Deferred tools (deferred)", exact=True).is_visible()
    assert dialog.evaluate("el=>el.scrollWidth<=el.clientWidth")
    page.screenshot(path=str(tmp_path / "context-mobile.png"))
    dialog.get_by_role("button", name="Refresh context breakdown").click()
    dialog.get_by_text("Context unavailable", exact=True).wait_for()
    assert dialog.get_by_text("1,500 / 10,000 tokens", exact=True).count() == 0
    dialog.get_by_role("button", name="Close context breakdown").click()
    assert page.evaluate("calls") == [["context"], ["context"]]
    assert not errors


def test_codex_skill_selection_persists_and_sends_exact_path_only_on_submit(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""() => {
      pane.dispose();
      controls.commands=async()=>({data:[{kind:'skill',name:'proof',path:'/project/.agents/skills/proof/SKILL.md',description:'Proof skill'}]});
      controls.submit=async message=>{calls.push(['submit',message.options]);};
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
      window.seq=0;
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});
    }""")
    page.get_by_role("button", name="Commands and skills", exact=True).click()
    page.get_by_role("button", name="$proof", exact=False).wait_for()
    page.screenshot(path=str(tmp_path / "codex-skills-mobile.png"))
    page.get_by_role("button", name="$proof", exact=False).click()
    assert page.evaluate("calls") == []
    assert page.get_by_role("button", name="Remove skill proof").is_visible()
    page.evaluate("""() => {pane.dispose();window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});window.seq=0;emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});}""")
    assert page.get_by_role("button", name="Remove skill proof").is_visible()
    page.locator('#left').get_by_role("button", name="Send message", exact=True).click()
    page.wait_for_function("calls.length===1")
    assert page.evaluate("calls[0]") == ["submit", {"skills": ["/project/.agents/skills/proof/SKILL.md"]}]
    assert page.get_by_role("button", name="Remove skill proof").count() == 0
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_codex_mcp_login_and_reload_are_explicit_and_wait_for_native_completion(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      pane.dispose();window.login=null;
      controls.mcpServers=async()=>({data:[{name:'proof',status:'authenticationRequired',authStatus:'notLoggedIn',toolCount:0,login}]});
      controls.mcpLogin=async name=>{calls.push(['login',name]);login={status:'pending',authorizationUrl:'https://auth.example/authorize?state=proof'};return login;};
      controls.mcpReload=async()=>{calls.push(['reload']);return controls.mcpServers();};
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
      window.seq=0;
    }""")
    page.get_by_role('button', name='MCP connections', exact=True).click()
    login = page.get_by_role('button', name='Sign in to proof')
    login.wait_for()
    assert page.evaluate('calls') == []
    login.click()
    link = page.get_by_role('link', name='Continue authorization')
    link.wait_for()
    assert link.get_attribute('href') == 'https://auth.example/authorize?state=proof'
    assert login.is_disabled()
    assert len(page.context.pages) == 1
    page.evaluate("login={status:'succeeded'};emit({method:'mcpServer/oauthLogin/completed',params:{threadId:'exact',name:'proof',success:true}})")
    page.get_by_text('Login: succeeded', exact=True).wait_for()
    assert link.count() == 0
    page.get_by_role('button', name='Reload MCP configuration').click()
    page.wait_for_function("calls.length===2")
    assert page.evaluate('calls') == [['login','proof'],['reload']]
    assert page.get_by_role('dialog').evaluate('el=>el.scrollWidth<=el.clientWidth')
    assert not errors


@pytest.mark.parametrize("width", [1440, 390])
def test_codex_mcp_setting_waits_for_effective_confirmation(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      pane.dispose();window.enabled=true;
      controls.mcpServers=async()=>({data:[{name:'proof',status:'unknown',enabled,settingsWritable:true}]});
      controls.setMcpEnabled=(name,value)=>{calls.push([name,value]);return new Promise(resolve=>window.finishSetting=async()=>{enabled=value;resolve({...await controls.mcpServers(),notice:'Saved in Codex user settings; another configuration layer overrides this setting.'});});};
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
    }""")
    page.get_by_role('button', name='MCP connections', exact=True).click()
    toggle = page.get_by_role('checkbox', name='Enable proof')
    toggle.wait_for()
    assert page.evaluate('calls') == []
    toggle.click()
    assert toggle.is_checked() and toggle.is_disabled()
    page.evaluate('finishSetting()')
    page.wait_for_function("!document.querySelector('.aw-mcp-server input').checked")
    assert not toggle.is_disabled()
    page.get_by_text('Saved in Codex user settings; another configuration layer overrides this setting.', exact=True).wait_for()
    assert page.evaluate('calls') == [['proof', False]]
    assert page.get_by_role('dialog').evaluate('el=>el.scrollWidth<=el.clientWidth')
    assert not errors


def test_codex_mcp_inventory_shows_unknown_state_without_unsupported_mutations(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""() => {
      pane.dispose();
      controls.mcpServers=async()=>{calls.push(['list']);return {data:[{name:'x'.repeat(180),status:'unknown',authStatus:'oAuth',toolCount:3}]};};
      controls.mcpServerControl=async()=>{throw Error('must not mutate');};
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
    }""")
    assert page.evaluate("calls") == []
    page.get_by_role("button", name="MCP connections", exact=True).click()
    dialog = page.get_by_role("dialog", name="MCP connections", exact=True)
    dialog.get_by_text("unknown", exact=True).wait_for()
    assert dialog.get_by_text("Authentication: oAuth", exact=True).is_visible()
    assert dialog.get_by_text("3 tools", exact=True).is_visible()
    assert dialog.get_by_role("checkbox").count() == 0
    assert dialog.get_by_role("button", name="Reconnect", exact=False).count() == 0
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
    page.screenshot(path=str(tmp_path / "codex-mcp-mobile.png"))
    dialog.get_by_role("button", name="Close MCP connections").click()
    assert page.evaluate("calls") == [["list"]]
    assert not errors


def test_mcp_connections_explicit_controls_and_failure_state(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""() => {
      pane.dispose();
      controls.mcpServers=async()=>{calls.push(['mcp-list']);return {data:[{name:'local',status:'connected'}]};};
      controls.mcpServerControl=async(name,action)=>{calls.push([name,action]);if(action==='reconnect')throw Error('Connection unavailable');return {data:[{name,status:'disabled'}]};};
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider:'Claude',controls});
    }""")
    assert page.evaluate("calls") == []
    page.get_by_role("button", name="MCP connections", exact=True).click()
    dialog = page.get_by_role("dialog", name="MCP connections", exact=True)
    dialog.get_by_role("button", name="Reconnect local").click()
    dialog.get_by_text("Connection unavailable", exact=True).wait_for()
    assert dialog.get_by_role("checkbox", name="Enable local").is_checked()
    dialog.get_by_role("button", name="Refresh MCP connections").click()
    dialog.get_by_role("checkbox", name="Enable local").uncheck()
    dialog.get_by_text("disabled", exact=True).wait_for()
    assert not dialog.get_by_role("checkbox", name="Enable local").is_checked()
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
    page.screenshot(path=str(tmp_path / "mcp-connections-mobile.png"))
    dialog.get_by_role("button", name="Close MCP connections").click()
    assert page.evaluate("calls") == [["mcp-list"], ["local", "reconnect"], ["mcp-list"], ["local", "disable"]]
    assert not errors


def test_claude_task_stop_does_not_claim_completion_on_acknowledgement(pane):
    page, errors = pane
    page.evaluate("""() => {
      pane.dispose();
      controls.backgroundTasks=async()=>({data:[{processId:'claude-task',command:'Research',cwd:'/project'}]});
      controls.terminateBackgroundTask=async id=>{calls.push(['stop-task',id]);return {terminated:false,pending:true};};
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider:'Claude',controls});
    }""")
    page.get_by_role("button", name="Background tasks", exact=True).click()
    dialog = page.get_by_role("dialog", name="Background tasks", exact=True)
    dialog.get_by_role("button", name="Stop task claude-task").click()
    dialog.get_by_text("Stop requested", exact=True).wait_for()
    assert dialog.get_by_text("Research", exact=True).is_visible()
    assert dialog.get_by_role("button", name="Stop task claude-task").is_disabled()
    assert page.evaluate("calls") == [["stop-task", "claude-task"]]
    assert not errors


@pytest.mark.parametrize('provider', ['Codex', 'Claude'])
@pytest.mark.parametrize('failure', [False, True, 'malformed'])
def test_bulk_stop_requires_confirmation_and_retains_partial_failures(pane, tmp_path, provider, failure):
    page, errors = pane
    page.set_viewport_size({'width': 390, 'height': 844})
    page.evaluate("""({provider,failure}) => {
      pane.dispose();window.failStop=failure;
      controls.backgroundTasks=async()=>({data:['one','two','three'].map(processId=>({processId,command:'sleep 60',cwd:'/project'}))});
      controls.terminateBackgroundTask=async id=>{
        calls.push(['stop',id]);if(id==='two' && failStop==='malformed')return {};
        if(id==='two' && failStop)throw Error('Native stop unavailable');
        return id==='two'?{pending:true}:{terminated:true};
      };
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider,controls});
      pane.input.value='Keep draft';
    }""", {'provider': provider, 'failure': failure})
    page.get_by_role('button', name='Background tasks', exact=True).click()
    dialog = page.get_by_role('dialog', name='Background tasks', exact=True)
    dialog.get_by_text('3 running', exact=True).wait_for()
    assert page.evaluate('calls') == []
    bulk = dialog.get_by_role('button', name='Stop all listed tasks', exact=True)
    assert bulk.is_disabled()
    dialog.get_by_role('checkbox').check()
    bulk.click()
    if failure:
        message = 'Task termination was not confirmed' if failure == 'malformed' else 'Native stop unavailable'
        dialog.get_by_text(f'1 stopped; 0 pending. {message}', exact=True).wait_for()
        assert page.evaluate('calls') == [['stop', 'one'], ['stop', 'two']]
        assert dialog.get_by_role('button', name='Stop task three', exact=True).is_visible()
    else:
        dialog.get_by_text('2 stopped; 1 stop requests pending', exact=True).wait_for()
        assert page.evaluate('calls') == [['stop', 'one'], ['stop', 'two'], ['stop', 'three']]
        assert dialog.get_by_role('button', name='Stop task two', exact=True).is_disabled()
    assert bulk.is_disabled()
    assert page.evaluate('pane.input.value') == 'Keep draft'
    assert dialog.evaluate('(d)=>d.scrollWidth<=d.clientWidth')
    page.screenshot(path=str(tmp_path / 'bulk-stop-mobile.png'))
    before = page.evaluate('calls')
    dialog.get_by_role('button', name='Close background tasks').click()
    assert page.evaluate('calls') == before
    assert not errors


def test_background_tasks_explicit_refresh_stop_and_mobile_layout(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""() => {
      pane.dispose();
      controls.backgroundTasks=async()=>{calls.push(['list']);return {data:[{processId:'native-p',command:'<img src=x onerror=alert(1)> '+ 'x'.repeat(300),cwd:'/project'}]};};
      controls.terminateBackgroundTask=async id=>{calls.push(['stop',id]);return {terminated:true};};
      window.pane=new pane.constructor(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
    }""")
    assert page.evaluate("calls") == []
    page.get_by_role("button", name="Background tasks", exact=True).click()
    dialog = page.get_by_role("dialog", name="Background tasks", exact=True)
    assert dialog.locator("img").count() == 0
    assert dialog.get_by_text("1 running", exact=True).is_visible()
    assert page.evaluate(
        "document.querySelector('.aw-tasks-dialog').scrollWidth <= document.querySelector('.aw-tasks-dialog').clientWidth"
    )
    page.screenshot(path=str(tmp_path / "background-tasks-mobile.png"))
    dialog.get_by_role("button", name="Stop task native-p").click()
    assert dialog.get_by_text("Task stopped", exact=True).is_visible()
    dialog.get_by_role("button", name="Close background tasks").click()
    assert page.evaluate("calls") == [["list"], ["stop", "native-p"]]
    assert not errors


def test_command_picker_preserves_draft_and_displays_native_output(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""() => {
      controls.commands=async()=>({data:[{name:'context',description:'Inspect context usage',argumentHint:''},{name:'clear',unavailableReason:'Session switching unavailable'}]});
      pane.commandsButton.hidden=false;
      pane.input.value='existing draft';
    }""")
    page.get_by_role("button", name="Commands and skills", exact=True).click()
    dialog = page.get_by_role("dialog", name="Commands and skills", exact=True)
    assert dialog.get_by_role("button", name="Close commands").locator("svg").count() == 1
    assert dialog.get_by_role("button", name="/clear", exact=False).is_disabled()
    dialog.get_by_role("searchbox").fill("usage")
    assert dialog.locator(".aw-command").count() == 1
    page.screenshot(path=str(tmp_path / "commands-mobile.png"))
    dialog.get_by_role("button", name="/context", exact=False).click()
    assert (
        page.get_by_role("textbox", name="Message Claude").input_value()
        == "/context existing draft"
    )
    assert page.evaluate("calls") == []
    page.evaluate(
        """() => emit({method:'item/completed',params:{turnId:'t',item:{id:'result',type:'commandOutput',text:'## Context Usage\\n\\nNative result'}}})"""
    )
    page.get_by_role("heading", name="Context Usage").wait_for(state="visible")
    assert page.get_by_text("Command result", exact=True).is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


def test_command_picker_reload_is_explicit_and_keeps_draft(pane):
    page, errors = pane
    page.evaluate("""() => {
      controls.commands=async()=>({data:[{name:'old'}]});
      controls.reloadSkills=async()=>{calls.push('reload');return {data:[{name:'fresh'}]};};
      pane.commandsButton.hidden=false;pane.input.value='draft';
    }""")
    page.get_by_role("button", name="Commands and skills", exact=True).click()
    dialog = page.get_by_role("dialog", name="Commands and skills", exact=True)
    dialog.get_by_role("button", name="/old", exact=True).wait_for()
    assert page.evaluate("calls") == []
    dialog.get_by_role("button", name="Reload skills from disk", exact=True).click()
    dialog.get_by_role("button", name="/fresh", exact=True).wait_for()
    assert dialog.get_by_role("button", name="/old", exact=True).count() == 0
    assert page.evaluate("calls") == ["reload"]
    assert page.get_by_role("textbox", name="Message Claude").input_value() == "draft"
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
@pytest.mark.parametrize("provider,command", [("Claude", command) for command in ("clear", "reset", "new", "fork")] + [("Codex", "fork")])
def test_session_slash_commands_open_confirmed_action_without_sending(pane, width, command, provider):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""({command,provider}) => {
      pane.provider=provider;
      controls.clearSession=async()=>{calls.push('clear');return {session_id:'11111111-1111-4111-8111-111111111111'};};
      controls.openCleared=async()=>{};
      controls.forkSession=async()=>{calls.push('fork');return {session_id:'11111111-1111-4111-8111-111111111111',indexed:true};};
      controls.openFork=async()=>{};
      pane.clearButton.hidden=false;pane.forkButton.hidden=false;
      pane.input.value='/'+command;pane.render();
    }""", {"command": command, "provider": provider})
    page.get_by_role("button", name="Send message", exact=True).first.click()
    dialog = page.get_by_role("dialog", name="Fork conversation" if command == "fork" else "Clear context", exact=True)
    dialog.wait_for()
    assert page.evaluate("calls") == []
    assert page.evaluate("pane.input.value") == "/" + command
    dialog.get_by_role("button", name="Create fork" if command == "fork" else "Confirm clear context", exact=True).click()
    page.wait_for_function("calls.length === 1")
    assert page.evaluate("calls") == ["fork" if command == "fork" else "clear"]
    assert not errors


@pytest.mark.parametrize("command", ["fork", "review", "compact", "mcp", "permissions", "skills", "ps", "stop", "clean", "mention"])
def test_codex_local_commands_use_controls_not_model_prompts(pane, command):
    page, errors = pane
    page.evaluate("""command => {
      pane.provider='Codex';
      for(const method of ['openFork','openReview','openMcpServers','openPermissions','openCommands','openBackgroundTasks','openFileSearch'])
        pane[method]=()=>calls.push('control');
      controls.compact=async()=>calls.push('control');
      for(const control of Object.values(pane.codexCommandControls()))control.hidden=false;
      pane.input.value='/'+command;pane.render();
    }""", command)
    page.get_by_role("button", name="Send message", exact=True).first.click()
    page.wait_for_function("calls.length === 1")
    assert page.evaluate("calls") == ["control"]
    assert page.evaluate("pane.input.value") == ("" if command == "compact" else "/" + command)
    assert not errors


@pytest.mark.parametrize("command", ["fork", "review", "compact", "mcp", "permissions", "skills", "model", "reasoning", "status", "ps"])
def test_codex_unavailable_or_argument_commands_do_not_submit(pane, command):
    page, errors = pane
    page.evaluate("""command=>{pane.provider='Codex';pane.input.value='/'+command+' extra';pane.render();}""", command)
    page.get_by_role("button", name="Send message", exact=True).first.click()
    page.get_by_text("Session commands do not accept arguments, attachments or skills", exact=True).wait_for()
    page.evaluate("""command=>{pane.input.value='/'+command;pane.codexCommandControls()[command].hidden=true;}""", command)
    page.get_by_role("button", name="Send message", exact=True).first.click()
    page.get_by_text("Session action is not available right now", exact=True).wait_for()
    assert page.evaluate("calls") == []
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_codex_inline_mention_replaces_command_only_after_file_selection(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""()=>{
      pane.provider='Codex';pane.mentionButton.hidden=false;
      controls.searchFiles=async query=>{calls.push(['search',query]);return {paths:['src/with space.py']};};
      pane.input.value='/mention src';pane.render();
    }""")
    page.get_by_role('button',name='Send message',exact=True).first.click()
    dialog=page.get_by_role('dialog',name='Mention project file')
    dialog.get_by_role('button',name='src/with space.py',exact=True).wait_for()
    assert page.evaluate('pane.input.value') == '/mention src'
    dialog.get_by_role('button',name='Close file search').click()
    assert page.evaluate('pane.input.value') == '/mention src'
    page.get_by_role('button',name='Send message',exact=True).first.click()
    dialog.get_by_role('button',name='src/with space.py',exact=True).click()
    assert page.evaluate('pane.input.value') == '@"src/with space.py" '
    assert page.evaluate('calls') == [['search','src'],['search','src']]
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
@pytest.mark.parametrize("command", ["model", "reasoning"])
@pytest.mark.parametrize("from_picker", [False, True])
def test_codex_selection_commands_focus_native_control_without_sending(pane, width, command, from_picker):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""command=>{
      pane.provider='Codex';
      controls.commands=async()=>({data:[]});pane.commandsButton.hidden=false;
      pane.input.value='/'+command;pane.render();
      const control=pane.codexCommandControls()[command];
      control.hidden=false;control.disabled=false;
      control.showPicker=()=>{throw Error('No transient activation');};
    }""", command)
    if from_picker:
        page.locator('#left').get_by_role('button', name='Commands and skills', exact=True).click()
        page.get_by_role('dialog', name='Commands and skills').get_by_role('button', name=f'/{command} ', exact=False).click()
    else:
        page.locator('#left').get_by_role('button', name='Send message', exact=True).click()
    assert page.evaluate("command=>document.activeElement===pane.codexCommandControls()[command]", command)
    assert page.evaluate('calls') == []
    assert page.evaluate('pane.input.value') == '/' + command
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_codex_status_is_read_only_updates_and_does_not_invent_values(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""()=>{
      pane.provider='Codex';pane.sessionStatusButton.hidden=false;pane.input.value='/status';
      emit({method:'workspace/settings',params:{model:'actual-model',reasoningEffort:'high',approvalPolicy:'on-request',sandbox:{type:'workspaceWrite',writableRoots:['/very-long-project/'.repeat(12)]}}});
      pane.render();
    }""")
    page.locator('#left').get_by_role('button', name='Send message', exact=True).click()
    dialog = page.get_by_role('dialog', name='Session status', exact=True)
    dialog.wait_for()
    assert 'actual-model' in dialog.inner_text()
    assert 'Unavailable' in dialog.inner_text()
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    page.evaluate("""()=>emit({method:'thread/tokenUsage/updated',params:{threadId:'exact',tokenUsage:{last:{totalTokens:0},total:{totalTokens:1234},modelContextWindow:null}}})""")
    page.wait_for_function("pane.sessionStatusDialog.textContent.includes('1,234')")
    assert dialog.locator('dt', has_text='Last request tokens').evaluate('el=>el.nextElementSibling.textContent') == '0'
    page.evaluate('pane.setSleeping(true)')
    assert 'sleeping' in dialog.inner_text()
    assert page.evaluate('calls') == []
    page.keyboard.press('Escape')
    assert page.evaluate('pane.input.value') == '/status'
    playwright.expect(page.locator('#left textarea')).to_be_focused()
    page.evaluate('pane.openSessionStatus();pane.dispose()')
    assert page.get_by_role('dialog', name='Session status', exact=True).count() == 0
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_codex_limits_refresh_is_explicit_and_missing_windows_are_not_zero(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""async()=>{
      pane.dispose();window.seq=0;
      const {WorkspacePane}=await import('/workspace-pane.mjs');
      controls.accountRateLimits=async()=>{calls.push('limits');return {observedAt:'2026-09-10T12:00:00Z',limits:[{name:'Codex',primary:null,secondary:{usedPercent:57,windowDurationMins:10080,resetsAt:null}}]};};
      window.pane=new WorkspacePane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
      emit({method:'workspace/history',params:{thread:{id:'exact',turns:[]}}});
      pane.input.value='draft';pane.openSessionStatus();
    }""")
    dialog = page.get_by_role('dialog', name='Session status', exact=True)
    assert page.evaluate('calls') == []
    assert 'Not checked' in dialog.inner_text()
    dialog.get_by_role('button', name='Refresh account limits', exact=True).click()
    page.wait_for_function("pane.sessionStatusDialog.textContent.includes('57% used')")
    assert 'Codex 7d' in dialog.inner_text()
    assert '0% used' not in dialog.inner_text()
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    page.evaluate("()=>{controls.accountRateLimits=async()=>{throw Error('Not authenticated');};}")
    dialog.get_by_role('button', name='Refresh account limits', exact=True).click()
    page.get_by_text('Account limits unavailable: Not authenticated', exact=True).wait_for()
    playwright.expect(dialog.get_by_role('status')).to_be_in_viewport()
    assert 'Last checked' in dialog.inner_text() and '57% used' in dialog.inner_text()
    assert page.evaluate('pane.input.value') == 'draft'
    assert page.evaluate('calls') == ['limits']
    artifact = STATIC.parents[1] / 'apps/desktop/build/workspace-proof' / f'codex-limits-{width}.png'
    artifact.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact))
    assert not errors


@pytest.mark.parametrize('width', [390, 1600])
def test_codex_plan_picker_is_explicit_and_preserves_draft(pane, width):
    page, errors = pane
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""()=>{
      pane.provider='Codex';pane.sessionModeButton.hidden=false;
      const options=[{name:'Plan',value:'plan'},{name:'Default',value:'default'}];
      controls.sessionModes=async()=>({currentValue:null,options});
      controls.setSessionMode=async mode=>{calls.push(['mode',mode]);return {currentValue:mode,options};};
      pane.input.value='/plan';pane.render();
    }""")
    page.locator('#left').get_by_role('button', name='Send message', exact=True).click()
    dialog = page.get_by_role('dialog', name='Session mode', exact=True)
    assert dialog.get_by_role('button', name='Apply', exact=True).is_disabled()
    assert page.evaluate('calls') == []
    dialog.get_by_role('combobox').select_option('plan')
    dialog.get_by_role('button', name='Apply', exact=True).click()
    page.wait_for_function("calls.length===1")
    assert dialog.get_by_role('status').inner_text() == 'Last confirmed: Plan'
    assert page.evaluate('calls') == [['mode', 'plan']]
    assert page.evaluate('pane.input.value') == '/plan'
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    assert not errors


def test_session_command_picker_uses_local_action_and_keeps_draft(pane):
    page, errors = pane
    page.evaluate("""() => {
      controls.commands=async()=>({data:[{name:'clear',workspaceAction:'clear'}]});
      pane.commandsButton.hidden=false;
      controls.clearSession=async()=>calls.push('clear');controls.openCleared=async()=>{};
      pane.clearButton.hidden=false;pane.input.value='keep draft';pane.render();
    }""")
    page.get_by_role("button", name="Commands and skills", exact=True).first.click()
    page.get_by_role("dialog", name="Commands and skills").get_by_role("button", name="/clear", exact=True).click()
    page.get_by_role("dialog", name="Clear context", exact=True).wait_for()
    assert page.evaluate("calls") == []
    assert page.evaluate("pane.input.value") == "keep draft"
    assert not errors


def test_codex_picker_lists_local_actions_and_preserves_draft(pane):
    page, errors = pane
    page.evaluate("""() => {
      pane.provider='Codex';controls.commands=async()=>({data:[]});
      controls.forkSession=async()=>calls.push('fork');controls.openFork=async()=>{};
      pane.commandsButton.hidden=false;pane.forkButton.hidden=false;
      pane.input.value='keep my draft';pane.render();
    }""")
    page.get_by_role("button", name="Commands and skills", exact=True).first.click()
    dialog = page.get_by_role("dialog", name="Commands and skills")
    assert dialog.get_by_role("button", name="/compact", exact=False).is_disabled()
    dialog.get_by_role("button", name="/fork", exact=False).click()
    page.get_by_role("dialog", name="Fork conversation", exact=True).wait_for()
    assert page.evaluate("calls") == []
    assert page.evaluate("pane.input.value") == "keep my draft"
    assert not errors


@pytest.mark.parametrize("invalid", ["/clear extra", "/fork other"])
def test_session_command_arguments_never_reach_native_submit(pane, invalid):
    page, errors = pane
    page.evaluate("text=>{pane.input.value=text;pane.render();}", invalid)
    page.get_by_role("button", name="Send message", exact=True).first.click()
    page.get_by_text("Session commands do not accept arguments, attachments or skills", exact=True).wait_for()
    assert page.evaluate("calls") == []
    assert page.evaluate("pane.input.value") == invalid
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
@pytest.mark.parametrize("provider", ["Claude", "Codex"])
def test_clear_requires_confirmation_and_recovers_exact_target_without_repeating(pane, width, tmp_path, provider):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""provider => {
      controls.clearSession=()=>{calls.push('clear');return new Promise(resolve=>window.finishClear=()=>resolve({session_id:'11111111-1111-4111-8111-111111111111'}));};
      controls.openCleared=async sid=>calls.push(['open',sid]);pane.clearButton.hidden=false;
      const Pane=pane.constructor;pane.dispose();window.pane=new Pane(document.querySelector('#left'),{sessionId:'exact',provider,controls});
      pane.conversation.status='ready';
      pane.input.value=provider==='Codex'?'/clear':'keep original draft';pane.render();
    }""", provider)
    if provider == 'Codex':
        page.locator('#left textarea').press('Enter')
    else:
        if not page.get_by_role('button', name="Clear context", exact=True).first.is_visible():
            page.get_by_role('button', name='Session actions', exact=True).first.click()
        page.get_by_role("button", name="Clear context", exact=True).click()
    dialog = page.get_by_role("dialog", name="Clear context", exact=True)
    assert page.evaluate("calls") == []
    dialog.get_by_role("button", name="Confirm clear context", exact=True).click()
    assert page.get_by_role("button", name="Send message", exact=True).first.is_disabled()
    dialog.get_by_role("button", name="Close clear context", exact=True).click()
    assert page.locator('#left').get_by_role("button", name="Clear context", exact=True, include_hidden=True).is_disabled()
    page.evaluate("finishClear()")
    if not page.get_by_role('button', name="Clear context", exact=True).first.is_visible():
        page.get_by_role('button', name='Session actions', exact=True).first.click()
    page.get_by_role("button", name="Clear context", exact=True).click()
    dialog = page.get_by_role("dialog", name="Clear context", exact=True)
    dialog.get_by_text("Context cleared", exact=True).wait_for()
    assert page.evaluate("calls") == ["clear"]
    assert page.evaluate("pane.input.value") == ('/clear' if provider == 'Codex' else 'keep original draft')
    assert page.get_by_role("button", name="Send message", exact=True).first.is_disabled()
    assert page.locator("body").evaluate("el=>el.scrollWidth<=innerWidth")
    page.screenshot(path=str(tmp_path / f"clear-{width}.png"))
    dialog.get_by_role("button", name="Open new conversation", exact=True).click()
    assert page.evaluate("calls") == ["clear", ["open", "11111111-1111-4111-8111-111111111111"]]
    assert not errors


@pytest.mark.parametrize("indexed", [True, False])
@pytest.mark.parametrize("provider", ["Claude", "Codex"])
def test_fork_dialog_never_creates_or_opens_automatically(pane, tmp_path, indexed, provider):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""indexed => {
      controls.forkSession=async()=>{calls.push('fork');return {session_id:'11111111-1111-4111-8111-111111111111',indexed,error:'Catalog unavailable'};};
      controls.openFork=async sid=>calls.push(['open',sid]);pane.forkButton.hidden=false;
      pane.input.value='draft stays';
    }""", indexed)
    if provider == "Codex":
        page.evaluate("""async()=>{
          const {WorkspacePane}=await import('/workspace-pane.mjs');
          pane.dispose();pane=new WorkspacePane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
          pane.conversation.status='ready';pane.render();pane.input.value='draft stays';
        }""")
    if not page.get_by_role('button', name="Fork conversation", exact=True).first.is_visible():
        page.get_by_role('button', name='Session actions', exact=True).first.click()
    page.get_by_role("button", name="Fork conversation", exact=True).click()
    dialog = page.get_by_role("dialog", name="Fork conversation", exact=True)
    assert page.evaluate("calls") == []
    dialog.get_by_role("button", name="Create fork", exact=True).click()
    dialog.get_by_text("11111111-1111-4111-8111-111111111111", exact=True).wait_for()
    assert page.evaluate("calls") == ["fork"]
    assert dialog.evaluate("el=>el.scrollWidth<=el.clientWidth")
    page.screenshot(path=str(tmp_path / f"fork-mobile-{indexed}.png"))
    if indexed:
        dialog.get_by_role("button", name="Open fork", exact=True).click()
        assert page.evaluate("calls") == ["fork", ["open", "11111111-1111-4111-8111-111111111111"]]
    else:
        assert dialog.get_by_role("button", name="Open fork", exact=True).count() == 0
        assert dialog.get_by_text("Catalog unavailable", exact=True).is_visible()
        dialog.get_by_role("button", name="Close fork", exact=True).click()
    assert page.get_by_role("textbox", name=f"Message {provider}").input_value() == "draft stays"
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
def test_shell_dialog_requires_explicit_confirmation_and_keeps_chat_draft(pane, width):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""async()=>{
      const {WorkspacePane}=await import('/workspace-pane.mjs');
      controls.shellCommand=async(command,confirmed)=>calls.push([command,confirmed]);
      pane.dispose();pane=new WorkspacePane(document.querySelector('#left'),{sessionId:'exact',provider:'Codex',controls});
      pane.conversation.status='ready';pane.render();pane.input.value='unsent draft';
    }""")
    if not page.get_by_role('button', name="Run shell command", exact=True).first.is_visible():
        page.get_by_role('button', name='Session actions', exact=True).first.click()
    page.get_by_role('button', name='Run shell command', exact=True).click()
    dialog = page.get_by_role('dialog', name='Run shell command')
    assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
    dialog.get_by_role('textbox', name='Shell command').fill('printf hello')
    dialog.get_by_role('button', name='Run command', exact=True).click()
    assert page.evaluate('calls') == []
    dialog.get_by_role('checkbox').check()
    dialog.get_by_role('button', name='Run command', exact=True).click()
    assert page.evaluate('calls') == [['printf hello', True]]
    assert page.locator('#left').get_by_role('textbox', name='Message Codex').input_value() == 'unsent draft'
    assert not errors


def test_native_older_history_is_explicit_and_preserves_scroll(pane):
    page, errors = pane
    page.evaluate("""()=>{
      controls.loadEarlier=async cursor=>calls.push(['older',cursor]);
      emit({method:'workspace/history',params:{historyCursor:'page-2',thread:{id:'exact',turns:[{id:'recent',status:'completed',items:Array.from({length:20},(_,i)=>({id:'recent-'+i,type:'agentMessage',text:'Recent message '+i+' '.repeat(3)+'content '.repeat(30)}))}]}}});
      pane.render();pane.log.scrollTop=0;
    }""")
    button = page.get_by_role("button", name="Load earlier messages")
    assert button.is_visible() and page.evaluate("calls") == []
    button.click()
    assert page.evaluate("calls") == [["older", "page-2"]]
    # Delivery is deliberately later than the command acknowledgement.
    page.evaluate("""()=>{
      window.anchor=pane.rendered.get(JSON.stringify(['recent','recent-0'])).element;
      window.anchorY=anchor.getBoundingClientRect().top;
      emit({method:'workspace/historyPage',params:{threadId:'exact',historyCursor:null,turns:[{id:'old',status:'completed',items:[{id:'old-0',type:'agentMessage',text:'The oldest real message.'}]}]}});
    }""")
    assert abs(page.evaluate("anchor.getBoundingClientRect().top-anchorY")) < 2
    assert page.get_by_text("The oldest real message.", exact=True).count() == 1
    assert button.is_hidden()
    assert not errors


def test_closing_pending_fork_cannot_start_second_creation(pane):
    page, errors = pane
    page.evaluate("""() => {
      controls.forkSession=()=>{calls.push('fork');return new Promise(resolve=>{window.finishFork=()=>resolve({session_id:'11111111-1111-4111-8111-111111111111',indexed:true});});};
      controls.openFork=()=>{};pane.forkButton.hidden=false;
    }""")
    if not page.get_by_role('button', name="Fork conversation", exact=True).first.is_visible():
        page.get_by_role('button', name='Session actions', exact=True).first.click()
    page.get_by_role("button", name="Fork conversation", exact=True).click()
    page.get_by_role("button", name="Create fork", exact=True).click()
    page.get_by_role("button", name="Close fork", exact=True).click()
    assert page.locator('#left').get_by_role("button", name="Fork conversation", exact=True, include_hidden=True).is_disabled()
    page.evaluate("pane.openFork()")
    assert page.get_by_role("dialog", name="Fork conversation", exact=True).count() == 0
    page.evaluate("finishFork()")
    page.wait_for_function("!pane.forkButton.disabled")
    if not page.get_by_role('button', name="Fork conversation", exact=True).first.is_visible():
        page.get_by_role('button', name='Session actions', exact=True).first.click()
    page.get_by_role("button", name="Fork conversation", exact=True).click()
    page.get_by_role("button", name="Open fork", exact=True).wait_for()
    assert page.evaluate("calls") == ["fork"]
    assert not errors


def test_saved_fork_recovery_never_recreates_session(pane):
    page, errors = pane
    page.evaluate("""() => {
      const saved={session_id:'11111111-1111-4111-8111-111111111111',request_id:'original',indexed:false,error:'Catalog unavailable'};
      controls.lastFork=()=>saved;controls.recoverFork=async id=>{calls.push(['recover',id]);return {...saved,indexed:true};};
      controls.forkSession=async()=>{throw Error('Must not recreate fork');};controls.openFork=()=>{};
      pane.forkButton.hidden=false;
    }""")
    if not page.get_by_role('button', name="Fork conversation", exact=True).first.is_visible():
        page.get_by_role('button', name='Session actions', exact=True).first.click()
    page.get_by_role("button", name="Fork conversation", exact=True).click()
    dialog = page.get_by_role("dialog", name="Fork conversation", exact=True)
    assert dialog.get_by_text("Catalog unavailable", exact=True).is_visible()
    assert page.evaluate("calls") == []
    dialog.get_by_role("button", name="Retry fork registration", exact=True).click()
    dialog.get_by_role("button", name="Open fork", exact=True).wait_for()
    assert page.evaluate("calls") == [["recover", "original"]]
    assert not errors


def test_permission_prompt_defaults_to_no_grants_and_exact_selected_scope(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate(
        """() => emit({id:91,method:'item/permissions/requestApproval',params:{threadId:'exact',cwd:'/project',permissions:{network:{enabled:true},fileSystem:{read:['/project/private']}},reason:'Need selected access'}})"""
    )
    field = page.get_by_role("checkbox", name="Network access", exact=True)
    field.wait_for()
    assert not field.is_checked()
    assert not page.get_by_role("checkbox", name="File access", exact=True).is_checked()
    assert page.get_by_role("combobox", name="Grant duration").input_value() == "turn"
    assert page.evaluate("calls") == []
    field.check()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / "permissions-mobile.png"))
    page.get_by_role("combobox", name="Grant duration").select_option("session")
    page.get_by_role("button", name="Grant selected", exact=True).click()
    assert page.evaluate("calls") == [
        ["answer", 91, {"permissions": {"network": {"enabled": True}}, "scope": "session"}]
    ]
    assert page.get_by_role("combobox", name="Grant duration").is_disabled()
    assert page.get_by_text("Need selected access", exact=True).is_visible()
    page.evaluate("""() => emit({method:'serverRequest/resolved',params:{requestId:91}})""")
    page.get_by_text("Need selected access", exact=True).wait_for(state="hidden")
    assert not errors


def test_bridge_queue_count_tracks_native_host_events(pane):
    page, errors = pane
    page.evaluate(
        """() => emit({method:'workspace/bridgeQueue',params:{threadId:'exact',count:2}})"""
    )
    page.wait_for_function(
        "document.querySelector('#left .aw-state').textContent.includes('2 queued')"
    )
    page.evaluate(
        """() => emit({method:'workspace/bridgeQueue',params:{threadId:'exact',count:0}})"""
    )
    page.wait_for_function(
        "!document.querySelector('#left .aw-state').textContent.includes('queued')"
    )
    assert page.evaluate("calls") == []
    assert not errors


def test_queue_dialog_cancels_only_selected_request_and_waits_for_host(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""() => {
      controls.cancelQueuedBridge=async id=>calls.push(['cancel',id]);
      emit({method:'workspace/bridgeQueue',params:{threadId:'exact',count:1,requests:[{id:'queued-1',prompt:'<img src=x onerror=alert(1)> Please review the change.'}]}});
    }""")
    page.get_by_role("button", name="Queued sibling messages", exact=True).click()
    dialog = page.get_by_role("dialog", name="Queued sibling messages", exact=True)
    assert dialog.locator("img").count() == 0
    assert page.evaluate("calls") == []
    page.screenshot(path=str(tmp_path / "queued-message-mobile.png"))
    dialog.get_by_role("button", name="Cancel queued message queued-1").click()
    assert page.evaluate("calls") == [["cancel", "queued-1"]]
    assert dialog.get_by_role("button", name="Cancel queued message queued-1").is_disabled()
    page.evaluate(
        """() => emit({method:'workspace/bridgeQueue',params:{threadId:'exact',count:0,requests:[]}})"""
    )
    dialog.get_by_text("No queued messages", exact=True).wait_for()
    dialog.get_by_role("button", name="Close queue").click()
    assert page.evaluate("calls") == [["cancel", "queued-1"]]
    assert not errors


def test_mcp_form_collects_typed_fields_and_safe_url(pane):
    page, errors = pane
    page.evaluate(
        """() => emit({id:88,method:'mcpServer/elicitation/request',params:{threadId:'exact',mode:'form',serverName:'Planner',message:'Choose settings',requestedSchema:{type:'object',properties:{name:{type:'string'},count:{type:'integer',minimum:1},enabled:{type:'boolean'}},required:['name','count']}}})"""
    )
    page.get_by_role("textbox", name="name", exact=True).fill("Ada")
    page.get_by_role("spinbutton", name="count", exact=True).fill("3")
    page.get_by_role("checkbox", name="enabled", exact=True).check()
    page.get_by_role("button", name="Submit", exact=True).click()
    page.wait_for_function("calls.length === 1")
    assert page.evaluate("calls[0]") == [
        "answer",
        88,
        {"action": "accept", "content": {"name": "Ada", "count": 3, "enabled": True}},
    ]
    page.evaluate(
        """() => {emit({method:'serverRequest/resolved',params:{requestId:88}});emit({id:89,method:'mcpServer/elicitation/request',params:{mode:'url',url:'javascript:alert(1)',message:'Unsafe'}});} """
    )
    page.get_by_role("button", name="Confirm completion").wait_for()
    assert page.get_by_role("button", name="Confirm completion").is_disabled()
    assert page.locator(".aw-question a").count() == 0
    page.get_by_role("button", name="Decline", exact=True).click()
    assert not errors


def test_mcp_nullable_defaults_render_without_inventing_optional_answers(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""() => emit({id:90,method:'mcpServer/elicitation/request',params:{
      threadId:'exact',mode:'form',serverName:'Planner',message:'Choose settings',
      requestedSchema:{type:'object',required:null,properties:{
        enabled:{type:'boolean',default:null},
        colors:{type:'array',default:null,items:{type:'string',enum:['pink','green']}},
        name:{type:'string',default:null}
      }}
    }})""")
    assert not page.get_by_role("checkbox", name="enabled").is_checked()
    assert page.get_by_role("textbox", name="name", exact=True).input_value() == ""
    page.screenshot(path=str(tmp_path / "mcp-form-mobile.png"))
    page.get_by_role("button", name="Submit", exact=True).click()
    page.wait_for_function("calls.length === 1")
    assert page.evaluate("calls[0]") == ["answer", 90, {"action": "accept", "content": {}}]
    assert not errors


def test_mcp_tool_permission_shows_arguments_without_executing_markup(pane):
    page, errors = pane
    page.evaluate("""() => emit({id:91,method:'mcpServer/elicitation/request',params:{
      threadId:'exact',mode:'form',serverName:'Planner',message:'Allow tool?',
      _meta:{codex_approval_kind:'mcp_tool_call',tool_params:{text:'<img src=x onerror=alert(1)>'}},
      requestedSchema:{type:'object',properties:{}}
    }})""")
    page.get_by_text("Tool arguments", exact=True).click()
    assert "<img" in page.locator(".aw-question pre").inner_text()
    assert page.locator(".aw-question img").count() == 0
    assert page.evaluate("calls") == []
    page.get_by_role("button", name="Submit", exact=True).click()
    page.wait_for_function("calls.length === 1")
    assert page.evaluate("calls[0]") == ["answer", 91, {"action": "accept", "content": {}}]
    assert not errors


def test_real_items_tool_expansion_and_injection_safety(pane, tmp_path):
    page, errors = pane
    assert page.locator("#left .aw-item").count() == 4
    assert page.locator("#left .aw-message").inner_text().endswith("<img src=x onerror=alert(1)>")
    assert page.locator("#left .aw-message img").count() == 0
    page.locator("#left .aw-tool summary").click()
    assert page.get_by_text("3 passed", exact=True).is_visible()
    assert page.get_by_text("Exit 0", exact=True).is_visible()
    assert page.locator(".aw-add").inner_text().strip() == "+new_value"
    page.screenshot(path=str(tmp_path / "rich-workspace-desktop.png"))
    print("Screenshot:", tmp_path / "rich-workspace-desktop.png")
    assert not errors


def test_pasted_image_drop_preview_and_mobile_cleanup(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""async () => {
      const canvas=document.createElement('canvas');canvas.width=8;canvas.height=8;
      canvas.getContext('2d').fillRect(0,0,8,8);
      const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
      const clipboard=new DataTransfer();clipboard.items.add(new File([blob],'screenshot.png',{type:'image/png'}));
      clipboard.setData('text/plain','pasted text');
      pane.input.dispatchEvent(new ClipboardEvent('paste',{clipboardData:clipboard,bubbles:true,cancelable:true}));
      const dropped=new DataTransfer();dropped.items.add(new File(['document'],'notes.txt',{type:'text/plain'}));
      pane.form.dispatchEvent(new DragEvent('drop',{dataTransfer:dropped,bubbles:true,cancelable:true}));
    }""")
    assert page.get_by_role("textbox", name="Message Claude").input_value() == "pasted text"
    page.wait_for_function("document.querySelector('.aw-attachment img').naturalWidth === 8")
    assert page.get_by_role("button", name="Remove notes.txt").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / "workspace-mobile-upload.png"))
    page.get_by_role("button", name="Remove screenshot.png").click()
    assert page.locator(".aw-attachment img").count() == 0
    assert page.evaluate("pane.previews.size") == 0
    page.evaluate("pane.dispose()")
    assert not errors


def test_advertised_model_effort_selection_reaches_submit_and_header(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""() => {
      window.optionsSent=null;
      controls.submit=async value=>{window.optionsSent=value.options;};
      emit({method:'workspace/models',params:{settings:{model:'configured',reasoningEffort:'medium'},data:[
        {model:'configured',displayName:'Configured',supportedReasoningEfforts:[{reasoningEffort:'medium'}],defaultReasoningEffort:'medium'},
        {model:'chosen',displayName:'Provider advertised model',supportedReasoningEfforts:[{reasoningEffort:'low'},{reasoningEffort:'xhigh'}],defaultReasoningEffort:'low',serviceTiers:[{id:'fast',name:'Fast',description:'Provider speed option'}]}
      ]}});
    }""")
    page.get_by_role("combobox", name="Model", exact=True).first.select_option("chosen")
    effort = page.get_by_role("combobox", name="Reasoning effort").first
    assert effort.input_value() == "low"
    assert effort.locator("option").all_text_contents() == ["Default (low)", "low", "xhigh"]
    effort.select_option("xhigh")
    page.get_by_role("combobox", name="Speed tier").first.select_option("fast")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / "workspace-mobile-model-controls.png"))
    page.get_by_role("textbox", name="Message Claude").fill("selected model")
    page.get_by_role("button", name="Send message", exact=True).first.click()
    page.wait_for_function("window.optionsSent !== null")
    assert page.evaluate("window.optionsSent") == {
        "model": "chosen",
        "effort": "xhigh",
        "serviceTier": "fast",
    }
    page.evaluate(
        "emit({method:'workspace/settings',params:{model:'chosen',reasoningEffort:'xhigh'}})"
    )
    page.locator("#left .aw-head small").filter(has_text="chosen").wait_for()
    page.get_by_role("combobox", name="Model", exact=True).first.select_option("")
    assert page.get_by_role("combobox", name="Reasoning effort", include_hidden=True).first.input_value() == ""
    assert page.get_by_role("combobox", name="Reasoning effort", include_hidden=True).first.is_hidden()
    assert page.get_by_role("combobox", name="Speed tier", include_hidden=True).first.input_value() == ""
    assert not errors


def test_claude_permission_is_explicit_and_waits_for_resolution(pane):
    page, errors = pane
    page.evaluate(
        "emit({id:'claude-q',method:'workspace/claudeApproval',params:{threadId:'exact',tool:'Bash',title:'Run proposed command?',input:{command:'echo hi'}}})"
    )
    page.get_by_text("Run proposed command?", exact=True).wait_for()
    assert page.evaluate("calls.length") == 0
    page.get_by_role("button", name="Allow once", exact=True).click()
    page.wait_for_function("calls.length === 1")
    assert page.evaluate("calls[0]") == ["answer", "claude-q", {"decision": "allow"}]
    assert page.get_by_text("Run proposed command?", exact=True).is_visible()
    page.evaluate("emit({method:'serverRequest/resolved',params:{requestId:'claude-q'}})")
    page.get_by_text("Run proposed command?", exact=True).wait_for(state="hidden")
    assert not errors


def test_codex_running_composer_steers_exact_turn(pane):
    page, errors = pane
    page.evaluate("""pane.provider='Codex'; controls.steer=async value=>calls.push(['steer',value]);
      emit({method:'turn/started',params:{threadId:'exact',turn:{id:'running-1',status:'inProgress'}}});""")
    page.get_by_role("button", name="Steer running turn").wait_for()
    page.locator("#left textarea").fill("use the other file")
    page.get_by_role("button", name="Steer running turn").click()
    page.wait_for_function("calls.length === 1")
    assert page.evaluate("calls[0]") == [
        "steer",
        {"text": "use the other file", "files": [], "expectedTurnId": "running-1"},
    ]
    assert not errors


def test_draft_reload_is_session_scoped_and_never_sends(pane):
    page, errors = pane
    page.locator("#left textarea").fill("unsent Claude text\nsecond line")
    page.locator("#right textarea").fill("different Codex draft")
    assert page.evaluate("calls.length") == 0
    page.reload()
    page.wait_for_function("window.pane && pane.conversation.sequence === 1")
    assert page.locator("#left textarea").input_value() == "unsent Claude text\nsecond line"
    assert page.locator("#right textarea").input_value() == "different Codex draft"
    assert page.evaluate("calls.length") == 0
    page.locator("#left").get_by_role("button", name="Send message", exact=True).click()
    page.wait_for_function("calls.length === 1")
    page.wait_for_function("document.querySelector('#left textarea').value === ''")
    page.reload()
    page.wait_for_function("window.pane && pane.conversation.sequence === 1")
    assert page.locator("#left textarea").input_value() == ""
    assert page.locator("#right textarea").input_value() == "different Codex draft"
    assert page.evaluate("calls.length") == 0
    assert not errors


def test_failed_send_keeps_draft_after_reload(pane):
    page, errors = pane
    page.locator("#left textarea").fill("keep after failure")
    page.evaluate("window.failSubmit=true")
    page.locator("#left").get_by_role("button", name="Send message", exact=True).click()
    page.get_by_role("alert").filter(has_text="Submission unconfirmed").wait_for()
    page.reload()
    page.wait_for_function("window.pane && pane.conversation.sequence === 1")
    assert page.locator("#left textarea").input_value() == "keep after failure"
    assert page.evaluate("calls.length") == 0
    assert not errors


def test_native_usage_and_completed_duration_are_not_invented(pane):
    page, errors = pane
    assert page.locator("#left .aw-usage").inner_text() == ""
    assert page.locator("#left .aw-turn-summary").count() == 0
    page.evaluate("""emit({method:'thread/tokenUsage/updated',params:{threadId:'exact',tokenUsage:{last:{totalTokens:1234},total:{totalTokens:99999}}}});
      emit({method:'turn/completed',params:{threadId:'exact',turn:{id:'t',status:'completed',durationMs:42000}}});""")
    page.get_by_text("Last request: 1,234 tokens", exact=True).wait_for()
    page.get_by_text("Worked for 42s", exact=True).wait_for()
    page.evaluate(
        "emit({method:'turn/completed',params:{threadId:'exact',turn:{id:'t',status:'failed',durationMs:65000}}})"
    )
    page.get_by_text("Failed after 1m 5s", exact=True).wait_for()
    assert page.locator("#left .aw-turn-summary").count() == 1
    assert not errors


def test_long_history_mounts_recent_items_and_preserves_reader_position(pane):
    page, errors = pane
    page.evaluate(
        "emit({method:'workspace/history',params:{thread:{id:'exact',turns:[{id:'long',status:'completed',items:Array.from({length:350},(_,i)=>({id:'item-'+i,type:'agentMessage',text:'Message '+i}))}]}}})"
    )
    page.wait_for_function("pane.rendered.size === 100")
    assert page.locator("#left [data-item-id='item-349']").count() == 1
    assert page.locator("#left [data-item-id='item-0']").count() == 0
    assert page.evaluate("pane.conversation.turns.get('long').items.size") == 350
    page.evaluate("pane.log.scrollTop=0")
    page.wait_for_function("pane.rendered.size >= 200")
    page.evaluate("pane.log.scrollTop=300")
    page.wait_for_timeout(50)
    before = page.evaluate("pane.log.scrollTop")
    page.evaluate(
        "emit({method:'item/completed',params:{threadId:'exact',turnId:'long',item:{id:'new',type:'agentMessage',text:'New arrival'}}})"
    )
    page.wait_for_function("pane.rendered.has(JSON.stringify(['long','new']))")
    assert abs(page.evaluate("pane.log.scrollTop") - before) < 2
    assert page.evaluate("pane.conversation.turns.get('long').items.size") == 351
    assert not errors


def test_review_dialog_routes_explicit_target_without_submitting_message(pane):
    page, errors = pane
    page.evaluate("""() => {
      controls.review=async target=>calls.push(['review',target]);
      right.reviewButton.hidden=false;
      right.receive({sequence:1,event:{method:'workspace/history',params:{thread:{id:'other',turns:[]}}}});
    }""")
    page.get_by_role("button", name="Review changes", exact=True).click()
    dialog = page.get_by_role("dialog", name="Review changes")
    dialog.get_by_label("Review target").select_option("baseBranch")
    dialog.get_by_label("Branch", exact=True).fill("main")
    dialog.get_by_role("button", name="Start review", exact=True).click()
    page.wait_for_function("calls.length === 1")
    assert page.evaluate("calls[0]") == ["review", {"type": "baseBranch", "branch": "main"}]
    dialog.wait_for(state="hidden")
    page.evaluate(
        "right.receive({sequence:2,event:{method:'item/completed',params:{threadId:'other',turnId:'review-turn',item:{id:'review-result',type:'exitedReviewMode',review:'**No findings**'}}}})"
    )
    page.locator("#right .aw-message strong").filter(has_text="No findings").wait_for()
    assert not errors


def test_compact_command_is_native_and_waits_for_provider_completion(pane):
    page, errors = pane
    page.evaluate("""() => {
      pane.provider='Codex';pane.compactButton.hidden=false;controls.compact=async()=>{
        calls.push(['compact']);emit({method:'workspace/activity',params:{threadId:'exact',status:'compacting'}});
      };
    }""")
    page.locator("#left textarea").fill("/compact")
    page.locator("#left").get_by_role("button", name="Send message", exact=True).click()
    page.wait_for_function("calls.length===1")
    assert page.evaluate("calls") == [["compact"]]
    assert (
        page.locator("#left").get_by_role("button", name="Send message", exact=True).is_disabled()
    )
    page.evaluate("""emit({method:'item/completed',params:{threadId:'exact',turnId:'c',item:{id:'compaction',type:'contextCompaction'}}});
      emit({method:'turn/completed',params:{threadId:'exact',turn:{id:'c',status:'completed'}}});""")
    page.get_by_text("Context compaction", exact=True).wait_for()
    page.wait_for_function("!pane.send.disabled")
    assert not errors


@pytest.mark.parametrize("mime,data", [("image/svg+xml", "PHN2Zz48L3N2Zz4="), ("image/png", "broken!"), ("image/png", "bm90IGEgcG5n")])
def test_assistant_image_rejects_unsupported_or_invalid_data(pane, mime, data):
    page, errors = pane
    page.evaluate("([mime,data]) => emit({method:'item/completed',params:{threadId:'exact',turnId:'t',item:{id:'invalid-image',type:'agentMessage',content:[{type:'image',source:{type:'base64',media_type:mime,data}}]}}})", [mime, data])
    page.get_by_text("Assistant image unavailable", exact=True).wait_for()
    assert page.locator('[data-item-id="invalid-image"] img').count() == 0
    assert page.evaluate('pane.historyImageUrls.size') == 0
    assert page.evaluate('calls') == [] and not errors


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini", "gemini-agent", "gemini-tool"])
@pytest.mark.parametrize("width", [390, 1600])
def test_history_image_renders_without_base64_text(pane, provider, width, tmp_path):
    import base64
    import io

    from PIL import Image

    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    image = io.BytesIO()
    Image.new("RGB", (800, 400), "green").save(image, format="PNG")
    data = base64.b64encode(image.getvalue()).decode()
    if provider == "codex":
        page.evaluate(
            "data => {controls.image=async token=>{window.imageToken=token;return new Blob([Uint8Array.from(atob(data),c=>c.charCodeAt(0))],{type:'image/png'});};}",
            data,
        )
        page.evaluate(
            "emit({method:'item/completed',params:{threadId:'exact',turnId:'t',item:{id:'photo',type:'userMessage',content:[{type:'localImage',previewToken:'owned-token',path:'/not-rendered.png'}]}}})"
        )
    elif provider.startswith("gemini"):
        from core.workspace_acp_events import AcpEvents

        events = AcpEvents("exact")
        events.begin("t")
        content = {"type": "image", "mimeType": "image/png", "data": data}
        update = {"sessionUpdate": "agent_message_chunk" if provider == "gemini-agent" else "user_message_chunk", "content": content}
        if provider == "gemini-tool":
            update = {"sessionUpdate": "tool_call", "toolCallId": "image", "title": "Image result", "status": "completed",
                      "content": [{"type": "content", "content": content}]}
        event = events.update({"sessionId": "exact", "update": update})
        event["params"]["item"]["id"] = "photo"
        page.evaluate("event => emit(event)", event)
        if provider == "gemini-tool":
            page.get_by_text("Image result", exact=True).click()
    else:
        page.evaluate(
            "data => emit({method:'item/completed',params:{threadId:'exact',turnId:'t',item:{id:'photo',type:'userMessage',content:[{type:'image',source:{type:'base64',media_type:'image/png',data}}]}}})",
            data,
        )
    page.wait_for_function("document.querySelector('.aw-history-image')?.naturalWidth === 800")
    assert data not in page.locator("#left").inner_text()
    assert page.locator(".aw-history-image").get_attribute("src").startswith("blob:")
    assert page.evaluate("pane.historyImageUrls.size") == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator('.aw-history-image').evaluate("image => {const c=document.createElement('canvas');c.width=c.height=8;const ctx=c.getContext('2d');ctx.drawImage(image,0,0);return [...ctx.getImageData(0,0,1,1).data]}") == [0, 128, 0, 255]
    page.screenshot(path=str(tmp_path / f"{provider}-{width}.png"))
    page.evaluate("pane.input.value='draft survives image inspection'")
    trigger = page.locator('.aw-image-trigger')
    trigger.focus()
    page.keyboard.press('Enter')
    dialog = page.get_by_role('dialog', name='Image viewer')
    dialog.wait_for()
    page.wait_for_function("document.querySelector('.aw-image-dialog img')?.naturalWidth === 800")
    assert page.evaluate('pane.historyImageUrls.size') == 1
    dialog.get_by_role('button', name='Actual size', exact=True).click()
    assert dialog.get_by_role('button', name='Actual size', exact=True).get_attribute('aria-pressed') == 'true'
    assert dialog.locator('img').evaluate('image=>image.getBoundingClientRect().width') == 800
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f"{provider}-{width}-viewer.png"))
    page.keyboard.press('Escape')
    dialog.wait_for(state='hidden')
    assert trigger.evaluate('button=>document.activeElement===button')
    assert page.evaluate('pane.input.value') == 'draft survives image inspection'
    trigger.click()
    dialog.wait_for()
    page.evaluate(
        "emit({method:'item/completed',params:{threadId:'exact',turnId:'t',item:{id:'photo',type:'userMessage',content:[{type:'text',text:'image replaced'}]}}})"
    )
    page.wait_for_function("pane.historyImageUrls.size === 0")
    dialog.wait_for(state='hidden')
    page.evaluate("pane.dispose()")
    assert page.evaluate("pane.historyImageUrls.size") == 0
    assert not errors


def test_markdown_code_copy_and_mobile_layout(pane, tmp_path):
    page, errors = pane
    page.evaluate("""() => {
      Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copied=text;}}});
      emit({method:'item/completed',params:{threadId:'exact',turnId:'t',item:{id:'a',type:'agentMessage',text:
        '## Result\\n\\n**Ready** with [documentation](https://example.com).\\n\\n- one\\n- two\\n\\n```js\\nconst value = 1;\\n```\\n\\n| Check | Result |\\n| --- | --- |\\n| Unit | Passed |\\n\\n![remote](https://example.com/track.png)'
      }}});
    }""")
    page.get_by_role("heading", name="Result").wait_for()
    assert (
        page.get_by_role("link", name="documentation").get_attribute("rel") == "noopener noreferrer"
    )
    assert page.locator(".aw-message img").count() == 0
    page.get_by_role("button", name="Copy code", exact=True).click()
    page.wait_for_function("window.copied === 'const value = 1;\\n'")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "markdown-mobile.png"))
    assert not errors


def test_confirmed_send_does_not_erase_newer_draft(pane):
    page, errors = pane
    page.evaluate("() => { controls.submit=()=>new Promise(resolve=>window.finishSend=resolve); }")
    page.locator("#left textarea").fill("sending now")
    page.locator("#left").get_by_role("button", name="Send message", exact=True).click()
    page.wait_for_function("typeof window.finishSend === 'function'")
    page.locator("#left textarea").fill("next thought")
    page.evaluate("window.finishSend({})")
    page.wait_for_function("!pane.sending")
    page.reload()
    page.wait_for_function("window.pane && pane.conversation.sequence === 1")
    assert page.locator("#left textarea").input_value() == "next thought"
    assert not errors


def test_claude_questions_send_selected_and_custom_answers(pane, tmp_path):
    page, errors = pane
    page.evaluate("""emit({id:'ask',method:'workspace/claudeApproval',params:{threadId:'exact',tool:'AskUserQuestion',input:{questions:[
      {question:'Which database?',multiSelect:false,options:[{label:'Postgres'},{label:'SQLite'}]},
      {question:'Which checks?',multiSelect:true,options:[{label:'Unit'},{label:'Browser'}]}
    ]}}})""")
    page.get_by_role("radio", name="Postgres", exact=True).check()
    page.get_by_role("checkbox", name="Unit", exact=True).check()
    page.get_by_role("checkbox", name="Browser", exact=True).check()
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(tmp_path / "claude-question-mobile.png"))
    page.get_by_role("group", name="Which database?").get_by_label("Custom answer").fill("MariaDB")
    page.get_by_role("button", name="Answer", exact=True).click()
    page.wait_for_function("calls.length === 1")
    assert page.evaluate("calls[0]") == [
        "answer",
        "ask",
        {
            "answers": {
                "Which database?": "MariaDB",
                "Which checks?": "Unit, Browser",
            }
        },
    ]
    assert page.get_by_role("group", name="Which database?").is_visible()
    page.evaluate("emit({method:'serverRequest/resolved',params:{requestId:'ask'}})")
    page.get_by_role("group", name="Which database?").wait_for(state="hidden")
    assert not errors


def test_composer_upload_failure_retains_draft_and_closing_does_not_cancel(pane):
    page, errors = pane
    composer = page.get_by_role("textbox", name="Message Claude")
    composer.fill("first line")
    composer.press("End")
    composer.press("Shift+Enter")
    composer.press("x")
    assert composer.input_value() == "first line\nx"
    page.locator("#left input[type=file]").set_input_files(
        {"name": "photo.png", "mimeType": "image/png", "buffer": b"upload-test"}
    )
    page.evaluate("window.failSubmit=true")
    page.get_by_role("button", name="Send message", exact=True).first.click()
    page.get_by_role("alert").filter(has_text="Submission unconfirmed").wait_for()
    assert composer.input_value() == "first line\nx"
    assert page.get_by_role("button", name="Remove photo.png").is_visible()
    assert page.evaluate("window.calls[0]") == [
        "submit",
        {"text": "first line\nx", "files": ["photo.png"]},
    ]
    page.evaluate("window.failSubmit=false")
    page.get_by_role("button", name="Send message", exact=True).first.click()
    page.wait_for_function("pane.input.value === ''")
    page.evaluate("pane.dispose()")
    assert page.evaluate("window.calls.map(x=>x[0])") == ["submit", "submit"]
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
@pytest.mark.parametrize("failure", [False, True])
def test_claude_busy_composer_queues_exact_turn_and_preserves_failed_draft(pane, width, failure):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("""failure => {
      controls.queueInput=async message=>{calls.push(['queue',{...message,files:message.files.map(file=>file.name)}]);if(failure)throw Error('Queue receipt unconfirmed');};
      emit({method:'turn/started',params:{turn:{id:'active',status:'inProgress'}}});
    }""", failure)
    page.get_by_role("textbox", name="Message Claude").fill("follow-up")
    page.get_by_role("button", name="Queue message", exact=True).click()
    page.wait_for_function("!pane.sending")
    assert page.evaluate("calls") == [["queue", {"text": "follow-up", "files": [], "expectedTurnId": "active"}]]
    assert page.get_by_role("textbox", name="Message Claude").input_value() == ("follow-up" if failure else "")
    assert page.evaluate("pane.modelSelect.disabled")
    if failure:
        assert page.get_by_text("Queue receipt unconfirmed", exact=True).is_visible()
    assert not errors


@pytest.mark.parametrize("width", [390, 1600])
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("newer", [False, True])
def test_explicit_queue_recovery_preserves_newer_draft(pane, width, failure, newer, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("""failure => {
      window.pendingQueue=[{requestId:'original-id',payload:{inputs:[{type:'text',text:'original'}],expectedTurnId:'old'}}];
      controls.pendingQueuedInputs=()=>structuredClone(pendingQueue);
      controls.retryQueuedInput=async id=>{calls.push(['retry',id]);if(failure)throw Error('Receipt still unconfirmed');pendingQueue=[];return {turn:{id:'confirmed'}};};
      pane.render();
    }""", failure)
    draft = "newer draft" if newer else "original"
    page.get_by_role("textbox", name="Message Claude").fill(draft)
    page.get_by_role("button", name="Unconfirmed queued messages", exact=True).click()
    dialog = page.get_by_role("dialog", name="Unconfirmed queued messages")
    assert dialog.locator("pre").inner_text() == "original"
    assert page.evaluate("calls") == []
    assert dialog.bounding_box()["width"] <= width
    if newer and not failure:
        page.screenshot(path=str(tmp_path / f"queue-recovery-{width}.png"))
    dialog.get_by_role("button", name="Retry original message").click()
    playwright.expect(dialog.get_by_role("status")).to_have_text("Receipt still unconfirmed" if failure else "Delivery confirmed")
    assert page.evaluate("calls") == [["retry", "original-id"]]
    assert page.get_by_role("textbox", name="Message Claude").input_value() == (draft if newer or failure else "")
    dialog.get_by_role("button", name="Close queued message recovery").click()
    assert not errors


def test_disposed_queue_recovery_does_not_clear_reopened_draft(pane):
    page, errors = pane
    page.evaluate("""() => {
      controls.pendingQueuedInputs=()=>[{requestId:'id',payload:{inputs:[{type:'text',text:'original'}]}}];
      controls.retryQueuedInput=()=>new Promise(resolve=>{window.finishRecovery=resolve;});
      pane.render();
    }""")
    page.get_by_role("textbox", name="Message Claude").fill("original")
    page.get_by_role("button", name="Unconfirmed queued messages", exact=True).click()
    page.get_by_role("button", name="Retry original message").click()
    page.evaluate("""() => {
      pane.dispose();
      pane.draftStorage.setItem(pane.draftKey,'newly reopened draft');
      finishRecovery({turn:{id:'confirmed'}});
    }""")
    assert page.evaluate("pane.draftStorage.getItem(pane.draftKey)") == "newly reopened draft"
    assert not errors


def test_stop_with_queued_claude_inputs_targets_oldest_active_turn(pane):
    page, errors = pane
    page.evaluate("""() => {
      controls.interrupt=async id=>calls.push(['interrupt',id]);
      emit({method:'turn/started',params:{turn:{id:'first',status:'inProgress'}}});
      emit({method:'turn/started',params:{turn:{id:'second',status:'inProgress'}}});
    }""")
    page.wait_for_function("!pane.stop.hidden")
    page.evaluate("pane.interrupt()")
    assert page.evaluate("calls") == [["interrupt", "first"]]
    assert not errors


def test_pending_turn_keeps_claude_busy_after_another_turn_completes(pane):
    page, errors = pane
    page.evaluate("""() => {
      emit({method:'turn/started',params:{turn:{id:'first',status:'inProgress'}}});
      emit({method:'turn/started',params:{turn:{id:'second',status:'inProgress'}}});
      emit({method:'turn/completed',params:{turn:{id:'first',status:'completed'}}});
    }""")
    page.wait_for_function("pane.status.textContent === 'running'")
    assert page.evaluate("pane.send.disabled")
    assert page.evaluate("!pane.stop.hidden")
    page.evaluate("emit({method:'turn/completed',params:{turn:{id:'second',status:'completed'}}})")
    page.wait_for_function("pane.status.textContent === 'completed' && !pane.send.disabled")
    assert page.evaluate("pane.stop.hidden")
    assert not errors


def test_acp_stream_deltas_render_exactly_before_and_after_completion(pane):
    from core.workspace_acp_events import AcpEvents

    page, errors = pane
    events = AcpEvents("exact")
    page.evaluate("event => emit(event)", events.begin("acp-stream"))
    chunks = [f"chunk {index} " for index in range(200)]
    for chunk in chunks:
        event = events.update({"sessionId": "exact", "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": chunk}}})
        page.evaluate("event => emit(event)", event)
    item = page.locator('#left [data-item-id="acp-stream:message:0"] .aw-message')
    playwright.expect(item).to_have_text("".join(chunks).strip())
    page.evaluate("event => emit(event)", events.complete("end_turn"))
    playwright.expect(item).to_have_text("".join(chunks).strip())
    assert page.locator('#left [data-item-id="acp-stream:message:0"]').count() == 1
    assert not errors


def test_questions_resolve_only_from_provider_and_stream_does_not_collapse_tools(pane):
    page, errors = pane
    page.locator("#left .aw-tool summary").click()
    page.evaluate(
        "emit({id:7,method:'item/tool/requestUserInput',params:{threadId:'exact',questions:[{id:'choice',question:'Choose a mode',options:[{label:'Read only',description:'No writes'}]}]}})"
    )
    page.get_by_role("button", name="Read only", exact=True).click()
    page.get_by_role("button", name="Answer", exact=True).click()
    assert page.evaluate("window.calls[0]") == [
        "answer",
        7,
        {"answers": {"choice": {"answers": ["Read only"]}}},
    ]
    assert page.get_by_text("Choose a mode", exact=True).is_visible()
    page.evaluate("emit({method:'serverRequest/resolved',params:{threadId:'exact',requestId:7}})")
    page.wait_for_function("pane.questionArea.children.length===0")
    page.evaluate(
        "emit({method:'item/agentMessage/delta',params:{threadId:'exact',turnId:'t',itemId:'a',delta:' Updated.'}})"
    )
    page.wait_for_function(
        "pane.log.querySelector('.aw-message').textContent.trimEnd().endsWith(' Updated.')"
    )
    assert page.locator("#left .aw-tool").evaluate("(el)=>el.open")
    assert not errors


def test_mobile_text_fits_and_composer_remains_visible(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth===innerWidth")
    box = page.get_by_role("textbox", name="Message Claude").bounding_box()
    assert box["y"] + box["height"] <= 844
    page.screenshot(path=str(tmp_path / "rich-workspace-mobile.png"))
    print("Screenshot:", tmp_path / "rich-workspace-mobile.png")
    assert not errors
