import {WorkspaceConversation} from './workspace-events.mjs';
import {renderWorkspaceMarkdown} from './workspace-markdown.mjs';
import {renderElicitation} from './workspace-elicitation.mjs';
import {installFileMentions} from './workspace-mentions.mjs';

const node = (tag, cls, text) => {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
};
const icon = name => { const el = node('i'); el.dataset.lucide = name; return el; };

/** A real session view. Controls are supplied by the session owner, not a CLI scraper. */
export class WorkspacePane {
  constructor(root, {sessionId, provider, model = '', controls, draftStorage}) {
    this.root = root;
    this.provider = provider;
    this.controls = controls;
    this.draftStorage = draftStorage;
    this.draftKey = `serena-workspace-draft:${provider.toLowerCase()}:${sessionId}`;
    this.conversation = new WorkspaceConversation(sessionId);
    this.rendered = new Map();
    this.visibleItemLimit = 100;
    this.lastItemCount = 0;
    this.files = [];
    this.selectedSkills = [];
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
    const badge = node('span', 'aw-badge', provider.slice(0, 1).toUpperCase());
    badge.dataset.provider = provider.toLowerCase();
    this.modelLabel = node('small', '', model);
    head.append(badge, node('strong', '', provider), this.modelLabel);
    this.status = node('span', 'aw-state', 'Connecting');
    this.status.setAttribute('role', 'status');
    head.append(this.status);
    const eventsButton=this.button('Session events','list-collapse',()=>this.openEvents());
    eventsButton.hidden=!controls.events;head.append(eventsButton);
    this.forkButton=this.button('Fork conversation','git-fork',()=>this.openFork());
    this.forkButton.hidden=!['Claude','Codex'].includes(provider) || !controls.forkSession || !controls.openFork;
    this.forkButton.disabled=true;
    head.append(this.forkButton);
    this.clearButton=this.button('Clear context','eraser',()=>this.openClear());
    this.clearButton.hidden=provider!=='Claude' || !controls.clearSession || !controls.openCleared;
    this.clearButton.disabled=true;head.append(this.clearButton);
    this.shellButton=this.button('Run shell command','terminal',()=>this.openShell());
    this.shellButton.hidden=provider!=='Codex' || !controls.shellCommand;
    head.append(this.shellButton);
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
      if(provider==='Codex' && Array.isArray(skills))this.selectedSkills=skills.filter(s=>typeof s?.name==='string' && typeof s?.path==='string');
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
    this.commandsButton.hidden = !['Claude','Codex'].includes(provider) || !controls.commands;
    footer.insertBefore(this.commandsButton, this.stop);
    this.mcpButton = this.button('MCP connections', 'plug', () => this.openMcpServers());
    this.mcpButton.hidden = !['Claude','Codex'].includes(provider) || !controls.mcpServers;
    footer.insertBefore(this.mcpButton, this.stop);
    const permissions=this.button('Permission mode','shield',()=>this.openPermissions());
    permissions.hidden=!['Codex','Claude'].includes(provider) || !controls.permissions;footer.insertBefore(permissions,this.stop);
    this.claudeEffortButton=this.button('Claude reasoning effort','gauge',()=>this.openClaudeEffort());
    this.claudeEffortButton.hidden=provider!=='Claude' || !controls.models || !controls.commands;
    footer.insertBefore(this.claudeEffortButton,this.stop);
    this.queueButton = this.button('Queued sibling messages', 'messages-square', () => this.openBridgeQueue());
    this.queueButton.hidden=true; footer.insertBefore(this.queueButton, this.stop);
    this.form.append(this.input, this.attachments, footer, this.fileInput);
    this.disposeMentions=installFileMentions({input:this.input,form:this.form,
      enabled:()=>['Claude','Codex'].includes(this.provider) && typeof this.controls.searchFiles==='function',
      search:query=>this.controls.searchFiles(query),persist:()=>this.persistDraft()});
    this.form.addEventListener('submit', e => { e.preventDefault(); this.submit(); });
    this.input.addEventListener('keydown', e => {
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
    const start=this.input.selectionStart,end=this.input.selectionEnd;
    const original=this.input.value;
    const dialog=node('dialog','aw-review-dialog aw-commands-dialog');
    dialog.setAttribute('aria-label','Mention project file');
    const search=node('input');search.type='search';search.maxLength=200;
    search.setAttribute('aria-label','Find project file');
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
    window.lucide?.createIcons();
  }

  async openCommands() {
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
      const matching=commands.filter(c => [c.name,c.description,...(c.aliases||[])].join(' ').toLowerCase().includes(query));
      for (const command of matching) {
        const button=node('button','aw-command'); button.type='button';
        button.append(node('strong','',`${command.kind==='skill'?'$':'/'}${command.name}`),node('small','',command.kind==='skill'?command.path:command.argumentHint || ''),node('span','',command.description || ''));
        button.disabled=busy;
        if(command.unavailableReason){button.disabled=true;button.title=command.unavailableReason;button.append(node('small','',command.unavailableReason));}
        button.addEventListener('click',()=>{
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
      plugins.disabled=true;reload.disabled=true;status.textContent='Reloading plugins...';
      try {
        const result=await this.controls.reloadPlugins();
        if(!dialog.open || this.disposed)return;
        commands=result.data;render();
        status.textContent+=` · ${result.plugins.length} plugins · ${result.error_count} plugin errors`;
      }catch(error){if(dialog.open)status.textContent=error.message;}
      finally{plugins.disabled=false;reload.disabled=false;}
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

  openBackgroundTasks() {
    if (this.tasksDialog?.open) return;
    const dialog = node('dialog', 'aw-review-dialog aw-tasks-dialog');
    dialog.setAttribute('aria-label', 'Background tasks');
    const heading = node('h3', '', 'Background tasks');
    const list = node('div', 'aw-task-list');
    const status = node('p'); status.setAttribute('role', 'status');
    const refresh = this.button('Refresh background tasks', 'refresh-cw', () => load());
    const close = this.button('Close background tasks', 'x', () => dialog.close());
    let busy = false;
    const load = async () => {
      if (busy || !dialog.open) return;
      busy = true; refresh.disabled = true; status.textContent = 'Loading...';
      try {
        const result = await this.controls.backgroundTasks();
        if (!dialog.open || this.disposed) return;
        list.replaceChildren();
        for (const task of result.data) {
          const row = node('div', 'aw-background-task');
          const command = node('pre', '', task.command);
          const location = node('small', '', task.cwd);
          const stop = this.button(`Stop task ${task.processId}`, 'square', async () => {
            stop.disabled = true;
            try {
              const result = await this.controls.terminateBackgroundTask(task.processId);
              status.textContent = result.pending ? 'Stop requested' : result.terminated ? 'Task stopped' : 'Task was already stopped';
              if(!result.pending)row.remove();
            } catch(error) { status.textContent = error.message; stop.disabled = false; }
          });
          stop.disabled = !this.controls.terminateBackgroundTask;
          row.append(command, location, stop); list.append(row);
        }
        status.textContent = result.data.length ? `${result.data.length} running` : 'No running background tasks';
        window.lucide?.createIcons();
      } catch(error) { if (dialog.open) status.textContent = error.message; }
      finally { busy = false; refresh.disabled = false; }
    };
    dialog.append(heading, refresh, close, status, list);
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

  receive(envelope) {
    if (this.disposed) return false;
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
    if(['mcpServer/oauthLogin/completed','mcpServer/startupStatus/updated'].includes(envelope.event?.method))this.refreshMcp?.();
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
    if(turns.length!==1){this.error(Error('Running turn identity is unavailable'));return;}
    this.interrupting=true;this.render();
    try{await this.controls.interrupt(turns[0].id);}
    catch(error){this.error(error);}
    finally{this.interrupting=false;if(!this.disposed)this.render();}
  }

  async submit() {
    const text = this.input.value;
    if (this.sending || this.send.disabled || (!text.trim() && !this.files.length && !this.selectedSkills.length)) return;
    const files = [...this.files];
    const skills = [...this.selectedSkills];
    this.sending = true; this.send.disabled = true; this.alert.hidden = true;
    try {
      // Uploads and text are submitted through one session-owner operation.
      const options = {};
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
      else if (this.canSteer()) await this.controls.steer({text, files, ...(skills.length?{options:{skills:options.skills}}:{}), expectedTurnId:[...this.conversation.turns.values()].find(t => t.status === 'inProgress')?.id});
      else await this.controls.submit({text, files, options});
      if (this.input.value === text) { this.input.value = ''; this.persistDraft(); }
      this.files = this.files.filter(file => !files.includes(file));
      this.selectedSkills=this.selectedSkills.filter(skill=>!skills.includes(skill));this.persistSkills();
      this.renderAttachments();
    } catch (error) { this.error(error); }
    finally { this.sending = false; if (!this.disposed) this.render(); }
  }

  canSteer() {
    return this.provider === 'Codex' && this.conversation.status === 'running' && typeof this.controls.steer === 'function';
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
    if (this.conversation.metadata.model) this.modelLabel.textContent = this.conversation.metadata.model;
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
    const model = this.conversation.models.find(m => m.model === (this.modelSelect.value || this.conversation.metadata.model));
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

  renderItem(item) {
    const entry = node('article', 'aw-item'); entry.dataset.itemId = item.id;
    if(item.parentToolUseId){
      entry.dataset.parentToolUseId=item.parentToolUseId;
      const origin=node('details','aw-agent-origin');
      origin.append(node('summary','',item.sourceModel?`Subagent - ${item.sourceModel}`:'Subagent'),node('code','',`Parent tool: ${item.parentToolUseId}`));
      entry.append(origin);
    }
    if (item.type === 'userMessage') {
      entry.append(node('div', 'aw-author', 'Raghav'));
      const message = node('div', 'aw-user-message');
      for (const part of item.content || []) {
        if (part.previewToken || part.type === 'image') {
          const image = node('img', 'aw-history-image');
          image.alt = part.name || 'Attached image';
          message.append(image);
          this.loadHistoryImage(image, part).catch(() => {
            image.replaceWith(node('span', '', 'Attached image unavailable'));
          });
        } else message.append(node('div', '', part.text ?? part.path ?? part.url ?? JSON.stringify(part)));
      }
      entry.append(message);
    } else if (['agentMessage','commandOutput','plan','enteredReviewMode','exitedReviewMode'].includes(item.type)) {
      entry.append(node('div', 'aw-author', item.type === 'commandOutput' ? 'Command result' : item.type === 'plan' ? 'Plan' : item.type.endsWith('ReviewMode') ? 'Review' : item.parentToolUseId ? 'Subagent response' : this.provider));
      const message = node('div', 'aw-message');
      message.innerHTML = renderWorkspaceMarkdown(item.text ?? item.review);
      for (const block of message.querySelectorAll('pre')) {
        const code = block.querySelector('code');
        if (!code) continue;
        const copy = this.button('Copy code', 'copy', () => {
          navigator.clipboard.writeText(code.textContent).catch(error => this.error(error));
        });
        block.append(copy);
      }
      entry.append(message);
    } else if (item.type === 'claudeToolCall') {
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
      if(item.output!==undefined && item.output!==null){
        detail.append(node('div','aw-author','Output'));
        const blocks=Array.isArray(item.output)?item.output:[item.output];
        for(const block of blocks){
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
      if (question.method === 'mcpServer/elicitation/request') {
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
      if (visibleTurn && ['completed','failed','interrupted'].includes(turn.status) && Number.isFinite(turn.durationMs) && turn.durationMs >= 0) {
        const key = JSON.stringify([turn.id,null]); keys.add(key);
        const signature = JSON.stringify([turn.status,turn.durationMs]);
        let summary = this.rendered.get(key);
        if (summary?.signature !== signature) {
          const seconds = Math.round(turn.durationMs / 1000);
          const elapsed = seconds >= 60 ? `${Math.floor(seconds/60)}m ${seconds%60}s` : `${seconds}s`;
          const label = turn.status === 'completed' ? 'Worked for' : turn.status === 'failed' ? 'Failed after' : 'Interrupted after';
          const element = node('div', 'aw-turn-summary', `${label} ${elapsed}`);
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
    this.status.textContent = this.conversation.status;
    if (this.conversation.metadata.bridgeQueueCount > 0) this.status.textContent += ` / ${this.conversation.metadata.bridgeQueueCount} queued`;
    this.queueButton.hidden = !this.controls.cancelQueuedBridge || !(this.conversation.metadata.bridgeQueueCount > 0);
    this.renderBridgeQueue();
    const tokens = this.conversation.metadata.tokenUsage?.last?.totalTokens;
    const usage = this.conversation.metadata.claudeUsage;
    this.usageLabel.textContent = Number.isFinite(tokens) && tokens >= 0 ? `Last request: ${tokens.toLocaleString()} tokens` :
      Number.isFinite(usage?.input_tokens) && Number.isFinite(usage?.output_tokens) ? `Turn: ${usage.input_tokens.toLocaleString()} input / ${usage.output_tokens.toLocaleString()} output` : '';
    this.stop.hidden = this.conversation.status !== 'running';
    this.stop.disabled = this.interrupting;
    this.reviewButton.disabled = !['ready','completed','interrupted','failed'].includes(this.conversation.status);
    this.compactButton.disabled = this.reviewButton.disabled;
    const steering = this.canSteer();
    this.send.title = steering ? 'Steer running turn' : 'Send message';
    this.send.setAttribute('aria-label', this.send.title);
    this.send.disabled = this.sending || this.clearing || Boolean(this.clearedSession) || (!steering && !['ready','completed','interrupted','failed'].includes(this.conversation.status));
    this.forkButton.disabled=this.forkCreating || this.sending || !['ready','completed','interrupted','failed'].includes(this.conversation.status);
    this.clearButton.disabled=this.clearing || this.sending || (!this.clearedSession && !['ready','completed','interrupted','failed'].includes(this.conversation.status));
    this.shellButton.disabled=this.shellSubmitting || !['ready','running','completed','interrupted'].includes(this.conversation.status);
    if (this.conversation.error) this.error(this.conversation.error);
    this.renderQuestions(); this.refreshIcons();
    if (follow) this.log.scrollTop = this.log.scrollHeight;
  }

  dispose() {
    this.disposeMentions?.();
    this.reviewDialog?.close();
    this.tasksDialog?.close();
    this.commandsDialog?.close();
    this.clearDialog?.close();
    this.fileSearchDialog?.close();
    this.mcpDialog?.close();
    this.contextDialog?.close();
    this.permissionsDialog?.close();
    this.eventsDialog?.close();
    this.queueEditDialog?.close();
    this.effortDialog?.close();
    this.queueDialog?.close();
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
