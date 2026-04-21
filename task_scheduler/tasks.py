import logging
import datetime
import json
import threading
import time
from celery import shared_task
from django.http import HttpRequest
from django.contrib.auth.models import AnonymousUser
from django.conf import settings

from .models import ScheduleTask, ScheduleExecutionLog
from adb_manager.models import ADBDevice
from task_orchestration.views import ExecuteOrchestrationAPIView

logger = logging.getLogger(__name__)

# ====================== 1. 抽离：单个定时任务执行核心逻辑 ======================
def _execute_single_schedule(schedule):
    """执行单个定时任务的核心逻辑（被 Celery 和后台线程共用）"""
    try:
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

        # 构建带匿名用户的模拟请求
        fake_request = HttpRequest()
        fake_request.method = 'POST'
        fake_request.user = AnonymousUser()

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

        return True

    except Exception as e:
        logger.error(f"执行定时任务【{schedule.name}】异常：{str(e)}", exc_info=True)
        if 'log' in locals():
            log.exec_status = "failed"
            log.error_msg = str(e)
            log.end_time = datetime.datetime.now()
            log.save()
        return False

# ====================== 2. 抽离：检查并执行所有到期任务的核心逻辑 ======================
def _check_and_execute_core():
    """检查并执行所有到期定时任务的核心逻辑（被 Celery 和后台线程共用）"""
    now = datetime.datetime.now()
    logger.info(f"开始检查定时任务，当前时间：{now}")

    active_schedules = ScheduleTask.objects.filter(is_active=True)
    executed_count = 0

    for schedule in active_schedules:
        try:
            if schedule.is_due():
                if _execute_single_schedule(schedule):
                    executed_count += 1
        except Exception as e:
            logger.error(f"处理定时任务【{schedule.name}】异常：{str(e)}", exc_info=True)

    logger.info(f"定时任务检查完成，共执行 {executed_count} 个任务")
    return f"已检查 {active_schedules.count()} 个定时任务，执行 {executed_count} 个"

# ====================== 3. Celery 异步任务入口（保持原有功能） ======================
@shared_task(name="task_scheduler.check_and_execute_schedules")
def check_and_execute_schedules():
    """Celery 定时触发入口（由 Celery Beat 调用）"""
    return _check_and_execute_core()

# ====================== 4. 后台线程同步入口（优雅降级模式） ======================
_scheduler_thread = None  # 全局线程实例，防止重复启动

def _scheduler_worker():
    """后台调度线程工作函数：每分钟检查一次到期任务"""
    logger.info("定时任务后台调度线程已启动（优雅降级模式）")
    while True:
        try:
            _check_and_execute_core()
        except Exception as e:
            logger.error(f"后台调度线程异常：{str(e)}", exc_info=True)
        time.sleep(60)  # 检查间隔：60秒（可根据需要调整）

def start_scheduler_thread():
    """启动后台调度线程（仅在 USE_CELERY=False 时调用）"""
    global _scheduler_thread
    if _scheduler_thread is None or not _scheduler_thread.is_alive():
        _scheduler_thread = threading.Thread(target=_scheduler_worker, daemon=True)
        _scheduler_thread.start()
    else:
        logger.info("定时任务后台调度线程已在运行中")