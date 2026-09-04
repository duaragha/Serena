'use strict';

const path = require('node:path');

function dialogOptions(value) {
  const input = value && typeof value === 'object' ? value : {};
  const title = String(input.title || 'Choose a folder').trim().slice(0, 120)
    || 'Choose a folder';
  const options = {
    title,
    properties: ['openDirectory'],
  };
  const startDir = typeof input.startDir === 'string' ? input.startDir.trim() : '';
  if (startDir && path.isAbsolute(startDir)) options.defaultPath = startDir;
  return options;
}

async function chooseFolder(dialog, owner, value) {
  if (!dialog || typeof dialog.showOpenDialog !== 'function') {
    throw new TypeError('an Electron dialog implementation is required');
  }
  const options = dialogOptions(value);
  const result = owner
    ? await dialog.showOpenDialog(owner, options)
    : await dialog.showOpenDialog(options);
  if (!result || result.canceled || !Array.isArray(result.filePaths)) return null;
  const selected = result.filePaths[0];
  return typeof selected === 'string' && path.isAbsolute(selected) ? selected : null;
}

module.exports = { chooseFolder, dialogOptions };
