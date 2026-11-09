from django.conf import settings
from django.core import checks


@checks.register()
def lease_outlives_the_bank_call(app_configs, **kwargs):
    # httpx applies the timeout to connect, write, read and pool separately, so
    # one call can legitimately take four of them. A lease shorter than that
    # hands a payout to a second worker while the first is still on the phone
    # to the bank. Harmless for money, because the key is the same, but it
    # burns attempts for nothing.
    worst_case = settings.BANK_TIMEOUT_SECONDS * 4
    if settings.PAYOUT_LEASE_SECONDS <= worst_case:
        return [
            checks.Error(
                f"PAYOUT_LEASE_SECONDS must be above {worst_case:g}, "
                "four times BANK_TIMEOUT_SECONDS.",
                id="payouts.E001",
            )
        ]
    return []
