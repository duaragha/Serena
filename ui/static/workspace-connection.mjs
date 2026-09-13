/** Browser transport for a persistent owner. Disposal never closes the owner. */
export class WorkspaceConnection {
  constructor({sessionId, token, receive, error, recovered = () => {}, runtime = () => {}, replaying = () => {}, streamReplay = false, fetcher = fetch, storage = sessionStorage}) {
    this.sessionId = sessionId;
    this.token = token;
    this.receive = receive;
    this.runtime = runtime;
    this.replaying = replaying;
    this.initialReplayComplete = false;
    this.streamReplay = streamReplay;
    this.error = error;
    this.recovered = recovered;
    this.pollError = null;
    this.pollFailures = 0;
    this.fetcher = fetcher;
    this.storage = storage;
    this.key = `serena-workspace-pending:${sessionId}`;
    this.forkKey = `serena-workspace-fork:${sessionId}`;
    this.clearKey = `serena-workspace-clear:${sessionId}`;
    this.storageFailure = null;
    this.pending = this.readSaved(this.key, {}, value => value && !Array.isArray(value) && typeof value==='object'
      && Object.entries(value).every(([key,id])=>{
        if(typeof id!=='string' || !id)return false;
        if(/^answer:[a-f0-9]{64}$/.test(key))return true;
        const receipt=JSON.parse(key);
        return receipt && typeof receipt.action==='string' && receipt.payload && typeof receipt.payload==='object' && !Array.isArray(receipt.payload);
      }));
    this.uploadKey = `serena-workspace-uploads:${sessionId}`;
    this.uploads = this.readSaved(this.uploadKey, {}, value => value && !Array.isArray(value) && typeof value==='object'
      && Object.values(value).every(token=>typeof token==='string' && token));
    for(const key of [this.clearKey,this.forkKey])this.readSaved(key,null,value=>value===null || (typeof value==='object' && !Array.isArray(value)));
    this.cursor = 0;
    this.stopped = false;
    this.polling = false;
    this.observing = false;
    this.visible = true;
    this.timer = null;
    this.base = `/api/workspace/${encodeURIComponent(sessionId)}`;
  }

  readSaved(key, fallback, validate) {
    try {
      const raw=this.storage.getItem(key);
      if(raw===null || raw===undefined)return fallback;
      const value=JSON.parse(raw);
      if(!validate(value))throw Error('Invalid saved record');
      return value;
    } catch {
      this.storageFailure=Error('Saved session receipts are unreadable. Commands are disabled to avoid duplicate delivery; saved data has not been cleared.');
      return fallback;
    }
  }

  requireReceipts() {
    if(this.storageFailure)throw this.storageFailure;
  }

  async request(path, body) {
    if (this.stopped) throw Error('Conversation view is closed');
    const fetcher = this.fetcher;
    const multipart = body instanceof FormData;
    const response = await fetcher(this.base + path, {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin',
      headers: {'X-Serena-Workspace-Token': this.token, ...(body === undefined || multipart ? {} : {'Content-Type': 'application/json'})},
      ...(body === undefined ? {} : {body: multipart ? body : JSON.stringify(body)}),
    });
    let data;
    try {
      data = await response.json();
    } catch {
      throw Error(`Workspace request failed (${response.status}): the backend returned an invalid response. Your request has not been automatically retried.`);
    }
    if (!response.ok) throw Error(data.error || `Workspace request failed (${response.status})`);
    return data;
  }

  async connect() {
    const result = await this.request('/attach', {});
    if (!result.ok) {
      const error=Error(result.error || 'Session attachment is not confirmed');
      if(result.session_id===this.sessionId && result.setting_recovery)error.settingRecovery=result.setting_recovery;
      throw error;
    }
    await this.poll({required: true});
    if(this.storageFailure)this.error(this.storageFailure);
    return result;
  }

  async open({resume = false} = {}) {
    const observing = await this.observe();
    if (!observing && resume) await this.connect();
    return observing || resume;
  }

  async observe() {
    const result = await this.request('/observe');
    if(result.session_id !== this.sessionId)throw Error('Session observation returned a different identity');
    if(result.observing !== true)return false;
    await this.poll({required:true});
    if(this.storageFailure)this.error(this.storageFailure);
    return true;
  }

  async replayStream() {
    const fetcher = this.fetcher;
    const response = await fetcher(this.base + `/replay?after=${this.cursor}`, {
      credentials:'same-origin',headers:{'X-Serena-Workspace-Token':this.token},
    });
    if(!response.ok || !response.body || !response.headers.get('Content-Type')?.includes('application/x-ndjson'))
      throw Error(`Session replay failed (${response.status})`);
    const reader=response.body.getReader(), decoder=new TextDecoder();
    let buffer='',complete=false;
    const accept=line=>{
      const frame=JSON.parse(line);
      if(complete)throw Error('Unexpected data after session replay');
      if(frame.complete===true){complete=true;return;}
      if(!Array.isArray(frame.events))throw Error('Invalid session replay frame');
      for(const envelope of frame.events){
        if(envelope.sequence!==this.cursor+1)throw Error('Session event replay has a gap');
        if(this.receive(envelope)===false)throw Error('Session event was not accepted by the view');
        this.cursor=envelope.sequence;
      }
    };
    try {
      while(!this.stopped){
        const {done,value}=await reader.read();
        if(this.stopped)return;
        buffer+=decoder.decode(value,{stream:!done});
        let end;
        while((end=buffer.indexOf('\n'))!==-1){accept(buffer.slice(0,end));buffer=buffer.slice(end+1);}
        if(done)break;
      }
      if(!this.stopped && (!complete || buffer))throw Error('Session replay was interrupted; retry will continue from the last received event');
    } finally {await reader.cancel();reader.releaseLock();}
  }

  async poll({required = false} = {}) {
    if (this.polling || this.stopped) return;
    this.observing = true;
    clearTimeout(this.timer);
    this.polling = true;
    let failed = false;
    const initialReplay = !this.initialReplayComplete;
    try {
      if (initialReplay) this.replaying(true);
      if (initialReplay && this.streamReplay) await this.replayStream();
      let page;
      do {
        page = await this.request(`/events?after=${this.cursor}`);
        if (this.stopped) return;
        if(page.runtime != null && (page.runtime.session_id !== this.sessionId || typeof page.runtime.sleeping !== 'boolean')){
          throw Error('Invalid session runtime snapshot');
        }
        for (const envelope of page.events) {
          if (envelope.sequence !== this.cursor + 1) throw Error('Session event replay has a gap');
          if (this.receive(envelope) === false) throw Error('Session event was not accepted by the view');
          this.cursor = envelope.sequence;
        }
      } while (page.has_more && !this.stopped);
      if(!this.stopped)this.runtime(page.runtime ?? null);
      if(!this.stopped)this.initialReplayComplete = true;
      if(!this.stopped){
        this.pollFailures = 0;
        const recovered = this.pollError;
        this.pollError = null;
        if(recovered)this.recovered(recovered);
      }
    } catch (error) {
      failed = true;
      this.pollFailures += 1;
      this.pollError = error;
      if (required) throw error;
      if (!this.stopped) this.error(error);
    } finally {
      this.polling = false;
      if (initialReplay) this.replaying(false);
      const delay = failed ? Math.min(10000, 500 * 2 ** Math.min(this.pollFailures - 1, 5)) : (this.visible ? 250 : 2000);
      if (!this.stopped && !(required && failed)) this.timer = setTimeout(() => this.poll(), delay);
    }
  }

  setVisible(visible) {
    if(typeof visible!=='boolean' || this.visible===visible)return;
    this.visible=visible;
    if(visible && this.observing && !this.stopped && !this.polling){
      clearTimeout(this.timer);
      this.poll();
    }
  }

  async restoreArchive({reconcile=false,requestId=null}={}) {
    this.requireReceipts();
    const signature=JSON.stringify({action:'restore_archive',payload:{confirmed:true}});
    if(requestId!==null){
      if(!reconcile || typeof requestId!=='string' || !/^[a-f0-9-]{36}$/.test(requestId))throw Error('Invalid restoration recovery receipt');
      if(this.pending[signature] && this.pending[signature]!==requestId)throw Error('Saved restoration receipt differs from the catalog');
      this.pending[signature]=requestId;
    }
    if(reconcile && !this.pending[signature])throw Error('No pending archive restoration receipt');
    const request_id=this.pending[signature] || crypto.randomUUID();
    this.pending[signature]=request_id;
    this.storage.setItem(this.key,JSON.stringify(this.pending));
    const receipt=await this.request(reconcile?'/reconcile-archive':'/restore-archive',{request_id,confirmed:true});
    if(!receipt.ok){
      const retryable=receipt.retryable===true && receipt.result?.session_id===this.sessionId && receipt.result?.archived===true;
      if(retryable)this.forgetPending(signature);
      const error=Error(receipt.error || 'Archive restoration is unconfirmed');error.archiveRestoreRetryable=retryable;throw error;
    }
    if(receipt.result?.session_id!==this.sessionId || receipt.result?.archived!==false)throw Error('Restored session identity is unconfirmed');
    this.forgetPending(signature);
    return receipt.result;
  }

  async archiveSession({reconcile=false,requestId=null}={}) {
    this.requireReceipts();
    const signature=JSON.stringify({action:'archive_session',payload:{confirmed:true}});
    if(requestId!==null){
      if(!reconcile || typeof requestId!=='string' || !/^[a-f0-9-]{36}$/.test(requestId))throw Error('Invalid archive recovery receipt');
      if(this.pending[signature] && this.pending[signature]!==requestId)throw Error('Saved archive receipt differs from the durable record');
      this.pending[signature]=requestId;
    }
    if(reconcile && !this.pending[signature])throw Error('No pending archive receipt');
    const request_id=this.pending[signature] || crypto.randomUUID();
    this.pending[signature]=request_id;
    this.storage.setItem(this.key,JSON.stringify(this.pending));
    let receipt;
    try{
      receipt=await this.request(reconcile?'/reconcile-archive-session':'/archive-session',{request_id,confirmed:true});
    }catch(error){error.archiveUncertain=true;throw error;}
    if(!receipt || typeof receipt!=='object' || Array.isArray(receipt)){
      const error=Error('Archive outcome is unconfirmed');error.archiveUncertain=true;throw error;
    }
    if(receipt.ok!==true){
      const retryable=receipt.retryable===true && receipt.result?.session_id===this.sessionId
        && receipt.result?.archived===false;
      if(retryable)this.forgetPending(signature);
      const error=Error(receipt.error || 'Archive outcome is unconfirmed');
      error.archiveRetryable=retryable;error.archiveUncertain=!retryable;
      throw error;
    }
    if(receipt.result?.session_id!==this.sessionId || receipt.result?.archived!==true
      || !Number.isSafeInteger(receipt.result?.thread_count) || receipt.result.thread_count<1){
      const error=Error('Archived session identity is unconfirmed');error.archiveUncertain=true;throw error;
    }
    this.forgetPending(signature);
    return receipt.result;
  }

  async deleteSession({reconcile=false,requestId=null}={}) {
    this.requireReceipts();
    const signature=JSON.stringify({action:'delete_session',payload:{confirmed:true}});
    if(requestId!==null){
      if(!reconcile || typeof requestId!=='string' || !/^[a-f0-9-]{36}$/.test(requestId))throw Error('Invalid delete recovery receipt');
      if(this.pending[signature] && this.pending[signature]!==requestId)throw Error('Saved delete receipt differs from the durable record');
      this.pending[signature]=requestId;
    }
    if(reconcile && !this.pending[signature])throw Error('No pending delete receipt');
    const request_id=this.pending[signature] || crypto.randomUUID();
    this.pending[signature]=request_id;
    this.storage.setItem(this.key,JSON.stringify(this.pending));
    let receipt;
    try{
      receipt=await this.request(reconcile?'/reconcile-delete-session':'/delete-session',{request_id,confirmed:true});
    }catch(error){error.deleteUncertain=true;error.deleteRequestId=request_id;throw error;}
    if(!receipt || typeof receipt!=='object' || Array.isArray(receipt)){
      const error=Error('Delete outcome is unconfirmed');error.deleteUncertain=true;error.deleteRequestId=request_id;throw error;
    }
    if(receipt.ok!==true){
      const retryable=receipt.retryable===true && receipt.result?.session_id===this.sessionId
        && receipt.result?.deleted===false;
      if(retryable)this.forgetPending(signature);
      const error=Error(receipt.error || 'Delete outcome is unconfirmed');
      error.deleteRetryable=retryable;error.deleteUncertain=!retryable;error.deleteRequestId=request_id;
      throw error;
    }
    const threadIds=receipt.result?.thread_ids;
    if(receipt.result?.session_id!==this.sessionId || receipt.result?.deleted!==true
      || !Array.isArray(threadIds) || threadIds[0]!==this.sessionId
      || threadIds.some(id=>typeof id!=='string' || !/^[a-f0-9-]{36}$/.test(id))
      || new Set(threadIds).size!==threadIds.length
      || receipt.result?.thread_count!==threadIds.length || threadIds.length<1){
      const error=Error('Deleted session identity is unconfirmed');error.deleteUncertain=true;error.deleteRequestId=request_id;throw error;
    }
    this.forgetPending(signature);
    return receipt.result;
  }

  async command(action, payload) {
    this.requireReceipts();
    const encoded = JSON.stringify({action, payload});
    const signature = action === 'answer' ? 'answer:' + [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(encoded)))].map(byte=>byte.toString(16).padStart(2,'0')).join('') : encoded;
    const request_id = this.pending[signature] || crypto.randomUUID();
    this.pending[signature] = request_id;
    // Persist before delivery. Reloading after a lost HTTP response must use
    // the same receipt, not silently issue another coding turn.
    this.storage.setItem(this.key, JSON.stringify(this.pending));
    const result = await this.request('/commands', {request_id, action, payload});
    if (!result.ok) {
      if(result.retryable === true){
        this.forgetPending(signature);
      }
      throw Error(result.error || 'Control delivery is unconfirmed');
    }
    let value=result.result;
    if(action==='clear_session'){
      value={...value,request_id};
      this.storage.setItem(this.clearKey,JSON.stringify(value));
    }
    if(action==='fork_session' || action==='register_fork'){
      value={...value,request_id:action==='fork_session'?request_id:payload.fork_request_id};
      this.storage.setItem(this.forkKey,JSON.stringify(value));
    }
    this.forgetPending(signature);
    return value;
  }

  forgetPending(signature) {
    const remaining={...this.pending};
    delete remaining[signature];
    // A failed cleanup must retain the receipt in memory as well as on disk.
    this.storage.setItem(this.key,JSON.stringify(remaining));
    this.pending=remaining;
  }

  async messageInputs(text, files = [], includeText = false) {
        this.requireReceipts();
        if (files.length > 16) throw Error('Attach up to 16 files per message');
        const inputs = text || includeText ? [{type: 'text', text: text || ''}] : [];
        for (const file of files) {
          if (!file.size || file.size > 25 * 1024 * 1024) throw Error('Attach non-empty files no larger than 25 MB');
          const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
          const key = JSON.stringify([file.name, ...new Uint8Array(digest)]);
          if (!this.uploads[key]) {
            const form = new FormData(); form.append('file', file, file.name);
            const result = await this.request('/uploads', form);
            if (!result.ok || !result.upload?.token) throw Error(result.error || 'Upload failed');
            this.uploads[key] = result.upload.token;
            this.storage.setItem(this.uploadKey, JSON.stringify(this.uploads));
          }
          inputs.push({type: 'upload', token: this.uploads[key]});
        }
        return inputs;
  }

  async sendAgentMessage(thread_id, expected_turn_id, text, files = []) {
    this.requireReceipts();
    if (!thread_id || !expected_turn_id) throw Error('Active agent identity is unavailable');
    const payload = files.length
      ? {thread_id, expected_turn_id, inputs: await this.messageInputs(text, files)}
      : {thread_id, expected_turn_id, text};
    if (this.pendingAgentMessages().some(value=>value.payload.thread_id===thread_id
        && JSON.stringify(value.payload)!==JSON.stringify(payload))) {
      throw Error('Resolve the unconfirmed agent message before sending different input');
    }
    return this.command('steer_agent', payload);
  }

  pendingAgentMessages() {
    return Object.entries(this.pending).filter(([key])=>key.startsWith('{')).map(([key,requestId])=>({
      ...JSON.parse(key),requestId,
    })).filter(value=>['steer_agent','continue_agent'].includes(value.action));
  }

  async retryAgentMessage(requestId) {
    const matches=this.pendingAgentMessages().filter(value=>value.requestId===requestId);
    if(matches.length!==1)throw Error('Agent receipt is no longer pending');
    return this.command(matches[0].action,matches[0].payload);
  }

  async sendMessage(action, {text, files = [], options = {}, expectedTurnId}) {
        this.requireReceipts();
        if (['steer','queue_input'].includes(action) && !expectedTurnId) throw Error('Running turn identity is unavailable');
        const inputs = await this.messageInputs(text, files, options.skills?.length || options.apps?.length);
        if(['queue_input','submit','enqueue'].includes(action)){
          const queued=this.pendingQueuedInputs();
          if(queued.length){
            const prior=queued.find(value=>JSON.stringify(value.payload.inputs)===JSON.stringify(inputs));
            if(!prior)throw Error('Resolve the unconfirmed queued message before sending different input');
            return this.command(prior.action,prior.payload);
          }
        }
        return this.command(action, {inputs, ...(['steer','queue_input'].includes(action) ? {expectedTurnId, ...(action === 'steer' && options.skills?.length ? {skills:options.skills} : {}), ...(action === 'steer' && options.apps?.length ? {apps:options.apps} : {})} : {}), ...(['submit','enqueue'].includes(action) && Object.keys(options).length ? {options} : {})});
  }

  pendingQueuedInputs() {
    return Object.entries(this.pending).filter(([key])=>key.startsWith('{')).map(([key,requestId])=>{
      const value=JSON.parse(key);
      return {...value,requestId};
    }).filter(value=>['queue_input','enqueue'].includes(value.action));
  }

  async retryQueuedInput(requestId) {
    const matches=this.pendingQueuedInputs().filter(value=>value.requestId===requestId);
    if(matches.length!==1)throw Error('Queued receipt is no longer pending');
    return this.command(matches[0].action,matches[0].payload);
  }

  controls() {
    return {
      events: after => {
        if (!Number.isSafeInteger(after) || after < 0) throw Error('Invalid event cursor');
        return this.request(`/events?after=${after}`);
      },
      image: async token => {
        if (this.stopped || !/^[a-f0-9]{32}$/.test(token)) throw Error('Image preview unavailable');
        const fetcher = this.fetcher;
        const response = await fetcher(`${this.base}/attachments/${token}`, {credentials:'same-origin',headers:{'X-Serena-Workspace-Token':this.token}});
        if (!response.ok) throw Error('Image preview unavailable in this session');
        return response.blob();
      },
      models: () => this.command('models', {}),
      commands: () => this.command('commands', {}),
      hooks: () => this.command('hooks', {}),
      apps: () => this.command('apps', {}),
      renameSession: name => this.command('rename_session', {name}),
      revertHistory: payload => this.command('revert_history', payload),
      projectDiff: () => this.command('project_diff', {}),
      reloadSkills: () => this.command('reload_skills', {}),
      reloadPlugins: () => this.command('reload_plugins', {}),
      setSkillEnabled: (path, enabled) => this.command('set_skill_enabled', {path, enabled}),
      configDiagnostics: () => this.command('config_diagnostics', {}),
      experimentalFeatures: () => this.command('experimental_features', {}),
      setExperimentalFeature: (name, enabled) => this.command('set_experimental_feature', {name, enabled, confirmed:true}),
      memorySettings: () => this.command('memory_settings', {}),
      setMemoryMode: mode => this.command('set_memory_mode', {mode, confirmed:true}),
      setMemoryDefaults: (use_memories, generate_memories) => this.command('set_memory_defaults', {use_memories, generate_memories, confirmed:true}),
      guardianDenial: () => this.command('guardian_denial', {}),
      approveGuardianDenial: review_id => this.command('approve_guardian_denial', {review_id, confirmed:true}),
      submitFeedback: (classification, reason, include_logs) => this.command('submit_feedback', {classification, reason, include_logs, confirmed:true}),
      detectExternalImports: () => this.command('detect_external_imports', {}),
      importExternalItems: candidate_ids => this.command('import_external_items', {candidate_ids, confirmed:true}),
      searchFiles: query => this.command('search_files', {query}),
      loadEarlier: cursor => this.command('load_earlier', {cursor}),
      shellCommand: (command,confirmed) => this.command('shell_command', {command,confirmed}),
      forkSession: () => this.command('fork_session', {}),
      clearSession: (name='') => this.command('clear_session', name?{confirmed:true,name}:{confirmed:true}),
      archiveSession: options => this.archiveSession(options),
      pendingArchive: () => {
        this.requireReceipts();
        return this.pending[JSON.stringify({action:'archive_session',payload:{confirmed:true}})] || null;
      },
      deleteSession: options => this.deleteSession(options),
      pendingDelete: () => {
        this.requireReceipts();
        return this.pending[JSON.stringify({action:'delete_session',payload:{confirmed:true}})] || null;
      },
      personality: () => this.command('personality', {}),
      setPersonality: value => this.command('set_personality', {value}),
      goal: () => this.command('goal', {}),
      agents: (cursor=null) => this.command('agents', {cursor}),
      inspectAgent: (thread_id,cursor=null) => this.command('inspect_agent', {thread_id,cursor}),
      interruptAgent: (thread_id,expected_turn_id) => this.command('interrupt_agent', {thread_id,expected_turn_id,confirmed:true}),
      steerAgent: (thread_id,expected_turn_id,text,files) => this.sendAgentMessage(thread_id,expected_turn_id,text,files),
      continueAgent: (thread_id,expected_latest_turn_id,text) => this.command('continue_agent', {thread_id,expected_latest_turn_id,text,confirmed:true}),
      speedTiers: () => this.command('speed_tiers', {}),
      setSpeedTier: (value,expected_model) => this.command('set_speed_tier', {value,expected_model}),
      pendingAgentMessages: () => this.pendingAgentMessages(),
      retryAgentMessage: id => this.retryAgentMessage(id),
      updateGoal: (changes,expected) => this.command('update_goal', {changes,expected,confirmed:true}),
      clearGoal: expected => this.command('clear_goal', {expected,confirmed:true}),
      disconnectSession: () => this.command('disconnect_session', {confirmed:true}),
      lastClear: () => this.readSaved(this.clearKey,null,value=>value===null || (typeof value==='object' && !Array.isArray(value))),
      forgetClear: () => {this.requireReceipts();this.storage.setItem(this.clearKey,'null');},
      recoverFork: fork_request_id => this.command('register_fork', {fork_request_id}),
      lastFork: () => this.readSaved(this.forkKey,null,value=>value===null || (typeof value==='object' && !Array.isArray(value))),
      clearForkReceipt: () => {this.requireReceipts();this.storage.setItem(this.forkKey,'null');},
      contextUsage: () => this.command('context_usage', {}),
      permissions: () => this.command('permissions', {}),
      sessionModes: () => this.command('session_modes', {}),
      setSessionMode: mode => this.command('set_session_mode', {mode}),
      setPermissions: (mode, confirmed) => this.command('set_permissions', {mode, confirmed}),
      mcpServers: (verbose=false) => this.command('mcp_servers', verbose?{verbose:true}:{}),
      mcpLogin: name => this.command('mcp_login', {name}),
      mcpReload: () => this.command('mcp_reload', {}),
      setMcpEnabled: (name, enabled) => this.command('set_mcp_enabled', {name, enabled}),
      mcpServerControl: (name, action) => this.command('mcp_server_control', {name, action}),
      cancelQueuedBridge: request_id => this.command('cancel_queued_bridge', {request_id}),
      editQueuedBridge: (request_id, prompt, expected_prompt) => this.command('edit_queued_bridge', {request_id, prompt, expected_prompt}),
      backgroundTasks: () => this.command('background_tasks', {}),
      terminateBackgroundTask: processId => this.command('terminate_background_task', {processId}),
      review: target => this.command('review', {target}),
      compact: () => this.command('compact', {}),
      submit: message => this.sendMessage('submit', message),
      steer: message => this.sendMessage('steer', message),
      queueInput: message => this.sendMessage('queue_input', message),
      enqueue: message => this.sendMessage('enqueue', message),
      pendingQueuedInputs: () => this.pendingQueuedInputs(),
      retryQueuedInput: requestId => this.retryQueuedInput(requestId),
      interrupt: expectedTurnId => this.command('interrupt', expectedTurnId === undefined ? {} : {expectedTurnId}),
      answer: (request_id, answer) => this.command('answer', {request_id, answer}),
    };
  }

  dispose() {
    this.stopped = true;
    clearTimeout(this.timer);
    // No interrupt, close, shutdown, or provider request here.
  }
}
