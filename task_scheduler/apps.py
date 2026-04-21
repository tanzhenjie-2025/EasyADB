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
        """应用启动时的初始化逻辑"""
        # 仅在 USE_CELERY=False 时启动后台调度线程
        if not settings.USE_CELERY:
            # 【关键修改】在新线程中延迟执行，避免阻塞 ready() 并打破导入链
            def _start_scheduler():
                try:
                    # 在线程内部导入，此时 Django 已完全初始化
                    from .tasks import start_scheduler_thread
                    start_scheduler_thread()
                except Exception as e:
                    logger.error(f"启动定时任务后台调度线程失败：{str(e)}", exc_info=True)

            # 启动线程
            threading.Thread(target=_start_scheduler, daemon=True).start()