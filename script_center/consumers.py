# script_center/consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import TaskExecutionLog

class ScriptLogConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.log_id = self.scope['url_route']['kwargs']['log_id']
        self.log_group_name = f'script_log_{self.log_id}'

        # 加入日志分组（正确）
        await self.channel_layer.group_add(
            self.log_group_name,
            self.channel_name
        )
        await self.accept()  # 必须先accept，否则连接失败
        # 连接成功后，主动推送一次最新日志
        log_data = await self.get_log_data()
        await self.send_log(log_data)

    async def disconnect(self, close_code):
        # 修复：group_discard参数顺序（正确是 group_name, channel_name）
        await self.channel_layer.group_discard(
            self.log_group_name,
            self.channel_name
        )

    # 接收分组消息并推送给前端（原有逻辑保留）
    async def log_update(self, event):
        log_data = event['data']
        await self.send_log(log_data)

    # 封装发送日志的函数（复用）
    async def send_log(self, log_data):
        await self.send(text_data=json.dumps({
            'type': 'log_update',
            'data': log_data
        }))

    # 异步查询日志（原有逻辑保留）
    @database_sync_to_async
    def get_log_data(self):
        try:
            log = TaskExecutionLog.objects.get(id=self.log_id)
            return {
                'stdout': log.stdout,
                'stderr': log.stderr,
                'status': log.exec_status
            }
        except TaskExecutionLog.DoesNotExist:
            return {'error': '日志不存在'}