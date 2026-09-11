/** Public SDK boundary. Caller owns admission, session lease and child reaping. */
import {isAbsolute, resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import {realpath} from 'node:fs/promises';

async function sameProject(candidate, owned) {
  if(typeof candidate!=='string' || !isAbsolute(candidate))return false;
  const normalized=resolve(candidate);
  if(normalized===owned)return true;
  if(process.platform!=='win32' || normalized.toLowerCase()!==owned.toLowerCase())return false;
  try{return await realpath(normalized)===await realpath(owned);}
  catch{return false;}
}

export class ClaudeSdkSession {
  constructor({sdk, sessionId, cwd, sessionDirectory=cwd, options, spawnOwned, publish, request}) {
    if (!sessionId || typeof spawnOwned !== 'function' || typeof request !== 'function') {
      throw new Error('Exact session, owned spawn and interactive request handler required');
    }
    Object.assign(this, {sdk, sessionId, cwd:resolve(cwd), options, spawnOwned, publish, request});
    this.sessionDirectory=resolve(sessionDirectory);
    this.state='closed';
    this.pending=[];
    this.started=false;
    this.spawned=false;
    this.outstanding=new Set();
    this.inFlight=new Set();
    this.receiptMode='unknown';
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
      const info=await this.sdk.getSessionInfo(this.sessionId,{dir:this.sessionDirectory});
      if (create && info) throw new Error('Creation identity already exists; refusing overwrite');
      if (!create && (!info || info.sessionId!==this.sessionId || (info.cwd && !await sameProject(info.cwd,this.sessionDirectory)))) {
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
      if (this.pending.length && (this.receiptMode==='exact' || !this.inFlight.size)) {
        const message=this.pending.shift();
        if(this.outstanding.has(message.uuid))this.inFlight.add(message.uuid);
        if(this.transition?.requestId===message.uuid)this.transition.delivered=true;
        yield message;
      }
      else await new Promise(done=>{this.wake=done;});
    }
  }

  async read() {
    try {
      for await (let message of this.stream) {
        if(this.transition){
          const transition=this.transition;
          if(message.type==='result' && transition.delivered && !message.user_message_uuid && !message.user_message_uuids?.length){
            message={...message,user_message_uuid:transition.requestId,workspaceReceiptSource:'single-inflight'};
          }
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
            if(transition.requestedName){
              try{
                if(typeof this.sdk.renameSession!=='function')throw new Error('Native session rename is unavailable');
                await this.sdk.renameSession(transition.target,transition.requestedName,{dir:this.cwd});
                const info=await this.sdk.getSessionInfo(transition.target,{dir:this.cwd});
                if(info?.sessionId!==transition.target || info.customTitle!==transition.requestedName){
                  throw new Error('Native session title did not match the requested name');
                }
                transition.nameConfirmed=true;
              }catch(error){
                transition.nameConfirmed=false;
                transition.nameError=String(error?.message || error).replace(/[\u0000-\u001f\u007f]+/g,' ').trim().slice(0,1000)
                  || 'Native title confirmation failed';
              }
            }
            this.state='awaiting-handoff';
            transition.resolve({sessionId:transition.target,
              ...(transition.requestedName?{requestedName:transition.requestedName,nameConfirmed:transition.nameConfirmed,
                ...(!transition.nameConfirmed?{nameError:transition.nameError}:{})}:{})});
          }
          continue;
        }
        if (message.session_id && message.session_id!==this.sessionId) {
          throw new Error('Native output belongs to a different session');
        }
        if(message.type==='result'){
          let ids=[message.user_message_uuid,...(message.user_message_uuids||[])].filter(Boolean);
          if(!ids.length && this.inFlight.size===1){
            // Older CLIs omit receipts. Only one delivered input can be attributed safely.
            ids=[...this.inFlight];
            this.receiptMode='serial';
            message={...message,user_message_uuid:ids[0],workspaceReceiptSource:'single-inflight'};
          }else if(ids.some(id=>this.inFlight.has(id)))this.receiptMode='exact';
          else if(!ids.length && this.inFlight.size>1)throw new Error('Native completion omitted concurrent input identities');
          for(const id of ids){
            if(this.inFlight.delete(id))this.outstanding.delete(id);
          }
        }
        await this.publish(message);
        if(message.type==='result'){this.wake?.();this.wake=null;}
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

  async beginClear(requestedName='') {
    this.requireReady();
    if(this.outstanding.size || this.pending.length)throw new Error('Finish pending inputs before clearing');
    if(typeof requestedName!=='string' || requestedName.length>1000 || requestedName!==requestedName.trim()
       || /[\u0000-\u001f\u007f]/.test(requestedName)){
      throw new Error('Conversation title must contain at most 1000 characters without control characters');
    }
    const requestId=randomUUID();
    let resolve,reject;
    const result=new Promise((done,fail)=>{resolve=done;reject=fail;});
    result.catch(()=>{});
    this.transition={source:this.sessionId,requestId,events:[],requestedName,resolve,reject};
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
    finally { this.state='closed';this.transition=null;this.outstanding.clear();this.inFlight.clear(); }
  }
}
