# task_scheduler/views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.urls import reverse
from django.http import JsonResponse
from django.utils import timezone
from django.contrib import messages
import logging
import datetime
from adb_manager.models import ADBDevice
from .models import ScheduleTask, ScheduleExecutionLog
from .forms import ScheduleTaskForm
from task_orchestration.views import ExecuteOrchestrationAPIView

logger = logging.getLogger(__name__)


class ScheduleTaskListView(View):
    """定时任务列表页"""

    def get(self, request):
        schedules = ScheduleTask.objects.all()
        return render(request, "task_scheduler/schedule_list.html", {
            "schedules": schedules,
            "page_title": "定时任务管理"
        })


class ScheduleTaskCreateView(View):
    """创建定时任务"""

    def get(self, request):
        form = ScheduleTaskForm()
        return render(request, "task_scheduler/schedule_form.html", {
            "form": form,
            "page_title": "创建定时任务"
        })

    def post(self, request):
        form = ScheduleTaskForm(request.POST)
        if form.is_valid():
            schedule = form.save()
            messages.success(request, "定时任务创建成功")
            return redirect(reverse("task_scheduler:list"))
        return render(request, "task_scheduler/schedule_form.html", {
            "form": form,
            "page_title": "创建定时任务"
        })


class ScheduleTaskEditView(View):
    """编辑定时任务"""

    def get(self, request, schedule_id):
        schedule = get_object_or_404(ScheduleTask, id=schedule_id)
        form = ScheduleTaskForm(instance=schedule)
        return render(request, "task_scheduler/schedule_form.html", {
            "form": form,
            "schedule": schedule,
            "page_title": f"编辑定时任务：{schedule.name}"
        })

    def post(self, request, schedule_id):
        schedule = get_object_or_404(ScheduleTask, id=schedule_id)
        form = ScheduleTaskForm(request.POST, instance=schedule)
        if form.is_valid():
            schedule = form.save()
            messages.success(request, "定时任务更新成功")
            return redirect(reverse("task_scheduler:list"))
        return render(request, "task_scheduler/schedule_form.html", {
            "form": form,
            "schedule": schedule,
            "page_title": f"编辑定时任务：{schedule.name}"
        })


class ScheduleTaskDetailView(View):
    """定时任务详情，查看执行日志"""

    def get(self, request, schedule_id):
        schedule = get_object_or_404(ScheduleTask, id=schedule_id)
        logs = schedule.logs.all()[:100]
        return render(request, "task_scheduler/schedule_detail.html", {
            "schedule": schedule,
            "logs": logs,
            "page_title": f"定时任务详情：{schedule.name}"
        })


class ScheduleTaskToggleView(View):
    """启用/禁用定时任务"""

    def post(self, request, schedule_id):
        schedule = get_object_or_404(ScheduleTask, id=schedule_id)
        schedule.is_active = not schedule.is_active
        schedule.save()
        status = "启用" if schedule.is_active else "禁用"
        messages.success(request, f"定时任务已{status}")
        return redirect(reverse("task_scheduler:list"))


class ExecuteScheduledTaskView(View):
    """手动执行定时任务"""

    def post(self, request, schedule_id):
        schedule = get_object_or_404(ScheduleTask, id=schedule_id)
        logger.info(f"开始执行定时任务 {schedule.name}，设备ID：{schedule.device.id if schedule.device else '无'}")

        # 记录执行日志
        log = ScheduleExecutionLog.objects.create(
            schedule=schedule,
            exec_status="running"
        )

        try:
            # 调试：打印设备信息
            if schedule.device:
                logger.info(f"指定设备：{schedule.device}，is_active：{schedule.device.is_active}")
                device = schedule.device if schedule.device.is_active else None
            else:
                logger.info("未指定设备，查询所有活跃设备")
                # 调试：打印查询语句
                active_devices = ADBDevice.objects.filter(is_active=True)
                logger.info(f"活跃设备数量：{active_devices.count()}")
                device = active_devices.first()

            if not device:
                raise Exception("没有可用的在线设备")

            log.device = device
            log.save()
            logger.info(f"选中执行设备：{device.device_name}")

            # 调用编排任务执行接口
            execution_view = ExecuteOrchestrationAPIView()
            logger.info(f"调用编排任务接口，ID：{schedule.orchestration.id}")
            response = execution_view.post(request, schedule.orchestration.id)

            if response.status_code == 200 and response.json().get("status") == "success":
                log.orchestration_log_id = response.json().get("log_id")
                log.exec_status = "success"
                messages.success(request, f"定时任务已手动执行")
            else:
                log.exec_status = "failed"
                log.error_msg = response.json().get("msg", "执行失败")
                messages.error(request, f"定时任务执行失败：{log.error_msg}")

        except Exception as e:
            logger.error(f"执行失败：{str(e)}", exc_info=True)  # 打印完整堆栈
            log.exec_status = "failed"
            log.error_msg = str(e)
            messages.error(request, f"定时任务执行失败：{str(e)}")
        finally:
            log.end_time = datetime.datetime.now()
            log.save()

            schedule.last_run_time = datetime.datetime.now()
            schedule.next_run_time = schedule.calculate_next_run_time()
            schedule.save()

        return redirect(reverse("task_scheduler:detail", args=[schedule_id]))