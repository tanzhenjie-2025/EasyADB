import json
from django.db.models.signals import post_save
from django.dispatch import receiver
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .models import OrchestrationLog, StepExecutionLog


@receiver(post_save, sender=OrchestrationLog)
def notify_orchestration_update(sender, instance, **kwargs):
    """编排日志更新时，通过WebSocket推送最新日志"""
    channel_layer = get_channel_layer()

    # 核心修正1：排序字段改为 TaskStep 的 execution_order（关联查询）
    step_logs = StepExecutionLog.objects.filter(
        orchestration_log=instance
    ).order_by('step__execution_order')  # 替换原错误的 order_by('order')

    # 核心修正2：step_data 中增加 step_log_id（匹配前端元素ID）
    step_data = [{
        'step_log_id': step.id,  # 步骤日志ID（前端元素ID）
        'order': step.step.execution_order,  # StepExecutionLog -> TaskStep -> execution_order
        'stdout': step.stdout,
        'stderr': step.stderr
    } for step in step_logs]

    # 推送日志到 WebSocket 分组
    async_to_sync(channel_layer.group_send)(
        f'orchestration_log_{instance.id}',
        {
            'type': 'log_update',
            'data': {
                'stdout': instance.stdout,
                'stderr': instance.stderr,
                'status': instance.exec_status,
                'step_data': step_data
            }
        }
    )


@receiver(post_save, sender=StepExecutionLog)
def notify_step_update(sender, instance, **kwargs):
    """步骤日志更新时，触发编排日志的推送（保证步骤日志实时更新）"""
    notify_orchestration_update(OrchestrationLog, instance.orchestration_log)