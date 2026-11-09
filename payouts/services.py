import random
from collections import defaultdict
from datetime import date, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import queue
from .models import Payout, Run, Sale

BACKOFF_CAP_SECONDS = 3600
# A pending payout normally has a message on the way. The grace stops the
# reaper queueing a second one for every row of a run that was committed a
# moment ago.
REAP_PENDING_GRACE_SECONDS = 60
REAP_BATCH = 500


class NotRetryable(Exception):
    pass


def _queue_after_commit(payout_id: int) -> None:
    # robust, so a broker failure on one message cannot stop the rest. Whatever
    # does not get queued here, the reaper finds.
    #
    # A named function and not functools.partial: when a robust callback
    # raises, Django logs func.__qualname__, a partial has none, and the
    # AttributeError escapes the very handler that was meant to contain it.
    def send() -> None:
        queue.enqueue_send(payout_id)

    transaction.on_commit(send, robust=True)


def create_run(period_end: date, user) -> tuple[Run, bool]:
    """Turn every unpaid sale up to period_end into one payout per seller."""
    try:
        with transaction.atomic():
            run = Run.objects.create(period_end=period_end, created_by=user)
            payouts = _collect_sales(run)
            for payout in payouts:
                _queue_after_commit(payout.pk)
    except IntegrityError:
        existing = Run.objects.filter(period_end=period_end).first()
        if existing is None:
            raise
        return existing, False
    return run, True


def _collect_sales(run: Run) -> list[Payout]:
    # Locked in id order so two overlapping runs queue behind each other
    # instead of deadlocking. The loser re-reads payout IS NULL after the lock
    # and finds those lines are gone.
    lines = (
        Sale.objects.select_for_update()
        .filter(payout__isnull=True, earned_on__lte=run.period_end)
        .order_by("pk")
        .values("pk", "seller_id", "amount_cents")
    )
    by_seller: dict[int, list[dict]] = defaultdict(list)
    for line in lines:
        by_seller[line["seller_id"]].append(line)

    payouts = []
    claimed = []
    for seller_id, seller_lines in by_seller.items():
        total = sum(line["amount_cents"] for line in seller_lines)
        # A seller whose refunds cancel their sales gets nothing this run. The
        # lines stay open and carry into the next one.
        if total <= 0:
            continue
        payouts.append(Payout(run=run, seller_id=seller_id, amount_cents=total))
        claimed.append([line["pk"] for line in seller_lines])

    Payout.objects.bulk_create(payouts)
    for payout, sale_ids in zip(payouts, claimed, strict=True):
        Sale.objects.filter(pk__in=sale_ids).update(payout=payout)
    return payouts


def claim(payout_id: int) -> Payout | None:
    """Take the payout for this worker, or return None if it is not available."""
    now = timezone.now()
    available = Q(status=Payout.Status.PENDING, next_attempt_at__lte=now) | Q(
        status=Payout.Status.SENDING, lease_until__lt=now
    )
    with transaction.atomic():
        payout = (
            Payout.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("seller")
            .filter(available, pk=payout_id)
            .first()
        )
        if payout is None:
            return None
        if payout.attempts >= settings.PAYOUT_MAX_ATTEMPTS:
            payout.status = Payout.Status.FAILED
            payout.failure = Payout.Failure.GAVE_UP
            payout.lease_until = None
            payout.save(update_fields=["status", "failure", "lease_until"])
            return None
        payout.status = Payout.Status.SENDING
        payout.attempts += 1
        payout.lease_until = now + timedelta(seconds=settings.PAYOUT_LEASE_SECONDS)
        payout.save(update_fields=["status", "attempts", "lease_until"])
    return payout


def _still_mine(payout: Payout):
    # The attempt number is a fencing token. A worker that stalled past its
    # lease and woke up finds the number has moved on, and writes nothing.
    return Payout.objects.filter(
        pk=payout.pk, status=Payout.Status.SENDING, attempts=payout.attempts
    )


def mark_sent(payout: Payout, reference: str) -> bool:
    return bool(
        _still_mine(payout).update(
            status=Payout.Status.SENT, bank_ref=reference, lease_until=None, last_error=""
        )
    )


def mark_rejected(payout: Payout, reason: str) -> bool:
    return bool(
        _still_mine(payout).update(
            status=Payout.Status.FAILED,
            failure=Payout.Failure.REJECTED,
            last_error=reason,
            lease_until=None,
        )
    )


def mark_retry(payout: Payout, reason: str) -> float | None:
    """Schedule another attempt. Returns the delay, or None if there will not be one."""
    if payout.attempts >= settings.PAYOUT_MAX_ATTEMPTS:
        _still_mine(payout).update(
            status=Payout.Status.FAILED,
            failure=Payout.Failure.GAVE_UP,
            last_error=reason,
            lease_until=None,
        )
        return None
    ceiling = settings.PAYOUT_BACKOFF_BASE_SECONDS * 2 ** (payout.attempts - 1)
    delay = random.uniform(0, min(BACKOFF_CAP_SECONDS, ceiling))
    updated = _still_mine(payout).update(
        status=Payout.Status.PENDING,
        next_attempt_at=timezone.now() + timedelta(seconds=delay),
        last_error=reason,
        lease_until=None,
    )
    return delay if updated else None


def reap(*, pending_grace_seconds: float = REAP_PENDING_GRACE_SECONDS) -> int:
    """Queue whatever should be moving and is not.

    Covers the two gaps a broker cannot: a message that never got sent because
    the broker was down at commit, and a worker that died holding a lease.
    """
    now = timezone.now()
    due = Payout.objects.filter(
        Q(
            status=Payout.Status.PENDING,
            next_attempt_at__lte=now - timedelta(seconds=pending_grace_seconds),
        )
        | Q(status=Payout.Status.SENDING, lease_until__lt=now)
    ).order_by("next_attempt_at")
    ids = list(due.values_list("pk", flat=True)[:REAP_BATCH])
    for payout_id in ids:
        queue.enqueue_send(payout_id)
    return len(ids)


def retry_failed(payout_id: int) -> Payout:
    with transaction.atomic():
        payout = Payout.objects.select_for_update().get(pk=payout_id)
        if payout.status != Payout.Status.FAILED:
            raise NotRetryable(payout.status)
        # A refusal is remembered under its key, so correcting the problem and
        # sending the same key would replay the refusal. After GAVE_UP the
        # opposite holds: the money may have moved, and only the old key is
        # safe.
        if payout.failure == Payout.Failure.REJECTED:
            payout.key_generation += 1
        payout.status = Payout.Status.PENDING
        payout.failure = Payout.Failure.NONE
        payout.last_error = ""
        payout.attempts = 0
        payout.next_attempt_at = timezone.now()
        payout.save()
        _queue_after_commit(payout.pk)
    return payout
