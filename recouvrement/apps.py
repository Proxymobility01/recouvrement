from django.apps import AppConfig


class RecouvrementConfig(AppConfig):
    name = 'recouvrement'

    def ready(self):
        import recouvrement.signals
