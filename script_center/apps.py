from django.apps import AppConfig

class ScriptCenterConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'script_center'

    def ready(self):
        import script_center.signals  # 注册信号