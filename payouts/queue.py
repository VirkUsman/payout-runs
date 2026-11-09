from celery import current_app


def enqueue_send(payout_id: int, countdown: float | None = None) -> None:
    current_app.send_task("payouts.send", args=[payout_id], countdown=countdown)
