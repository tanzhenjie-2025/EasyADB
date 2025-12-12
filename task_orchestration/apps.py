from django.apps import AppConfig

class TaskOrchestrationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'task_orchestration'

    def ready(self):
        import task_orchestration.signals  # 注册信号