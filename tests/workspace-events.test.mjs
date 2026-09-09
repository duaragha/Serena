import assert from 'node:assert/strict';
import test from 'node:test';
import {WorkspaceConversation} from '../ui/static/workspace-events.mjs';

const history = {method:'workspace/history', params:{thread:{id:'exact',turns:[]}}};
const wrap = (sequence, event) => ({sequence,event});

test('native history model reaches controls without overwriting explicit resume settings',()=>{
  const model=new WorkspaceConversation('exact');
  model.apply(wrap(1,{method:'workspace/history',params:{thread:{id:'exact',model:'last-real',turns:[]}}}));
  assert.equal(model.metadata.model,'last-real');
  model.apply(wrap(2,{method:'workspace/history',params:{model:'explicit-resume',thread:{id:'exact',model:'last-real',turns:[]}}}));
  assert.equal(model.metadata.model,'explicit-resume');
});

test('provider usage is retained without deriving invented context percentages', () => {
  const model = new WorkspaceConversation('exact');
  model.apply(wrap(1,{method:'thread/tokenUsage/updated',params:{threadId:'exact',tokenUsage:{last:{totalTokens:1234},total:{totalTokens:90000},modelContextWindow:null}}}));
  assert.equal(model.metadata.tokenUsage.last.totalTokens,1234);
  assert.equal(model.metadata.tokenUsage.modelContextWindow,null);
  model.apply(wrap(2,{method:'workspace/claudeUsage',params:{threadId:'exact',usage:{input_tokens:12,output_tokens:34}}}));
  assert.deepEqual(model.metadata.claudeUsage,{input_tokens:12,output_tokens:34});
});

test('history, streaming and completion update one real item', () => {
  const model = new WorkspaceConversation('exact');
  model.apply(wrap(1,history));
  model.apply(wrap(2,{method:'turn/started',params:{threadId:'exact',turn:{id:'t',status:'inProgress'}}}));
  model.apply(wrap(3,{method:'item/started',params:{threadId:'exact',turnId:'t',item:{id:'a',type:'agentMessage',text:''}}}));
  const delta = wrap(4,{method:'item/agentMessage/delta',params:{threadId:'exact',turnId:'t',itemId:'a',delta:'hello'}});
  assert.equal(model.apply(delta),true);
  assert.equal(model.apply(delta),false);
  assert.equal(model.turns.get('t').items.get('a').text,'hello');
  model.apply(wrap(5,{method:'item/completed',params:{threadId:'exact',turnId:'t',item:{id:'a',type:'agentMessage',text:'hello world'}}}));
  assert.equal(model.turns.get('t').items.size,1);
  assert.equal(model.turns.get('t').items.get('a').text,'hello world');
  model.apply(wrap(6,{method:'turn/completed',params:{threadId:'exact',turn:{id:'t',status:'completed'}}}));
  assert.equal(model.status,'completed');
  assert.equal(model.turns.get('t').items.size,1);
});

test('gaps and wrong session events cannot corrupt the conversation', () => {
  const model = new WorkspaceConversation('exact');
  model.apply(wrap(1,history));
  assert.throws(()=>model.apply(wrap(3,history)),/gap/);
  assert.throws(()=>model.apply(wrap(2,{method:'turn/started',params:{threadId:'wrong'}})),/another session/);
  assert.equal(model.sequence,1);
  assert.equal(model.turns.size,0);
});

test('tool output, real exit code, patches and unknown metadata are preserved', () => {
  const model = new WorkspaceConversation('exact');
  const command = {id:'cmd',type:'commandExecution',command:'pytest',status:'inProgress',pluginId:'plugin'};
  model.apply(wrap(1,{method:'item/started',params:{threadId:'exact',turnId:'t',item:command}}));
  model.apply(wrap(2,{method:'item/commandExecution/outputDelta',params:{threadId:'exact',turnId:'t',itemId:'cmd',delta:'failed'}}));
  model.apply(wrap(3,{method:'item/completed',params:{threadId:'exact',turnId:'t',item:{...command,status:'failed',exitCode:1,aggregatedOutput:'failed'}}}));
  const patch = {id:'patch',type:'fileChange',status:'completed',changes:[{path:'file.py',diff:'-old\n+new',kind:{type:'update'}}]};
  model.apply(wrap(4,{method:'item/completed',params:{threadId:'exact',turnId:'t',item:patch}}));
  assert.equal(model.turns.get('t').items.get('cmd').exitCode,1);
  assert.deepEqual(model.turns.get('t').items.get('patch'),patch);
  model.apply(wrap(5,{method:'new/providerFeature',params:{threadId:'exact',value:42}}));
  assert.equal(model.otherEvents[0].params.value,42);
});

test('approvals remain visible until provider resolves them', () => {
  const model = new WorkspaceConversation('exact');
  model.apply(wrap(1,{id:7,method:'item/tool/requestUserInput',params:{threadId:'exact',questions:[{id:'q',question:'Choose'}]}}));
  assert.equal(model.questions.size,1);
  model.apply(wrap(2,{method:'serverRequest/resolved',params:{threadId:'exact',requestId:7}}));
  assert.equal(model.questions.size,0);
  model.apply(wrap(3,{method:'workspace/transportClosed',params:{reason:'EOF'}}));
  assert.equal(model.status,'unavailable');
  assert.equal(model.error,'EOF');
});
