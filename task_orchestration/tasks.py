from celery import shared_task
from .models import OrchestrationTask
from .views import ExecuteOrchestrationView

@shared_task(name="schedule_orchestration_task")
def schedule_orchestration_task(task_id):
    """定时执行编排任务"""
    try:
        task = OrchestrationTask.objects.get(id=task_id, status="active")
        executor = ExecuteOrchestrationView()
        executor.post(None, task_id)  # 调用执行视图
        return f"编排任务 {task.name} 已触发"
    except Exception as e:
        return f"触发失败: {str(e)}"