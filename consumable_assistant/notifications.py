from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from .database import connect


def validate_subscription(subscription: dict) -> None:
    url = urlsplit(subscription.get("endpoint") or "")
    allowed = (
        "fcm.googleapis.com",
        "push.services.mozilla.com",
        "push.apple.com",
        "notify.windows.com",
    )
    host = url.hostname or ""
    if (
        url.scheme != "https"
        or url.port not in (None, 443)
        or url.username
        or url.password
        or not any(host == suffix or host.endswith("." + suffix) for suffix in allowed)
    ):
        raise ValueError("推送地址不属于受支持的浏览器服务")
    keys = subscription.get("keys") or {}
    if (
        not keys.get("auth")
        or not keys.get("p256dh")
        or len(json.dumps(subscription)) > 8192
    ):
        raise ValueError("推送订阅无效")
    for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(entry[4][0]).is_global:
            raise ValueError("推送地址不能指向内部网络")


def send(subscription: dict, *, test: bool = False) -> None:
    import requests
    from pywebpush import webpush

    validate_subscription(subscription)

    class NoRedirectSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs["allow_redirects"] = False
            return super().request(*args, **kwargs)

    webpush(
        subscription_info=subscription,
        data=json.dumps(
            {
                "title": "耗材管理测试提醒" if test else "耗材管理提醒",
                "body": "这是设备通知测试，请进入校内提醒页面查看。" if test else "库存或实验安排需要关注，请进入校内系统查看。",
                "url": "/?tab=alerts",
            },
            ensure_ascii=False,
        ),
        vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
        vapid_claims={"sub": os.environ["VAPID_SUBJECT"]},
        ttl=3600,
        timeout=12,
        requests_session=NoRedirectSession(),
    )


def tick() -> None:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE push_deliveries SET status='cancelled' WHERE status='pending' AND event_id IN (SELECT e.id FROM notification_events e JOIN alert_states a ON a.item_id=e.item_id WHERE a.is_low=0 OR a.acknowledged=1)"
        )
        for row in conn.execute(
            "SELECT * FROM alert_states WHERE is_low=1 AND acknowledged=0 AND (snooze_until IS NULL OR julianday(snooze_until)<=julianday('now')) AND (last_reminded_at IS NULL OR julianday(last_reminded_at)<julianday('now','-1 day'))"
        ).fetchall():
            initial = conn.execute(
                "SELECT 1 FROM push_deliveries WHERE event_id=? AND status IN ('pending','sent')",
                (row["event_id"],),
            ).fetchone()
            if row["last_reminded_at"] or not initial:
                event = conn.execute(
                    "INSERT INTO notification_events(item_id,kind) VALUES (?,'reminder')",
                    (row["item_id"],),
                ).lastrowid
                conn.execute(
                    "INSERT INTO push_deliveries(event_id,subscription_id) SELECT ?,id FROM push_subscriptions WHERE active=1",
                    (event,),
                )
            conn.execute(
                "UPDATE alert_states SET last_reminded_at=CURRENT_TIMESTAMP WHERE item_id=?",
                (row["item_id"],),
            )
        conn.commit()
        if not os.environ.get("VAPID_PRIVATE_KEY") or not os.environ.get(
            "VAPID_SUBJECT"
        ):
            return
        deliveries = conn.execute(
            "SELECT d.*,s.subscription,s.active,e.kind AS event_kind FROM push_deliveries d JOIN push_subscriptions s ON s.id=d.subscription_id JOIN notification_events e ON e.id=d.event_id LEFT JOIN alert_states a ON a.item_id=e.item_id WHERE d.status='pending' AND julianday(d.next_attempt)<=julianday('now') AND (a.item_id IS NULL OR (a.is_low=1 AND a.acknowledged=0 AND (a.snooze_until IS NULL OR julianday(a.snooze_until)<=julianday('now')))) LIMIT 20"
        ).fetchall()
    for delivery in deliveries:
        state, error = "sent", None
        attempts = delivery["attempts"] + 1
        try:
            if not delivery["active"]:
                state = "disabled"
            else:
                subscription = json.loads(delivery["subscription"])
                if delivery["event_kind"] == "delivery_test":
                    send(subscription, test=True)
                else:
                    send(subscription)
        except Exception as exc:  # noqa: BLE001 - Persist provider errors without exposing subscription data.
            response = getattr(exc, "response", None)
            code = getattr(response, "status_code", None)
            state = (
                "expired"
                if code in (404, 410)
                else "failed"
                if attempts >= 8
                else "pending"
            )
            error = f"{type(exc).__name__}:{code}" if code else type(exc).__name__
        with connect() as conn:
            if state == "expired":
                conn.execute(
                    "UPDATE push_subscriptions SET active=0 WHERE id=?",
                    (delivery["subscription_id"],),
                )
            due = (
                datetime.now(UTC) + timedelta(seconds=min(3600, 30 * 2**attempts))
            ).isoformat()
            conn.execute(
                "UPDATE push_deliveries SET status=?,attempts=?,error=?,next_attempt=? WHERE event_id=? AND subscription_id=?",
                (
                    state,
                    attempts,
                    error,
                    due,
                    delivery["event_id"],
                    delivery["subscription_id"],
                ),
            )


def main() -> None:
    from .genai import check_health
    from .settings import DB_PATH

    worker_lock = (DB_PATH.parent / ".notification-worker.lock").open("a")
    fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    last_check = 0.0
    while True:
        try:
            tick()
            if time.monotonic() - last_check >= 300:
                check_health()
                last_check = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - Keep scheduled checks running after an individual failure.
            print("Worker check failed:", type(exc).__name__, flush=True)
        time.sleep(10)


if __name__ == "__main__":
    main()
