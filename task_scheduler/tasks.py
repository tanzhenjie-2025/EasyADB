# task_scheduler/tasks.py
import logging
from celery import shared_task
import datetime
from .models import ScheduleTask, ScheduleExecutionLog
from adb_manager.models import ADBDevice
from task_orchestration.views import ExecuteOrchestrationAPIView
from django.http import HttpRequest

logger = logging.getLogger(__name__)

@shared_task(name="task_scheduler.check_and_execute_schedules")
def check_and_execute_schedules():
    """检查并执行到达执行时间的定时任务（基于Cron表达式，适配USE_TZ=False）"""
    now = datetime.datetime.now()
    logger.info(f"开始检查定时任务，当前时间：{now}")

    # 筛选启用的定时任务
    active_schedules = ScheduleTask.objects.filter(is_active=True)
    executed_count = 0

    for schedule in active_schedules:
        try:
            # 检查是否到了执行时间
            if schedule.is_due():
                logger.info(f"开始执行定时任务：{schedule.name}")

                # 创建执行日志
                log = ScheduleExecutionLog.objects.create(
                    schedule=schedule,
                    exec_status="running"
                )

                # 修复：设备判断逻辑（去掉device_status）
                if schedule.device and schedule.device.is_active:  # 关键修改1
                    device = schedule.device
                else:
                    # 修复：过滤条件只保留is_active
                    device = ADBDevice.objects.filter(
                        is_active=True  # 关键修改2：删除 device_status="online"
                    ).first()

                if not device:
                    raise Exception("没有可用的在线设备")

                log.device = device
                log.save()

                # 模拟请求执行编排任务
                fake_request = HttpRequest()
                fake_request.method = 'POST'

                # 执行编排任务
                execution_view = ExecuteOrchestrationAPIView()
                response = execution_view.post(fake_request, schedule.orchestration.id)

                if response.status_code == 200 and response.json().get("status") == "success":
                    log.orchestration_log_id = response.json().get("log_id")
                    log.exec_status = "success"
                    logger.info(f"定时任务{schedule.name}执行成功")
                else:
                    log.exec_status = "failed"
                    log.error_msg = response.json().get("msg", "执行失败")
                    logger.error(f"定时任务{schedule.name}执行失败：{log.error_msg}")

                # 更新日志和任务信息
                log.end_time = datetime.datetime.now()
                log.save()

                schedule.last_run_time = datetime.datetime.now()
                schedule.next_run_time = schedule.calculate_next_run_time()
                schedule.save()

                executed_count += 1

        except Exception as e:
            logger.error(f"执行定时任务{schedule.name}异常：{str(e)}")
            if 'log' in locals():
                log.exec_status = "failed"
                log.error_msg = str(e)
                log.end_time = datetime.datetime.now()
                log.save()

    logger.info(f"定时任务检查完成，共执行 {executed_count} 个任务")
    return f"已检查 {active_schedules.count()} 个定时任务，执行 {executed_count} 个"