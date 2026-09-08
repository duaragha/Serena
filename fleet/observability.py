"""Read-only, bounded autonomy receipts for the dashboard and inspectors."""

import json


def autonomy_projection(store, run_id):
    with store._connect() as db:
        rows = db.execute(
            "SELECT event_seq AS seq,type,leg_id,attempt_id,payload_json,created_at FROM fleet_events WHERE run_id = ? AND ("
            "type LIKE 'run.recover%' OR type LIKE 'worker.progress.%' OR type = 'worker.stalled' OR "
            "type LIKE 'peer.request.%' OR type LIKE 'peer.help.%' OR type = 'peer.retry.queued' OR "
            "type LIKE 'learning.review%' OR type IN ('learning.proposed','learning.promoted','learning.revoked') OR "
            "type LIKE '%capacity%' OR type = 'leg.difficult_retry_queued') ORDER BY event_seq DESC LIMIT 80",
            (run_id,),
        ).fetchall()
        recoveries = db.execute(
            "SELECT COUNT(*) FROM fleet_events WHERE run_id = ? AND type = 'run.recovered'",
            (run_id,),
        ).fetchone()[0]
    return {
        "recoveries": recoveries,
        "max_recoveries": 2,
        "timeline": [
            {
                "seq": row["seq"],
                "type": row["type"],
                "leg_id": row["leg_id"],
                "attempt_id": row["attempt_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ],
        "scope": "Observed runtime receipts, not proof of improved speed or quality. Latest 80 autonomy events.",
    }
