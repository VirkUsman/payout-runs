"""A real Celery worker process, a real Redis broker, and a real SIGKILL."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from django.db import connection
from django.utils import timezone

from payouts import services
from payouts.models import Payout
from tests.conftest import PERIOD, client_for, make_sale, make_seller

pytestmark = pytest.mark.django_db(transaction=True)

ROOT = Path(__file__).resolve().parent.parent


def start_worker(bank_url: str, name: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "POSTGRES_DB": connection.settings_dict["NAME"],
        "BANK_URL": bank_url,
        "BANK_TIMEOUT_SECONDS": "5",
        "PAYOUT_LEASE_SECONDS": "30",
    }
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "config",
            "worker",
            "--pool=solo",
            "--loglevel=WARNING",
            "-n",
            f"{name}@%h",
            "--without-gossip",
            "--without-mingle",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for(condition, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def stop(worker: subprocess.Popen):
    if worker.poll() is None:
        worker.terminate()
        try:
            worker.wait(10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()


@pytest.fixture
def broker():
    from config.celery import app

    app.control.purge()
    return app


def test_run_over_http_is_paid_by_a_real_worker(staff, bank, broker):
    for _ in range(20):
        make_sale(make_seller(), 500)
    worker = start_worker(bank.url, "happy")
    try:
        response = client_for(staff).post("/api/runs/", {"period_end": PERIOD})
        assert response.status_code == 201
        run_id = response.data["id"]

        assert wait_for(lambda: bank.transfers == 20)
        assert wait_for(lambda: Payout.objects.filter(run_id=run_id, status="sent").count() == 20)
    finally:
        stop(worker)

    assert bank.total_cents == 20 * 500


def test_worker_killed_while_the_bank_is_answering(staff, bank, broker):
    """SIGKILL after the bank applied the money and before the row says so."""
    make_sale(make_seller(), 7500)
    bank.script = ["hold_after_applying"]
    first = start_worker(bank.url, "doomed")
    try:
        client_for(staff).post("/api/runs/", {"period_end": PERIOD})
        assert bank.arrived.wait(30), "the worker never reached the bank"
        first.send_signal(signal.SIGKILL)
        first.wait(10)
    finally:
        stop(first)

    payout = Payout.objects.get()
    assert payout.status == Payout.Status.SENDING
    assert bank.transfers == 1

    # The lease has to run out before anyone else may touch it. Pretend it did.
    Payout.objects.filter(pk=payout.pk).update(lease_until=timezone.now())

    second = start_worker(bank.url, "survivor")
    try:
        assert services.reap() == 1
        assert wait_for(lambda: Payout.objects.get(pk=payout.pk).status == Payout.Status.SENT)
    finally:
        stop(second)

    payout.refresh_from_db()
    assert bank.transfers == 1
    assert bank.total_cents == 7500
    assert payout.attempts == 2
    assert bank.keys_seen == [payout.idempotency_key, payout.idempotency_key]
