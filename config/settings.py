import os
from datetime import timedelta

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-a-secret-at-least-32-bytes-long")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "rest_framework",
    "payouts",
]
MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "payout_runs"),
        "USER": os.environ.get("POSTGRES_USER", ""),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", ""),
        "PORT": os.environ.get("POSTGRES_PORT", ""),
    }
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.CursorPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}
SIMPLE_JWT = {"ACCESS_TOKEN_LIFETIME": timedelta(minutes=15)}

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": int(os.environ.get("BROKER_VISIBILITY_TIMEOUT", "3600")),
}
CELERY_BEAT_SCHEDULE = {
    "reap-payouts": {"task": "payouts.reap", "schedule": 60.0},
}

BANK_URL = os.environ.get("BANK_URL", "http://127.0.0.1:9100")
BANK_TIMEOUT_SECONDS = float(os.environ.get("BANK_TIMEOUT_SECONDS", "10"))
PAYOUT_LEASE_SECONDS = float(os.environ.get("PAYOUT_LEASE_SECONDS", "120"))
PAYOUT_MAX_ATTEMPTS = int(os.environ.get("PAYOUT_MAX_ATTEMPTS", "6"))
PAYOUT_BACKOFF_BASE_SECONDS = float(os.environ.get("PAYOUT_BACKOFF_BASE_SECONDS", "30"))
