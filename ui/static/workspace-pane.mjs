import {WorkspaceConversation} from './workspace-events.mjs';
import {renderWorkspaceMarkdown} from './workspace-markdown.mjs';
import {renderElicitation} from './workspace-elicitation.mjs';
import {installFileMentions} from './workspace-mentions.mjs';
import {installSessionActions} from './workspace-actions.mjs';

const node = (tag, cls, text) => {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
};
const icon = name => { const el = node('i'); el.dataset.lucide = name; return el; };
const promptColors={default:'#50354a',red:'#ff7979',blue:'#82b3ff',green:'#77d99b',yellow:'#ead976',purple:'#c09bff',orange:'#f4ae75',pink:'#ff80bf',cyan:'#70dbe1'};

/** A real session view. Controls are supplied by the session owner, not a CLI scraper. */
export class WorkspacePane {
  constructor(root, {sessionId, provider, model = '', controls, draftStorage}) {
    this.root = root;
    this.provider = provider;
    this.controls = controls;
    this.draftStorage = draftStorage;
    this.draftKey = `serena-workspace-draft:${provider.toLowerCase()}:${sessionId}`;
    this.conversation = new WorkspaceConversation(sessionId);
    this.sleeping = false;
    this.rendered = new Map();
    this.visibleItemLimit = 100;
    this.lastItemCount = 0;
    this.files = [];
    this.selectedSkills = [];
    this.selectedApps = [];
    this.previews = new Map();
    this.historyImageUrls = new Set();
    this.sending = false;
    this.interrupting = false;
    this.disposed = false;
    this.frame = 0;
    try{this.clearedSession=controls.lastClear?.();}catch{this.clearedSession=null;}
    root.classList.add('agent-workspace-pane');
    root.setAttribute('aria-label', `${provider} conversation`);
    const head = node('header', 'aw-head');
    const badge = node('span', 'aw-badge', provider.toLowerCase()==='codex'?'X':provider.slice(0, 1).toUpperCase());
    badge.dataset.provider = provider.toLowerCase();
    this.modelLabel = node('small', '', model);
    head.append(badge, node('strong', '', provider), this.modelLabel);
    this.status = node('span', 'aw-state', 'Connecting');
    this.status.setAttribute('role', 'status');
    head.append(this.status);
    this.resumeButton=this.button('Open saved conversation','history',()=>this.openSessions());
    this.resumeButton.hidden=!['Claude','Codex'].includes(provider) || !controls.listSessions || !controls.openSession;
    head.append(this.resumeButton);
    this.colorButton=this.button('Prompt color','palette',()=>this.openPromptColor());
    head.append(this.colorButton);
    this.diagnosticsButton=this.button('Installation diagnostics','stethoscope',()=>this.openDiagnostics());
    this.diagnosticsButton.hidden=provider!=='Claude' || !controls.diagnostics;
    head.append(this.diagnosticsButton);
    this.accountButton=this.button('Codex account','user-round',()=>this.openAccount());
    this.accountButton.hidden=provider!=='Codex' || !controls.accountStatus;
    head.append(this.accountButton);
    this.sessionStatusButton=this.button('Session status','info',()=>this.openSessionStatus());
    this.sessionStatusButton.hidden=provider!=='Codex';head.append(this.sessionStatusButton);
    this.copyOutputButton=this.button('Copy latest completed output','copy',()=>this.copyLatestOutput());
    this.copyOutputButton.hidden=provider!=='Codex';head.append(this.copyOutputButton);
    this.hooksButton=this.button('Lifecycle hooks','webhook',()=>this.openHooks());
    this.hooksButton.hidden=provider!=='Codex' || !controls.hooks;head.append(this.hooksButton);
    this.appsButton=this.button('Apps and connectors','blocks',()=>this.openApps());
    this.appsButton.hidden=provider!=='Codex' || !controls.apps;head.append(this.appsButton);
    this.renameButton=this.button('Rename conversation','pencil',()=>this.openRename());
    this.renameButton.hidden=provider!=='Codex' || !controls.renameSession;head.append(this.renameButton);
    this.rewindButton=this.button('Rewind conversation','undo-2',()=>this.openRewind());
    this.rewindButton.hidden=provider!=='Codex' || !controls.revertHistory;head.append(this.rewindButton);
    this.diffButton=this.button('Project diff','file-diff',()=>this.openProjectDiff());
    this.diffButton.hidden=provider!=='Codex' || !controls.projectDiff;head.append(this.diffButton);
    const eventsButton=this.button('Session events','list-collapse',()=>this.openEvents());
    eventsButton.hidden=!controls.events;head.append(eventsButton);
    this.forkButton=this.button('Fork conversation','git-fork',()=>this.openFork());
    this.forkButton.hidden=!['Claude','Codex'].includes(provider) || !controls.forkSession || !controls.openFork;
    this.forkButton.disabled=true;
    head.append(this.forkButton);
    this.clearButton=this.button('Clear context','eraser',()=>this.openClear());
    this.clearButton.hidden=provider!=='Claude' || !controls.clearSession || !controls.openCleared;
    this.clearButton.disabled=true;head.append(this.clearButton);
    this.newConversationButton=this.button('New conversation','square-pen',()=>{
      const title=/^\/new(?:\s+(.*))?$/.exec(this.input.value.trim())?.[1] || '';
      this.controls.newConversation(title);
    });
    this.newConversationButton.hidden=!controls.newConversation;head.append(this.newConversationButton);
    this.personalityButton=this.button('Codex personality','smile',()=>this.openPersonality());
    this.personalityButton.hidden=provider!=='Codex' || !controls.personality || !controls.setPersonality;head.append(this.personalityButton);
    this.goalButton=this.button('Session goal','flag',()=>this.openGoal());
    this.goalButton.hidden=provider!=='Codex' || !controls.goal || !controls.updateGoal || !controls.clearGoal;head.append(this.goalButton);
    this.agentsButton=this.button('Delegated agents','users',()=>this.openAgents());
    this.agentsButton.hidden=provider!=='Codex' || !controls.agents || !controls.inspectAgent;head.append(this.agentsButton);
    this.disconnectButton=this.button('Disconnect session','unplug',()=>this.openDisconnect());
    this.disconnectButton.hidden=!controls.disconnectSession;
    this.disconnectButton.disabled=true;head.append(this.disconnectButton);
    this.shellButton=this.button('Run shell command','terminal',()=>this.openShell());
    this.shellButton.hidden=provider!=='Codex' || !controls.shellCommand;
    head.append(this.shellButton);
    this.disposeActions = installSessionActions(head, this.button('Session actions', 'ellipsis'));
    this.log = node('div', 'aw-transcript');
    this.log.tabIndex = 0;
    this.log.setAttribute('aria-label', `${provider} messages and tool output`);
    this.earlier = this.button('Load earlier messages', 'arrow-up', () => this.loadEarlier());
    this.earlier.classList.add('aw-load-earlier');
    this.log.addEventListener('scroll', () => {
      if (this.log.scrollTop < 40 && this.visibleItemLimit < this.lastItemCount) this.loadEarlier();
    });
    this.questionArea = node('div', 'aw-questions');
    this.alert = node('div', 'aw-error');
    this.alert.setAttribute('role', 'alert');
    this.alert.hidden = true;
    this.form = node('form', 'aw-composer');
    this.input = node('textarea');
    this.input.placeholder = `Message ${provider}...`;
    this.input.setAttribute('aria-label', `Message ${provider}`);
    this.input.rows = 2;
    try {
      this.draftStorage ??= window.sessionStorage;
      this.input.value = this.draftStorage.getItem(this.draftKey) || '';
      const skills=JSON.parse(this.draftStorage.getItem(`${this.draftKey}:skills`) || '[]');
      this.promptColor=this.draftStorage.getItem(`${this.draftKey}:color`) || 'default';
      if(!Object.hasOwn(promptColors,this.promptColor))this.promptColor='default';
      this.form.style.borderColor=promptColors[this.promptColor];
      if(provider==='Codex' && Array.isArray(skills))this.selectedSkills=skills.filter(s=>typeof s?.name==='string' && typeof s?.path==='string');
      const apps=JSON.parse(this.draftStorage.getItem(`${this.draftKey}:apps`) || '[]');
      if(provider==='Codex' && Array.isArray(apps))this.selectedApps=apps.filter(a=>typeof a?.name==='string' && typeof a?.id==='string').slice(0,20);
    }
    catch (error) { this.error(new Error(`Draft storage unavailable: ${error.message}`)); }
    this.input.addEventListener('input', () => this.persistDraft());
    this.attachments = node('div', 'aw-attachments');
    const footer = node('div', 'aw-composer-tools');
    this.fileInput = node('input');
    this.fileInput.type = 'file'; this.fileInput.multiple = true; this.fileInput.hidden = true;
    this.fileInput.addEventListener('change', () => {
      this.files.push(...this.fileInput.files);
      this.fileInput.value = '';
      this.renderAttachments();
    });
    const attach = this.button('Attach files', 'paperclip', () => this.fileInput.click());
    this.modelSelect = node('select', 'aw-model-select');
    this.modelSelect.setAttribute('aria-label', 'Model'); this.modelSelect.title = 'Model';
    this.effortSelect = node('select', 'aw-effort-select');
    this.effortSelect.setAttribute('aria-label', 'Reasoning effort'); this.effortSelect.title = 'Reasoning effort';
    this.tierSelect = node('select', 'aw-effort-select');
    this.tierSelect.setAttribute('aria-label', 'Speed tier'); this.tierSelect.title = 'Speed tier'; this.tierSelect.hidden = true;
    this.modelSelect.hidden = this.effortSelect.hidden = true;
    this.modelSelect.addEventListener('change', () => this.renderEfforts(true));
    this.stop = this.button('Interrupt turn', 'square', () => this.interrupt());
    this.stop.title = 'Interrupt turn (Escape)';
    this.stop.setAttribute('aria-keyshortcuts','Escape');
    this.stop.hidden = true;
    this.send = this.button('Send message', 'arrow-up'); this.send.type = 'submit';
    footer.append(attach, this.modelSelect, this.effortSelect, this.tierSelect, this.stop, this.send);
    this.mentionButton=this.button('Mention project file','file-search',()=>this.openFileSearch());
    this.mentionButton.hidden=!['Claude','Codex'].includes(provider) || !controls.searchFiles;
    footer.insertBefore(this.mentionButton,this.modelSelect);
    this.reviewButton = this.button('Review changes', 'scan-eye', () => this.openReview());
    this.reviewButton.hidden = provider !== 'Codex' || !controls.review;
    footer.insertBefore(this.reviewButton, this.stop);
    this.compactButton = this.button('Compact conversation', 'minimize-2', async () => {
      this.compactButton.disabled=true;
      try { await this.controls.compact(); }
      catch(error){ this.error(error); this.render(); }
    });
    this.compactButton.hidden=provider !== 'Codex' || !controls.compact;
    footer.insertBefore(this.compactButton,this.stop);
    this.tasksButton = this.button('Background tasks', 'list-tree', () => this.openBackgroundTasks());
    this.tasksButton.hidden = !['Codex','Claude'].includes(provider) || !controls.backgroundTasks;
    footer.insertBefore(this.tasksButton, this.stop);
    this.commandsButton = this.button('Commands and skills', 'slash', () => this.openCommands());
    this.commandsButton.hidden = !['Claude','Codex','Gemini'].includes(provider) || !controls.commands;
    footer.insertBefore(this.commandsButton, this.stop);
    this.mcpButton = this.button('MCP connections', 'plug', () => this.openMcpServers());
    this.mcpButton.hidden = !['Claude','Codex'].includes(provider) || !controls.mcpServers;
    footer.insertBefore(this.mcpButton, this.stop);
    const permissions=this.button('Permission mode','shield',()=>this.openPermissions());
    this.permissionsButton=permissions;
    permissions.hidden=!['Codex','Claude'].includes(provider) || !controls.permissions;footer.insertBefore(permissions,this.stop);
    this.sessionModeButton=this.button('Session mode','sliders-horizontal',()=>this.openSessionMode());
    this.sessionModeButton.hidden=!['Codex','Gemini'].includes(provider) || !controls.sessionModes || !controls.setSessionMode;
    footer.insertBefore(this.sessionModeButton,this.stop);
    this.claudeEffortButton=this.button('Claude reasoning effort','gauge',()=>this.openClaudeEffort());
    this.claudeEffortButton.hidden=provider!=='Claude' || !controls.models || !controls.commands;
    footer.insertBefore(this.claudeEffortButton,this.stop);
    this.queueButton = this.button('Queued sibling messages', 'messages-square', () => this.openBridgeQueue());
    this.queueButton.hidden=true; footer.insertBefore(this.queueButton, this.stop);
    this.queueRecoveryButton=this.button('Unconfirmed queued messages','rotate-ccw',()=>this.openQueueRecovery());
    this.queueRecoveryButton.hidden=true;footer.insertBefore(this.queueRecoveryButton,this.stop);
    this.form.append(this.input, this.attachments, footer, this.fileInput);
    this.disposeMentions=installFileMentions({input:this.input,form:this.form,
      enabled:()=>['Claude','Codex'].includes(this.provider) && typeof this.controls.searchFiles==='function',
      search:query=>this.controls.searchFiles(query),persist:()=>this.persistDraft()});
    this.form.addEventListener('submit', e => { e.preventDefault(); this.submit(); });
    this.input.addEventListener('keydown', e => {
      if(this.provider==='Codex' && e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && e.key.toLowerCase()==='o' && !e.isComposing){
        e.preventDefault();this.copyLatestOutput();return;
      }
      if(e.key==='Escape' && !e.isComposing && !e.repeat && !e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && this.conversation.status==='running'){
        e.preventDefault();e.stopPropagation();this.interrupt();return;
      }
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.form.requestSubmit(); }
    });
    this.input.addEventListener('paste', e => {
      const files = [...(e.clipboardData?.files || [])];
      if (!files.length) return;
      e.preventDefault();
      const text = e.clipboardData.getData('text/plain');
      if (text) this.input.setRangeText(text, this.input.selectionStart, this.input.selectionEnd, 'end');
      this.persistDraft();
      this.files.push(...files); this.renderAttachments();
    });
    this.form.addEventListener('dragover', e => {
      if ([...(e.dataTransfer?.types || [])].includes('Files')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; }
    });
    this.form.addEventListener('drop', e => {
      const files = [...(e.dataTransfer?.files || [])];
      if (!files.length) return;
      e.preventDefault(); this.files.push(...files); this.renderAttachments(); this.input.focus();
    });
    const identity = node('footer', 'aw-identity');
    const copy = this.button(`Copy session ID ${sessionId}`, 'copy', () => {
      navigator.clipboard.writeText(sessionId).catch(e => this.error(e));
    });
    identity.append(node('span', '', sessionId.slice(0, 8)), copy);
    this.usageLabel = node('span', 'aw-usage');
    identity.append(this.usageLabel);
    const context=this.button('Context breakdown','chart-pie',()=>this.openContext());
    context.hidden=provider!=='Claude' || !controls.contextUsage;identity.append(context);
    root.replaceChildren(head, this.log, this.questionArea, this.alert, this.form, identity);
    this.renderAttachments();
    this.refreshIcons();
    this.render();
  }

  button(label, symbol, action) {
    const button = node('button'); button.type = 'button';
    button.title = label; button.setAttribute('aria-label', label);
    button.append(icon(symbol));
    if (action) button.addEventListener('click', action);
    return button;
  }

  refreshIcons() { window.lucide?.createIcons({root: this.root}); }
  error(error) { this.alert.hidden = false; this.alert.textContent = error.message || String(error); }

  persistDraft() {
    try {
      if (this.input.value) this.draftStorage.setItem(this.draftKey, this.input.value);
      else this.draftStorage.removeItem(this.draftKey);
    } catch (error) { this.error(new Error(`Draft could not be saved: ${error.message}`)); }
  }

  openQueueRecovery() {
    if(this.queueRecoveryDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog');dialog.setAttribute('aria-label','Unconfirmed queued messages');
    const close=this.button('Close queued message recovery','x',()=>dialog.close());
    dialog.append(node('h3','','Unconfirmed queued messages'),close);
    for(const pending of this.controls.pendingQueuedInputs()){
      const inputs=pending.payload.inputs;
      const text=inputs.filter(input=>input.type==='text').map(input=>input.text).join('');
      const uploads=inputs.filter(input=>input.type==='upload').length;
      const preview=node('pre','aw-command',text);preview.style.whiteSpace='pre-wrap';preview.style.overflowWrap='anywhere';
      const status=node('p','','Delivery unconfirmed');status.setAttribute('role','status');
      const retry=node('button','','Retry original message');retry.type='button';
      retry.addEventListener('click',async()=>{
        if(retry.disabled)return;
        retry.disabled=true;
        try{
          await this.controls.retryQueuedInput(pending.requestId);
          if(!this.disposed && !uploads && !this.files.length && this.input.value===text){this.input.value='';this.persistDraft();}
          status.textContent='Delivery confirmed';
          if(!this.disposed){this.alert.hidden=true;this.render();}
        }catch(error){status.textContent=error.message;retry.disabled=false;if(!this.disposed)this.render();}
      });
      dialog.append(preview);
      if(uploads)dialog.append(node('p','',`${uploads} attachment${uploads===1?'':'s'}`));
      dialog.append(status,retry);
    }
    dialog.addEventListener('close',()=>dialog.remove());this.queueRecoveryDialog=dialog;
    this.root.append(dialog);this.refreshIcons();dialog.showModal();close.focus();
  }

  openBridgeQueue() {
    if(this.queueDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-tasks-dialog');
    dialog.setAttribute('aria-label','Queued sibling messages');
    dialog.append(node('h3','','Queued sibling messages'),this.button('Close queue','x',()=>dialog.close()));
    this.queueList=node('div'); dialog.append(this.queueList);
    dialog.addEventListener('close',()=>dialog.remove());
    this.queueDialog=dialog; this.queueSignature=null;
    this.root.append(dialog); dialog.showModal(); this.renderBridgeQueue(); this.refreshIcons();
  }

  renderBridgeQueue() {
    if(!this.queueDialog?.open)return;
    const requests=this.conversation.metadata.bridgeQueue || [];
    const signature=JSON.stringify(requests);
    if(signature===this.queueSignature)return;
    this.queueSignature=signature; this.queueList.replaceChildren();
    if(!requests.length)this.queueList.append(node('p','','No queued messages'));
    for(const request of requests){
      const row=node('div','aw-background-task'); row.append(node('pre','',request.prompt));
      const cancel=this.button(`Cancel queued message ${request.id}`,'x',async()=>{
        cancel.disabled=true;
        try{await this.controls.cancelQueuedBridge(request.id);}
        catch(error){this.error(error);cancel.disabled=false;}
      });
      const actions=node('div','aw-queue-actions');
      if(this.controls.editQueuedBridge)actions.append(this.button(`Edit queued message ${request.id}`,'pencil',()=>this.openQueueEdit(request)));
      actions.append(cancel);row.append(actions); this.queueList.append(row);
    }
  }

  openQueueEdit(request) {
    if(this.queueEditDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog');dialog.setAttribute('aria-label','Edit queued message');
    const form=node('form');const text=node('textarea');text.setAttribute('aria-label','Queued message text');text.rows=6;text.value=request.prompt;
    const status=node('p');status.setAttribute('role','status');
    const save=node('button','','Save');save.type='submit';
    const close=this.button('Discard queue edit','x',()=>dialog.close());
    form.append(text,save);dialog.append(node('h3','','Edit queued message'),close,status,form);
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(save.disabled || !text.value.trim())return;
      save.disabled=true;text.disabled=true;
      try{await this.controls.editQueuedBridge(request.id,text.value,request.prompt);dialog.close();}
      catch(error){if(dialog.open)status.textContent=error.message;}
      finally{save.disabled=false;text.disabled=false;}
    });
    dialog.addEventListener('close',()=>dialog.remove());this.queueEditDialog=dialog;this.root.append(dialog);this.refreshIcons();dialog.showModal();text.focus();
  }

  openDisconnect() {
    if(this.disconnectDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog');dialog.setAttribute('aria-label','Disconnect session');
    const cancel=node('button','','Cancel');cancel.type='button';cancel.addEventListener('click',()=>dialog.close());
    const status=node('p','','Conversation history will be kept.');
    const confirm=node('button','','Disconnect');confirm.type='button';
    confirm.addEventListener('click',async()=>{
      confirm.disabled=true;
      try { await this.controls.disconnectSession();dialog.close(); }
      catch(error){status.textContent=error.message;confirm.disabled=false;}
    });
    dialog.append(node('h3','','Disconnect session?'),status,cancel,confirm);
    dialog.addEventListener('close',()=>dialog.remove());this.disconnectDialog=dialog;
    this.root.append(dialog);dialog.showModal();cancel.focus();
  }

  openClear() {
    if(this.clearDialog?.open || this.clearing)return;
    try{this.clearedSession ??= this.controls.lastClear?.();}
    catch(error){this.error(error);return;}
    const dialog=node('dialog','aw-review-dialog');dialog.setAttribute('aria-label','Clear context');
    const status=node('p','','Start a new Claude conversation? Current history will be kept.');status.setAttribute('role','status');
    const identity=node('code');identity.style.overflowWrap='anywhere';
    const close=this.button('Close clear context','x',()=>dialog.close());
    const open=this.button('Open new conversation','arrow-up-right',async()=>{
      try{await this.controls.openCleared(this.clearedSession.session_id);dialog.close();}
      catch(error){status.textContent=error.message;}
    });
    const render=()=>{
      open.hidden=!this.clearedSession;confirm.hidden=Boolean(this.clearedSession);
      if(this.clearedSession){status.textContent='Context cleared';identity.textContent=this.clearedSession.session_id;}
    };
    const confirm=this.button('Confirm clear context','eraser',async()=>{
      this.clearing=true;confirm.disabled=true;status.textContent='Clearing context...';this.render();
      try{
        const result=await this.controls.clearSession();
        if(typeof result?.session_id!=='string' || !/^[a-f0-9-]{36}$/.test(result.session_id) || result.session_id===this.conversation.sessionId)throw Error('Clear identity is unavailable');
        this.clearedSession=result;
        this.conversation.status='unavailable';
        if(dialog.open && !this.disposed)render();
      }catch(error){if(dialog.open){status.textContent=error.message;confirm.disabled=false;}}
      finally{this.clearing=false;if(!this.disposed)this.render();}
    });
    dialog.append(node('h3','','Clear context'),close,status,identity,confirm,open);
    dialog.addEventListener('close',()=>dialog.remove());
    this.clearDialog=dialog;this.root.append(dialog);render();dialog.showModal();this.refreshIcons();
  }

  openFork() {
    if(this.forkDialog?.open || this.forkCreating)return;
    try{this.createdFork ??= this.controls.lastFork?.();}
    catch(error){this.error(error);return;}
    const dialog=node('dialog','aw-review-dialog');
    dialog.setAttribute('aria-label','Fork conversation');
    const status=node('p');status.setAttribute('role','status');
    const identity=node('code');identity.style.overflowWrap='anywhere';
    const close=this.button('Close fork','x',()=>dialog.close());
    const open=this.button('Open fork','arrow-up-right',async()=>{
      try{await this.controls.openFork(this.createdFork.session_id);dialog.close();}
      catch(error){status.textContent=error.message;}
    });
    open.hidden=true;
    const create=this.button('Create fork','git-fork',async()=>{
      this.forkCreating=true;this.forkButton.disabled=true;
      create.disabled=true;status.textContent='Creating...';
      try{
        const result=await this.controls.forkSession();
        if(typeof result?.session_id!=='string' || !/^[a-f0-9-]{36}$/.test(result.session_id))throw Error('Fork identity is unavailable');
        this.createdFork=result;
        if(!dialog.open || this.disposed)return;
        render();
      }catch(error){if(dialog.open){status.textContent=error.message;create.disabled=false;}}
      finally{this.forkCreating=false;if(!this.disposed)this.render();}
    });
    const another=this.button('Create another fork','plus',()=>{
      try{this.controls.clearForkReceipt?.();}
      catch(error){status.textContent=error.message;return;}
      this.createdFork=null;identity.textContent='';status.textContent='';
      create.hidden=false;create.disabled=false;open.hidden=true;another.hidden=true;
    });
    another.hidden=true;
    const recover=this.button('Retry fork registration','refresh-cw',async()=>{
      recover.disabled=true;
      try{
        this.createdFork=await this.controls.recoverFork(this.createdFork.request_id);
        if(dialog.open && !this.disposed)render();
      }catch(error){if(dialog.open)status.textContent=error.message;}
      finally{recover.disabled=false;}
    });
    recover.hidden=true;
    const render=()=>{
      const result=this.createdFork;
      if(!result)return;
      identity.textContent=result.session_id;
      status.textContent=result.indexed?'Fork created':(result.error || 'Fork created; catalog registration failed');
      open.hidden=!result.indexed;create.hidden=true;
      another.hidden=!result.indexed;
      recover.hidden=Boolean(result.indexed) || !result.request_id || !this.controls.recoverFork;
    };
    dialog.append(node('h3','','Fork conversation'),close,identity,status,create,open,another,recover);
    dialog.addEventListener('close',()=>dialog.remove());
    this.forkDialog=dialog;this.root.append(dialog);render();dialog.showModal();
    window.lucide?.createIcons();
  }

  openFileSearch() {
    if(this.fileSearchDialog?.open)return;
    const original=this.input.value;
    const command=this.provider==='Codex' && /^\/mention(?:\s+(.*))?$/.exec(original.trim());
    const start=command?0:this.input.selectionStart,end=command?original.length:this.input.selectionEnd;
    const dialog=node('dialog','aw-review-dialog aw-commands-dialog');
    dialog.setAttribute('aria-label','Mention project file');
    const search=node('input');search.type='search';search.maxLength=200;
    search.setAttribute('aria-label','Find project file');
    if(command?.[1])search.value=command[1];
    const status=node('p');status.setAttribute('role','status');
    const list=node('div','aw-command-list');
    let generation=0;
    list.addEventListener('keydown',e=>{
      if(e.key==='ArrowDown' || e.key==='ArrowUp'){
        e.preventDefault();
        (e.key==='ArrowDown'?e.target.nextElementSibling:e.target.previousElementSibling)?.focus();
      }
    });
    const run=async()=>{
      if(!search.value.trim())return;
      const request=++generation;
      status.textContent='Searching...';list.replaceChildren();
      try{
        const result=await this.controls.searchFiles(search.value);
        if(!dialog.open || this.disposed || request!==generation)return;
        status.textContent=`${result.paths.length} files`;
        for(const path of result.paths){
          const item=node('button','aw-command',path);item.type='button';
          item.addEventListener('click',()=>{
            const mention='@'+(/\s|["\\]/.test(path)?JSON.stringify(path):path)+' ';
            if(this.input.value===original)this.input.setRangeText(mention,start,end,'end');
            else this.input.setRangeText(mention,this.input.selectionStart,this.input.selectionEnd,'end');
            this.persistDraft();dialog.close();this.input.focus();
          });
          list.append(item);
        }
      }catch(error){if(dialog.open && request===generation)status.textContent=error.message;}
    };
    const find=this.button('Search project files','search',run);
    search.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();run();}else if(e.key==='ArrowDown'){e.preventDefault();list.querySelector('button')?.focus();}});
    const close=this.button('Close file search','x',()=>dialog.close());
    dialog.addEventListener('close',()=>{generation++;dialog.remove();});
    dialog.append(node('h3','','Mention project file'),close,search,find,status,list);
    this.fileSearchDialog=dialog;this.root.append(dialog);dialog.showModal();search.focus();
    if(search.value)run();
    window.lucide?.createIcons();
  }

  async openSessions() {
    if(this.sessionsDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-commands-dialog');dialog.setAttribute('aria-label','Saved conversations');
    const search=node('input');search.type='search';search.setAttribute('aria-label','Search saved conversations');
    const status=node('p');status.setAttribute('role','status');
    const list=node('div','aw-command-list');
    let generation=0,next=null,query='';
    const more=this.button('Load more conversations','chevron-down',()=>load(query,next));more.hidden=true;
    const load=async(value,offset=0)=>{
      const request=++generation;query=value;more.disabled=true;status.textContent='Searching...';
      if(offset===0)list.replaceChildren();
      try{
        const result=await this.controls.listSessions(value,offset);
        if(!dialog.open || this.disposed || request!==generation)return;
        for(const session of result.data){
          const button=node('button','aw-command');button.type='button';
          button.append(node('strong','',session.title),node('small','',session.session_id),node('span','',session.cwd || ''));
          button.disabled=session.session_id===this.conversation.sessionId;
          button.addEventListener('click',async()=>{
            button.disabled=true;
            try{await this.controls.openSession(session.session_id);dialog.close();}
            catch(error){status.textContent=error.message;button.disabled=false;}
          });list.append(button);
        }
        next=result.nextOffset;more.hidden=next===null;
        status.textContent=list.childElementCount?`${list.childElementCount} conversations`:'No matching conversations';
      }catch(error){if(request===generation && dialog.open)status.textContent=error.message;}
      finally{if(request===generation)more.disabled=false;}
    };
    const find=this.button('Search saved conversations','search',()=>load(search.value));
    search.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();load(search.value);}});
    const close=this.button('Close saved conversations','x',()=>dialog.close());
    dialog.addEventListener('close',()=>{generation++;dialog.remove();this.input.focus();});
    dialog.append(node('h3','','Saved conversations'),close,search,find,status,list,more);
    this.sessionsDialog=dialog;this.root.append(dialog);dialog.showModal();search.focus();
    window.lucide?.createIcons();await load('');
  }

  async openAccount() {
    if(this.accountDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-commands-dialog');dialog.setAttribute('aria-label','Codex account');
    const status=node('p');status.setAttribute('role','status');
    const details=node('p');details.style.overflowWrap='anywhere';
    const loginStatus=node('p');loginStatus.setAttribute('aria-live','polite');
    const link=node('a','','Continue browser sign-in');link.target='_blank';link.rel='noopener noreferrer';link.hidden=true;
    let login=null,busy=false,timer=null;
    const showLogin=value=>{
      login=value;link.hidden=true;link.removeAttribute('href');
      const pending=['pending','uncertain'].includes(login?.status);
      signIn.disabled=busy || pending;cancel.hidden=!pending || !login?.loginId;cancel.disabled=busy;
      loginStatus.textContent=login ? `Sign-in: ${login.status}` : '';
      if(login?.status==='pending' && login.authUrl){
        try{
          const url=new URL(login.authUrl);
          if(url.protocol!=='https:' || !['auth.openai.com','auth0.openai.com','chatgpt.com'].includes(url.hostname) || url.username || url.password || (url.port && url.port!=='443'))throw Error('Unsafe sign-in URL');
          link.href=url.href;link.hidden=false;
        }catch{loginStatus.textContent='Sign-in URL unavailable';}
      }
      clearTimeout(timer);
      if(pending && dialog.open)timer=setTimeout(()=>refresh.click(),2000);
    };
    const act=async action=>{
      if(busy)return;busy=true;signIn.disabled=true;cancel.disabled=true;refresh.disabled=true;
      try{const value=await action();if(dialog.open && !this.disposed)showLogin(value);}
      catch(error){if(dialog.open)loginStatus.textContent=error.message;}
      finally{busy=false;refresh.disabled=false;cancel.disabled=false;signIn.disabled=['pending','uncertain'].includes(login?.status);}
    };
    const signIn=this.button('Sign in with ChatGPT','log-in',()=>act(()=>this.controls.accountLogin()));
    signIn.hidden=!this.controls.accountLogin;
    const cancel=this.button('Cancel browser sign-in','x',()=>act(()=>this.controls.cancelAccountLogin(login.loginId)));cancel.hidden=true;
    const refresh=this.button('Refresh account status','refresh-cw',async()=>{
      if(busy)return;
      busy=true;signIn.disabled=true;cancel.disabled=true;
      refresh.disabled=true;status.textContent='Checking account...';details.textContent='';
      try{
        const result=await this.controls.accountStatus();
        if(!dialog.open || this.disposed)return;
        const account=result.account;
        status.textContent=account ? (account.type==='chatgpt' ? 'ChatGPT account saved' : `Account type: ${account.type}`) : 'Not signed in';
        details.textContent=[account?.email,account?.planType,account ? 'Credential validity has not been verified.' : 'No account is saved for this session runtime.'].filter(Boolean).join(' · ');
        showLogin(result.login);
      }catch(error){if(dialog.open)status.textContent=error.message;}
      finally{busy=false;refresh.disabled=false;cancel.disabled=false;signIn.disabled=['pending','uncertain'].includes(login?.status);}
    });
    const close=this.button('Close account','x',()=>dialog.close());
    dialog.append(node('h3','','Codex account'),close,status,details,loginStatus,link,signIn,cancel,refresh);
    dialog.addEventListener('close',()=>{clearTimeout(timer);dialog.remove();this.input.focus();});
    this.accountDialog=dialog;this.root.append(dialog);dialog.showModal();close.focus();window.lucide?.createIcons();refresh.click();
  }

  async openDiagnostics() {
    if(this.diagnosticsDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-commands-dialog');dialog.setAttribute('aria-label','Installation diagnostics');
    const status=node('p');status.setAttribute('role','status');
    const output=node('pre');output.style.whiteSpace='pre-wrap';output.style.overflowWrap='anywhere';
    const run=this.button('Run installation diagnostics','play',async()=>{
      run.disabled=true;status.textContent='Checking installation...';output.textContent='';
      try{
        const result=await this.controls.diagnostics();
        if(!dialog.open || this.disposed)return;
        status.textContent=`${result.command} exited ${result.exitCode}`;output.textContent=result.output;
      }catch(error){if(dialog.open)status.textContent=error.message;}
      finally{run.disabled=false;}
    });
    const close=this.button('Close installation diagnostics','x',()=>dialog.close());
    dialog.append(node('h3','','Installation diagnostics'),close,run,status,output);
    dialog.addEventListener('close',()=>{dialog.remove();this.input.focus();});
    this.diagnosticsDialog=dialog;this.root.append(dialog);dialog.showModal();run.focus();window.lucide?.createIcons();
  }

  setPromptColor(value) {
    if(!Object.hasOwn(promptColors,value))throw Error('Choose one of the available prompt colors');
    if(value==='default')this.draftStorage.removeItem(`${this.draftKey}:color`);
    else this.draftStorage.setItem(`${this.draftKey}:color`,value);
    this.promptColor=value;this.form.style.borderColor=promptColors[value];
  }

  openPromptColor() {
    if(this.colorDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog');dialog.setAttribute('aria-label','Prompt color');
    const choices=node('div','aw-color-swatches');
    const refresh=()=>{for(const button of choices.children)button.setAttribute('aria-pressed',String(button.dataset.color===(this.promptColor || 'default')));};
    for(const [name,color] of Object.entries(promptColors)){
      const button=node('button','aw-color-swatch');button.type='button';button.title=name;
      button.setAttribute('aria-label',name==='default'?'Default prompt color':`${name} prompt color`);
      button.dataset.color=name;button.style.backgroundColor=color;
      button.addEventListener('click',()=>{try{this.setPromptColor(name);refresh();}catch(error){this.error(error);}});
      choices.append(button);
    }
    const close=this.button('Close prompt color','x',()=>dialog.close());
    dialog.append(node('h3','','Prompt color'),close,choices);refresh();
    dialog.addEventListener('close',()=>{dialog.remove();this.input.focus();});
    this.colorDialog=dialog;this.root.append(dialog);dialog.showModal();choices.querySelector('[aria-pressed="true"]').focus();
    window.lucide?.createIcons();
  }

  async openCommands(initialAction=null) {
    if (this.commandsDialog?.open) return;
    const dialog = node('dialog', 'aw-review-dialog aw-commands-dialog');
    dialog.setAttribute('aria-label', 'Commands and skills');
    const search = node('input'); search.type='search'; search.placeholder='Search commands';
    search.setAttribute('aria-label','Search commands');
    const close = this.button('Close commands', 'x', () => dialog.close());
    const status = node('p', '', 'Loading...'); status.setAttribute('role','status');
    const list = node('div', 'aw-command-list');
    let commands=[],busy=false;
    const render = () => {
      list.replaceChildren();
      const query=search.value.toLowerCase();
      const localCommands=this.provider==='Codex'?Object.entries(this.codexCommandControls()).map(([name,control])=>({name,kind:'command',description:control.getAttribute('aria-label'),paneControl:control})):[];
      const matching=[...localCommands,...commands].filter(c => [c.name,c.description,...(c.aliases||[])].join(' ').toLowerCase().includes(query));
      for (const command of matching) {
        const button=node('button','aw-command'); button.type='button';
        button.append(node('strong','',`${command.kind==='skill'?'$':'/'}${command.name}`),node('small','',command.kind==='skill'?command.path:command.argumentHint || ''),node('span','',command.description || ''));
        button.disabled=busy;
        if(command.paneControl)button.disabled=busy || command.paneControl.hidden || command.paneControl.disabled;
        const actionControls={clear:this.clearButton,fork:this.forkButton,resume:this.resumeButton,color:this.colorButton,'reload-plugins':plugins,'reload-skills':reload};
        const localAction=this.provider==='Claude' && Object.hasOwn(actionControls,command.workspaceAction) ? command.workspaceAction : null;
        if(localAction){
          const control=actionControls[localAction];
          button.disabled=busy || control.hidden || (!localAction.startsWith('reload-') && control.disabled);
        }
        if(command.unavailableReason){button.disabled=true;button.title=command.unavailableReason;button.append(node('small','',command.unavailableReason));}
        button.addEventListener('click',()=>{
          if(command.paneControl){dialog.close();this.activateCommandControl(command.paneControl);return;}
          if(localAction){if(!localAction.startsWith('reload-'))dialog.close();actionControls[localAction].click();return;}
          if(command.kind==='skill'){
            if(!this.selectedSkills.some(s=>s.path===command.path))this.selectedSkills.push({name:command.name,path:command.path});
            this.persistSkills();this.renderAttachments();
          }else this.input.value=`/${command.name} ${this.input.value}`;
          this.persistDraft(); dialog.close(); this.input.focus();
        });
        if(this.provider==='Codex' && command.kind==='skill' && typeof command.enabled==='boolean' && this.controls.setSkillEnabled){
          const row=node('div','aw-skill-row');
          const label=node('label','','Enabled in Codex settings');
          const toggle=node('input');toggle.type='checkbox';toggle.checked=command.enabled;toggle.disabled=busy;
          toggle.setAttribute('aria-label',`Enable skill ${command.name}`);label.prepend(toggle);
          toggle.addEventListener('change',async()=>{
            const requested=toggle.checked;toggle.checked=command.enabled;
            if(busy)return;
            busy=true;reload.disabled=true;plugins.disabled=true;render();status.textContent='Saving skill setting...';
            try{
              const result=await this.controls.setSkillEnabled(command.path,requested);
              if(!dialog.open || this.disposed)return;
              commands=result.data;busy=false;render();
              status.textContent=result.effectiveEnabled===requested ? `Skill ${requested?'enabled':'disabled'}` : `Policy kept skill ${result.effectiveEnabled?'enabled':'disabled'}`;
            }catch(error){if(dialog.open){busy=false;render();status.textContent=error.message;}}
            finally{busy=false;reload.disabled=false;plugins.disabled=false;}
          });
          row.append(button,label);list.append(row);
        }else list.append(button);
      }
      status.textContent=matching.length ? `${matching.length} command${matching.length === 1 ? '' : 's'}` : 'No matching commands';
    };
    search.addEventListener('input',render);
    const reload = this.button('Reload skills from disk', 'refresh-cw', async () => {
      if(busy)return;
      busy=true;render();
      reload.disabled=true; plugins.disabled=true; status.textContent='Reloading...';
      try {
        const result=await (this.provider==='Codex'?this.controls.commands():this.controls.reloadSkills());
        if(!dialog.open || this.disposed)return;
        commands=result.data;busy=false;render();
      } catch(error){if(dialog.open){busy=false;render();status.textContent=error.message;}}
      finally{busy=false;reload.disabled=false;plugins.disabled=false;}
    });
    reload.hidden=this.provider==='Codex'?!this.controls.commands:this.provider!=='Claude' || !this.controls.reloadSkills;
    reload.disabled=true;
    const plugins = this.button('Reload plugins from disk', 'plug', async () => {
      if(busy)return;
      busy=true;render();
      plugins.disabled=true;reload.disabled=true;status.textContent='Reloading plugins...';
      try {
        const result=await this.controls.reloadPlugins();
        if(!dialog.open || this.disposed)return;
        commands=result.data;busy=false;render();
        status.textContent+=` · ${result.plugins.length} plugins · ${result.error_count} plugin errors`;
      }catch(error){if(dialog.open){busy=false;render();status.textContent=error.message;}}
      finally{busy=false;plugins.disabled=false;reload.disabled=false;}
    });
    plugins.hidden=this.provider!=='Claude' || !this.controls.reloadPlugins;
    plugins.disabled=true;
    dialog.append(node('h3','','Commands and skills'),close,reload,plugins,search,status,list);
    dialog.addEventListener('close',()=>dialog.remove());
    this.commandsDialog=dialog; this.root.append(dialog); dialog.showModal(); search.focus();
    window.lucide?.createIcons();
    try { const result=await this.controls.commands(); if(!dialog.open || this.disposed)return; commands=result.data; render(); }
    catch(error){if(dialog.open)status.textContent=error.message;}
    finally{reload.disabled=false;plugins.disabled=false;}
    if(dialog.open && !this.disposed){
      const control=initialAction==='reload-plugins'?plugins:initialAction==='reload-skills'?reload:null;
      if(control && !control.hidden && !control.disabled && commands.some(command=>command.workspaceAction===initialAction))control.click();
    }
  }

  openSessionStatus() {
    if(this.sessionStatusDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-session-status-dialog');
    dialog.setAttribute('aria-label','Session status');
    const list=node('dl');
    const close=this.button('Close session status','x',()=>dialog.close());
    const limitStatus=node('p');limitStatus.setAttribute('role','status');
    const refreshLimits=this.button('Refresh account limits','refresh-cw',async()=>{
      refreshLimits.disabled=true;limitStatus.textContent='Checking account limits...';
      try{
        const result=await this.controls.accountRateLimits();
        if(!dialog.open || this.disposed)return;
        this.conversation.metadata.accountLimits=result;this.refreshSessionStatus();
        limitStatus.textContent='Account limits updated';
      }catch(error){if(dialog.open){limitStatus.textContent=`Account limits unavailable: ${error.message}`;limitStatus.scrollIntoView({block:'nearest'});}}
      finally{refreshLimits.disabled=false;}
    });
    refreshLimits.hidden=!this.controls.accountRateLimits;
    this.refreshSessionStatus=()=>{
      const m=this.conversation.metadata;
      const token=value=>Number.isSafeInteger(value) && value>=0 ? value.toLocaleString() : null;
      const rows=[['Session',this.conversation.sessionId],['State',this.sleeping?'sleeping':this.conversation.status],
        ['Project',m.thread?.cwd],['Model',m.model],['Reasoning effort',m.reasoningEffort],['Mode',m.collaborationMode],
        ['Speed tier',m.serviceTier],['Permission profile',m.permissionProfile],
        ['Approval policy',m.approvalPolicy],['Sandbox',m.sandbox || m.sandboxPolicy],
        ['Last request tokens',token(m.tokenUsage?.last?.totalTokens)],
        ['Total tokens',token(m.tokenUsage?.total?.totalTokens)],
        ['Context window',token(m.tokenUsage?.modelContextWindow)]];
      list.replaceChildren();
      for(const [label,value] of rows){
        const display=value==null || value==='' ? 'Unavailable' : typeof value==='object' ? JSON.stringify(value,null,2) : String(value);
        list.append(node('dt','',label),node('dd','',display));
      }
      const limits=m.accountLimits;
      if(!limits)list.append(node('dt','','Account limits'),node('dd','','Not checked'));
      for(const bucket of limits?.limits || []){
        for(const name of ['primary','secondary']){
          const window=bucket[name];
          const minutes=window?.windowDurationMins;
          const duration=Number.isSafeInteger(minutes) && minutes>0 ? minutes%1440===0 ? `${minutes/1440}d` : minutes%60===0 ? `${minutes/60}h` : `${minutes}m` : name;
          const value=node('dd','',window ? `${window.usedPercent}% used` : 'Unavailable');
          const reset=window?.resetsAt==null ? null : new Date(window.resetsAt*1000);
          if(reset && Number.isFinite(reset.getTime()))value.append(node('div','',`Resets ${reset.toLocaleString()}`));
          list.append(node('dt','',`${bucket.name} ${duration}`),value);
        }
      }
      if(limits?.observedAt){
        const checked=new Date(limits.observedAt);
        list.append(node('dt','','Last checked'),node('dd','',Number.isFinite(checked.getTime())?checked.toLocaleString():'Unavailable'));
      }
    };
    dialog.append(node('h3','','Session status'),close,list,refreshLimits,limitStatus);
    dialog.addEventListener('close',()=>{this.refreshSessionStatus=null;this.sessionStatusDialog=null;dialog.remove();this.input.focus();});
    this.sessionStatusDialog=dialog;this.root.append(dialog);this.refreshSessionStatus();
    this.refreshIcons();dialog.showModal();close.focus();
  }

  async openEvents() {
    if(this.eventsDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-events-dialog');dialog.setAttribute('aria-label','Session events');
    const status=node('p');status.setAttribute('role','status');const list=node('div');
    const navigation=node('div','aw-event-navigation');
    const close=this.button('Close session events','x',()=>dialog.close());
    let after=0, cursor=0, busy=false;const previous=[];
    const back=this.button('Previous event page','arrow-left',()=>{if(!busy && previous.length){after=previous.pop();load();}});
    const next=this.button('Next event page','arrow-right',()=>{if(!busy){previous.push(after);after=cursor;load();}});
    const refresh=this.button('Refresh event page','refresh-cw',()=>load());
    const load=async()=>{
      if(busy)return;busy=true;back.disabled=next.disabled=refresh.disabled=true;list.replaceChildren();status.textContent='Loading...';
      try{
        const page=await this.controls.events(after);if(!dialog.open || this.disposed)return;
        if(!Array.isArray(page.events) || !Number.isSafeInteger(page.cursor) || page.cursor<after)throw Error('Invalid event page');
        cursor=page.cursor;
        for(const envelope of page.events){
          const detail=node('details','aw-tool');detail.append(node('summary','',`${envelope.sequence} ${envelope.event.method}`));
          detail.addEventListener('toggle',()=>{
            if(detail.open && !detail.querySelector('pre'))detail.append(node('pre','',JSON.stringify(envelope.event,null,2)));
            else if(!detail.open)detail.querySelector('pre')?.remove();
          });list.append(detail);
        }
        status.textContent=page.events.length?`${page.events[0].sequence} - ${cursor}`:'No events';
        next.disabled=!page.has_more || cursor<=after;
      }catch(error){if(dialog.open)status.textContent=error.message;}
      finally{busy=false;back.disabled=!previous.length;refresh.disabled=false;}
    };
    navigation.append(back,refresh,next);dialog.append(node('h3','','Session events'),close,status,list,navigation);
    dialog.addEventListener('close',()=>{dialog.replaceChildren();dialog.remove();if(this.eventsDialog===dialog)this.eventsDialog=null;});this.eventsDialog=dialog;this.root.append(dialog);this.refreshIcons();dialog.showModal();close.focus();await load();
  }

  async openClaudeEffort() {
    if(this.effortDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog');dialog.setAttribute('aria-label','Claude reasoning effort');
    const status=node('p','','Loading...');status.setAttribute('role','status');
    const form=node('form');const label=node('label','','Effort');const select=node('select');select.setAttribute('aria-label','Claude effort level');label.append(select);
    const apply=node('button','','Apply');apply.type='submit';apply.disabled=true;
    const close=this.button('Close reasoning effort','x',()=>dialog.close());
    let levels=[];
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(apply.disabled || this.sending)return;
      if(!['ready','completed','interrupted','failed'].includes(this.conversation.status)){status.textContent='Finish the current turn before changing effort';return;}
      if(!levels.includes(select.value))return;
      apply.disabled=true;this.sending=true;this.render();
      try {
        // The native local command owns acknowledgement; never infer settings
        // from a successful transport receipt or consume the composer's draft.
        await this.controls.submit({text:`/effort ${select.value}`,files:[],options:{}});
        dialog.close();
      } catch(error){if(dialog.open)status.textContent=error.message;}
      finally{this.sending=false;apply.disabled=false;this.render();}
    });
    form.append(label,apply);dialog.append(node('h3','','Reasoning effort'),close,status,form);
    dialog.addEventListener('close',()=>dialog.remove());this.effortDialog=dialog;this.root.append(dialog);this.refreshIcons();dialog.showModal();close.focus();
    try {
      const commands=await this.controls.commands();
      const command=commands.data.find(c=>c.name==='effort');
      if(!command || command.unavailableReason)throw Error(command?.unavailableReason || 'Claude did not advertise an effort command');
      const catalog=await this.controls.models();if(!dialog.open || this.disposed)return;
      const current=this.conversation.metadata.model;
      const model=catalog.data.find(m=>m.model===current || m.claudeCapabilities?.resolvedModel===current) || (!current && catalog.data.find(m=>m.model==='default'));
      levels=(model?.claudeCapabilities?.supportedEffortLevels || []).filter(level=>['low','medium','high','xhigh','max'].includes(level));
      if(!model?.claudeCapabilities?.supportsEffort || !levels.length)throw Error('Effort choices are unavailable for the current model');
      for(const level of levels){const option=node('option','',level);option.value=level;select.append(option);}
      const choose=node('option','','Select effort');choose.value='';choose.disabled=true;select.prepend(choose);select.value='';
      select.addEventListener('change',()=>{apply.disabled=!levels.includes(select.value);});
      status.textContent='';select.focus();
    }catch(error){if(dialog.open)status.textContent=error.message;}
  }

  async openAgents() {
    if(this.agentsDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-agents-dialog');dialog.setAttribute('aria-label','Delegated agents');
    const status=node('p','','Loading agents...');status.setAttribute('role','status');
    const list=node('div','aw-agent-list');list.setAttribute('aria-label','Agent threads');
    const output=node('section','aw-agent-history');output.setAttribute('aria-label','Selected agent history');
    const refresh=this.button('Refresh agents','refresh-cw',()=>loadList(true));
    const more=this.button('More agents','chevron-down',()=>loadList(false));more.hidden=true;
    const reload=this.button('Refresh selected agent','refresh-cw',()=>inspect(selected,false));reload.hidden=true;
    const earlier=this.button('Earlier agent turns','arrow-up',()=>inspect(selected,true));earlier.hidden=true;
    const close=this.button('Close agents','x',()=>dialog.close());
    const stopConfirm=node('input');stopConfirm.type='checkbox';
    const stopLabel=node('label','aw-agent-stop');stopLabel.append(stopConfirm,document.createTextNode(' Stop this agent\'s current turn'));stopLabel.hidden=true;
    const stop=this.button('Stop agent turn','square',()=>stopAgent());stop.hidden=true;
    let busy=false,selected=null,listCursor=null,historyCursor=null,activeTurn=null;
    const stops=new Set();
    const listCursors=new Set(),historyCursors=new Set(),rows=new Map(),turns=new Map();
    const changes=new Map();
    this.notifyAgentChange=params=>{
      changes.set(params.agentThreadId,(changes.get(params.agentThreadId)||0)+1);
      if(selected===params.agentThreadId)status.textContent='Agent changed; refresh snapshot';
    };
    const enable=()=>{
      refresh.disabled=more.disabled=reload.disabled=earlier.disabled=busy;
      for(const row of rows.values())row.disabled=busy;
      stop.hidden=stopLabel.hidden=!this.controls.interruptAgent || !activeTurn;
      stop.disabled=busy || !stopConfirm.checked || stops.has(JSON.stringify([selected,activeTurn]));
      stopConfirm.disabled=busy;
    };
    stopConfirm.addEventListener('change',enable);
    const stopAgent=async()=>{
      if(busy || stop.disabled || !activeTurn)return;
      const target=selected,turn=activeTurn;busy=true;enable();
      try{
        const result=await this.controls.interruptAgent(target,turn);
        if(result?.requested!==true || result.threadId!==target || result.turnId!==turn)throw Error('Agent interruption was not confirmed');
        stops.add(JSON.stringify([target,turn]));
        if(dialog.open)status.textContent='Stop requested; refresh agent status';
      }catch(error){if(dialog.open)status.textContent=error.message;}
      finally{busy=false;stopConfirm.checked=false;enable();}
    };
    const inspect=async(id,older)=>{
      if(busy || !id)return;busy=true;enable();status.textContent='Reading agent snapshot...';
      const revision=changes.get(id)||0;
      try{
        const result=await this.controls.inspectAgent(id,older?historyCursor:null);
        if(!dialog.open)return;
        if(result.thread?.id!==id || !Array.isArray(result.thread.turns))throw Error('Agent identity or history is invalid');
        if(older && result.historyCursor && historyCursors.has(result.historyCursor))throw Error('Agent history pagination did not advance');
        if(!older){
          turns.clear();historyCursors.clear();stopConfirm.checked=false;
          const running=result.thread.turns.filter(turn=>turn.status==='inProgress');
          activeTurn=running.length===1?running[0].id:null;
        }
        const merged=new Map(result.thread.turns.map(turn=>[turn.id,turn]));
        for(const [key,value] of turns)if(!merged.has(key))merged.set(key,value);
        turns.clear();for(const [key,value] of merged)turns.set(key,value);
        selected=id;historyCursor=result.historyCursor;historyCursors.add(historyCursor);
        for(const [key,row] of rows)row.setAttribute('aria-pressed',String(key===selected));
        output.replaceChildren(node('h4','',result.thread.agentNickname || result.thread.name || 'Agent'),node('code','aw-agent-id',id));
        for(const turn of turns.values()){
          output.append(node('p','aw-author',`Turn ${turn.id} / ${turn.status || 'Unknown'}`));
          for(const item of turn.items)output.append(this.renderItem(item,{historyImages:false,userLabel:'Agent input'}));
        }
        if(!turns.size)output.append(node('p','','No persisted turns'));
        status.textContent=(changes.get(id)||0)!==revision?'Agent changed; refresh snapshot':`Snapshot / ${result.thread.status?.type || 'Status unavailable'}`;
        reload.hidden=false;earlier.hidden=!historyCursor;this.refreshIcons();
      }catch(error){if(dialog.open)status.textContent=error.message;}
      finally{busy=false;enable();}
    };
    const loadList=async reset=>{
      if(busy)return;busy=true;enable();status.textContent='Loading agents...';
      try{
        const result=await this.controls.agents(reset?null:listCursor);
        if(!dialog.open)return;
        if(!Array.isArray(result.data))throw Error('Agent list is unavailable');
        if(!reset && result.nextCursor && listCursors.has(result.nextCursor))throw Error('Agent list pagination did not advance');
        if(reset){list.replaceChildren();rows.clear();listCursors.clear();}
        for(const thread of result.data){
          if(rows.has(thread.id))continue;
          const row=node('button','aw-agent-row');row.type='button';row.setAttribute('aria-pressed',String(thread.id===selected));
          row.append(node('strong','',thread.agentNickname || thread.name || 'Agent'),node('code','aw-agent-id',thread.id),node('small','',thread.status?.type || 'Status unavailable'));
          if(thread.agentRole)row.append(node('small','',thread.agentRole));
          row.addEventListener('click',()=>inspect(thread.id,false));rows.set(thread.id,row);list.append(row);
        }
        listCursor=result.nextCursor;listCursors.add(listCursor);more.hidden=!listCursor;
        status.textContent=rows.size?'Select an agent':'No delegated agents';
      }catch(error){if(dialog.open)status.textContent=error.message;}
      finally{busy=false;enable();}
    };
    dialog.addEventListener('close',()=>{this.notifyAgentChange=null;dialog.remove();this.input.focus();});
    dialog.append(node('h3','','Delegated agents'),close,refresh,status,list,more,reload,earlier,stopLabel,stop,output);
    this.agentsDialog=dialog;this.root.append(dialog);dialog.showModal();close.focus();this.refreshIcons();await loadList(true);
  }

  async openGoal() {
    if(this.goalDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-goal-dialog');dialog.setAttribute('aria-label','Session goal');
    const status=node('p','','Loading...');status.setAttribute('role','status');
    const objective=node('textarea');objective.setAttribute('aria-label','Goal objective');objective.maxLength=4000;
    const mode=node('select');mode.setAttribute('aria-label','Goal status');
    for(const value of ['active','paused','complete']){const option=node('option','',value);option.value=value;mode.append(option);}
    const budget=node('input');budget.type='number';budget.min='1';budget.step='1';budget.placeholder='Unlimited';budget.setAttribute('aria-label','Goal token budget');
    const confirm=node('input');confirm.type='checkbox';
    const label=node('label');label.append(confirm,document.createTextNode(' Confirm goal changes. A different objective resets usage accounting.'));
    const apply=node('button','','Apply');apply.type='button';
    const clear=this.button('Clear goal','trash-2',()=>save(true));
    const refresh=this.button('Refresh goal','refresh-cw',()=>load());
    const close=this.button('Close goal','x',()=>dialog.close());
    let expected=null,loaded=false,busy=false;
    const enable=()=>{apply.disabled=clear.disabled=!loaded || busy || !confirm.checked;refresh.disabled=busy;close.disabled=busy;};
    confirm.addEventListener('change',enable);
    const render=result=>{
      expected=result.goal;loaded=true;confirm.checked=false;
      objective.value=expected?.objective || '';mode.value=expected?.status || 'paused';
      budget.value=expected?.tokenBudget ?? '';
      status.textContent=expected?`${expected.status} / ${expected.tokensUsed} tokens / ${expected.timeUsedSeconds}s`:'No goal';
      enable();
    };
    const load=async()=>{
      if(busy)return;busy=true;loaded=false;enable();
      try{const result=await this.controls.goal();if(dialog.open)render(result);}
      catch(error){status.textContent=error.message;}
      finally{busy=false;enable();}
    };
    const save=async remove=>{
      if(busy || !loaded || !confirm.checked)return;
      const changes={};
      if(!remove){
        if(!objective.value.trim() || !mode.value || !budget.checkValidity()){status.textContent='Enter a goal, status and valid token budget';return;}
        if(objective.value!==expected?.objective)changes.objective=objective.value;
        if(mode.value!==expected?.status)changes.status=mode.value;
        const value=budget.value===''?null:Number(budget.value);
        if(value!==expected?.tokenBudget)changes.tokenBudget=value;
        if(!Object.keys(changes).length){status.textContent='No changes';return;}
      }
      busy=true;enable();objective.disabled=mode.disabled=budget.disabled=true;
      try{const result=remove?await this.controls.clearGoal(expected):await this.controls.updateGoal(changes,expected);if(dialog.open)render(result);}
      catch(error){status.textContent=error.message;}
      finally{busy=false;objective.disabled=mode.disabled=budget.disabled=false;confirm.checked=false;enable();}
    };
    apply.addEventListener('click',()=>save(false));
    dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
    dialog.addEventListener('close',()=>{dialog.remove();this.input.focus();});
    const field=(name,input)=>{const label=node('label','',name);label.append(input);return label;};
    dialog.append(node('h3','','Session goal'),close,refresh,status,field('Objective',objective),field('Status',mode),field('Token budget',budget),label,apply,clear);
    this.goalDialog=dialog;this.root.append(dialog);dialog.showModal();close.focus();this.refreshIcons();enable();await load();
  }

  async openPersonality() {
    if(this.personalityDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog');dialog.setAttribute('aria-label','Codex personality');
    const status=node('p','','Loading...');status.setAttribute('role','status');
    const select=node('select');select.setAttribute('aria-label','Personality');select.disabled=true;
    const apply=node('button','','Apply');apply.type='button';apply.disabled=true;
    const close=this.button('Close personality','x',()=>dialog.close());
    let busy=false;
    select.addEventListener('change',()=>{apply.disabled=busy || !select.value;});
    const render=result=>{
      select.replaceChildren();
      const unknown=node('option','','Select personality');unknown.value='';unknown.disabled=true;select.append(unknown);
      for(const value of result.options){const option=node('option','',value);option.value=value;select.append(option);}
      select.value=result.currentValue || '';select.disabled=!result.options.length;
      apply.disabled=!select.value;
      status.textContent=result.options.length?`Last confirmed: ${result.currentValue || 'Unavailable'}`:'This model does not support personality';
    };
    apply.addEventListener('click',async()=>{
      if(busy || !select.value)return;
      busy=true;apply.disabled=true;select.disabled=true;close.disabled=true;
      try{const result=await this.controls.setPersonality(select.value);if(dialog.open)render(result);}
      catch(error){status.textContent=error.message;select.disabled=false;apply.disabled=false;}
      finally{busy=false;close.disabled=false;}
    });
    dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
    dialog.addEventListener('close',()=>{dialog.remove();this.input.focus();});
    dialog.append(node('h3','','Codex personality'),close,status,select,apply);
    this.personalityDialog=dialog;this.root.append(dialog);dialog.showModal();close.focus();this.refreshIcons();
    try{const result=await this.controls.personality();if(dialog.open && !this.disposed)render(result);}
    catch(error){status.textContent=error.message;}
  }

  async openSessionMode() {
    if(this.sessionModeDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog');dialog.setAttribute('aria-label','Session mode');
    const status=node('p','','Loading modes...');status.setAttribute('role','status');
    const description=node('p');let choices=[];
    const select=node('select');select.setAttribute('aria-label','Session mode');select.disabled=true;
    select.addEventListener('change',()=>{description.textContent=choices.find(choice=>choice.value===select.value)?.description || '';apply.disabled=!choices.some(choice=>choice.value===select.value) || !['ready','completed','interrupted','failed'].includes(this.conversation.status);});
    const apply=node('button','','Apply');apply.type='button';apply.disabled=true;
    const close=this.button('Close session mode','x',()=>dialog.close());
    const render=result=>{
      choices=result.options;
      select.replaceChildren();
      for(const choice of result.options){const option=node('option','',choice.name);option.value=choice.value;option.title=choice.description || '';select.append(option);}
      select.value=result.currentValue;select.disabled=false;
      description.textContent=choices.find(choice=>choice.value===select.value)?.description || '';
      status.textContent=`Last confirmed: ${result.options.find(choice=>choice.value===result.currentValue)?.name || result.currentValue || 'Unavailable'}`;
      apply.disabled=!choices.some(choice=>choice.value===select.value) || !['ready','completed','interrupted','failed'].includes(this.conversation.status);
    };
    apply.addEventListener('click',async()=>{
      apply.disabled=true;select.disabled=true;
      try{const result=await this.controls.setSessionMode(select.value);if(dialog.open)render(result);}
      catch(error){if(dialog.open)status.textContent=error.message;}
    });
    dialog.append(node('h3','','Session mode'),close,status,select,description,apply);
    dialog.addEventListener('close',()=>dialog.remove());this.sessionModeDialog=dialog;
    this.root.append(dialog);this.refreshIcons();dialog.showModal();close.focus();
    try{const result=await this.controls.sessionModes();if(dialog.open && !this.disposed)render(result);}
    catch(error){if(dialog.open)status.textContent=error.message;}
  }

  async openPermissions() {
    if(this.permissionsDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog');dialog.setAttribute('aria-label','Permission mode');
    const form=node('form');const status=node('p');status.setAttribute('role','status');status.textContent='Loading...';
    const label=node('label','','Mode');const select=node('select');select.setAttribute('aria-label','Permission mode');label.append(select);
    const codex=this.provider==='Codex';
    const confirmLabel=node('label','',codex?'Apply this permission profile to subsequent turns':'Allow tools without permission prompts');const confirm=node('input');confirm.type='checkbox';confirmLabel.prepend(confirm);confirmLabel.hidden=!codex;
    const apply=node('button','','Apply');apply.type='submit';apply.disabled=true;
    const close=this.button('Close permission mode','x',()=>dialog.close());
    const labels=codex?{}:{default:'Ask when needed',acceptEdits:'Accept file edits',plan:'Plan',dontAsk:'Deny unapproved tools',auto:'Automatic decisions',bypassPermissions:'Bypass permission prompts'};
    let busy=false;
    select.addEventListener('change',()=>{confirm.checked=false;confirmLabel.hidden=!codex && select.value!=='bypassPermissions';});
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(busy || apply.disabled)return;
      if((codex || select.value==='bypassPermissions') && !confirm.checked){status.textContent=codex?'Confirm applying the permission profile first':'Confirm bypassing permission prompts first';return;}
      busy=true;apply.disabled=true;select.disabled=true;
      try{const result=await this.controls.setPermissions(select.value,confirm.checked);if(dialog.open){select.value=result.mode;status.textContent=`Last confirmed: ${labels[result.mode] || result.mode}`;}}
      catch(error){if(dialog.open)status.textContent=error.message;}
      finally{busy=false;apply.disabled=false;select.disabled=false;}
    });
    form.append(label,confirmLabel,apply);dialog.append(node('h3','','Permission mode'),close,status,form);
    dialog.addEventListener('close',()=>dialog.remove());this.permissionsDialog=dialog;this.root.append(dialog);this.refreshIcons();dialog.showModal();close.focus();
    try{
      const result=await this.controls.permissions();if(!dialog.open || this.disposed)return;
      for(const mode of result.modes){const profile=result.profiles?.find(p=>p.id===mode);const option=node('option','',labels[mode] || mode);option.value=mode;option.disabled=profile?.allowed===false;option.title=profile?.description || '';select.append(option);}
      if(result.mode && result.modes.includes(result.mode))select.value=result.mode;
      else {const unknown=node('option','','Select a mode');unknown.value='';unknown.disabled=true;select.prepend(unknown);select.value='';}
      status.textContent=result.mode?`Last confirmed: ${labels[result.mode] || result.mode}`:'Current mode unavailable';
      confirmLabel.hidden=!codex && select.value!=='bypassPermissions';apply.disabled=!result.modes.length;
    }catch(error){if(dialog.open)status.textContent=error.message;}
  }

  openShell() {
    if(this.shellDialog?.open || this.shellSubmitting)return;
    const dialog=node('dialog','aw-review-dialog');dialog.setAttribute('aria-label','Run shell command');
    const close=this.button('Close shell command','x',()=>dialog.close());
    const input=node('textarea');input.setAttribute('aria-label','Shell command');input.rows=4;
    const confirm=node('input');confirm.type='checkbox';
    const label=node('label');label.append(confirm,document.createTextNode('Run outside the Codex sandbox'));
    const status=node('p');status.setAttribute('role','status');
    const run=this.button('Run command','play',async()=>{
      if(this.shellSubmitting || !confirm.checked || !input.value.trim())return;
      this.shellSubmitting=true;run.disabled=true;input.disabled=true;confirm.disabled=true;
      try {await this.controls.shellCommand(input.value,true);if(dialog.open)dialog.close();}
      catch(error){if(dialog.open)status.textContent=error.message;}
      finally{this.shellSubmitting=false;run.disabled=false;input.disabled=false;confirm.disabled=false;}
    });
    dialog.append(node('h3','','Run shell command'),close,input,label,status,run);
    dialog.addEventListener('close',()=>dialog.remove());this.shellDialog=dialog;this.root.append(dialog);this.refreshIcons();dialog.showModal();input.focus();
  }

  openContext() {
    if(this.contextDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-context-dialog');dialog.setAttribute('aria-label','Context breakdown');
    const status=node('p');status.setAttribute('role','status');const content=node('div');
    const close=this.button('Close context breakdown','x',()=>dialog.close());
    const refresh=this.button('Refresh context breakdown','refresh-cw',()=>load());
    let busy=false;
    const load=async()=>{
      if(busy || !dialog.open)return;
      busy=true;refresh.disabled=true;status.textContent='Loading...';
      try{
        const usage=await this.controls.contextUsage();
        if(!dialog.open || this.disposed)return;
        content.replaceChildren(node('p','',usage.model),node('p','',`${usage.totalTokens.toLocaleString()} / ${usage.maxTokens.toLocaleString()} tokens`));
        const progress=node('progress');progress.max=100;progress.value=Math.min(100,usage.percentage);progress.setAttribute('aria-label','Context used');content.append(progress);
        const list=node('dl');
        for(const category of usage.categories){list.append(node('dt','',category.name+(category.isDeferred?' (deferred)':'')),node('dd','',category.tokens.toLocaleString()));}
        content.append(list);status.textContent=`${usage.percentage.toFixed(1)}% used`;
      }catch(error){if(dialog.open){content.replaceChildren();status.textContent=error.message;}}
      finally{busy=false;refresh.disabled=false;}
    };
    dialog.append(node('h3','','Context breakdown'),refresh,close,status,content);
    dialog.addEventListener('close',()=>dialog.remove());this.contextDialog=dialog;this.root.append(dialog);this.refreshIcons();dialog.showModal();close.focus();load();
  }

  openMcpServers() {
    if(this.mcpDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-mcp-dialog');dialog.setAttribute('aria-label','MCP connections');
    const list=node('div');const status=node('p');status.setAttribute('role','status');
    const close=this.button('Close MCP connections','x',()=>dialog.close());
    let busy=false,refreshPending=false;
    const refresh=this.button('Refresh MCP connections','refresh-cw',()=>load());
    const reload=this.button('Reload MCP configuration','rotate-cw',()=>load(null,'reload'));
    reload.hidden=this.provider!=='Codex' || !this.controls.mcpReload;
    const load=async(name,action)=>{
      if(busy || !dialog.open)return;
      busy=true;refresh.disabled=true;reload.disabled=true;
      for(const control of list.querySelectorAll('button,input'))control.disabled=true;
      status.textContent='Loading...';
      try{
        let result;
        if(action==='login') {await this.controls.mcpLogin(name);result=await this.controls.mcpServers();}
        else if(action==='reload')result=await this.controls.mcpReload();
        else if(this.provider==='Codex' && ['enable','disable'].includes(action))result=await this.controls.setMcpEnabled(name,action==='enable');
        else result=action ? await this.controls.mcpServerControl(name,action) : await this.controls.mcpServers();
        if(!dialog.open || this.disposed)return;
        list.replaceChildren();
        for(const server of result.data){
          const row=node('div','aw-background-task aw-mcp-server');row.append(node('strong','',server.name),node('p','',server.status));
          if(this.provider==='Codex'){
            row.append(node('p','',`Authentication: ${server.authStatus || 'unknown'}`),node('p','',`${server.toolCount ?? 0} tools`));
            if(typeof server.enabled==='boolean'){
              const label=node('label','','Enabled in Codex user settings');const toggle=node('input');toggle.type='checkbox';toggle.checked=server.enabled;
              toggle.setAttribute('aria-label',`Enable ${server.name}`);toggle.disabled=!server.settingsWritable || !this.controls.setMcpEnabled;
              toggle.addEventListener('change',()=>{const action=toggle.checked?'enable':'disable';toggle.checked=server.enabled;load(server.name,action);});
              label.prepend(toggle);row.append(label);
            }
            if(this.controls.mcpLogin && ['notLoggedIn','oAuth'].includes(server.authStatus)){
              const login=this.button(`Sign in to ${server.name}`,'log-in',()=>load(server.name,'login'));
              login.disabled=['pending','uncertain'].includes(server.login?.status);row.append(login);
            }
            if(server.login){
              row.append(node('p','',`Login: ${server.login.status}`));
              if(server.login.error)row.append(node('p','',server.login.error));
              if(server.login.status==='pending' && server.login.authorizationUrl){
                try{
                  const url=new URL(server.login.authorizationUrl);
                  if(!['https:','http:'].includes(url.protocol) || url.username || url.password)throw Error('Unsafe authorization URL');
                  const link=node('a','','Continue authorization');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';row.append(link);
                }catch{row.append(node('p','','Authorization URL unavailable'));}
              }
            }
            list.append(row);continue;
          }
          const label=node('label','','Enabled');const toggle=node('input');toggle.type='checkbox';toggle.checked=server.status!=='disabled';toggle.setAttribute('aria-label',`Enable ${server.name}`);
          toggle.disabled=!this.controls.mcpServerControl;
          toggle.addEventListener('change',()=>{const action=toggle.checked?'enable':'disable';toggle.checked=server.status!=='disabled';load(server.name,action);});label.prepend(toggle);
          const reconnect=this.button(`Reconnect ${server.name}`,'refresh-cw',()=>load(server.name,'reconnect'));
          reconnect.disabled=!this.controls.mcpServerControl || server.status==='disabled';
          row.append(label,reconnect);list.append(row);
        }
        status.textContent=result.notice || (result.data.length ? `${result.data.length} connection${result.data.length===1?'':'s'}` : 'No MCP connections');
        window.lucide?.createIcons();
      }catch(error){if(dialog.open)status.textContent=error.message;}
      finally{
        busy=false;refresh.disabled=false;reload.disabled=false;
        if(refreshPending){refreshPending=false;load();}
      }
    };
    dialog.append(node('h3','','MCP connections'),refresh,reload,close,status,list);
    this.refreshMcp=()=>{if(busy)refreshPending=true;else load();};
    dialog.addEventListener('close',()=>{this.refreshMcp=null;dialog.remove();});this.mcpDialog=dialog;
    this.root.append(dialog);dialog.showModal();close.focus();load();
  }

  openProjectDiff() {
    if(this.diffDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog');dialog.setAttribute('aria-label','Project diff');
    dialog.style.width='min(1000px, calc(100vw - 32px))';
    const status=node('p');status.setAttribute('role','status');const content=node('div');
    const load=async()=>{
      refresh.disabled=true;status.textContent='Loading...';
      try{
        const result=await this.controls.projectDiff();
        if(!dialog.open || this.disposed)return;
        content.replaceChildren();
        for(const key of ['staged','unstaged','untracked']){
          if(!result[key])continue;
          const details=node('details');details.open=true;
          const output=node('pre','',result[key]);output.style.whiteSpace='pre-wrap';output.style.overflowWrap='anywhere';
          details.append(node('summary','',key[0].toUpperCase()+key.slice(1)),output);content.append(details);
        }
        status.textContent=result.omitted.length?`Not displayed (symlinks or non-regular files): ${result.omitted.join(', ')}`
          :content.childElementCount?'Current working-tree snapshot':'No changes';
        status.style.overflowWrap='anywhere';
      }catch(error){if(dialog.open)status.textContent=error.message;}
      finally{refresh.disabled=false;}
    };
    const refresh=this.button('Refresh project diff','refresh-cw',load);
    const close=this.button('Close project diff','x',()=>dialog.close());
    dialog.append(node('h3','','Project diff'),close,refresh,status,content);
    dialog.addEventListener('close',()=>{dialog.remove();this.input.focus();});
    this.diffDialog=dialog;this.root.append(dialog);dialog.showModal();close.focus();this.refreshIcons();load();
  }

  openRewind() {
    if(this.rewindDialog?.open)return;
    const turns=[...this.conversation.turns.values()];
    const latest=turns.at(-1)?.id;
    if(!latest || this.conversation.status==='running'){this.error(Error('Finish the active turn before rewinding history'));return;}
    const dialog=node('dialog','aw-review-dialog aw-commands-dialog aw-rewind-dialog');dialog.setAttribute('aria-label','Rewind conversation');
    const form=node('form');const select=node('select');select.setAttribute('aria-label','Rewind before turn');
    for(const turn of turns){
      const message=[...turn.items.values()].find(item=>item.type==='userMessage');
      const text=(message?.content || []).filter(part=>part.type==='text').map(part=>part.text).join(' ');
      const option=node('option','',`${turn.id} - ${(text || 'Turn').slice(0,100)}`);option.value=turn.id;select.append(option);
    }
    select.value=latest;
    const confirm=node('input');confirm.type='checkbox';confirm.required=true;
    const label=node('label');label.append(confirm,document.createTextNode(' Remove this turn and all later conversation history. Project files stay unchanged.'));
    const status=node('p');status.setAttribute('role','alert');
    const save=node('button','','Rewind');save.type='submit';
    const close=this.button('Cancel rewind','x',()=>dialog.close());
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(save.disabled || !confirm.checked)return;
      save.disabled=true;close.disabled=true;select.disabled=true;confirm.disabled=true;status.textContent='';
      try{
        await this.controls.revertHistory({before_turn_id:select.value,expected_latest_turn_id:latest,confirmed:true});
        if(!this.disposed)dialog.close();
      }catch(error){status.textContent=error.message;}
      finally{save.disabled=false;close.disabled=false;select.disabled=false;confirm.disabled=false;}
    });
    dialog.addEventListener('cancel',event=>{if(save.disabled)event.preventDefault();});
    dialog.addEventListener('close',()=>{dialog.remove();this.input.focus();});
    form.append(select,label,status,save);dialog.append(node('h3','','Rewind conversation'),close,form);
    this.rewindDialog=dialog;this.root.append(dialog);dialog.showModal();select.focus();this.refreshIcons();
  }

  openRename() {
    if(this.renameDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-commands-dialog');dialog.setAttribute('aria-label','Rename conversation');
    const form=node('form');const input=node('input');input.required=true;input.maxLength=1000;input.setAttribute('aria-label','Conversation title');
    const original=this.input.value;
    const command=/^\/rename(?:\s+(.+))?$/.exec(original.trim());
    if(command?.[1])input.value=command[1];
    const status=node('p');status.setAttribute('role','alert');
    const save=node('button','','Rename');save.type='submit';
    const close=this.button('Cancel rename','x',()=>dialog.close());
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(save.disabled)return;
      save.disabled=true;close.disabled=true;input.disabled=true;status.textContent='';
      try{
        await this.controls.renameSession(input.value);
        if(this.disposed)return;
        if(command && this.input.value===original){this.input.value='';this.persistDraft();}
        dialog.close();
      }catch(error){status.textContent=error.message;}
      finally{save.disabled=false;close.disabled=false;input.disabled=false;}
    });
    dialog.addEventListener('cancel',event=>{if(save.disabled)event.preventDefault();});
    dialog.addEventListener('close',()=>{dialog.remove();this.input.focus();});
    form.append(input,status,save);dialog.append(node('h3','','Rename conversation'),close,form);
    this.renameDialog=dialog;this.root.append(dialog);dialog.showModal();input.focus();this.refreshIcons();
  }

  openApps() {
    if(this.appsDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-commands-dialog');dialog.setAttribute('aria-label','Apps and connectors');
    const status=node('p');status.setAttribute('role','status');
    const search=node('input');search.type='search';search.setAttribute('aria-label','Search apps');
    const list=node('div');let apps=[];
    const render=()=>{
      list.replaceChildren();
      for(const app of apps.filter(a=>`${a.name} ${a.description}`.toLowerCase().includes(search.value.toLowerCase()))){
        const row=node('div','aw-background-task');row.style.display='block';row.style.overflowWrap='anywhere';
        row.append(node('strong','',app.name),node('p','',app.description));
        row.append(node('small','',app.callable?'Available':!app.accessible?'Not connected':!app.enabled?'Disabled':'Unavailable under current policy'));
        const select=this.button(`Select app ${app.name}`,'plus',()=>{
          if(this.selectedApps.length>=20){status.textContent='Select at most 20 apps';return;}
          if(!this.selectedApps.some(a=>a.id===app.id))this.selectedApps.push({id:app.id,name:app.name});
          this.persistApps();this.renderAttachments();
          if(this.input.value.trim()==='/apps'){this.input.value='';this.persistDraft();}
          dialog.close();this.input.focus();
        });
        select.disabled=!app.callable || this.selectedApps.some(a=>a.id===app.id);row.append(select);list.append(row);
      }
      this.refreshIcons();
    };
    const load=async()=>{
      refresh.disabled=true;status.textContent='Loading...';
      try{const result=await this.controls.apps();if(!dialog.open || this.disposed)return;apps=result.data;render();status.textContent=apps.length?`${apps.length} installed apps`:'No installed apps in this session';}
      catch(error){if(dialog.open){apps=[];render();status.textContent=error.message;}}
      finally{refresh.disabled=false;}
    };
    const refresh=this.button('Refresh apps','refresh-cw',load);
    const close=this.button('Close apps','x',()=>dialog.close());
    search.addEventListener('input',render);
    dialog.append(node('h3','','Apps and connectors'),close,refresh,search,status,list);
    dialog.addEventListener('close',()=>{dialog.remove();this.input.focus();});
    this.appsDialog=dialog;this.root.append(dialog);dialog.showModal();search.focus();this.refreshIcons();load();
  }

  openHooks() {
    if(this.hooksDialog?.open)return;
    const dialog=node('dialog','aw-review-dialog aw-commands-dialog');dialog.setAttribute('aria-label','Lifecycle hooks');
    const status=node('p');status.setAttribute('role','status');const list=node('div');
    const load=async()=>{
      refresh.disabled=true;status.textContent='Loading...';
      try{
        const result=await this.controls.hooks();
        if(!dialog.open || this.disposed)return;
        list.replaceChildren();
        for(const hook of result.data){
          const row=node('div','aw-background-task');
          row.style.overflowWrap='anywhere';row.style.display='block';
          row.append(node('strong','',hook.eventName),node('p','',`${hook.enabled?'Enabled':'Disabled'} · ${hook.trustStatus}${hook.isManaged?' · Managed':''}`));
          const detail=node('pre','',hook.command || [hook.server,hook.tool].filter(Boolean).join(' / ') || hook.handlerType);
          detail.style.whiteSpace='pre-wrap';detail.style.overflowWrap='anywhere';row.append(detail);
          const source=node('p','',`${hook.source}: ${hook.sourcePath}`);source.style.overflowWrap='anywhere';row.append(source);
          for(const key of ['matcher','pluginId','statusMessage'])if(hook[key])row.append(node('p','',`${key}: ${hook[key]}`));
          list.append(row);
        }
        status.textContent=[`${result.data.length} hook${result.data.length===1?'':'s'}`,...result.warnings,...result.errors.map(e=>`${e.path}: ${e.message}`)].join('\n');
        status.style.whiteSpace='pre-wrap';status.style.overflowWrap='anywhere';
      }catch(error){if(dialog.open)status.textContent=error.message;}
      finally{refresh.disabled=false;}
    };
    const refresh=this.button('Refresh hooks','refresh-cw',load);
    const close=this.button('Close hooks','x',()=>dialog.close());
    dialog.append(node('h3','','Lifecycle hooks'),close,refresh,status,list);
    dialog.addEventListener('close',()=>{dialog.remove();this.input.focus();});
    this.hooksDialog=dialog;this.root.append(dialog);dialog.showModal();close.focus();this.refreshIcons();load();
  }

  openBackgroundTasks() {
    if (this.tasksDialog?.open) return;
    const dialog = node('dialog', 'aw-review-dialog aw-tasks-dialog');
    dialog.setAttribute('aria-label', 'Background tasks');
    const heading = node('h3', '', 'Background tasks');
    const list = node('div', 'aw-task-list');
    const status = node('p'); status.setAttribute('role', 'status');
    const refresh = this.button('Refresh background tasks', 'refresh-cw', () => load());
    const close = this.button('Close background tasks', 'x', () => dialog.close());
    const terminate=async id=>{
      const result=await this.controls.terminateBackgroundTask(id);
      if(!result || (result.pending!==true && typeof result.terminated!=='boolean'))throw Error('Task termination was not confirmed');
      return result;
    };
    const confirmed=node('input');confirmed.type='checkbox';
    const confirmation=node('label');confirmation.append(confirmed,document.createTextNode(' Stop all listed background tasks'));
    const stopAll=this.button('Stop all listed tasks','square',async()=>{
      if(busy || !confirmed.checked || !tasks.size)return;
      busy=true;refresh.disabled=true;close.disabled=true;confirmed.disabled=true;stopAll.disabled=true;
      for(const entry of tasks.values())entry.stop.disabled=true;
      let stopped=0,pending=0;
      try{
        for(const [id,entry] of [...tasks]){
          if(entry.pending){pending++;continue;}
          const result=await terminate(id);
          if(result.pending){pending++;entry.pending=true;}else{entry.row.remove();tasks.delete(id);stopped++;}
        }
        status.textContent=`${stopped} stopped; ${pending} stop requests pending`;
      }catch(error){status.textContent=`${stopped} stopped; ${pending} pending. ${error.message}`;}
      finally{
        busy=false;refresh.disabled=false;close.disabled=false;confirmed.disabled=false;confirmed.checked=false;
        for(const entry of tasks.values())entry.stop.disabled=Boolean(entry.pending);
      }
    });
    stopAll.disabled=true;confirmation.hidden=stopAll.hidden=!this.controls.terminateBackgroundTask;
    confirmed.addEventListener('change',()=>{stopAll.disabled=busy || !confirmed.checked || !tasks.size;});
    const tasks=new Map();
    let busy = false;
    const load = async () => {
      if (busy || !dialog.open) return;
      busy = true; refresh.disabled = true; stopAll.disabled=true;confirmed.checked=false;tasks.clear();list.replaceChildren();status.textContent = 'Loading...';
      try {
        const result = await this.controls.backgroundTasks();
        if (!dialog.open || this.disposed) return;
        list.replaceChildren();tasks.clear();
        for (const task of result.data) {
          const row = node('div', 'aw-background-task');
          const command = node('pre', '', task.command);
          const location = node('small', '', task.cwd);
          const stop = this.button(`Stop task ${task.processId}`, 'square', async () => {
            if(busy)return;
            busy=true;stop.disabled = true;stopAll.disabled=true;refresh.disabled=true;
            try {
              const result = await terminate(task.processId);
              status.textContent = result.pending ? 'Stop requested' : result.terminated ? 'Task stopped' : 'Task was already stopped';
              if(result.pending)tasks.get(task.processId).pending=true;
              if(!result.pending){row.remove();tasks.delete(task.processId);}
            } catch(error) { status.textContent = error.message; stop.disabled = false; }
            finally{busy=false;refresh.disabled=false;confirmed.checked=false;}
          });
          stop.disabled = !this.controls.terminateBackgroundTask;
          row.append(command, location, stop); list.append(row);
          tasks.set(task.processId,{row,stop});
        }
        status.textContent = result.data.length ? `${result.data.length} running` : 'No running background tasks';
        window.lucide?.createIcons();
      } catch(error) { if (dialog.open) status.textContent = error.message; }
      finally { busy = false; refresh.disabled = false; }
    };
    dialog.append(heading, refresh, close, status, list,confirmation,stopAll);
    dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
    dialog.addEventListener('close', () => dialog.remove());
    this.tasksDialog = dialog; this.root.append(dialog); dialog.showModal(); close.focus(); load();
  }

  openReview() {
    if (this.reviewDialog?.open) return;
    const dialog = node('dialog', 'aw-review-dialog');
    dialog.setAttribute('aria-label', 'Review changes');
    const form = node('form');
    form.append(node('h2', '', 'Review changes'));
    const label = node('label', '', 'Review target');
    const select = node('select'); label.append(select);
    for (const [value, title] of [['uncommittedChanges','Uncommitted changes'],['baseBranch','Base branch'],['commit','Commit'],['custom','Custom instructions']]) {
      const option = node('option','',title); option.value=value; select.append(option);
    }
    const detail = node('label');
    const input = node('textarea'); input.rows=3;
    const update = () => {
      detail.replaceChildren(node('span','',({baseBranch:'Branch',commit:'Commit SHA',custom:'Instructions'})[select.value] || ''),input);
      detail.hidden=select.value==='uncommittedChanges'; input.required=!detail.hidden;
    };
    select.addEventListener('change',update); update();
    const cancel = node('button','','Cancel'); cancel.type='button'; cancel.addEventListener('click',()=>dialog.close());
    const run = node('button','','Start review'); run.type='submit';
    form.append(label,detail,cancel,run); dialog.append(form); this.root.append(dialog);
    dialog.addEventListener('close',()=>dialog.remove()); this.reviewDialog=dialog;
    form.addEventListener('submit',async event=>{
      event.preventDefault(); run.disabled=true;
      const target={type:select.value};
      const key=({baseBranch:'branch',commit:'sha',custom:'instructions'})[select.value];
      if(key) target[key]=input.value.trim();
      try { await this.controls.review(target); dialog.close(); }
      catch(error){ this.error(error); run.disabled=false; }
    });
    dialog.showModal(); select.focus();
  }

  setSleeping(sleeping) {
    if(this.disposed || this.sleeping === sleeping)return;
    this.sleeping = sleeping;
    this.renderStatus();
  }

  renderStatus() {
    this.status.textContent = this.sleeping ? 'sleeping' : this.conversation.status;
    if(this.conversation.metadata.activeAgentCount>0)this.status.textContent+=` / ${this.conversation.metadata.activeAgentCount} agents working`;
    if (this.conversation.metadata.bridgeQueueCount > 0) this.status.textContent += ` / ${this.conversation.metadata.bridgeQueueCount} queued`;
    this.refreshSessionStatus?.();
  }

  receive(envelope) {
    if (this.disposed) return false;
    const previousError=this.conversation.error;
    const older=envelope.event?.method==='workspace/historyPage';
    const scroll=older?{height:this.log.scrollHeight,top:this.log.scrollTop,count:[...this.conversation.turns.values()].reduce((n,t)=>n+t.items.size,0)}:null;
    try {
      if (!this.conversation.apply(envelope)) return true;
    } catch (error) {
      this.error(error);
      this.send.disabled = true;
      this.controls.replay?.(this.conversation.sequence);
      return false;
    }
    if(previousError && !this.conversation.error && this.alert.textContent===previousError){
      this.alert.hidden=true;this.alert.textContent='';
    }
    if(['mcpServer/oauthLogin/completed','mcpServer/startupStatus/updated'].includes(envelope.event?.method))this.refreshMcp?.();
    if(envelope.event?.method==='workspace/agentEvent')this.notifyAgentChange?.(envelope.event.params);
    if(older){
      if(this.frame){cancelAnimationFrame(this.frame);this.frame=0;}
      const count=[...this.conversation.turns.values()].reduce((n,t)=>n+t.items.size,0);
      this.visibleItemLimit+=Math.max(0,count-scroll.count);this.lastItemCount=count;
      this.render();this.log.scrollTop=scroll.top+this.log.scrollHeight-scroll.height;
    } else if (!this.frame) this.frame = requestAnimationFrame(() => { this.frame = 0; this.render(); });
    return true;
  }

  async interrupt() {
    if(this.disposed || this.interrupting || this.conversation.status!=='running')return;
    const turns=[...this.conversation.turns.values()].filter(turn=>turn.status==='inProgress');
    if(!turns.length || (this.provider!=='Claude' && turns.length!==1)){this.error(Error('Running turn identity is unavailable'));return;}
    this.interrupting=true;this.render();
    try{await this.controls.interrupt(turns[0].id);}
    catch(error){this.error(error);}
    finally{this.interrupting=false;if(!this.disposed)this.render();}
  }

  codexCommandControls() {
    return {resume:this.resumeButton,fork:this.forkButton,review:this.reviewButton,compact:this.compactButton,
      mcp:this.mcpButton,permissions:this.permissionsButton,skills:this.commandsButton,ps:this.tasksButton,stop:this.tasksButton,clean:this.tasksButton,mention:this.mentionButton,hooks:this.hooksButton,diff:this.diffButton,apps:this.appsButton,
      agent:this.agentsButton,subagents:this.agentsButton,model:this.modelSelect,reasoning:this.effortSelect,status:this.sessionStatusButton,plan:this.sessionModeButton,goal:this.goalButton,personality:this.personalityButton,copy:this.copyOutputButton,rename:this.renameButton,new:this.newConversationButton};
  }

  async copyLatestOutput() {
    if(this.disposed)return;
    if(this.conversation.copyUnavailableAfterRevert){this.error(Error('Copy is unavailable after a history revert until new output completes'));return;}
    for(const turn of [...this.conversation.turns.values()].reverse()){
      if(turn.status!=='completed')continue;
      const item=[...turn.items.values()].reverse().find(item=>
        ['agentMessage','plan'].includes(item.type) && !item.parentToolUseId && typeof item.text==='string' && item.text.length);
      if(!item)continue;
      try{await navigator.clipboard.writeText(item.text);}
      catch(error){this.error(error);}
      return;
    }
    this.error(Error('No completed output is available to copy'));
  }

  activateCommandControl(control) {
    if(control.hidden || control.disabled){this.error(Error('Session action is not available right now'));return;}
    if(control.tagName==='SELECT'){
      control.focus();
      // Browser support and transient user activation vary; keyboard focus still works.
      try{control.showPicker?.();}catch{}
    }else control.click();
  }

  async submit() {
    const text = this.input.value;
    if(this.provider==='Codex' && /^\/copy(?:\s|$)/.test(text.trim())){
      if(text.trim()!=='/copy' || this.files.length || this.selectedSkills.length || this.selectedApps.length){this.error(Error('Copy does not accept arguments or attachments'));return;}
      await this.copyLatestOutput();return;
    }
    if(this.provider==='Codex' && /^\/[A-Za-z]/.test(text.trim()) && this.selectedApps.length){this.error(Error('Remove selected apps before running a session command'));return;}
    const readOnlyCommand=this.provider==='Codex' && /^\/(agent|subagents|goal|new|ps|stop|clean|mention|hooks|diff|apps)(?:\s|$)/.test(text.trim());
    if (this.sending || (this.send.disabled && !readOnlyCommand) || (!text.trim() && !this.files.length && !this.selectedSkills.length)) return;
    const colorCommand=this.provider==='Claude' && /^\/color(?:\s+(.*))?$/.exec(text.trim());
    if(colorCommand){
      if(this.files.length || this.selectedSkills.length){this.error(Error('Prompt color does not accept attachments or skills'));return;}
      try{if(colorCommand[1])this.setPromptColor(colorCommand[1]);else this.openPromptColor();}catch(error){this.error(error);}
      return;
    }
    const reloadCommand=this.provider==='Claude' && /^\/(reload-plugins|reload-skills)(?:\s|$)/.exec(text.trim());
    if(reloadCommand){
      if(text.trim()!==`/${reloadCommand[1]}` || this.files.length || this.selectedSkills.length){
        this.error(Error('Reload commands do not accept arguments, attachments or skills'));return;
      }
      await this.openCommands(reloadCommand[1]);return;
    }
    const codexCommand=this.provider==='Codex' && /^\/([A-Za-z][A-Za-z0-9_:-]*)(?:\s|$)/.exec(text.trim());
    const codexControl=codexCommand && this.codexCommandControls()[codexCommand[1]];
    if(codexCommand && !codexControl){this.error(Error(`/${codexCommand[1]} is not implemented in this pane; nothing was sent`));return;}
    if(codexControl){
      if(codexCommand[1]==='new' && !this.files.length && !this.selectedSkills.length){
        if(codexControl.hidden){this.error(Error('New conversation is unavailable outside the app'));return;}
        this.activateCommandControl(codexControl);return;
      }
      if(codexCommand[1]==='rename' && !this.files.length && !this.selectedSkills.length){this.activateCommandControl(codexControl);return;}
      if(codexCommand[1]==='mention' && /^\/mention\s+\S/.test(text.trim()) && !this.files.length && !this.selectedSkills.length){
        if(text.trim().slice('/mention'.length).trim().length>200){this.error(Error('File search is limited to 200 characters'));return;}
        this.activateCommandControl(codexControl);return;
      }
      if(text.trim()!==`/${codexCommand[1]}` || this.files.length || this.selectedSkills.length){
        this.error(Error('Session commands do not accept arguments, attachments or skills'));return;
      }
      if(codexControl.hidden || codexControl.disabled){this.error(Error('Session action is not available right now'));return;}
      if(codexCommand[1]!=='compact'){this.activateCommandControl(codexControl);return;}
    }
    const localCommand=this.provider==='Claude' && /^\/(clear|reset|new|fork|resume)(?:\s|$)/.exec(text.trim());
    if(localCommand){
      const control=localCommand[1]==='fork'?this.forkButton:localCommand[1]==='resume'?this.resumeButton:this.clearButton;
      if(text.trim()!==`/${localCommand[1]}` || this.files.length || this.selectedSkills.length){
        this.error(Error('Session commands do not accept arguments, attachments or skills'));return;
      }
      if(control.hidden || control.disabled){this.error(Error('Session action is not available right now'));return;}
      if(localCommand[1]==='fork')this.openFork();else if(localCommand[1]==='resume')this.openSessions();else this.openClear();
      return;
    }
    const files = [...this.files];
    const skills = [...this.selectedSkills];
    const apps = [...this.selectedApps];
    this.sending = true; this.send.disabled = true; this.alert.hidden = true;
    try {
      // Uploads and text are submitted through one session-owner operation.
      const options = {};
      if(apps.length)options.apps=apps.map(a=>a.id);
      if(skills.length){
        options.skills=skills.map(s=>s.path);
      }
      if (this.modelSelect.value) options.model = this.modelSelect.value;
      if (this.effortSelect.value) options.effort = this.effortSelect.value;
      if (this.tierSelect.value) options.serviceTier = this.tierSelect.value === '__default' ? null : this.tierSelect.value;
      if (this.provider === 'Codex' && text.trim() === '/compact') {
        if (files.length || skills.length || !this.controls.compact) throw Error('Compaction does not accept attachments or skills');
        await this.controls.compact();
      }
      else if (this.canSteer()) await this.controls.steer({text, files, ...(skills.length || apps.length?{options:{...(skills.length?{skills:options.skills}:{}),...(apps.length?{apps:options.apps}:{})}}:{}), expectedTurnId:[...this.conversation.turns.values()].find(t => t.status === 'inProgress')?.id});
      else if (this.canQueue()) await this.controls.queueInput({text, files,
        expectedTurnId:[...this.conversation.turns.values()].find(t => t.status === 'inProgress')?.id});
      else await this.controls.submit({text, files, options});
      if (this.input.value === text) { this.input.value = ''; this.persistDraft(); }
      this.files = this.files.filter(file => !files.includes(file));
      this.selectedSkills=this.selectedSkills.filter(skill=>!skills.includes(skill));this.persistSkills();
      this.selectedApps=this.selectedApps.filter(app=>!apps.includes(app));this.persistApps();
      this.renderAttachments();
    } catch (error) { this.error(error); }
    finally { this.sending = false; if (!this.disposed) this.render(); }
  }

  canSteer() {
    return this.provider === 'Codex' && this.conversation.status === 'running' && typeof this.controls.steer === 'function';
  }

  canQueue() {
    return this.provider === 'Claude' && this.conversation.status === 'running' && typeof this.controls.queueInput === 'function';
  }

  persistApps() {
    try{this.draftStorage.setItem(`${this.draftKey}:apps`,JSON.stringify(this.selectedApps));}
    catch(error){this.error(error);}
  }

  persistSkills() {
    try {this.draftStorage.setItem(`${this.draftKey}:skills`,JSON.stringify(this.selectedSkills));}
    catch(error){this.error(error);}
  }

  renderAttachments() {
    for (const [file, url] of this.previews) {
      if (!this.files.includes(file)) { URL.revokeObjectURL(url); this.previews.delete(file); }
    }
    this.attachments.replaceChildren();
    for(const app of this.selectedApps){
      const row=node('span','aw-attachment',app.name);row.title=`app://${app.id}`;
      row.append(this.button(`Remove app ${app.name}`,'x',()=>{this.selectedApps=this.selectedApps.filter(a=>a.id!==app.id);this.persistApps();this.renderAttachments();}));
      this.attachments.append(row);
    }
    for(const skill of this.selectedSkills){
      const row=node('span','aw-attachment',`$${skill.name}`);row.title=skill.path;
      row.append(this.button(`Remove skill ${skill.name}`,'x',()=>{this.selectedSkills=this.selectedSkills.filter(s=>s.path!==skill.path);this.persistSkills();this.renderAttachments();}));
      this.attachments.append(row);
    }
    for (const file of this.files) {
      const row = node('span', 'aw-attachment', file.name);
      if (/^image\/(png|jpeg|gif|webp)$/.test(file.type)) {
        if (!this.previews.has(file)) this.previews.set(file, URL.createObjectURL(file));
        const preview = node('img'); preview.src = this.previews.get(file); preview.alt = file.name;
        preview.width = 40; preview.height = 40; preview.style.objectFit = 'contain';
        row.prepend(preview);
      }
      row.append(this.button(`Remove ${file.name}`, 'x', () => {
        this.files = this.files.filter(f => f !== file); this.renderAttachments();
      }));
      this.attachments.append(row);
    }
    this.refreshIcons();
  }

  renderModelControls() {
    const models = this.conversation.models;
    const signature = JSON.stringify([models, this.conversation.metadata.model, this.conversation.metadata.reasoningEffort, this.conversation.metadata.serviceTier]);
    if (this.modelSignature === signature) return;
    this.modelSignature = signature;
    if (Object.hasOwn(this.conversation.metadata, 'model')) this.modelLabel.textContent = this.conversation.metadata.model || 'Model unavailable';
    const selected = this.modelSelect.value;
    this.modelSelect.replaceChildren();
    const unchanged = node('option', '', this.conversation.metadata.model || 'Session model');
    unchanged.value = ''; this.modelSelect.append(unchanged);
    for (const model of models) {
      if (model.hidden || !model.model) continue;
      const option = node('option', '', model.displayName || model.model);
      option.value = model.model; this.modelSelect.append(option);
    }
    this.modelSelect.value = [...this.modelSelect.options].some(o => o.value === selected) ? selected : '';
    this.modelSelect.hidden = !models.length;
    this.renderEfforts(false);
  }

  renderEfforts(changedModel) {
    const selected = this.effortSelect.value;
    const model = this.provider==='Claude' && !this.modelSelect.value ? null : this.conversation.models.find(m => m.model === (this.modelSelect.value || this.conversation.metadata.model));
    this.effortSelect.replaceChildren();
    const unchanged = node('option', '', this.modelSelect.value ? `Default (${model?.defaultReasoningEffort || 'provider'})` : (this.conversation.metadata.reasoningEffort || 'Session effort'));
    unchanged.value = ''; this.effortSelect.append(unchanged);
    for (const effort of model?.supportedReasoningEfforts || []) {
      const option = node('option', '', effort.reasoningEffort);
      option.value = effort.reasoningEffort; option.title = effort.description || '';
      this.effortSelect.append(option);
    }
    const value = changedModel ? (this.modelSelect.value ? model?.defaultReasoningEffort : '') : selected;
    this.effortSelect.value = [...this.effortSelect.options].some(o => o.value === value) ? value : '';
    this.effortSelect.hidden = !(model?.supportedReasoningEfforts?.length);
    const tier = this.tierSelect.value;
    this.tierSelect.replaceChildren();
    const same = node('option', '', this.conversation.metadata.serviceTier || 'Session speed'); same.value = '';
    const standard = node('option', '', 'Provider default'); standard.value = '__default';
    this.tierSelect.append(same, standard);
    for (const service of model?.serviceTiers || []) {
      const option = node('option', '', service.name || service.id); option.value = service.id; option.title = service.description || '';
      this.tierSelect.append(option);
    }
    this.tierSelect.value = changedModel ? (this.modelSelect.value ? '__default' : '') : ([...this.tierSelect.options].some(o => o.value === tier) ? tier : '');
    this.tierSelect.hidden = !(model?.serviceTiers?.length);
  }

  renderItem(item,{historyImages=true,userLabel='Raghav'}={}) {
    const entry = node('article', 'aw-item'); entry.dataset.itemId = item.id;
    if(item.parentToolUseId){
      entry.dataset.parentToolUseId=item.parentToolUseId;
      const origin=node('details','aw-agent-origin');
      origin.append(node('summary','',item.sourceModel?`Subagent - ${item.sourceModel}`:'Subagent'),node('code','',`Parent tool: ${item.parentToolUseId}`));
      entry.append(origin);
    }
    if (item.type === 'userMessage') {
      entry.append(node('div', 'aw-author', userLabel));
      const message = node('div', 'aw-user-message');
      for (const part of item.content || []) {
        if (part.previewToken || part.type === 'image') {
          if(historyImages)this.appendHistoryImage(message, part, 'Attached image');
          else message.append(node('p','','Agent image preview unavailable'));
        } else if(part.type==='mention' && typeof part.path==='string' && part.path.startsWith('app://')){
          const app=node('span','aw-attachment',part.name || part.path);app.title=part.path;message.append(app);
        } else message.append(node('div', '', part.text ?? part.path ?? part.url ?? JSON.stringify(part)));
      }
      entry.append(message);
    } else if (['agentMessage','commandOutput','plan','enteredReviewMode','exitedReviewMode'].includes(item.type)) {
      entry.append(node('div', 'aw-author', item.type === 'commandOutput' ? 'Command result' : item.type === 'plan' ? 'Plan' : item.type.endsWith('ReviewMode') ? 'Review' : item.parentToolUseId ? 'Subagent response' : this.provider));
      const message = node('div', 'aw-message');
      message.innerHTML = renderWorkspaceMarkdown(item.text ?? item.review);
      for (const part of Array.isArray(item.content) ? item.content : []) {
        if (part.type === 'image') {
          if(historyImages)this.appendHistoryImage(message, part, 'Assistant image');
          else message.append(node('p','','Agent image preview unavailable'));
        }
      }
      for (const block of message.querySelectorAll('pre')) {
        const code = block.querySelector('code');
        if (!code) continue;
        const copy = this.button('Copy code', 'copy', () => {
          navigator.clipboard.writeText(code.textContent).catch(error => this.error(error));
        });
        block.append(copy);
      }
      entry.append(message);
    } else if (item.type === 'acpPlan') {
      entry.append(node('div','aw-author','Plan'));
      const list=node('ol','aw-plan');list.setAttribute('aria-label','Agent plan');
      for(const step of item.entries || []){
        const row=node('li','aw-plan-step');row.dataset.status=step.status;
        const status={pending:'Pending',in_progress:'In progress',completed:'Completed'}[step.status] || 'Unknown';
        const mark=node('span','aw-plan-status');mark.title=status;mark.setAttribute('aria-label',status);
        mark.append(icon({pending:'circle',in_progress:'circle-dot',completed:'circle-check'}[step.status] || 'circle-help'));
        row.append(mark,node('span','aw-plan-content',step.content),node('small','aw-plan-priority',`${step.priority} priority`));
        list.append(row);
      }
      entry.append(list);
    } else if (['claudeToolCall','acpToolCall'].includes(item.type)) {
      const detail=node('details','aw-tool');
      const summary=node('summary');summary.append(node('span','',item.input?.description || item.tool || 'Tool'));
      if(item.status)summary.append(node('small','',item.status));detail.append(summary);
      const input=item.input || {};
      if(typeof input.file_path==='string')detail.append(node('div','aw-file-name',input.file_path));
      if(item.inputStreaming || item.inputUnavailable){
        detail.append(node('div','aw-author',item.inputStreaming?'Receiving tool input':'Tool input incomplete'),node('pre','aw-tool-input',item.inputJson || ''));
      }
      if(item.tool==='Bash' && typeof input.command==='string')detail.append(node('pre','aw-command',input.command));
      else if(item.tool==='Edit' && typeof input.old_string==='string' && typeof input.new_string==='string'){
        detail.append(node('div','aw-author','Requested edit'));
        const diff=node('pre','aw-diff');
        for(const [text,marker,style] of [[input.old_string,'-','aw-remove'],[input.new_string,'+','aw-add']]){
          for(const line of text.split('\n'))diff.append(node('span',style,marker+line+'\n'));
        }
        detail.append(diff);
      }else if(item.tool==='Write' && typeof input.content==='string'){
        detail.append(node('div','aw-author','Requested file content'),node('pre','',input.content));
      }else if(Object.keys(input).length)detail.append(node('pre','',JSON.stringify(input,null,2)));
      const output=item.type==='acpToolCall' && Array.isArray(item.displayContent) && item.displayContent.length?item.displayContent:item.output;
      if(output!==undefined && output!==null){
        detail.append(node('div','aw-author','Output'));
        const blocks=Array.isArray(output)?output:[output];
        for(const block of blocks){
          if(item.type==='acpToolCall' && block?.type==='diff' && typeof block.path==='string' && (block.oldText===null || typeof block.oldText==='string') && typeof block.newText==='string'){
            detail.append(node('div','aw-file-name',block.path));
            const diff=node('pre','aw-diff');
            for(const [text,marker,style] of [[block.oldText,'-','aw-remove'],[block.newText,'+','aw-add']]){
              if(text===null || text==='')continue;
              for(const line of text.split('\n'))diff.append(node('span',style,marker+line+'\n'));
            }
            detail.append(diff);
            continue;
          }
          if(item.type==='acpToolCall' && block?.type==='content' && block.content?.type==='text' && typeof block.content.text==='string'){
            detail.append(node('pre','aw-tool-output',block.content.text));
            continue;
          }
          if(item.type==='acpToolCall' && block?.type==='content' && block.content?.type==='image'){
            this.appendHistoryImage(detail, {source:{type:'base64',data:block.content.data,media_type:block.content.mimeType}}, 'Tool result image');
            continue;
          }
          const text=typeof block==='string'?block:block?.type==='text' && typeof block.text==='string'?block.text:JSON.stringify(block,null,2);
          detail.append(node('pre','aw-tool-output',text));
        }
      }
      const original=node('details');original.append(node('summary','','Native details'),node('pre','',JSON.stringify(item,null,2)));
      detail.append(original);entry.append(detail);
    } else if (item.type === 'contextCompaction') {
      entry.append(node('div', 'aw-author', 'Context compaction'));
      entry.append(node('div', '', item.status === 'completed' ? 'Completed' : 'In progress'));
    } else if (item.type === 'fileChange') {
      for (const change of item.changes || []) {
        entry.append(node('div', 'aw-file-name', change.path));
        const diff = node('pre', 'aw-diff');
        for (const line of (change.diff || '').split('\n')) {
          diff.append(node('span', line.startsWith('+') ? 'aw-add' : line.startsWith('-') ? 'aw-remove' : '', line + '\n'));
        }
        entry.append(diff);
      }
    } else {
      const detail = node('details', 'aw-tool');
      const summary = node('summary');
      summary.append(node('span', '', item.command || item.tool || item.query || item.type));
      if (item.status) summary.append(node('small', '', item.status));
      detail.append(summary);
      // Unknown tools remain fully inspectable, including all provider metadata.
      if (item.type === 'commandExecution') {
        if (item.aggregatedOutput) detail.append(node('pre', 'aw-tool-output', item.aggregatedOutput));
      } else detail.append(node('pre', '', item.aggregatedOutput ?? JSON.stringify(item, null, 2)));
      if (item.exitCode !== undefined && item.exitCode !== null) detail.append(node('div', 'aw-exit', `Exit ${item.exitCode}`));
      entry.append(detail);
    }
    return entry;
  }

  appendHistoryImage(parent, part, label) {
    const image = node('img', 'aw-history-image');
    image.alt = part.name || label;
    const trigger=node('button','aw-image-trigger');trigger.type='button';
    trigger.setAttribute('aria-label',`Open image: ${image.alt}`);trigger.title='Open image';trigger.disabled=true;
    trigger.append(image);trigger.addEventListener('click',()=>this.openImage(image));
    image.addEventListener('load',()=>{trigger.disabled=false;},{once:true});
    const unavailable = () => {
      if(this.imageDialogSource===image)this.imageDialog?.close();
      if (this.historyImageUrls.delete(image.src)) URL.revokeObjectURL(image.src);
      trigger.replaceWith(node('span', '', `${label} unavailable`));
    };
    image.addEventListener('error', unavailable, {once:true});
    parent.append(trigger);
    this.loadHistoryImage(image, part).catch(unavailable);
  }

  openImage(source) {
    if(this.disposed || !source.isConnected || !source.naturalWidth || !this.historyImageUrls.has(source.src))return;
    this.imageDialog?.close();
    const dialog=node('dialog','aw-review-dialog aw-image-dialog');dialog.setAttribute('aria-label','Image viewer');
    const header=node('header');const title=node('span','',source.alt);
    const viewport=node('div','aw-image-viewport');viewport.tabIndex=0;viewport.setAttribute('aria-label','Image');
    const image=node('img');image.alt=source.alt;image.src=source.src;viewport.append(image);
    const size=this.button('Actual size','expand',()=>{
      const actual=viewport.classList.toggle('aw-image-actual');
      size.setAttribute('aria-pressed',String(actual));viewport.scrollTop=viewport.scrollLeft=0;
    });size.setAttribute('aria-pressed','false');
    const close=this.button('Close image','x',()=>dialog.close());
    header.append(title,size,close);dialog.append(header,viewport);
    dialog.addEventListener('close',()=>{
      image.removeAttribute('src');dialog.remove();
      if(this.imageDialog===dialog){this.imageDialog=null;this.imageDialogSource=null;}
    },{once:true});
    this.imageDialog=dialog;this.imageDialogSource=source;
    this.root.append(dialog);this.refreshIcons();dialog.showModal();close.focus();
  }

  async loadHistoryImage(image, part) {
    let blob;
    if (part.previewToken) blob = await this.controls.image(part.previewToken);
    else {
      const source = part.source;
      if (source?.type !== 'base64' || typeof source.data !== 'string' || source.data.length > 35 * 1024 * 1024) throw Error('Unsupported image');
      const bytes = Uint8Array.from(atob(source.data), character => character.charCodeAt(0));
      blob = new Blob([bytes], {type:source.media_type});
    }
    if (!['image/png','image/jpeg','image/gif','image/webp'].includes(blob.type) || blob.size > 25 * 1024 * 1024) throw Error('Unsupported image');
    await Promise.resolve();
    if (this.disposed || !image.isConnected) return;
    const url = URL.createObjectURL(blob);
    this.historyImageUrls.add(url);
    image.src = url;
  }

  renderQuestions() {
    const signature = JSON.stringify([...this.conversation.questions]);
    if (signature === this.questionSignature) return;
    this.questionSignature = signature;
    this.questionArea.replaceChildren();
    for (const [id, question] of this.conversation.questions) {
      const form = node('form', 'aw-question');
      const p = question.params || {};
      if(p.agentThreadId)form.append(node('p','aw-agent-id',`Agent: ${p.agentThreadId}`));
      if (question.method === 'session/request_permission') {
        form.append(node('p','',p.toolCall?.title || 'Tool permission requested'));
        if(p.toolCall?.rawInput!==undefined)form.append(node('pre','',JSON.stringify(p.toolCall.rawInput,null,2)));
        for(const option of p.options || []){
          const button=node('button','',option.name);button.type='button';
          button.addEventListener('click',()=>this.answer(id,{outcome:{outcome:'selected',optionId:option.optionId}},form));
          form.append(button);
        }
        const cancel=node('button','','Cancel');cancel.type='button';
        cancel.addEventListener('click',()=>this.answer(id,{outcome:{outcome:'cancelled'}},form));form.append(cancel);
      } else if (question.method === 'mcpServer/elicitation/request') {
        renderElicitation(form,p,answer=>this.answer(id,answer,form));
      } else if (['item/commandExecution/requestApproval', 'item/fileChange/requestApproval'].includes(question.method)) {
        form.append(node('p', '', p.reason || p.command || 'Approve proposed file changes?'));
        for (const [decision, label] of [['decline','Decline'], ['accept','Approve once'], ['acceptForSession','Approve for session']]) {
          const button = node('button', '', label); button.type = 'button';
          button.addEventListener('click', () => this.answer(id, {decision}, form));
          form.append(button);
        }
      } else if (question.method === 'item/permissions/requestApproval') {
        form.append(node('p', '', p.reason || 'Additional permissions requested'));
        if (p.cwd) form.append(node('small', '', p.cwd));
        const fields = [];
        for (const [key, title] of [['network','Network access'],['fileSystem','File access']]) {
          if (!p.permissions?.[key]) continue;
          const group = node('fieldset');
          const label = node('label', '', title);
          const input = node('input'); input.type='checkbox';
          label.prepend(input); group.append(label, node('pre','',JSON.stringify(p.permissions[key],null,2)));
          form.append(group); fields.push([key,input]);
        }
        const label = node('label', '', 'Grant duration');
        const scope = node('select');
        for (const [value,title] of [['turn','This turn'],['session','This session']]) {
          const option=node('option','',title); option.value=value; scope.append(option);
        }
        label.append(scope); form.append(label);
        const deny=node('button','','Deny'); deny.type='button';
        deny.addEventListener('click',()=>this.answer(id,{permissions:{},scope:'turn'},form));
        const grant=node('button','','Grant selected'); grant.type='submit';
        form.append(deny,grant);
        form.addEventListener('submit',event=>{
          event.preventDefault();
          const permissions=Object.fromEntries(fields.filter(([,input])=>input.checked).map(([key])=>[key,p.permissions[key]]));
          this.answer(id,{permissions,scope:scope.value},form);
        });
      } else if (question.method === 'workspace/claudeApproval' && p.tool === 'AskUserQuestion') {
        const fields = [];
        for (const [index, q] of (p.input?.questions || []).entries()) {
          const group = node('fieldset');
          group.append(node('legend', '', q.question));
          const choices = [];
          for (const option of q.options || []) {
            const label = node('label', '', option.label);
            const choice = node('input');
            choice.type = q.multiSelect ? 'checkbox' : 'radio';
            choice.name = `claude-${id}-${index}`; choice.value = option.label;
            label.prepend(choice); label.title = option.description || '';
            group.append(label); choices.push(choice);
          }
          const label = node('label', '', 'Custom answer');
          const custom = node('input'); custom.type = 'text';
          label.append(custom); group.append(label); form.append(group);
          fields.push({question:q.question, choices, custom, multi:q.multiSelect});
        }
        const send = node('button', '', 'Answer'); send.type = 'submit'; form.append(send);
        const deny = node('button', '', 'Dismiss'); deny.type = 'button';
        deny.addEventListener('click', () => this.answer(id, {decision:'deny'}, form)); form.append(deny);
        form.addEventListener('submit', e => {
          e.preventDefault();
          const answers = Object.fromEntries(fields.map(field => {
            const selected = field.choices.filter(choice => choice.checked).map(choice => choice.value);
            const custom = field.custom.value.trim();
            return [field.question, field.multi ? [...selected, ...(custom ? [custom] : [])].join(', ') : custom || selected[0] || ''];
          }));
          if (!fields.length || Object.values(answers).some(value => !value)) {
            this.error(new Error('Answer every question before submitting.')); return;
          }
          this.answer(id, {answers}, form);
        });
      } else if (question.method === 'workspace/claudeApproval') {
        form.append(node('p', '', p.title || `Allow ${p.tool || 'Claude tool'}?`));
        form.append(node('pre', '', JSON.stringify(p.input || {}, null, 2)));
        if(p.suggestions?.length){
          const group=node('fieldset');group.append(node('legend','','Permission changes'));
          const destinations={session:'This session',localSettings:'Project local settings (persistent)',projectSettings:'Shared project settings (persistent)',userSettings:'User settings (all projects)',cliArg:'CLI arguments'};
          const fields=p.suggestions.map((suggestion,index)=>{
            const label=node('label','',`${suggestion.type} - ${destinations[suggestion.destination] || suggestion.destination}`);
            const check=node('input');check.type='checkbox';label.prepend(check);
            group.append(label,node('pre','',JSON.stringify(suggestion,null,2)));
            return {check,index};
          });
          const apply=node('button','','Allow and apply selected changes');apply.type='button';apply.disabled=true;
          group.addEventListener('change',()=>{apply.disabled=!fields.some(field=>field.check.checked);});
          apply.addEventListener('click',()=>this.answer(id,{decision:'allow',suggestions:fields.filter(field=>field.check.checked).map(field=>field.index)},form));
          group.append(apply);form.append(group);
        }
        for (const [decision, label] of [['deny','Deny'], ['allow','Allow once']]) {
          const button = node('button', '', label); button.type = 'button';
          button.addEventListener('click', () => this.answer(id, {decision}, form));
          form.append(button);
        }
      } else if (question.method === 'item/tool/requestUserInput') {
        const fields = [];
        for (const q of p.questions || []) {
          const label = node('label', '', q.question);
          const field = node('input'); field.name = q.id; field.required = true;
          field.type = q.isSecret ? 'password' : 'text';
          label.append(field); form.append(label); fields.push([q.id,field]);
          for (const option of q.options || []) {
            const button = node('button', '', option.label); button.type = 'button';
            button.title = option.description || option.label;
            button.addEventListener('click', () => { field.value = option.label; }); form.append(button);
          }
        }
        const send = node('button', '', 'Answer'); send.type = 'submit'; form.append(send);
        form.addEventListener('submit', e => {
          e.preventDefault(); this.answer(id, {answers:Object.fromEntries(fields.map(([key,field]) => [key,{answers:[field.value]}]))}, form);
        });
      } else {
        form.append(node('p', '', 'This request needs a control that is not implemented yet.'));
        form.append(node('pre', '', JSON.stringify(question, null, 2)));
      }
      this.questionArea.append(form);
    }
  }

  async answer(id, value, form) {
    const controls = [...form.querySelectorAll('button,input,select,textarea')];
    controls.forEach(el => { el.disabled = true; });
    try { await this.controls.answer(id, value); }
    catch (error) { this.error(error); controls.forEach(el => { el.disabled = false; }); }
    // Only the provider's resolution event dismisses the question.
  }

  releaseHistoryImages(element) {
    for (const image of element.querySelectorAll('.aw-history-image')) {
      if(this.imageDialogSource===image)this.imageDialog?.close();
      if (this.historyImageUrls.delete(image.src)) URL.revokeObjectURL(image.src);
    }
  }

  async loadEarlier() {
    if (this.disposed || this.loadingEarlier) return;
    const height = this.log.scrollHeight, top = this.log.scrollTop;
    if (this.visibleItemLimit >= this.lastItemCount) {
      const cursor=this.conversation.metadata.historyCursor;
      if (!cursor || !this.controls.loadEarlier) return;
      this.loadingEarlier=true;this.earlier.disabled=true;
      try { await this.controls.loadEarlier(cursor); }
      catch(error) { this.error(error); }
      finally { this.loadingEarlier=false;this.earlier.disabled=false; }
      return;
    }
    this.visibleItemLimit += 100;
    this.render();
    this.log.scrollTop = top + this.log.scrollHeight - height;
  }

  render() {
    this.copyOutputButton.disabled = this.conversation.copyUnavailableAfterRevert;
    this.rewindButton.disabled = !['ready','completed','interrupted','failed'].includes(this.conversation.status) || !this.conversation.turns.size;
    if (this.disposed) return;
    this.renderModelControls();
    const follow = this.log.scrollHeight - this.log.scrollTop - this.log.clientHeight < 60;
    const keys = new Set();
    let count = 0;
    for (const turn of this.conversation.turns.values()) count += turn.items.size;
    // Keep the reader's existing window when new items arrive away from the tail.
    if (!follow && count > this.lastItemCount && this.lastItemCount) this.visibleItemLimit += count - this.lastItemCount;
    this.lastItemCount = count;
    this.earlier.hidden = count <= this.visibleItemLimit && !(this.conversation.metadata.historyCursor && this.controls.loadEarlier);
    const ordered = [this.earlier];
    let skip = Math.max(0, count - this.visibleItemLimit);
    for (const turn of this.conversation.turns.values()) {
      let visibleTurn = false;
      for (const item of turn.items.values()) {
        if (skip-- > 0) continue;
        visibleTurn = true;
        const key = JSON.stringify([turn.id,item.id]); keys.add(key);
        const signature = JSON.stringify(item);
        const prior = this.rendered.get(key);
        if (prior?.signature === signature) { ordered.push(prior.element); continue; }
        const element = this.renderItem(item);
        if (prior) {
          const details=element.querySelectorAll('details');
          prior.element.querySelectorAll('details').forEach((old,index)=>{if(old.open && details[index])details[index].open=true;});
          this.releaseHistoryImages(prior.element);
          prior.element.replaceWith(element);
        } else this.log.append(element);
        this.rendered.set(key, {signature,element});
        ordered.push(element);
      }
      const stopLabel={max_tokens:'Stopped: token limit reached',max_turn_requests:'Stopped: model request limit reached',refusal:'Provider declined to continue',cancelled:'Turn cancelled'}[turn.stopReason];
      const hasDuration=Number.isFinite(turn.durationMs) && turn.durationMs >= 0;
      const combined=turn.status==='completed' && typeof turn.combinedWithTurnId==='string'
        && turn.combinedWithTurnId!==turn.id && this.conversation.turns.has(turn.combinedWithTurnId);
      if (visibleTurn && ['completed','failed','interrupted'].includes(turn.status) && (hasDuration || stopLabel || combined)) {
        const key = JSON.stringify([turn.id,null]); keys.add(key);
        const signature = JSON.stringify([turn.status,turn.durationMs,turn.stopReason,combined]);
        let summary = this.rendered.get(key);
        if (summary?.signature !== signature) {
          const seconds = Math.round(turn.durationMs / 1000);
          const elapsed = seconds >= 60 ? `${Math.floor(seconds/60)}m ${seconds%60}s` : `${seconds}s`;
          const label = turn.status === 'completed' ? 'Worked for' : turn.status === 'failed' ? 'Failed after' : 'Interrupted after';
          const element = node('div', 'aw-turn-summary', combined ? 'Included in combined reply' : stopLabel ? `${stopLabel}${hasDuration?` (${elapsed})`:''}` : `${label} ${elapsed}`);
          summary?.element.remove();
          summary = {signature,element}; this.rendered.set(key,summary);
        }
        ordered.push(summary.element);
      }
    }
    for (const [key, prior] of this.rendered) if (!keys.has(key)) { this.releaseHistoryImages(prior.element); prior.element.remove(); this.rendered.delete(key); }
    let cursor = this.log.firstChild;
    for (const element of ordered) {
      if (element !== cursor) this.log.insertBefore(element, cursor);
      cursor = element.nextSibling;
    }
    this.renderStatus();
    this.queueButton.hidden = !this.controls.cancelQueuedBridge || !(this.conversation.metadata.bridgeQueueCount > 0);
    this.renderBridgeQueue();
    this.queueRecoveryButton.hidden=this.provider!=='Claude' || !this.controls.retryQueuedInput
      || !this.controls.pendingQueuedInputs?.().length;
    const tokens = this.conversation.metadata.tokenUsage?.last?.totalTokens;
    const usage = this.conversation.metadata.claudeUsage;
    const acpUsage = this.conversation.metadata.acpUsage;
    this.usageLabel.textContent = Number.isSafeInteger(acpUsage?.used) && acpUsage.used >= 0 && Number.isSafeInteger(acpUsage?.size) && acpUsage.size > 0 ? `Context: ${acpUsage.used.toLocaleString()} / ${acpUsage.size.toLocaleString()} tokens (${Math.round(acpUsage.used/acpUsage.size*100)}%)` :
      Number.isFinite(tokens) && tokens >= 0 ? `Last request: ${tokens.toLocaleString()} tokens` :
      Number.isFinite(usage?.input_tokens) && Number.isFinite(usage?.output_tokens) ? `Turn: ${usage.input_tokens.toLocaleString()} input / ${usage.output_tokens.toLocaleString()} output` : '';
    this.stop.hidden = this.conversation.status !== 'running';
    this.stop.disabled = this.interrupting;
    this.reviewButton.disabled = !['ready','completed','interrupted','failed'].includes(this.conversation.status);
    this.compactButton.disabled = this.reviewButton.disabled;
    const steering = this.canSteer();
    const queueing = this.canQueue();
    this.modelSelect.disabled=queueing;
    this.send.title = steering ? 'Steer running turn' : queueing ? 'Queue message' : 'Send message';
    this.send.setAttribute('aria-label', this.send.title);
    this.send.disabled = this.sending || this.clearing || Boolean(this.clearedSession) || (!steering && !queueing && !['ready','completed','interrupted','failed'].includes(this.conversation.status));
    this.forkButton.disabled=this.forkCreating || this.sending || !['ready','completed','interrupted','failed'].includes(this.conversation.status);
    this.clearButton.disabled=this.clearing || this.sending || (!this.clearedSession && !['ready','completed','interrupted','failed'].includes(this.conversation.status));
    this.disconnectButton.disabled=this.clearing || this.sending || !['ready','completed','interrupted','failed'].includes(this.conversation.status);
    this.shellButton.disabled=this.shellSubmitting || !['ready','running','completed','interrupted'].includes(this.conversation.status);
    if (this.conversation.error) this.error(this.conversation.error);
    this.renderQuestions(); this.refreshIcons();
    if (follow) this.log.scrollTop = this.log.scrollHeight;
  }

  dispose() {
    this.goalDialog?.close();
    this.agentsDialog?.close();
    this.personalityDialog?.close();
    this.rewindDialog?.close();
    this.disposeActions?.();
    this.sessionStatusDialog?.close();
    this.imageDialog?.close();
    this.disposeMentions?.();
    this.reviewDialog?.close();
    this.tasksDialog?.close();
    this.commandsDialog?.close();
    this.colorDialog?.close();
    this.diagnosticsDialog?.close();
    this.accountDialog?.close();
    this.sessionsDialog?.close();
    this.clearDialog?.close();
    this.disconnectDialog?.close();
    this.sessionModeDialog?.close();
    this.fileSearchDialog?.close();
    this.mcpDialog?.close();
    this.contextDialog?.close();
    this.permissionsDialog?.close();
    this.eventsDialog?.close();
    this.queueEditDialog?.close();
    this.effortDialog?.close();
    this.queueDialog?.close();
    this.queueRecoveryDialog?.close();
    for (const url of this.historyImageUrls) URL.revokeObjectURL(url);
    this.historyImageUrls.clear();
    this.disposed = true;
    for (const url of this.previews.values()) URL.revokeObjectURL(url);
    this.previews.clear();
    cancelAnimationFrame(this.frame);
    this.root.replaceChildren();
    // No stop/interrupt/close call: the owner outlives this view.
  }
}
