import os
from celery import Celery
import django
#创建celery实例对象
app = Celery("sms")
#把celery和django进行组合，识别和加载django的配置文件
os.environ.setdefault('DJANGO_SETTINGS_MODULE','CheckObjection.settings')
django.setup()
#通过app对象加载配置
app.config_from_object("mycelery.config")
#加载任务
#参数必须必须是一个列表，里面的每一个任务都是任务的路径名称
#app.autodiscover_tasks（["任务i","任务2"]）
app.autodiscover_tasks(["mycelery.sms", "mycelery.email", "task_orchestration"])
#启动celery的命令
# 强烈建议切换目录到mycelery根目录下启动
# celery -A mycelery.main worker --loglevel=info


# 自动发现任务
app.autodiscover_tasks()

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')