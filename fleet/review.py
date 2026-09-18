"""Bounded review rounds, persisted in the existing event journal."""
from fleet.completion import extract_envelope, severity_histogram


def latest_findings(store, run_id):
    findings = []
    for output in store.completed_outputs(run_id):
        if output['phase'] != 'verify':
            continue
        payload, _, error = extract_envelope(output['output_text'])
        if payload and not error:
            findings.extend(item for unit in payload.get('units', []) for item in unit.get('findings', [])
                            if isinstance(item, dict))
    return findings


def review_decision(findings, rounds, budget, gated):
    open_items = [f for f in findings if str(f.get('severity', '')).casefold() in ('blocker', 'major')]
    units = {f['unit_id'] for f in open_items}
    if not open_items:
        return 'clean', units
    if rounds < budget:
        return 'retry', units
    return ('failed' if gated and any(str(f['severity']).casefold() == 'blocker' for f in open_items) else 'unresolved'), units


def advance_review(store, run_id, policy):
    """Return retry/clean/unresolved/failed after a complete verify/finalize cycle."""
    from fleet.dag import reset_leg_for_retry
    findings = latest_findings(store, run_id)
    with store._connect() as db:
        rounds = db.execute("SELECT COUNT(*) FROM fleet_events WHERE run_id=? AND type='run.review.round'", (run_id,)).fetchone()[0]
    decision, units = review_decision(findings, rounds, policy.review_rounds, policy.blocker_gates_run)
    if decision == 'retry':
        with store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            legs = db.execute("SELECT DISTINCT p.leg_id FROM fleet_work_unit_phases p JOIN fleet_legs l USING(leg_id) "
                "WHERE p.run_id=? AND l.phase='verify' AND p.unit_id IN (" + ','.join('?' for _ in units) + ')',
                (run_id, *sorted(units))).fetchall()
            for leg in legs:
                reset_leg_for_retry(db, leg_id=leg['leg_id'], include_completed=True, reset_completed_descendants=True)
            store._insert_event(db, run_id=run_id, event_type='run.review.round',
                payload={'round': rounds + 1, 'unit_ids': sorted(units)})
    elif decision in ('failed', 'unresolved'):
        store.append_event(run_id, 'run.review.unresolved',
            {'rounds': rounds, 'findings': findings, 'severity_histogram': severity_histogram(findings)})
    return decision


def report_review(store, run_id):
    findings = latest_findings(store, run_id)
    with store._connect() as db:
        unresolved = db.execute("SELECT 1 FROM fleet_events WHERE run_id=? AND type='run.review.unresolved' LIMIT 1", (run_id,)).fetchone()
    return {'severity_histogram': severity_histogram(findings),
            'unresolved_findings': [f for f in findings if str(f.get('severity', '')).casefold() in ('blocker', 'major')] if unresolved else [],
            'self_review_note': 'Solo runs re-check their own fixes; this does not provide independent review.'
                if len((store.get_run(run_id) or {}).get('policy', {}).get('workstreams', [])) == 1 else ''}
