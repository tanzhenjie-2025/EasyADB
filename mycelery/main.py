# mycelery/main.py 最终修正版
import os
import django
from celery import Celery

# 1. 设置 Django 环境变量
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'EasyADB.settings')

# 2. 【关键修复】只有在 Django 未初始化时才调用 setup()，防止重复 setup
if not django.apps.apps.ready:
    django.setup()

# 3. 创建 Celery 实例
app = Celery("EasyADB")

# 4. 从 Django 配置加载 Celery 设置
app.config_from_object('django.conf:settings', namespace='CELERY')

# 5. 自动发现任务（不传参数，Celery 会自动从 INSTALLED_APPS 查找）
app.autodiscover_tasks()

# 6. 定时任务配置（保留你原来的业务逻辑）
app.conf.beat_schedule = {
    'check-scheduled-tasks-every-minute': {
        'task': 'task_scheduler.check_and_execute_schedules',
        'schedule': 60.0,
    },
}

# 调试任务（保留）
@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
    print(f"Celery Broker: {app.conf.broker_url}")