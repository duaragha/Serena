import {WorkspaceConversation} from './workspace-events.mjs';
import {renderWorkspaceMarkdown} from './workspace-markdown.mjs';

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
    this.previews = new Map();
    this.historyImageUrls = new Set();
    this.sending = false;
    this.disposed = false;
    this.frame = 0;
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
    this.log = node('div', 'aw-transcript');
    this.log.tabIndex = 0;
    this.log.setAttribute('aria-label', `${provider} messages and tool output`);
    this.earlier = this.button('Load earlier messages', 'arrow-up', () => this.loadEarlier());
    this.earlier.classList.add('aw-load-earlier');
    this.log.addEventListener('scroll', () => {
      if (this.log.scrollTop < 40 && !this.earlier.hidden) this.loadEarlier();
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
    this.stop = this.button('Interrupt turn', 'square', async () => {
      this.stop.disabled = true;
      try { await this.controls.interrupt(); } catch (e) { this.error(e); }
      finally { if (!this.disposed) this.stop.disabled = false; }
    });
    this.stop.hidden = true;
    this.send = this.button('Send message', 'arrow-up'); this.send.type = 'submit';
    footer.append(attach, this.modelSelect, this.effortSelect, this.tierSelect, this.stop, this.send);
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
    this.tasksButton.hidden = provider !== 'Codex' || !controls.backgroundTasks;
    footer.insertBefore(this.tasksButton, this.stop);
    this.commandsButton = this.button('Commands and skills', 'slash', () => this.openCommands());
    this.commandsButton.hidden = provider !== 'Claude' || !controls.commands;
    footer.insertBefore(this.commandsButton, this.stop);
    this.queueButton = this.button('Queued sibling messages', 'messages-square', () => this.openBridgeQueue());
    this.queueButton.hidden=true; footer.insertBefore(this.queueButton, this.stop);
    this.form.append(this.input, this.attachments, footer, this.fileInput);
    this.form.addEventListener('submit', e => { e.preventDefault(); this.submit(); });
    this.input.addEventListener('keydown', e => {
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
    root.replaceChildren(head, this.log, this.questionArea, this.alert, this.form, identity);
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
      row.append(cancel); this.queueList.append(row);
    }
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
    let commands=[];
    const render = () => {
      list.replaceChildren();
      const query=search.value.toLowerCase();
      const matching=commands.filter(c => [c.name,c.description,...(c.aliases||[])].join(' ').toLowerCase().includes(query));
      for (const command of matching) {
        const button=node('button','aw-command'); button.type='button';
        button.append(node('strong','',`/${command.name}`),node('small','',command.argumentHint || ''),node('span','',command.description || ''));
        if(command.unavailableReason){button.disabled=true;button.title=command.unavailableReason;button.append(node('small','',command.unavailableReason));}
        button.addEventListener('click',()=>{
          this.input.value=`/${command.name} ${this.input.value}`;
          this.persistDraft(); dialog.close(); this.input.focus();
        });
        list.append(button);
      }
      status.textContent=matching.length ? `${matching.length} command${matching.length === 1 ? '' : 's'}` : 'No matching commands';
    };
    search.addEventListener('input',render);
    dialog.append(node('h3','','Commands and skills'),close,search,status,list);
    dialog.addEventListener('close',()=>dialog.remove());
    this.commandsDialog=dialog; this.root.append(dialog); dialog.showModal(); search.focus();
    window.lucide?.createIcons();
    try { const result=await this.controls.commands(); if(!dialog.open || this.disposed)return; commands=result.data; render(); }
    catch(error){if(dialog.open)status.textContent=error.message;}
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
              status.textContent = result.terminated ? 'Task stopped' : 'Task was already stopped';
              row.remove();
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
    try {
      if (!this.conversation.apply(envelope)) return true;
    } catch (error) {
      this.error(error);
      this.send.disabled = true;
      this.controls.replay?.(this.conversation.sequence);
      return false;
    }
    if (!this.frame) this.frame = requestAnimationFrame(() => { this.frame = 0; this.render(); });
    return true;
  }

  async submit() {
    const text = this.input.value;
    if (this.sending || this.send.disabled || (!text.trim() && !this.files.length)) return;
    const files = [...this.files];
    this.sending = true; this.send.disabled = true; this.alert.hidden = true;
    try {
      // Uploads and text are submitted through one session-owner operation.
      const options = {};
      if (this.modelSelect.value) options.model = this.modelSelect.value;
      if (this.effortSelect.value) options.effort = this.effortSelect.value;
      if (this.tierSelect.value) options.serviceTier = this.tierSelect.value === '__default' ? null : this.tierSelect.value;
      if (this.provider === 'Codex' && text.trim() === '/compact') {
        if (files.length || !this.controls.compact) throw Error('Compaction does not accept attachments');
        await this.controls.compact();
      }
      else if (this.canSteer()) await this.controls.steer({text, files, expectedTurnId:[...this.conversation.turns.values()].find(t => t.status === 'inProgress')?.id});
      else await this.controls.submit({text, files, options});
      if (this.input.value === text) { this.input.value = ''; this.persistDraft(); }
      this.files = this.files.filter(file => !files.includes(file));
      this.renderAttachments();
    } catch (error) { this.error(error); }
    finally { this.sending = false; if (!this.disposed) this.render(); }
  }

  canSteer() {
    return this.provider === 'Codex' && this.conversation.status === 'running' && typeof this.controls.steer === 'function';
  }

  renderAttachments() {
    for (const [file, url] of this.previews) {
      if (!this.files.includes(file)) { URL.revokeObjectURL(url); this.previews.delete(file); }
    }
    this.attachments.replaceChildren();
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
      entry.append(node('div', 'aw-author', item.type === 'commandOutput' ? 'Command result' : item.type === 'plan' ? 'Plan' : item.type.endsWith('ReviewMode') ? 'Review' : this.provider));
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
      detail.append(node('pre', '', item.aggregatedOutput ?? JSON.stringify(item, null, 2)));
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
      if (['item/commandExecution/requestApproval', 'item/fileChange/requestApproval'].includes(question.method)) {
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

  loadEarlier() {
    if (this.disposed || this.visibleItemLimit >= this.lastItemCount) return;
    const height = this.log.scrollHeight, top = this.log.scrollTop;
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
    this.earlier.hidden = count <= this.visibleItemLimit;
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
          const wasOpen = prior.element.querySelector('details')?.open;
          if (wasOpen && element.querySelector('details')) element.querySelector('details').open = true;
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
    this.reviewButton.disabled = !['ready','completed','interrupted','failed'].includes(this.conversation.status);
    this.compactButton.disabled = this.reviewButton.disabled;
    const steering = this.canSteer();
    this.send.title = steering ? 'Steer running turn' : 'Send message';
    this.send.setAttribute('aria-label', this.send.title);
    this.send.disabled = this.sending || (!steering && !['ready','completed','interrupted','failed'].includes(this.conversation.status));
    if (this.conversation.error) this.error(this.conversation.error);
    this.renderQuestions(); this.refreshIcons();
    if (follow) this.log.scrollTop = this.log.scrollHeight;
  }

  dispose() {
    this.reviewDialog?.close();
    this.tasksDialog?.close();
    this.commandsDialog?.close();
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
