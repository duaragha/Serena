/** Public SDK boundary. Caller owns admission, session lease and child reaping. */
import {resolve} from 'node:path';
import {randomUUID} from 'node:crypto';

export class ClaudeSdkSession {
  constructor({sdk, sessionId, cwd, options, spawnOwned, publish, request}) {
    if (!sessionId || typeof spawnOwned !== 'function' || typeof request !== 'function') {
      throw new Error('Exact session, owned spawn and interactive request handler required');
    }
    Object.assign(this, {sdk, sessionId, cwd:resolve(cwd), options, spawnOwned, publish, request});
    this.state='closed';
    this.pending=[];
    this.started=false;
    this.spawned=false;
    this.outstanding=new Set();
  }

  async open() {
    return this.openMode(false);
  }

  async create() {
    if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(this.sessionId)) {
      throw new Error('Native creation requires a reserved UUID');
    }
    return this.openMode(true);
  }

  async openMode(create) {
    if (this.started) throw new Error('Session driver cannot be started twice');
    this.started=true;
    this.state='opening';
    try {
      const info=await this.sdk.getSessionInfo(this.sessionId,{dir:this.cwd});
      if (create && info) throw new Error('Creation identity already exists; refusing overwrite');
      if (!create && (!info || info.sessionId!==this.sessionId || (info.cwd && resolve(info.cwd)!==this.cwd))) {
        throw new Error('Exact persisted session is unavailable in this project');
      }
      if (this.state!=='opening') throw new Error('Session opening was cancelled');
      this.stream=this.sdk.query({prompt:this.input(),options:{
        ...this.options, cwd:this.cwd, resume:create?undefined:this.sessionId, forkSession:false,
        ...(create?{sessionId:this.sessionId,continue:false,resumeSessionAt:undefined,persistSession:true}:{}),
        spawnClaudeCodeProcess:options=>{
          if (this.spawned || this.state!=='opening') throw new Error('Duplicate or late CLI spawn rejected');
          this.spawned=true;
          return this.spawnOwned(options);
        },
        canUseTool:(toolName,input,options)=>this.request('canUseTool',{toolName,input},options),
        onElicitation:async(request,options)=>{
          const answer=await this.request('elicitation',request,options);
          if (!answer || !['accept','decline','cancel'].includes(answer.action)) {
            throw new Error('An explicit elicitation response is required');
          }
          return answer;
        },
      }});
      this.done=this.read();
      this.done.catch(()=>{});
      const infoResult=await this.stream.initializationResult();
      if (this.state!=='opening') throw new Error('Session ended during initialization');
      this.state='ready';
      return infoResult;
    } catch(error) {
      this.failure=error;
      this.state='unavailable';
      this.stopInput();
      this.stream?.close();
      throw error;
    }
  }

  async *input() {
    while (!this.stopped) {
      if (this.pending.length) yield this.pending.shift();
      else await new Promise(done=>{this.wake=done;});
    }
  }

  async read() {
    try {
      for await (const message of this.stream) {
        if(this.transition){
          const transition=this.transition;
          if(transition.events.length>=256)throw new Error('Session transition emitted excessive output');
          transition.events.push(structuredClone(message));
          if(message.session_id && message.session_id!==transition.source){
            if(!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(message.session_id) || (transition.target && message.session_id!==transition.target)){
              throw new Error('Native clear returned conflicting session identities');
            }
            transition.target=message.session_id;
          }
          if(message.type==='result'){
            const ids=[message.user_message_uuid,...(message.user_message_uuids||[])];
            if(this.state!=='clearing' || !ids.includes(transition.requestId) || message.is_error || message.subtype!=='success'
               || !transition.target || message.session_id!==transition.target){
              throw new Error('Native clear did not confirm the requested session transition');
            }
            this.state='awaiting-handoff';
            transition.resolve({sessionId:transition.target});
          }
          continue;
        }
        if (message.session_id && message.session_id!==this.sessionId) {
          throw new Error('Native output belongs to a different session');
        }
        if(message.type==='result'){
          for(const id of [message.user_message_uuid,...(message.user_message_uuids||[])])this.outstanding.delete(id);
        }
        await this.publish(message);
      }
      if (!this.stopped) throw new Error('Native session stream ended unexpectedly');
    } catch(error) {
      this.failure=error;
      this.state='unavailable';
      this.transition?.reject(error);
      this.stopInput();
      this.stream.close();
      throw error;
    }
  }

  send(message) {
    this.requireReady();
    if (message?.type!=='user' || message.session_id!==this.sessionId) {
      throw new Error('Input must target the exact session');
    }
    const queued=structuredClone(message);
    queued.uuid ??= randomUUID();
    if(typeof queued.uuid!=='string' || !queued.uuid || this.outstanding.has(queued.uuid))throw new Error('Unique input identity is required');
    this.pending.push(queued);
    this.outstanding.add(queued.uuid);
    this.wake?.();
    this.wake=null;
  }

  requireReady() {
    if (this.state!=='ready') throw new Error('Native session is not ready');
  }

  async beginClear() {
    this.requireReady();
    if(this.outstanding.size || this.pending.length)throw new Error('Finish pending inputs before clearing');
    const requestId=randomUUID();
    let resolve,reject;
    const result=new Promise((done,fail)=>{resolve=done;reject=fail;});
    result.catch(()=>{});
    this.transition={source:this.sessionId,requestId,events:[],resolve,reject};
    this.state='clearing';
    this.pending.push({type:'user',uuid:requestId,session_id:this.sessionId,parent_tool_use_id:null,
      message:{role:'user',content:'/clear'}});
    this.wake?.();this.wake=null;
    return result;
  }

  async commitClear(sessionId) {
    if(this.state!=='awaiting-handoff' || !this.transition || sessionId!==this.transition.target){
      throw new Error('Exact pending session handoff is required');
    }
    this.state='committing-handoff';
    this.sessionId=sessionId;
    try{
      for(const event of this.transition.events){
        if(event.session_id===sessionId)await this.publish(event);
        if(this.state!=='committing-handoff')throw new Error('Session ended during handoff');
      }
      this.transition=null;
      this.state='ready';
      return {sessionId};
    }catch(error){
      this.state='unavailable';this.stopInput();this.stream.close();throw error;
    }
  }

  async control(method,...args) {
    this.requireReady();
    if(method==='forkSession'){
      if(args.length)throw new Error('Fork uses only the owned session and project');
      const result=await this.sdk.forkSession(this.sessionId,{dir:this.cwd});
      if(!result?.sessionId || result.sessionId===this.sessionId)throw new Error('Native fork did not return a new identity');
      return result;
    }
    const allowed=['applyFlagSettings','supportedAgents','reloadSkills','reloadPlugins',
      'supportedCommands','supportedModels','setModel','setPermissionMode',
      'mcpServerStatus','getContextUsage','interrupt','stopTask','reconnectMcpServer','toggleMcpServer'];
    if (!allowed.includes(method) || typeof this.stream[method]!=='function') {
      throw new Error('Unsupported native control');
    }
    return this.stream[method](...args);
  }

  stopInput() {
    this.stopped=true;
    this.pending.length=0;
    this.wake?.();
    this.wake=null;
  }

  async close() {
    this.transition?.reject(new Error('Session owner closed during transition'));
    this.stopInput();
    this.stream?.close();
    try { await this.done; }
    finally { this.state='closed';this.transition=null;this.outstanding.clear(); }
  }
}
