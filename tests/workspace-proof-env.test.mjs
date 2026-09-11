import assert from 'node:assert/strict';
import test from 'node:test';
import {join} from 'node:path';
import {isolatedProofEnv} from '../scripts/workspace-proof-env.mjs';

for (const platform of ['linux','win32']) test(`isolated proof environment on ${platform}`,()=>{
  const source={Path:'native-bin',SystemRoot:'system',WINDIR:'windows',COMSPEC:'cmd',PATHEXT:'.EXE',
    USERPROFILE:'real-user',HOME:'real-user',APPDATA:'real-config',LOCALAPPDATA:'real-local',TEMP:'real-temp',
    ANTHROPIC_API_KEY:'secret',CLAUDE_CODE_OAUTH_TOKEN:'secret',NODE_OPTIONS:'--require injected',
    CLAUDE_CONFIG_DIR:'real-claude',SERENA_WORKSPACE_ROOT:'real-workspace'};
  const env=isolatedProofEnv(source,'isolated',platform);
  assert.equal(env.PATH,'native-bin');
  assert.equal(env.HOME,'isolated');
  assert.equal(env.CLAUDE_CONFIG_DIR,join('isolated','config'));
  for (const key of ['ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN','NODE_OPTIONS','SERENA_WORKSPACE_ROOT']) assert(!(key in env));
  if (platform==='win32') {
    assert.equal(env.SystemRoot,'system');
    assert.equal(env.COMSPEC,'cmd');
    assert.equal(env.USERPROFILE,'isolated');
    assert.equal(env.APPDATA,join('isolated','appdata'));
    assert.equal(env.LOCALAPPDATA,join('isolated','localappdata'));
    assert.equal(env.TEMP,'isolated');
    assert.equal(env.TMP,'isolated');
  } else assert(!('SystemRoot' in env));
  assert.equal(source.HOME,'real-user');
});
