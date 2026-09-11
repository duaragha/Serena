/* Lossless provider data behind the custom conversation view. No terminal scraping. */
export class WorkspaceConversation {
  constructor(sessionId) {
    this.sessionId = sessionId;
    this.turns = new Map();
    this.questions = new Map();
    this.sequence = 0;
    this.status = 'connecting';
    this.error = null;
    this.copyUnavailableAfterRevert = false;
    this.historyRevision = 0;
    this.metadata = {};
    this.models = [];
    this.commands = [];
    this.otherEvents = [];
    this.otherEventSizes = [];
    this.otherEventBytes = 0;
    this.otherEventsOmitted = 0;
  }

  rememberEvent(event) {
    const bytes = JSON.stringify(event).length * 2;
    // Full records stay in the disk journal. This is only a recent browser cache.
    if (bytes > 1024 * 1024) { this.otherEventsOmitted++; return; }
    while (this.otherEvents.length >= 100 || this.otherEventBytes + bytes > 1024 * 1024) {
      this.otherEvents.shift(); this.otherEventBytes -= this.otherEventSizes.shift();
      this.otherEventsOmitted++;
    }
    this.otherEvents.push(structuredClone(event));
    this.otherEventSizes.push(bytes); this.otherEventBytes += bytes;
  }

  turn(id) {
    if (!id) throw new Error('Missing turn identity');
    if (!this.turns.has(id)) this.turns.set(id, {id, status: 'unknown', items: new Map()});
    return this.turns.get(id);
  }

  item(turnId, itemId, type) {
    if (!itemId) throw new Error('Missing item identity');
    const turn = this.turn(turnId);
    if (!turn.items.has(itemId)) turn.items.set(itemId, {id: itemId, type});
    return turn.items.get(itemId);
  }

  apply(envelope) {
    const {sequence, event} = envelope;
    if (!Number.isSafeInteger(sequence) || sequence < 1) throw new Error('Invalid event sequence');
    if (sequence <= this.sequence) return false;
    if (sequence !== this.sequence + 1) throw new Error('Event gap: request replay before continuing');
    const {method, params = {}} = event;
    const sid = params.threadId || (method === 'workspace/history' ? params.thread?.id : null);
    if (sid && sid !== this.sessionId) throw new Error('Event belongs to another session');
    const p = structuredClone(params);
    if (method === 'workspace/history') {
      if (!p.thread || p.thread.id !== this.sessionId) throw new Error('History identity mismatch');
      this.error = null;
      this.metadata = p;
      this.copyUnavailableAfterRevert = p.copyUnavailableAfterRevert === true;
      this.historyRevision = p.historyRevision ?? 0;
      if (!this.metadata.model && typeof p.thread.model === 'string') this.metadata.model = p.thread.model;
      this.turns.clear();
      for (const source of p.thread.turns || []) {
        const turn = this.turn(source.id);
        Object.assign(turn, source, {items: new Map((source.items || []).map(i => [i.id, i]))});
      }
      this.status = [...this.turns.values()].some(t => t.status === 'inProgress') ? 'running' : 'ready';
    } else if (method === 'thread/reverted') {
      if (p.threadId !== this.sessionId) throw new Error('Revert identity mismatch');
      this.copyUnavailableAfterRevert = true;
      this.historyRevision++;
      this.turns.clear();
      this.metadata.historyCursor = null;
      this.status = 'reconciling';
    } else if (method === 'workspace/historyPage') {
      if ((p.historyRevision ?? 0) !== this.historyRevision) { this.sequence=sequence; return true; }
      const older = new Map();
      for (const source of p.turns || []) {
        older.set(source.id, {...source,items:new Map((source.items || []).map(i=>[i.id,i]))});
      }
      this.turns=new Map([...older,...this.turns]);
      this.metadata.historyCursor=p.historyCursor;
    } else if (method === 'thread/tokenUsage/updated') {
      this.metadata.tokenUsage = p.tokenUsage;
    } else if (method === 'workspace/claudeUsage') {
      this.metadata.claudeUsage = p.usage;
    } else if (method === 'workspace/accountLimits') {
      this.metadata.accountLimits = p;
    } else if (method === 'workspace/acpUsage') {
      this.metadata.acpUsage = p.usage;
    } else if (method === 'workspace/bridgeQueue') {
      this.metadata.bridgeQueueCount = p.count;
      this.metadata.bridgeQueue = p.requests || [];
    } else if (method === 'workspace/agentEvent') {
      if(Number.isSafeInteger(p.activeAgentCount) && p.activeAgentCount>=0)this.metadata.activeAgentCount=p.activeAgentCount;
      this.rememberEvent(event);
    } else if (method === 'workspace/activity') {
      this.status = p.status;
    } else if (method === 'workspace/commands') {
      this.commands = p.data || [];
    } else if (method === 'workspace/models') {
      this.models = p.data || [];
      Object.assign(this.metadata, p.settings || {});
    } else if (method === 'workspace/settings') {
      Object.assign(this.metadata, p);
    } else if (method === 'turn/started' || method === 'turn/completed') {
      const turn = this.turn(p.turn.id);
      const items = turn.items;
      Object.assign(turn, p.turn, {items, status: p.turn.status || (method === 'turn/started' ? 'inProgress' : 'completed')});
      for (const item of p.turn.items || []) items.set(item.id, item);
      if (method === 'turn/completed' && turn.status === 'completed' && [...items.values()].some(item =>
        ['agentMessage','plan'].includes(item.type) && !item.parentToolUseId && typeof item.text === 'string' && item.text.length)) {
        this.copyUnavailableAfterRevert = false;
      }
      this.status = method === 'turn/started' || [...this.turns.values()].some(item => item.status === 'inProgress')
        ? 'running' : (p.turn.status || 'completed');
    } else if (method === 'item/started' || method === 'item/completed') {
      if (!p.item?.id) throw new Error('Missing provider item');
      if (p.item.type === 'contextCompaction') p.item.status = method === 'item/completed' ? 'completed' : 'inProgress';
      this.turn(p.turnId).items.set(p.item.id, p.item);
    } else if (method === 'item/agentMessage/delta' || method === 'item/plan/delta') {
      const item = this.item(p.turnId, p.itemId, method.includes('/plan/') ? 'plan' : 'agentMessage');
      if (typeof p.parentToolUseId === 'string') item.parentToolUseId = p.parentToolUseId;
      if (typeof p.sourceModel === 'string') item.sourceModel = p.sourceModel;
      item.text = (item.text || '') + (p.delta || '');
    } else if (method === 'item/commandExecution/outputDelta') {
      const item = this.item(p.turnId, p.itemId, 'commandExecution');
      item.aggregatedOutput = (item.aggregatedOutput || '') + (p.delta || '');
    } else if (method === 'serverRequest/resolved') {
      this.questions.delete(p.requestId);
    } else if (method === 'workspace/transportClosed' || method === 'workspace/error' || method === 'workspace/archived') {
      this.status = 'unavailable';
      this.error = p.reason || (method === 'workspace/archived' ? 'Conversation archived' : 'Session connection unavailable');
      this.questions.clear();
    } else if (method === 'error') {
      this.error = p.error?.message || p.message || 'Agent reported an error';
      this.rememberEvent(event);
    } else if ('id' in event) {
      this.questions.set(event.id, structuredClone(event));
    } else {
      // New provider event types remain inspectable instead of being discarded.
      this.rememberEvent(event);
    }
    this.sequence = sequence;
    return true;
  }
}
