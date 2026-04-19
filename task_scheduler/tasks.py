import logging
from celery import shared_task
import datetime
import json
from django.http import HttpRequest
from django.contrib.auth.models import AnonymousUser  # 新增：匿名用户
from .models import ScheduleTask, ScheduleExecutionLog
from adb_manager.models import ADBDevice
from task_orchestration.views import ExecuteOrchestrationAPIView

logger = logging.getLogger(__name__)

@shared_task(name="task_scheduler.check_and_execute_schedules")
def check_and_execute_schedules():
    """检查并执行到达执行时间的定时任务"""
    now = datetime.datetime.now()
    logger.info(f"开始检查定时任务，当前时间：{now}")

    active_schedules = ScheduleTask.objects.filter(is_active=True)
    executed_count = 0

    for schedule in active_schedules:
        try:
            # 检查是否到期（使用修复后的 is_due 逻辑）
            if schedule.is_due():
                logger.info(f"定时任务【{schedule.name}】到期，开始执行")

                # 创建执行日志
                log = ScheduleExecutionLog.objects.create(
                    schedule=schedule,
                    exec_status="running"
                )

                # 设备选择逻辑
                if schedule.device and schedule.device.is_active:
                    device = schedule.device
                else:
                    device = ADBDevice.objects.filter(is_active=True).first()

                if not device:
                    raise Exception("没有可用的在线设备")

                log.device = device
                log.save()
                logger.info(f"选中设备：{device.device_name}")

                # 修复：构建带匿名用户的模拟请求
                fake_request = HttpRequest()
                fake_request.method = 'POST'
                fake_request.user = AnonymousUser()  # 关键修复：防止视图取 user 报错

                # 执行编排任务
                execution_view = ExecuteOrchestrationAPIView()
                response = execution_view.post(fake_request, schedule.orchestration.id)

                # 解析响应
                response_data = json.loads(response.content)
                if response.status_code == 200 and response_data.get("status") == "success":
                    log.orchestration_log_id = response_data.get("log_id")
                    log.exec_status = "success"
                    logger.info(f"定时任务【{schedule.name}】执行成功")
                else:
                    log.exec_status = "failed"
                    log.error_msg = response_data.get("msg", "执行失败")
                    logger.error(f"定时任务【{schedule.name}】执行失败：{log.error_msg}")

                # 更新时间
                log.end_time = datetime.datetime.now()
                log.save()

                schedule.last_run_time = datetime.datetime.now()
                schedule.next_run_time = schedule.calculate_next_run_time()
                schedule.save()

                executed_count += 1

        except Exception as e:
            logger.error(f"执行定时任务【{schedule.name}】异常：{str(e)}", exc_info=True)
            if 'log' in locals():
                log.exec_status = "failed"
                log.error_msg = str(e)
                log.end_time = datetime.datetime.now()
                log.save()

    logger.info(f"定时任务检查完成，共执行 {executed_count} 个任务")
    return f"已检查 {active_schedules.count()} 个定时任务，执行 {executed_count} 个"