import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {WorkspacePane} from '../ui/static/workspace-pane.mjs';

// The breakdown only reads conversation metadata, so it can be exercised
// without a document.
const breakdown = (metadata) =>
  WorkspacePane.prototype.codexContextUsage.call({conversation:{metadata}});

// The exact shape Codex sends in thread/tokenUsage/updated, taken from a real
// event rather than invented: inputTokens already contains cachedInputTokens,
// and totalTokens is inputTokens + outputTokens.
const REAL = {
  model:'gpt-6-astra',
  tokenUsage:{
    total:{totalTokens:1788766621,inputTokens:1783839354,cachedInputTokens:1745753472,
           cacheWriteInputTokens:0,outputTokens:4927267,reasoningOutputTokens:1620890},
    last:{totalTokens:44401,inputTokens:44291,cachedInputTokens:41344,
          cacheWriteInputTokens:0,outputTokens:110,reasoningOutputTokens:36},
    modelContextWindow:258400,
  },
};
const rows = (usage) => Object.fromEntries(usage.categories.map(c=>[c.name,c.tokens]));

test('reports the window the model actually has, not the running total', () => {
  // total.totalTokens is cumulative across the whole thread and would read as
  // 692,000% of the window; the context is what the last request occupied.
  const usage = breakdown(REAL);
  assert.equal(usage.totalTokens, 44401);
  assert.equal(usage.maxTokens, 258400);
  assert.ok(usage.percentage > 17 && usage.percentage < 18, String(usage.percentage));
});

test('the cached part is not counted twice', () => {
  const usage = breakdown(REAL);
  const r = rows(usage);
  assert.equal(r['Cached input'], 41344);
  assert.equal(r['New input'], 44291 - 41344);
  assert.equal(r['Cached input'] + r['New input'], 44291, 'input split must rebuild inputTokens');
});

test('reasoning is separated from the rest of the output', () => {
  const r = rows(breakdown(REAL));
  assert.equal(r['Reasoning'], 36);
  assert.equal(r['Output'], 110 - 36);
});

test('the rows add up to the context window', () => {
  const usage = breakdown(REAL);
  const r = rows(usage);
  const used = r['Cached input'] + r['New input'] + r['Reasoning'] + r['Output'];
  assert.equal(used, usage.totalTokens, 'used rows must rebuild the total');
  assert.equal(used + r['Free space'], usage.maxTokens, 'every token is accounted for');
});

test('a full window leaves no negative free space', () => {
  const usage = breakdown({tokenUsage:{modelContextWindow:1000,
    last:{totalTokens:1200,inputTokens:1200,cachedInputTokens:0,outputTokens:0,reasoningOutputTokens:0}}});
  assert.equal(rows(usage)['Free space'], 0);
  assert.equal(usage.percentage, 100, 'percentage is clamped for the progress bar');
});

test('missing counters degrade to zero instead of NaN', () => {
  const usage = breakdown({tokenUsage:{modelContextWindow:1000,
    last:{totalTokens:400,inputTokens:400}}});
  const r = rows(usage);
  assert.equal(r['Cached input'], 0);
  assert.equal(r['New input'], 400);
  assert.equal(r['Reasoning'], 0);
  assert.equal(r['Output'], 0);
  for(const value of Object.values(r)) assert.ok(Number.isSafeInteger(value), 'no NaN reached the list');
});

test('a total Codex did not send is rebuilt from its parts', () => {
  const usage = breakdown({tokenUsage:{modelContextWindow:1000,
    last:{inputTokens:300,cachedInputTokens:100,outputTokens:50,reasoningOutputTokens:10}}});
  assert.equal(usage.totalTokens, 350);
});

test('it names the model, and says so when it cannot', () => {
  assert.equal(breakdown(REAL).model, 'gpt-6-astra');
  assert.equal(breakdown({tokenUsage:REAL.tokenUsage}).model, 'Codex');
});

test('it says the numbers describe the last request', () => {
  assert.match(breakdown(REAL).caption, /most recent request/);
});

for(const [name, metadata] of [
  ['no usage at all', {}],
  ['usage without a window', {tokenUsage:{last:{totalTokens:1}}}],
  ['a window of zero', {tokenUsage:{modelContextWindow:0,last:{totalTokens:1}}}],
  ['no last request yet', {tokenUsage:{modelContextWindow:1000,total:{totalTokens:5}}}],
]) test(`explains itself rather than showing nothing: ${name}`, () => {
  assert.throws(() => breakdown(metadata), /Codex reports context after its first reply/);
});

test('the button is offered for Codex and still gated for Claude', () => {
  const source = readFileSync(new URL('../ui/static/workspace-pane.mjs', import.meta.url), 'utf8');
  assert.match(source, /context\.hidden=provider==='Claude' \? !controls\.contextUsage : provider!=='Codex';/);
});

test('Codex answers locally instead of making a request Claude has to serve', () => {
  const source = readFileSync(new URL('../ui/static/workspace-pane.mjs', import.meta.url), 'utf8');
  assert.match(source, /this\.provider==='Codex' \? this\.codexContextUsage\(\) : await this\.controls\.contextUsage\(\)/);
});
