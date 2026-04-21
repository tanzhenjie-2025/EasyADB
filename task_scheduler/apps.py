# task_scheduler/apps.py
from django.apps import AppConfig
from django.conf import settings
import logging
import threading

logger = logging.getLogger(__name__)

class TaskSchedulerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'task_scheduler'

    def ready(self):
        if not settings.USE_CELERY:
            # 在线程里启动，避免阻塞Django初始化
            def start():
                try:
                    from .tasks import start_scheduler_thread
                    start_scheduler_thread()
                except Exception as e:
                    logger.error(f"启动调度线程失败: {e}", exc_info=True)

            threading.Thread(target=start, daemon=True).start()