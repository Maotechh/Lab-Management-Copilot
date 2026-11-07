"""List devices or queue a marked, generic notification for one selected device.

Run as the application service user with its environment. The existing worker
sends the event; no inventory, threshold, reservation or chat content changes.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from consumable_assistant.database import connect


def queue(conn, device_id: str, delay_seconds: int) -> dict:
    subscriptions = conn.execute(
        'SELECT id FROM push_subscriptions WHERE device_id=? AND active=1', (device_id,),
    ).fetchall()
    if not subscriptions:
        raise ValueError('The selected device has no active notification subscription')
    due = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
    event_id = conn.execute("INSERT INTO notification_events(kind) VALUES ('delivery_test')").lastrowid
    conn.executemany('INSERT INTO push_deliveries(event_id,subscription_id,next_attempt) VALUES (?,?,?)',
                     [(event_id, row['id'], due) for row in subscriptions])
    conn.commit()
    return {'event_id': event_id, 'device_id': device_id, 'next_attempt': due, 'test': True}


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--device-id', help='Queue a test only for this subscribed device')
    action.add_argument('--event-id', type=int, help='Read delivery status; sent does not prove device receipt')
    parser.add_argument('--delay-seconds', type=int, default=45, choices=range(0, 301))
    args = parser.parse_args()
    with connect() as conn:
        if args.device_id:
            result = queue(conn, args.device_id, args.delay_seconds)
        elif args.event_id:
            result = {'event_id': args.event_id, 'deliveries': [dict(row) for row in conn.execute(
                'SELECT s.device_id,d.status,d.attempts,d.error,d.next_attempt FROM push_deliveries d '
                'JOIN push_subscriptions s ON s.id=d.subscription_id WHERE d.event_id=?', (args.event_id,),
            )]}
        else:
            result = {'devices': [dict(row) for row in conn.execute(
                'SELECT device_id,actor,active,created_at FROM push_subscriptions ORDER BY id',
            )]}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
