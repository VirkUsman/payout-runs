from payouts.checks import lease_outlives_the_bank_call


def test_lease_too_short(settings):
    settings.BANK_TIMEOUT_SECONDS = 10
    settings.PAYOUT_LEASE_SECONDS = 40

    errors = lease_outlives_the_bank_call(None)

    assert [e.id for e in errors] == ["payouts.E001"]


def test_defaults_pass(settings):
    settings.BANK_TIMEOUT_SECONDS = 10
    settings.PAYOUT_LEASE_SECONDS = 120

    assert lease_outlives_the_bank_call(None) == []
