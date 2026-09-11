import assert from 'node:assert/strict';
import test from 'node:test';
import {WorkspaceConversation} from '../ui/static/workspace-events.mjs';

const history = {method:'workspace/history', params:{thread:{id:'exact',turns:[]}}};
const wrap = (sequence, event) => ({sequence,event});

test('child lifecycle updates never complete or replace the parent turn',()=>{
  const model=new WorkspaceConversation('exact');
  model.apply(wrap(1,history));
  model.apply(wrap(2,{method:'turn/started',params:{turn:{id:'parent',status:'inProgress'}}}));
  model.apply(wrap(3,{method:'workspace/agentEvent',params:{threadId:'exact',agentThreadId:'child',activeAgentCount:0,event:{method:'turn/completed',params:{threadId:'child',turn:{id:'child-turn',status:'completed'}}}}}));
  assert.equal(model.status,'running');
  assert.equal(model.turns.size,1);
  assert.equal(model.turns.has('child-turn'),false);
  assert.equal(model.metadata.activeAgentCount,0);
  assert.equal(model.otherEvents.at(-1).params.agentThreadId,'child');
  model.apply(wrap(4,{id:77,method:'item/commandExecution/requestApproval',params:{threadId:'exact',agentThreadId:'child'}}));
  assert.equal(model.questions.get(77).params.agentThreadId,'child');
  model.apply(wrap(5,{method:'serverRequest/resolved',params:{threadId:'exact',agentThreadId:'child',requestId:77}}));
  assert.equal(model.questions.size,0);
});

test('reverted history suppresses copy until a new completed main output',()=>{
  const model=new WorkspaceConversation('exact');
  model.apply(wrap(1,history));
  model.apply(wrap(2,{method:'thread/reverted',params:{threadId:'exact'}}));
  assert.equal(model.status,'reconciling');
  assert.equal(model.copyUnavailableAfterRevert,true);
  model.apply(wrap(3,{method:'workspace/history',params:{historyRevision:1,copyUnavailableAfterRevert:true,thread:{id:'exact',turns:[
    {id:'old',status:'completed',items:[{id:'a',type:'agentMessage',text:'old answer'}]}]}}}));
  assert.equal(model.copyUnavailableAfterRevert,true);
  model.apply(wrap(4,{method:'turn/completed',params:{turn:{id:'shell',status:'completed',items:[]}}}));
  assert.equal(model.copyUnavailableAfterRevert,true);
  model.apply(wrap(5,{method:'turn/completed',params:{turn:{id:'new',status:'completed',items:[{id:'b',type:'agentMessage',text:'new answer'}]}}}));
  assert.equal(model.copyUnavailableAfterRevert,false);
  assert.throws(()=>model.apply(wrap(6,{method:'thread/reverted',params:{threadId:'foreign'}})),/another session/);
  assert.equal(model.sequence,5);
  model.apply(wrap(6,{method:'workspace/historyPage',params:{historyRevision:0,turns:[{id:'discarded',items:[]}],historyCursor:'stale'}}));
  assert.equal(model.turns.has('discarded'),false);
});

test('completion of one input does not mark another accepted turn ready',()=>{
  const model=new WorkspaceConversation('exact');
  model.apply(wrap(1,history));
  model.apply(wrap(2,{method:'turn/started',params:{turn:{id:'first',status:'inProgress'}}}));
  model.apply(wrap(3,{method:'turn/started',params:{turn:{id:'second',status:'inProgress'}}}));
  model.apply(wrap(4,{method:'turn/completed',params:{turn:{id:'first',status:'completed'}}}));
  assert.equal(model.status,'running');
  model.apply(wrap(5,{method:'turn/completed',params:{turn:{id:'second',status:'completed',combinedWithTurnId:'first'}}}));
  assert.equal(model.status,'completed');
  assert.equal(model.turns.get('second').combinedWithTurnId,'first');
});

test('implicit turn statuses do not leave the pane permanently busy',()=>{
  const model=new WorkspaceConversation('exact');
  model.apply(wrap(1,{method:'turn/started',params:{turn:{id:'one'}}}));
  assert.equal(model.turns.get('one').status,'inProgress');
  model.apply(wrap(2,{method:'turn/completed',params:{turn:{id:'one'}}}));
  assert.equal(model.status,'completed');
  assert.equal(model.turns.get('one').status,'completed');
});

test('fresh exact history clears stale disconnection error but older pages do not',()=>{
  const model=new WorkspaceConversation('exact');
  model.apply(wrap(1,history));
  model.apply(wrap(2,{method:'workspace/transportClosed',params:{reason:'Session disconnected'}}));
  model.apply(wrap(3,{method:'workspace/historyPage',params:{turns:[],historyCursor:null}}));
  assert.equal(model.error,'Session disconnected');
  assert.throws(()=>model.apply(wrap(4,{method:'workspace/history',params:{thread:{id:'wrong',turns:[]}}})),/session/);
  assert.equal(model.error,'Session disconnected');
  model.apply(wrap(4,history));
  assert.equal(model.error,null);assert.equal(model.status,'ready');
});

test('durable archive completion makes the exact pane unavailable',()=>{
  const model=new WorkspaceConversation('exact');
  model.apply(wrap(1,history));
  model.apply(wrap(2,{method:'workspace/archived',params:{threadId:'exact',threadIds:['exact'],count:1}}));
  assert.equal(model.status,'unavailable');
  assert.equal(model.error,'Conversation archived');
  assert.throws(()=>model.apply(wrap(3,{method:'workspace/archived',params:{threadId:'foreign'}})),/another session/);
});

test('durable delete completion makes only the exact pane unavailable',()=>{
  const model=new WorkspaceConversation('exact');
  model.apply(wrap(1,history));
  model.apply(wrap(2,{method:'workspace/deleted',params:{threadId:'exact',threadIds:['exact','child'],count:2}}));
  assert.equal(model.status,'unavailable');
  assert.equal(model.error,'Conversation deleted');
  assert.equal(model.questions.size,0);
  assert.throws(()=>model.apply(wrap(3,{method:'workspace/deleted',params:{threadId:'foreign',threadIds:['foreign'],count:1}})),/another session/);
  assert.equal(model.sequence,2);
});

test('older pages prepend without overwriting live turns or changing working status',()=>{
  const model=new WorkspaceConversation('exact');
  model.apply(wrap(1,{method:'workspace/history',params:{historyCursor:'older',thread:{id:'exact',turns:[{id:'live',status:'inProgress',items:[{id:'a',type:'agentMessage',text:'current'}]}]}}}));
  model.apply(wrap(2,{method:'workspace/historyPage',params:{threadId:'exact',historyCursor:null,turns:[{id:'old',status:'completed',items:[]},{id:'live',status:'completed',items:[]}]}}));
  assert.deepEqual([...model.turns.keys()],['old','live']);
  assert.equal(model.turns.get('live').items.get('a').text,'current');
  assert.equal(model.status,'running');
  assert.equal(model.metadata.historyCursor,null);
});

test('subagent provenance survives streaming and completion without replacing parent model',()=>{
  const model=new WorkspaceConversation('exact');
  model.apply(wrap(1,{method:'workspace/settings',params:{model:'parent-model'}}));
  model.apply(wrap(2,{method:'item/agentMessage/delta',params:{turnId:'t',itemId:'child',delta:'partial',parentToolUseId:'agent-tool'}}));
  assert.equal(model.turns.get('t').items.get('child').parentToolUseId,'agent-tool');
  model.apply(wrap(3,{method:'item/completed',params:{turnId:'t',item:{id:'child',type:'agentMessage',text:'complete',parentToolUseId:'agent-tool',sourceModel:'child-model'}}}));
  assert.equal(model.turns.get('t').items.size,1);
  assert.equal(model.turns.get('t').items.get('child').sourceModel,'child-model');
  assert.equal(model.metadata.model,'parent-model');
});

test('raw event cache is count and size bounded without losing replay position',()=>{
  const model=new WorkspaceConversation('exact');
  for(let sequence=1;sequence<=1000;sequence++)model.apply(wrap(sequence,{method:'workspace/claude',params:{record:{text:'delta'}}}));
  assert.equal(model.otherEvents.length,100);
  assert.equal(model.otherEventsOmitted,900);
  assert.equal(model.sequence,1000);
  model.apply(wrap(1001,{method:'large/native',params:{text:'x'.repeat(600000)}}));
  assert.equal(model.otherEvents.length,100);
  assert.equal(model.otherEventsOmitted,901);
  assert.equal(model.sequence,1001);
  for(let sequence=1002;sequence<1100;sequence++)model.apply(wrap(sequence,{method:'medium/native',params:{text:'x'.repeat(50000)}}));
  assert.ok(model.otherEventBytes<=1024*1024);
  assert.equal(model.otherEventSizes.length,model.otherEvents.length);
});

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
  const limits={limits:[{name:'Codex',primary:null,secondary:{usedPercent:57}}],observedAt:'2026-09-10T12:00:00Z'};
  model.apply(wrap(3,{method:'workspace/accountLimits',params:limits}));
  assert.deepEqual(model.metadata.accountLimits,limits);
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
