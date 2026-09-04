'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');

const { chooseFolder, dialogOptions } = require('../folder-picker');

test('folder picker accepts only a bounded title and absolute start directory', () => {
  const startDir = path.resolve('/tmp', 'serena-project');
  assert.deepEqual(dialogOptions({ title: '  New project  ', startDir }), {
    title: 'New project',
    properties: ['openDirectory'],
    defaultPath: startDir,
  });
  assert.deepEqual(dialogOptions({ title: '', startDir: 'relative/path' }), {
    title: 'Choose a folder',
    properties: ['openDirectory'],
  });
});

test('folder picker returns the selected absolute directory in the owning window', async () => {
  const selected = path.resolve('/tmp', 'chosen-project');
  const owner = { name: 'main-window' };
  const calls = [];
  const dialog = {
    async showOpenDialog(...args) {
      calls.push(args);
      return { canceled: false, filePaths: [selected] };
    },
  };

  assert.equal(await chooseFolder(dialog, owner, { title: 'Choose' }), selected);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], owner);
  assert.deepEqual(calls[0][1].properties, ['openDirectory']);
});

test('folder picker returns null for cancellation or an unsafe result', async () => {
  const canceled = { showOpenDialog: async () => ({ canceled: true, filePaths: [] }) };
  assert.equal(await chooseFolder(canceled, null, {}), null);

  const relative = { showOpenDialog: async () => ({ canceled: false, filePaths: ['relative'] }) };
  assert.equal(await chooseFolder(relative, null, {}), null);
});

test('folder picker rejects a missing dialog implementation', async () => {
  await assert.rejects(() => chooseFolder(null, null, {}), /dialog implementation/);
});
