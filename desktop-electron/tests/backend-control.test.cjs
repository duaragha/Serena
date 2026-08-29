// The server the window talks to can be older than the code on disk.
//
// Serena runs from a checkout that changes under her, and the shell usually
// attaches to a long-lived systemd server rather than owning one. A fix can
// land and every request keeps being served by the process that started hours
// before, with nothing saying so. That cost an afternoon once: the only symptom
// was that a correct fix appeared not to work.

const assert = require('node:assert/strict');
const test = require('node:test');

const control = require('../backend-control');

function server(body) {
  return async () => body;
}

function dead(message) {
  return async () => {
    throw new Error(message);
  };
}

test('a server running the current code is not flagged', async () => {
  const state = await control.freshness('http://127.0.0.1:8767', server({
    ok: true, stale: false, stale_by_seconds: 0, source_root: '/repo', pid: 7,
  }));

  assert.equal(state.reachable, true);
  assert.equal(state.stale, false);
  assert.equal(control.staleLabel(state), null, 'nothing to say when it is current');
});

test('a stale server says how far behind it is', async () => {
  const state = await control.freshness('http://127.0.0.1:8767', server({
    ok: true, stale: true, stale_by_seconds: 7200, source_root: '/repo', pid: 7,
  }));

  assert.equal(state.stale, true);
  assert.match(control.staleLabel(state), /2 h behind/);
});

test('the label scales with how long it has been wrong', () => {
  assert.match(control.staleLabel({ stale: true, staleBySeconds: 30 }), /code on disk is newer/);
  assert.match(control.staleLabel({ stale: true, staleBySeconds: 600 }), /10 min behind/);
  assert.match(control.staleLabel({ stale: true, staleBySeconds: 36000 }), /10 h behind/);
});

test('an unreachable server is unknown, not alarming', async () => {
  const state = await control.freshness('http://127.0.0.1:8767', dead('ECONNREFUSED'));

  assert.equal(state.reachable, false);
  assert.equal(state.stale, false, 'a server that cannot be asked must not be called stale');
  assert.equal(control.staleLabel(state), null);
});

test('an older server without the endpoint does not break the menu', async () => {
  const state = await control.freshness('http://127.0.0.1:8767', dead('HTTP 404'));
  assert.equal(state.stale, false);
});

test('with no backend at all there is nothing to report', async () => {
  const state = await control.freshness('', server({ stale: true }));
  assert.equal(state.reachable, false);
  assert.equal(state.stale, false);
});

test('the restart goes through the helper, never a bare systemctl', () => {
  // A bare restart is issued from inside the unit being restarted, so the
  // shell is killed partway through and the outcome is unknowable.
  const launch = control.sharedRestartCommand('/home/raghav/Documents/Projects/serena');

  assert.match(launch.args[0], /serena-host-restart\.sh$/);
  assert.equal(launch.args[1], 'serena-mobile-host.service');
  assert.equal(launch.env.SERENA_ALLOW_HOST_RESTART, '1', 'the helper refuses without this');
  assert.ok(!JSON.stringify(launch).includes('systemctl'), 'must not shell out to systemctl');
});

test('restarting without knowing where the checkout is refuses', () => {
  assert.throws(() => control.sharedRestartCommand(''), /where it runs from/);
});

test('waiting returns as soon as the new server answers', async () => {
  let calls = 0;
  const getJson = async () => {
    calls += 1;
    if (calls < 3) throw new Error('ECONNREFUSED');
    return { ok: true, pid: 4242 };
  };

  const result = await control.waitForBackend('http://127.0.0.1:8767', getJson, {
    intervalMs: 0,
    sleep: async () => {},
  });

  assert.deepEqual({ ok: result.ok, pid: result.pid }, { ok: true, pid: 4242 });
  assert.equal(calls, 3);
});

test('waiting gives up rather than hanging the menu forever', async () => {
  let clock = 0;
  const result = await control.waitForBackend('http://127.0.0.1:8767', dead('ECONNREFUSED'), {
    timeoutMs: 1000,
    intervalMs: 0,
    sleep: async () => { clock += 400; },
    now: () => clock,
  });

  assert.equal(result.ok, false);
  assert.match(result.reason, /ECONNREFUSED/);
});
