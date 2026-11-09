from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView

from .api import PayoutList, PayoutRetry, RunDetail, RunList

urlpatterns = [
    path("token/", TokenObtainPairView.as_view()),
    path("runs/", RunList.as_view()),
    path("runs/<int:pk>/", RunDetail.as_view()),
    path("payouts/", PayoutList.as_view()),
    path("payouts/<int:pk>/retry/", PayoutRetry.as_view()),
]
