from django.apps import AppConfig


class FastPayConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'fastpay'
    verbose_name = 'FastPay'

    def ready(self):
        from . import logs

        logs.configure()
