// A crash with no log is what made the last backend failure take an afternoon.
//
// The packaged Windows app has no console, so process.stdout goes nowhere and
// every line the Python sidecar printed was thrown away. All anyone ever saw
// was "Failed to fetch" from a renderer whose server had quietly died, with no
// exit code recorded anywhere, let alone the traceback above it.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const logging = require('../logging');

function scratch() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'serena-logging-'));
  logging.configure(() => dir);
  return dir;
}

function contents(dir) {
  return fs.readFileSync(path.join(dir, 'logs', 'backend.log'), 'utf8');
}

test('backend output is written where it can be read back', () => {
  const dir = scratch();

  logging.backend('stderr', 'Traceback (most recent call last):\n  MemoryError\n');
  logging.close();

  const text = contents(dir);
  assert.match(text, /backend:stderr/);
  assert.match(text, /Traceback/);
  assert.match(text, /MemoryError/);
});

test('each line is timestamped and tagged with where it came from', () => {
  const dir = scratch();

  logging.backend('stdout', 'listening on 127.0.0.1:50144');
  logging.note('backend exited after 4210ms: code=1 signal=null');
  logging.close();

  const lines = contents(dir).trim().split('\n');
  assert.equal(lines.length, 2);
  for (const line of lines) {
    assert.match(line, /^\d{4}-\d{2}-\d{2}T[\d:.]+Z /, `not timestamped: ${line}`);
  }
  assert.match(lines[0], /\[backend:stdout\]/);
  assert.match(lines[1], /\[desktop\]/, 'the shell decisions must be distinguishable');
  assert.match(lines[1], /code=1/, 'the exit code is the whole point');
});

test('blank lines are not written', () => {
  const dir = scratch();

  logging.backend('stdout', '\n\n   \n');
  logging.close();

  const file = path.join(dir, 'logs', 'backend.log');
  assert.ok(!fs.existsSync(file) || contents(dir) === '', 'empty output should not create noise');
});

test('the log cannot grow without bound', () => {
  const dir = scratch();
  const chunk = 'x'.repeat(64 * 1024);

  // Comfortably past the rotation threshold.
  for (let i = 0; i < Math.ceil(logging.MAX_BYTES / chunk.length) + 4; i += 1) {
    logging.backend('stdout', chunk);
  }
  logging.close();

  const files = fs.readdirSync(path.join(dir, 'logs'));
  const total = files.reduce((sum, name) => sum + fs.statSync(path.join(dir, 'logs', name)).size, 0);

  assert.ok(files.length <= 2, `expected at most two files, got ${files.join(', ')}`);
  assert.ok(total <= logging.MAX_BYTES * 3, `log grew to ${total} bytes`);
});

test('a directory that cannot be written does not take the app down', () => {
  logging.configure(() => path.join(os.tmpdir(), 'serena-logging-does-not-exist', '\0bad'));

  assert.doesNotThrow(() => {
    logging.backend('stderr', 'this must not throw');
    logging.note('nor this');
  });
});

test('logging is inert until it is told where to write', () => {
  logging.configure(null);
  assert.equal(logging.logPath(), null);
  assert.doesNotThrow(() => logging.note('dropped on the floor'));
});
