import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import OrchestrationLog, StepExecutionLog


class OrchestrationLogConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.log_id = self.scope['url_route']['kwargs']['log_id']
        self.log_group_name = f'orchestration_log_{self.log_id}'

        await self.channel_layer.group_add(
            self.log_group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        # 修复：group_discard参数顺序（先group_name，后channel_name）
        await self.channel_layer.group_discard(
            self.channel_name,
            self.log_group_name
        )

    async def log_update(self, event):
        log_data = event['data']
        await self.send(text_data=json.dumps({
            'type': 'log_update',
            'data': log_data
        }))

    @database_sync_to_async
    def get_log_data(self):
        try:
            orch_log = OrchestrationLog.objects.get(id=self.log_id)
            step_logs = StepExecutionLog.objects.filter(
                orchestration_log=orch_log
            ).order_by('step__execution_order')  # 修正排序字段

            # 核心修改：返回step_log.id，匹配前端元素ID
            step_data = [{
                'step_log_id': step.id,  # 步骤日志ID（前端元素ID）
                'order': step.step.execution_order,  # 步骤序号
                'stdout': step.stdout,
                'stderr': step.stderr
            } for step in step_logs]

            return {
                'stdout': orch_log.stdout,
                'stderr': orch_log.stderr,
                'status': orch_log.exec_status,
                'step_data': step_data
            }
        except OrchestrationLog.DoesNotExist:
            return {'error': '日志不存在'}