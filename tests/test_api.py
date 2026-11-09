import datetime as dt

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from payouts import services
from payouts.models import Payout
from payouts.tasks import send_payout
from tests.conftest import PERIOD, client_for, make_sale, make_seller

pytestmark = pytest.mark.django_db


def test_token_then_call(staff):
    client = APIClient()
    token = client.post("/api/token/", {"username": "ops", "password": "pw"}).data["access"]
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    assert client.get("/api/runs/").status_code == 200


def test_no_token():
    assert APIClient().get("/api/payouts/").status_code == 401


def test_seller_cannot_start_a_run():
    seller = make_seller()

    response = client_for(seller.user).post("/api/runs/", {"period_end": PERIOD})

    assert response.status_code == 403


def test_create_then_repeat(staff, queued):
    make_sale(make_seller(), 1000)
    client = client_for(staff)

    first = client.post("/api/runs/", {"period_end": PERIOD})
    second = client.post("/api/runs/", {"period_end": PERIOD})

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.data["id"] == second.data["id"]


def test_future_period_refused(staff):
    tomorrow = timezone.localdate() + dt.timedelta(days=1)

    response = client_for(staff).post("/api/runs/", {"period_end": tomorrow})

    assert response.status_code == 400


def test_sellers_see_only_their_own(staff, queued):
    mine, theirs = make_seller(), make_seller()
    make_sale(mine, 1000)
    make_sale(theirs, 2000)
    services.create_run(PERIOD, staff)

    seller_view = client_for(mine.user).get("/api/payouts/").data["results"]
    ops_view = client_for(staff).get("/api/payouts/").data["results"]

    assert [p["amount_cents"] for p in seller_view] == [1000]
    assert len(ops_view) == 2


def test_filter_by_status(staff, queued, bank):
    make_sale(make_seller(), 1000)
    make_sale(make_seller(), 2000)
    run, _ = services.create_run(PERIOD, staff)
    send_payout(run.payouts.order_by("pk").first().pk)

    rows = client_for(staff).get("/api/payouts/?status=sent").data["results"]

    assert [p["amount_cents"] for p in rows] == [1000]


def test_run_summary_counts_and_money(staff, queued, bank):
    make_sale(make_seller(), 1000)
    make_sale(make_seller(), 2000)
    run, _ = services.create_run(PERIOD, staff)
    send_payout(run.payouts.order_by("pk").first().pk)

    body = client_for(staff).get(f"/api/runs/{run.pk}/").data

    assert body["total_cents"] == 3000
    assert body["sent_cents"] == 1000
    assert (body["sent"], body["pending"], body["finished"]) == (1, 1, False)


def test_run_finished_when_nothing_is_open(staff, queued, bank):
    make_sale(make_seller(), 1000)
    bank.script = ["refuse"]
    run, _ = services.create_run(PERIOD, staff)
    send_payout(run.payouts.get().pk)

    body = client_for(staff).get(f"/api/runs/{run.pk}/").data

    assert (body["failed"], body["finished"]) == (1, True)


def test_payout_list_does_not_query_per_row(staff, queued, django_assert_num_queries):
    for _ in range(25):
        make_sale(make_seller(), 100)
    services.create_run(PERIOD, staff)
    client = client_for(staff)

    # auth user, then the page. The seller name must come from the join.
    with django_assert_num_queries(2):
        assert len(client.get("/api/payouts/").data["results"]) == 25


def test_retry_endpoint(staff, queued, bank):
    make_sale(make_seller(), 1000)
    bank.script = ["refuse"]
    run, _ = services.create_run(PERIOD, staff)
    payout = run.payouts.get()
    send_payout(payout.pk)

    response = client_for(staff).post(f"/api/payouts/{payout.pk}/retry/")

    assert response.status_code == 202
    payout.refresh_from_db()
    assert payout.status == Payout.Status.PENDING
    assert payout.key_generation == 2


def test_retry_a_paid_payout_conflicts(staff, queued, bank):
    make_sale(make_seller(), 1000)
    run, _ = services.create_run(PERIOD, staff)
    payout = run.payouts.get()
    send_payout(payout.pk)

    assert client_for(staff).post(f"/api/payouts/{payout.pk}/retry/").status_code == 409


def test_retry_missing_payout(staff):
    assert client_for(staff).post("/api/payouts/999999/retry/").status_code == 404
