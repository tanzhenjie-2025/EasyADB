# mycelery/main.py 最终修正版
import os
from celery import Celery
import django
from django.apps import apps

# 1. 【第一步】先设置Django环境变量
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'EasyADB.settings')

# 2. 【第二步】初始化Django（必须先做这个！）
django.setup()

# 3. 【第三步】现在才能调用Django的apps API（修复位置！）
installed_apps = [app.name for app in apps.get_app_configs()]

# 4. 创建Celery实例
app = Celery("EasyADB")

# 5. 从Django配置加载Celery设置
app.config_from_object('django.conf:settings', namespace='CELERY')

# 6. 自动发现任务
app.autodiscover_tasks(installed_apps)

# 7. 定时任务配置
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