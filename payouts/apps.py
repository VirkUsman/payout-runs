from django.apps import AppConfig


class PayoutsConfig(AppConfig):
    name = "payouts"

    def ready(self):
        from . import checks  # noqa: F401
