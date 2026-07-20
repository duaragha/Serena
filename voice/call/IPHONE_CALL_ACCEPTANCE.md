# One iPhone call for cold-open, integrity, and cost acceptance

One physical call closes both v2b cold-open and v2c call-integrity. The call
must begin after the host baseline, last at least 20 minutes, include a real
network roam, and end with the in-app hang-up control.

## Before touching the phone

From the Serena repository, run:

```bash
.venv/bin/python -m voice.call.iphone_acceptance prepare
```

Continue only when the JSON says `"ok": true`. This checks the resident brain,
the warmed mobile call host, the private call token, the Tailnet app URL, and
stages an append-only telemetry and billing baseline so this exact call is
isolated.

## Make the one call

1. Confirm Tailscale is connected on the iPhone and microphone access for the
   Serena site is already allowed.
2. Fully close the current Serena browser tab or home-screen web app. Reopen
   <https://raghavslaptop.tail4d6220.ts.net:8445/app> as a fresh page.
3. Tap the Serena contact card once. It now opens the call and starts it in the
   same tap. Do not reload after tapping.
4. Listen for the automatic greeting. The call screen should show `hello` below
   5.0 seconds.
5. Keep the call open for at least `20:30`. Make at least three complete
   push-to-talk turns, and wait for each spoken answer to finish.
6. Around minute 8, turn Wi-Fi off so the iPhone moves to cellular with
   Tailscale still connected. Wait for `reconnecting` to clear, then make one
   complete push-to-talk turn. Turn Wi-Fi back on later and let any second
   reconnect settle before speaking.
7. Pay attention to the whole call. Any click, repeated chunk, overlap, long
   audio break, or distorted speech means the acoustic gate did not pass.
8. End with the in-app `hang up` button. Do not close the tab, swipe the app
   away, or let the phone lock as the way of ending the call.

The cleanest schedule is one turn around minute 2, one immediately after the
cellular roam, and one around minute 18. More normal conversation is fine.

## Close all three gates

After the call, open the Anthropic billing dashboard and confirm it shows no
metered API charge for the staged call window.

Only if the entire physical call sounded clean, run:

```bash
.venv/bin/python -m voice.call.iphone_acceptance verify \
  --heard-clean \
  --billing-dashboard-clear
```

The verifier requires all of the following from that one staged call:

- one cold greeting under 5 seconds
- at least 20 minutes and three completed turns
- at least one network-roam reconnect
- clean final hang-up
- no sequence gap, underrun, queue overflow, or call error
- content playback for every transcript turn
- every turn present in the resident Claude session store
- the explicit human `--heard-clean` acoustic attestation
- verified subscription auth before and after the exact call
- no metered provider process, worker, or environment override
- the explicit human `--billing-dashboard-clear` billing attestation

Success writes the durable combined report to:

```text
~/.local/state/serena/iphone-call-acceptance.json
```

If the call did not sound clean, omit `--heard-clean`. The verifier will record
the failure honestly, and the call can be retried later with a fresh `prepare`.
