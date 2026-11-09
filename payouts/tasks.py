from celery import shared_task

from . import bank, queue, services


@shared_task(name="payouts.send")
def send_payout(payout_id: int) -> None:
    payout = services.claim(payout_id)
    if payout is None:
        return

    # No transaction is open here on purpose. Holding a row lock across a
    # network call to a bank would stall every other query that touches it.
    try:
        reference = bank.transfer(
            idempotency_key=payout.idempotency_key,
            iban=payout.seller.iban,
            amount_cents=payout.amount_cents,
        )
    except bank.BankRejected as exc:
        services.mark_rejected(payout, str(exc))
    except bank.BankUnavailable as exc:
        delay = services.mark_retry(payout, str(exc))
        if delay is not None:
            queue.enqueue_send(payout.pk, countdown=delay)
    else:
        services.mark_sent(payout, reference)


@shared_task(name="payouts.reap")
def reap_payouts() -> int:
    return services.reap()
