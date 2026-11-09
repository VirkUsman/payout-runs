import datetime as dt
import itertools

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from payouts import queue
from payouts.models import Sale, Seller
from tests.fake_bank import FakeBank

PERIOD = dt.date(2026, 10, 31)
_names = itertools.count(1)


@pytest.fixture
def bank(settings):
    fake = FakeBank()
    settings.BANK_URL = fake.url
    settings.BANK_TIMEOUT_SECONDS = 0.5
    settings.PAYOUT_LEASE_SECONDS = 120
    yield fake
    fake.close()


@pytest.fixture
def queued(monkeypatch):
    """Replace the broker with a list. The end to end tests use the real one."""
    sent: list[tuple[int, float | None]] = []
    monkeypatch.setattr(queue, "enqueue_send", lambda pk, countdown=None: sent.append((pk, countdown)))
    return sent


@pytest.fixture
def staff(db):
    return get_user_model().objects.create_user("ops", password="pw", is_staff=True)


def make_seller(name=None, iban="DE89370400440532013000") -> Seller:
    name = name or f"seller{next(_names)}"
    user = get_user_model().objects.create_user(name, password="pw")
    return Seller.objects.create(user=user, name=name, iban=iban)


def make_sale(seller, cents, day=PERIOD) -> Sale:
    return Sale.objects.create(seller=seller, amount_cents=cents, earned_on=day)


def client_for(user) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
    return client
