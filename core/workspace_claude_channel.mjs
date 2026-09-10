/** Bidirectional JSONL methods used by WorkspaceRpc; never a renderer endpoint. */
import {ClaudeSdkSession} from './workspace_claude_sdk.mjs';

export class ClaudeSdkChannel {
  constructor({write,sessionOptions,createSession=options=>new ClaudeSdkSession(options)}) {
    Object.assign(this,{write,sessionOptions,createSession});
    this.pending=new Map();
    this.inflight=new Set();
    this.nextId=0;
    this.closed=false;
  }

  async receive(message) {
    if (!message || typeof message!=='object' || Array.isArray(message)) throw new Error('Expected RPC object');
    if (!Object.hasOwn(message,'method')) {
      const pending=this.pending.get(message.id);
      if (!pending) throw new Error('Approval request is no longer pending');
      this.pending.delete(message.id);
      pending.cleanup();
      if (Object.hasOwn(message,'error')) pending.reject(new Error('Approval response failed'));
      else if (Object.hasOwn(message,'result')) pending.resolve(message.result);
      else pending.reject(new Error('Approval response has no result'));
      return;
    }
    const {id,method,params={}}=message;
    if (!['string','number'].includes(typeof id) || this.inflight.has(id)) {
      throw new Error('Invalid or duplicate RPC request ID');
    }
    this.inflight.add(id);
    try {
      if (this.closed) throw new Error('Session channel is closed');
      if (!params || typeof params!=='object' || Array.isArray(params)) throw new Error('Expected method parameters');
      let result;
      switch(method) {
        case 'open':
        case 'create':
          if (Object.keys(params).length) throw new Error('Session startup takes no parameters');
          if (this.session) throw new Error('Session owner already exists');
          this.session=this.createSession({...this.sessionOptions,
            publish:message=>this.write({method:'claude/message',params:{message}}),
            request:(kind,request,options)=>this.ask(kind,request,options),
          });
          result=await this.session[method]();
          this.session.done?.catch(error=>{
            if(!this.closed) return this.write({method:'workspace/error',params:{reason:error.message}});
          }).catch(()=>{});
          break;
        case 'send':
          if (!this.session) throw new Error('Session is not open');
          this.session.send(params.message);
          result={accepted:true};
          break;
        case 'control':
          if (!this.session) throw new Error('Session is not open');
          if (!Array.isArray(params.args)) throw new Error('Control arguments must be an array');
          result=await this.session.control(params.method,...params.args);
          break;
        case 'begin_clear':
          if(!this.session || Object.keys(params).length || this.pending.size)throw new Error('Clear requires an open session without pending approvals');
          result=await this.session.beginClear();
          break;
        case 'commit_clear':
          if(!this.session || Object.keys(params).length!==1 || typeof params.sessionId!=='string')throw new Error('Exact pending session identity required');
          result=await this.session.commitClear(params.sessionId);
          break;
        case 'close':
          await this.close();
          result={closed:true};
          break;
        default: throw new Error('Unsupported session method');
      }
      await this.write({id,result:result??null});
    } catch(error) {
      await this.write({id,error:{message:error.message}});
    } finally {
      this.inflight.delete(id);
    }
  }

  ask(kind,request,{signal,requestId,...options}={}) {
    if (this.closed || signal?.aborted) return Promise.reject(new Error('Interactive request cancelled'));
    const id=`claude-${++this.nextId}`;
    return new Promise((resolve,reject)=>{
      const cleanup=()=>signal?.removeEventListener('abort',abort);
      const abort=()=>{
        if (!this.pending.delete(id)) return;
        cleanup();reject(new Error('Interactive request cancelled'));
        Promise.resolve(this.write({method:'serverRequest/resolved',params:{requestId:id}})).catch(()=>{});
      };
      this.pending.set(id,{resolve,reject,cleanup});
      signal?.addEventListener('abort',abort,{once:true});
      Promise.resolve().then(()=>{
        if(this.pending.has(id)) return this.write({id,method:`claude/${kind}`,
          params:{request,nativeRequestId:requestId??null,options}});
      }).catch(error=>{
        if(this.pending.delete(id)){cleanup();reject(error);}
      });
    });
  }

  async close() {
    this.closed=true;
    for(const {reject,cleanup} of this.pending.values()) {
      cleanup();reject(new Error('Session owner closed'));
    }
    this.pending.clear();
    await this.session?.close();
  }
}
