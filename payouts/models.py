from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Seller(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    iban = models.CharField(max_length=34)

    def __str__(self):
        return self.name


class Sale(models.Model):
    """One line of what a seller is owed. Refunds are negative lines."""

    seller = models.ForeignKey(Seller, on_delete=models.PROTECT, related_name="sales")
    amount_cents = models.BigIntegerField()
    earned_on = models.DateField()
    payout = models.ForeignKey(
        "Payout", null=True, blank=True, on_delete=models.PROTECT, related_name="sales"
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["earned_on"], condition=Q(payout__isnull=True), name="sale_unpaid_idx"
            ),
        ]


class Run(models.Model):
    period_end = models.DateField(unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)


class Payout(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        SENDING = "sending"
        SENT = "sent"
        FAILED = "failed"

    class Failure(models.TextChoices):
        NONE = ""
        REJECTED = "rejected"  # the bank said no, so no money moved
        GAVE_UP = "gave_up"  # out of attempts, and the money may have moved

    run = models.ForeignKey(Run, on_delete=models.PROTECT, related_name="payouts")
    seller = models.ForeignKey(Seller, on_delete=models.PROTECT, related_name="payouts")
    amount_cents = models.BigIntegerField()

    status = models.CharField(max_length=10, choices=Status, default=Status.PENDING)
    failure = models.CharField(max_length=10, choices=Failure, default=Failure.NONE, blank=True)
    last_error = models.TextField(blank=True)

    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True, blank=True)

    key_generation = models.PositiveIntegerField(default=1)
    bank_ref = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "seller"], name="one_payout_per_seller_per_run"),
            models.CheckConstraint(condition=Q(amount_cents__gt=0), name="payout_amount_positive"),
        ]
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at"],
                condition=Q(status__in=["pending", "sending"]),
                name="payout_open_idx",
            ),
        ]

    @property
    def idempotency_key(self) -> str:
        return f"payout-{self.pk}-{self.key_generation}"
