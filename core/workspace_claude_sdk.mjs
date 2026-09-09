/** Public SDK boundary. Caller owns admission, session lease and child reaping. */
import {resolve} from 'node:path';

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
  }

  async open() {
    if (this.started) throw new Error('Session driver cannot be started twice');
    this.started=true;
    this.state='opening';
    try {
      const info=await this.sdk.getSessionInfo(this.sessionId,{dir:this.cwd});
      if (!info || info.sessionId!==this.sessionId || (info.cwd && resolve(info.cwd)!==this.cwd)) {
        throw new Error('Exact persisted session is unavailable in this project');
      }
      if (this.state!=='opening') throw new Error('Session opening was cancelled');
      this.stream=this.sdk.query({prompt:this.input(),options:{
        ...this.options, cwd:this.cwd, resume:this.sessionId, forkSession:false,
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
        if (message.session_id && message.session_id!==this.sessionId) {
          throw new Error('Native output belongs to a different session');
        }
        await this.publish(message);
      }
      if (!this.stopped) throw new Error('Native session stream ended unexpectedly');
    } catch(error) {
      this.failure=error;
      this.state='unavailable';
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
    this.pending.push(structuredClone(message));
    this.wake?.();
    this.wake=null;
  }

  requireReady() {
    if (this.state!=='ready') throw new Error('Native session is not ready');
  }

  async control(method,...args) {
    this.requireReady();
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
    this.stopInput();
    this.stream?.close();
    try { await this.done; }
    finally { this.state='closed'; }
  }
}
