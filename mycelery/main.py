# mycelery/main.py 最终稳定版
import os
import django
from celery import Celery

# 1. 设置 Django 环境
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'EasyADB.settings')

# 2. 初始化 Django（必须这样写，才能同时支持 celery + 不报错）
django.setup()

# 3. 安全获取已安装应用
from django.conf import settings
installed_apps = settings.INSTALLED_APPS

# 4. 创建 Celery
app = Celery("EasyADB")

# 5. 加载配置
app.config_from_object('django.conf:settings', namespace='CELERY')

# 6. 自动发现任务
app.autodiscover_tasks([
    'adb_manager',
    'script_center',
    'task_scheduler',    # 强制加载
    'task_orchestration',
])

# 7. 定时任务
app.conf.beat_schedule = {
    'check-scheduled-tasks-every-minute': {
        'task': 'task_scheduler.check_and_execute_schedules',
        'schedule': 60.0,
    },
}

# 调试任务
@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
    print(f"Celery Broker: {app.conf.broker_url}")