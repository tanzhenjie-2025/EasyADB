# mycelery/main.py 最终版
import os
from celery import Celery
import django

# 1. 强制设置Django环境变量（优先级最高）
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'EasyADB.settings')

# 2. 初始化Django（必须在Celery实例创建前）
django.setup()

# 3. 创建Celery实例（名称和项目一致）
app = Celery("EasyADB")

# 4. 核心：从Django的settings.py读取Celery配置（覆盖所有其他配置）
app.config_from_object('django.conf:settings', namespace='CELERY')

# 5. 自动发现所有已安装APP中的tasks.py
app.autodiscover_tasks(["mycelery.sms","mycelery.email","task_orchestration","script_center","task_scheduler"])

app.conf.beat_schedule = {
    'check-scheduled-tasks-every-minute': {
        'task': 'task_scheduler.check_and_execute_schedules',
        'schedule': 60.0,  # 每分钟执行一次检查
    },
}
# 调试任务（验证Celery是否正常）
@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
    print(f"Celery Broker: {app.conf.broker_url}")  # 打印当前broker，验证是否为Redis