from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, pagination, permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import Payout, Run


class NewestFirst(pagination.CursorPagination):
    ordering = "-pk"


class PayoutSerializer(serializers.ModelSerializer):
    seller = serializers.CharField(source="seller.name")

    class Meta:
        model = Payout
        fields = ["id", "run", "seller", "amount_cents", "status", "failure", "attempts",
                  "bank_ref"]


class RunSerializer(serializers.ModelSerializer):
    class Meta:
        model = Run
        fields = ["id", "period_end", "created_at"]
        read_only_fields = ["id", "created_at"]
        # The uniqueness of a period is handled by create_run, which answers a
        # repeat with the existing run. Left in, this validator would answer
        # it with a 400.
        extra_kwargs = {"period_end": {"validators": []}}

    def validate_period_end(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("a period cannot end in the future")
        return value


class RunList(generics.ListCreateAPIView):
    serializer_class = RunSerializer
    permission_classes = [permissions.IsAdminUser]
    pagination_class = NewestFirst
    queryset = Run.objects.all()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run, created = services.create_run(serializer.validated_data["period_end"], request.user)
        code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(RunSerializer(run).data, status=code)


class RunDetail(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request, pk):
        run = get_object_or_404(Run, pk=pk)
        statuses = Payout.Status.values
        counts = run.payouts.aggregate(
            total_cents=Sum("amount_cents", default=0),
            sent_cents=Sum("amount_cents", filter=Q(status="sent"), default=0),
            **{name: Count("pk", filter=Q(status=name)) for name in statuses},
        )
        open_count = counts["pending"] + counts["sending"]
        return Response({**RunSerializer(run).data, **counts, "finished": open_count == 0})


class PayoutList(generics.ListAPIView):
    serializer_class = PayoutSerializer
    pagination_class = NewestFirst

    def get_queryset(self):
        payouts = Payout.objects.select_related("seller")
        if not self.request.user.is_staff:
            payouts = payouts.filter(seller__user=self.request.user)
        wanted = self.request.query_params.get("status")
        return payouts.filter(status=wanted) if wanted else payouts


class PayoutRetry(APIView):
    permission_classes = [permissions.IsAdminUser]

    def post(self, request, pk):
        get_object_or_404(Payout, pk=pk)
        try:
            payout = services.retry_failed(pk)
        except services.NotRetryable as exc:
            return Response(
                {"detail": f"a {exc} payout cannot be retried"}, status=status.HTTP_409_CONFLICT
            )
        return Response(PayoutSerializer(payout).data, status=status.HTTP_202_ACCEPTED)
