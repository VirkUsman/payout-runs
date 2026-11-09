import datetime as dt
import threading

import pytest
from django.db import connections, transaction

from payouts import queue, services
from payouts.models import Payout, Run, Sale
from tests.conftest import PERIOD, make_sale, make_seller

pytestmark = pytest.mark.django_db


def test_one_payout_per_seller_summing_their_lines(staff, queued):
    a, b = make_seller(), make_seller()
    make_sale(a, 1000)
    make_sale(a, 2500, PERIOD - dt.timedelta(days=4))
    make_sale(b, 700)

    run, created = services.create_run(PERIOD, staff)

    assert created
    assert {p.seller_id: p.amount_cents for p in run.payouts.all()} == {a.pk: 3500, b.pk: 700}


def test_lines_after_the_period_wait(staff, queued):
    seller = make_seller()
    make_sale(seller, 1000)
    later = make_sale(seller, 999, PERIOD + dt.timedelta(days=1))

    run, _ = services.create_run(PERIOD, staff)

    assert run.payouts.get().amount_cents == 1000
    later.refresh_from_db()
    assert later.payout is None


def test_refunds_net_off(staff, queued):
    seller = make_seller()
    make_sale(seller, 5000)
    make_sale(seller, -1200)

    run, _ = services.create_run(PERIOD, staff)

    assert run.payouts.get().amount_cents == 3800


def test_seller_in_the_red_carries_over(staff, queued):
    seller = make_seller()
    make_sale(seller, 500)
    make_sale(seller, -900)

    run, _ = services.create_run(PERIOD, staff)
    assert not run.payouts.exists()
    assert Sale.objects.filter(payout__isnull=True).count() == 2

    make_sale(seller, 1000, PERIOD + dt.timedelta(days=30))
    next_run, _ = services.create_run(PERIOD + dt.timedelta(days=30), staff)
    assert next_run.payouts.get().amount_cents == 600


def test_same_period_twice_is_the_same_run(staff, queued):
    make_sale(make_seller(), 1000)

    first, created_first = services.create_run(PERIOD, staff)
    queued.clear()
    second, created_second = services.create_run(PERIOD, staff)

    assert (created_first, created_second) == (True, False)
    assert first.pk == second.pk
    assert queued == []
    assert Payout.objects.count() == 1


def _race(worker, count):
    barrier = threading.Barrier(count)
    results, errors = [], []

    def body(index):
        try:
            barrier.wait()
            results.append(worker(index))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=body, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    return results


@pytest.mark.django_db(transaction=True)
def test_ten_people_pressing_create_at_once(staff, queued):
    for _ in range(5):
        make_sale(make_seller(), 1000)

    results = _race(lambda i: services.create_run(PERIOD, staff)[1], 10)

    assert results.count(True) == 1
    assert Run.objects.count() == 1
    assert Payout.objects.count() == 5
    assert len(queued) == 5


@pytest.mark.django_db(transaction=True)
def test_overlapping_periods_never_pay_a_line_twice(staff, queued):
    sellers = [make_seller() for _ in range(10)]
    for seller in sellers:
        for _ in range(20):
            make_sale(seller, 100, PERIOD - dt.timedelta(days=5))

    periods = [PERIOD, PERIOD + dt.timedelta(days=1), PERIOD + dt.timedelta(days=2)]
    _race(lambda i: services.create_run(periods[i], staff), 3)

    assert sum(p.amount_cents for p in Payout.objects.all()) == 10 * 20 * 100
    assert not Sale.objects.filter(payout__isnull=True).exists()
    assert len(queued) == Payout.objects.count() == 10


@pytest.mark.django_db(transaction=True)
def test_a_rolled_back_run_queues_nothing(staff, queued):
    make_sale(make_seller(), 1000)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with transaction.atomic():
            services.create_run(PERIOD, staff)
            assert queued == []
            raise Boom

    assert queued == []
    assert not Run.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_messages_go_only_after_the_commit(staff, queued):
    make_sale(make_seller(), 1000)

    with transaction.atomic():
        services.create_run(PERIOD, staff)
        assert queued == []

    assert len(queued) == 1


@pytest.mark.django_db(transaction=True)
def test_broker_down_at_commit_keeps_the_run_and_the_reaper_finds_it(staff, monkeypatch):
    make_sale(make_seller(), 1000)

    def broker_down(pk, countdown=None):
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(queue, "enqueue_send", broker_down)
    run, created = services.create_run(PERIOD, staff)
    assert created
    assert run.payouts.get().status == Payout.Status.PENDING

    picked_up = []
    monkeypatch.setattr(queue, "enqueue_send", lambda pk, countdown=None: picked_up.append(pk))
    assert services.reap(pending_grace_seconds=0) == 1
    assert picked_up == [run.payouts.get().pk]
