import json
from django.db.models.signals import post_save
from django.dispatch import receiver
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .models import TaskExecutionLog

@receiver(post_save, sender=TaskExecutionLog)
def notify_log_update(sender, instance, **kwargs):
    """当日志更新时，通过WebSocket推送更新"""
    channel_layer = get_channel_layer()
    # 发送到对应的日志分组
    async_to_sync(channel_layer.group_send)(
        f'script_log_{instance.id}',
        {
            'type': 'log_update',  # 对应consumer中的log_update方法
            'data': {
                'stdout': instance.stdout,
                'stderr': instance.stderr,
                'status': instance.exec_status
            }
        }
    )