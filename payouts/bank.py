import httpx
from django.conf import settings


class BankRejected(Exception):
    """The bank refused the transfer. No money moved."""


class BankUnavailable(Exception):
    """No usable answer. The money may or may not have moved."""


# 408, 425 and 429 are "not now", and 409 is the bank saying the same key is
# still being processed. None of them is a refusal.
_RETRYABLE_4XX = {408, 409, 425, 429}


def transfer(*, idempotency_key: str, iban: str, amount_cents: int) -> str:
    try:
        response = httpx.post(
            f"{settings.BANK_URL}/transfers",
            json={"iban": iban, "amount_cents": amount_cents, "currency": "EUR"},
            headers={"Idempotency-Key": idempotency_key},
            timeout=settings.BANK_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise BankUnavailable(f"{type(exc).__name__}: {exc}") from exc

    if response.is_success:
        return response.json()["reference"]
    if response.is_client_error and response.status_code not in _RETRYABLE_4XX:
        raise BankRejected(f"{response.status_code}: {response.text[:200]}")
    raise BankUnavailable(f"{response.status_code}: {response.text[:200]}")
