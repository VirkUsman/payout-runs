import datetime as dt
import threading

import pytest
from django.db import connections
from django.utils import timezone

from payouts import services
from payouts.models import Payout
from payouts.tasks import send_payout
from tests.conftest import PERIOD, make_sale, make_seller

pytestmark = pytest.mark.django_db


@pytest.fixture
def payout(staff, queued, bank):
    make_sale(make_seller(), 4200)
    run, _ = services.create_run(PERIOD, staff)
    queued.clear()
    return run.payouts.get()


def expire_lease(payout):
    Payout.objects.filter(pk=payout.pk).update(
        lease_until=timezone.now() - dt.timedelta(seconds=1)
    )


def make_due(payout):
    Payout.objects.filter(pk=payout.pk).update(next_attempt_at=timezone.now())


def test_paid(payout, bank):
    send_payout(payout.pk)

    payout.refresh_from_db()
    assert payout.status == Payout.Status.SENT
    assert payout.bank_ref == next(iter(bank.applied.values()))["reference"]
    assert bank.total_cents == 4200


def test_delivered_again_after_success(payout, bank):
    send_payout(payout.pk)
    send_payout(payout.pk)

    assert len(bank.keys_seen) == 1


def test_503_comes_back_later(payout, bank, queued):
    bank.script = ["unavailable"]

    send_payout(payout.pk)

    payout.refresh_from_db()
    assert payout.status == Payout.Status.PENDING
    assert payout.next_attempt_at > timezone.now() - dt.timedelta(seconds=1)
    assert queued and queued[0][0] == payout.pk and queued[0][1] is not None
    assert bank.transfers == 0


def test_answer_lost_after_the_bank_applied_it(payout, bank, queued):
    """The case the whole design is for. The money moved and we were not told."""
    bank.script = ["drop_after_applying"]

    send_payout(payout.pk)
    payout.refresh_from_db()
    assert payout.status == Payout.Status.PENDING
    assert bank.transfers == 1

    make_due(payout)
    send_payout(payout.pk)

    payout.refresh_from_db()
    assert payout.status == Payout.Status.SENT
    assert bank.transfers == 1
    assert bank.total_cents == 4200
    assert len(set(bank.keys_seen)) == 1 and len(bank.keys_seen) == 2


def test_refusal_stops_at_once(payout, bank, queued):
    bank.script = ["refuse"]

    send_payout(payout.pk)

    payout.refresh_from_db()
    assert (payout.status, payout.failure) == (Payout.Status.FAILED, Payout.Failure.REJECTED)
    assert queued == []
    assert "422" in payout.last_error


def test_key_in_progress_is_not_a_refusal(payout, bank):
    bank.script = ["busy"]

    send_payout(payout.pk)

    payout.refresh_from_db()
    assert payout.status == Payout.Status.PENDING


def test_out_of_attempts(payout, bank, queued, settings):
    settings.PAYOUT_MAX_ATTEMPTS = 3
    bank.script = ["unavailable"] * 3

    for _ in range(3):
        make_due(payout)
        send_payout(payout.pk)

    payout.refresh_from_db()
    assert (payout.status, payout.failure) == (Payout.Status.FAILED, Payout.Failure.GAVE_UP)
    assert payout.attempts == 3
    assert len(queued) == 2


def test_early_redelivery_during_backoff_does_nothing(payout, bank):
    bank.script = ["unavailable"]
    send_payout(payout.pk)
    Payout.objects.filter(pk=payout.pk).update(
        next_attempt_at=timezone.now() + dt.timedelta(hours=1)
    )

    send_payout(payout.pk)

    assert len(bank.keys_seen) == 1


def test_lease_blocks_a_second_worker(payout):
    assert services.claim(payout.pk) is not None
    assert services.claim(payout.pk) is None


def test_expired_lease_can_be_taken(payout):
    first = services.claim(payout.pk)
    expire_lease(payout)

    second = services.claim(payout.pk)

    assert second is not None
    assert second.attempts == first.attempts + 1


def test_worker_that_wakes_up_late_is_fenced(payout):
    stalled = services.claim(payout.pk)
    expire_lease(payout)
    current = services.claim(payout.pk)

    assert services.mark_sent(stalled, "from-the-stalled-worker") is False
    assert services.mark_sent(current, "from-the-current-worker") is True

    payout.refresh_from_db()
    assert payout.bank_ref == "from-the-current-worker"


def test_late_retry_cannot_undo_a_success(payout):
    stalled = services.claim(payout.pk)
    expire_lease(payout)
    current = services.claim(payout.pk)
    services.mark_sent(current, "ref")

    assert services.mark_retry(stalled, "timed out") is None

    payout.refresh_from_db()
    assert payout.status == Payout.Status.SENT


@pytest.mark.django_db(transaction=True)
def test_eight_workers_one_transfer(staff, queued, bank):
    make_sale(make_seller(), 900)
    run, _ = services.create_run(PERIOD, staff)
    payout = run.payouts.get()
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            send_payout(payout.pk)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(bank.keys_seen) == 1
    assert bank.transfers == 1


def test_crash_loop_ends_in_gave_up(payout, settings):
    settings.PAYOUT_MAX_ATTEMPTS = 2
    for _ in range(2):
        assert services.claim(payout.pk) is not None
        expire_lease(payout)

    assert services.claim(payout.pk) is None

    payout.refresh_from_db()
    assert (payout.status, payout.failure) == (Payout.Status.FAILED, Payout.Failure.GAVE_UP)


def test_reaper_takes_expired_leases_and_stale_pending_only(staff, queued, bank):
    for _ in range(3):
        make_sale(make_seller(), 100)
    run, _ = services.create_run(PERIOD, staff)
    queued.clear()
    fresh, held, crashed = run.payouts.order_by("pk")
    services.claim(held.pk)
    services.claim(crashed.pk)
    expire_lease(crashed)

    assert services.reap() == 1

    assert [pk for pk, _ in queued] == [crashed.pk]


def test_retry_after_refusal_uses_a_new_key(payout, bank, queued):
    bank.script = ["refuse"]
    send_payout(payout.pk)

    payout = services.retry_failed(payout.pk)
    send_payout(payout.pk)

    payout.refresh_from_db()
    assert payout.status == Payout.Status.SENT
    assert len(set(bank.keys_seen)) == 2
    assert bank.transfers == 1


def test_retry_after_giving_up_keeps_the_key(payout, bank, queued, settings):
    settings.PAYOUT_MAX_ATTEMPTS = 1
    bank.script = ["drop_after_applying"]
    send_payout(payout.pk)
    payout.refresh_from_db()
    assert payout.failure == Payout.Failure.GAVE_UP

    payout = services.retry_failed(payout.pk)
    send_payout(payout.pk)

    payout.refresh_from_db()
    assert payout.status == Payout.Status.SENT
    assert bank.transfers == 1
    assert len(set(bank.keys_seen)) == 1


def test_only_failed_payouts_can_be_retried(payout):
    with pytest.raises(services.NotRetryable):
        services.retry_failed(payout.pk)
