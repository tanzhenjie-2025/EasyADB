# task_scheduler/views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.urls import reverse
from django.http import JsonResponse
from django.utils import timezone
from django.contrib import messages
import logging
import datetime
import json
import django.db.models as models  # 新增：用于日志搜索
from adb_manager.models import ADBDevice
from .models import ScheduleTask, ScheduleExecutionLog, ScheduleManagementLog  # 新增管理日志模型
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
            # 记录新增管理日志
            ScheduleManagementLog.objects.create(
                schedule=schedule,
                operation="create",
                operator=request.user.username if request.user.is_authenticated else "匿名用户",
                details=f"创建定时任务：{schedule.name}，关联编排任务：{schedule.orchestration.name}，Cron表达式：{schedule.cron_expression}，指定设备：{schedule.device.device_name if schedule.device else '自动选择'}"
            )
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
        # 记录编辑前的关键信息
        old_name = schedule.name
        old_cron = schedule.cron_expression
        old_orch = schedule.orchestration.name
        old_device = schedule.device.device_name if schedule.device else "自动选择"
        old_active = schedule.is_active

        form = ScheduleTaskForm(request.POST, instance=schedule)
        if form.is_valid():
            updated_schedule = form.save()
            # 构建编辑详情
            details = []
            if old_name != updated_schedule.name:
                details.append(f"任务名称从 '{old_name}' 修改为 '{updated_schedule.name}'")
            if old_cron != updated_schedule.cron_expression:
                details.append(f"Cron表达式从 '{old_cron}' 修改为 '{updated_schedule.cron_expression}'")
            if old_orch != updated_schedule.orchestration.name:
                details.append(f"关联编排任务从 '{old_orch}' 修改为 '{updated_schedule.orchestration.name}'")
            new_device = updated_schedule.device.device_name if updated_schedule.device else "自动选择"
            if old_device != new_device:
                details.append(f"执行设备从 '{old_device}' 修改为 '{new_device}'")

            # 记录编辑管理日志
            ScheduleManagementLog.objects.create(
                schedule=updated_schedule,
                operation="edit",
                operator=request.user.username if request.user.is_authenticated else "匿名用户",
                details="；".join(details) if details else f"编辑定时任务 '{updated_schedule.name}'，未修改关键信息"
            )
            messages.success(request, "定时任务更新成功")
            return redirect(reverse("task_scheduler:list"))
        return render(request, "task_scheduler/schedule_form.html", {
            "form": form,
            "schedule": schedule,
            "page_title": f"编辑定时任务：{schedule.name}"
        })


class ScheduleTaskDeleteView(View):
    """删除定时任务（新增）"""

    def post(self, request, schedule_id):
        try:
            schedule = get_object_or_404(ScheduleTask, id=schedule_id)
            schedule_name = schedule.name
            # 先记录删除日志再删除
            ScheduleManagementLog.objects.create(
                schedule=schedule,
                operation="delete",
                operator=request.user.username if request.user.is_authenticated else "匿名用户",
                details=f"删除定时任务：{schedule_name}，关联编排任务：{schedule.orchestration.name}，Cron表达式：{schedule.cron_expression}"
            )
            schedule.delete()
            messages.success(request, f"定时任务【{schedule_name}】删除成功")
        except Exception as e:
            logger.error(f"删除定时任务失败：{str(e)}")
            messages.error(request, f"删除定时任务失败：{str(e)}")
        return redirect(reverse("task_scheduler:list"))


class ScheduleTaskDetailView(View):
    """定时任务详情，查看执行日志"""

    def get(self, request, schedule_id):
        schedule = get_object_or_404(ScheduleTask, id=schedule_id)
        logs = schedule.logs.all()[:100]

        # 关联查询对应的编排执行日志（用于显示停止按钮）
        orch_logs = []
        for log in logs:
            if log.orchestration_log_id:
                try:
                    from task_orchestration.models import OrchestrationLog
                    orch_log = OrchestrationLog.objects.get(id=log.orchestration_log_id)
                    log.orch_log = orch_log
                except Exception as e:
                    logger.error(f"获取编排日志失败：{str(e)}")
                    log.orch_log = None
            else:
                log.orch_log = None

        return render(request, "task_scheduler/schedule_detail.html", {
            "schedule": schedule,
            "logs": logs,
            "page_title": f"定时任务详情：{schedule.name}"
        })


class ScheduleTaskToggleView(View):
    """启用/禁用定时任务"""

    def post(self, request, schedule_id):
        schedule = get_object_or_404(ScheduleTask, id=schedule_id)
        old_status = "启用" if schedule.is_active else "禁用"
        schedule.is_active = not schedule.is_active
        schedule.save()
        new_status = "启用" if schedule.is_active else "禁用"

        # 记录启用/禁用管理日志
        ScheduleManagementLog.objects.create(
            schedule=schedule,
            operation="toggle",
            operator=request.user.username if request.user.is_authenticated else "匿名用户",
            details=f"将定时任务【{schedule.name}】从{old_status}状态切换为{new_status}状态"
        )

        messages.success(request, f"定时任务已{new_status}")
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

            # 解析响应
            response_data = json.loads(response.content)
            if response.status_code == 200 and response_data.get("status") == "success":
                log.orchestration_log_id = response_data.get("log_id")
                log.exec_status = "success"
                messages.success(request, f"定时任务已手动执行")
            else:
                log.exec_status = "failed"
                log.error_msg = response_data.get("msg", "执行失败")
                messages.error(request, f"定时任务执行失败：{log.error_msg}")

        except Exception as e:
            logger.error(f"执行失败：{str(e)}", exc_info=True)
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


class ScheduleManagementLogView(View):
    """查看定时任务管理日志（新增）"""

    def get(self, request):
        # 获取搜索关键词
        search_query = request.GET.get('search', '').strip()

        # 基础查询集，按操作时间倒序
        logs_query = ScheduleManagementLog.objects.all().order_by("-operation_time")

        # 多条件搜索过滤
        if search_query:
            logs_query = logs_query.filter(
                models.Q(schedule__name__icontains=search_query) |
                models.Q(operator__icontains=search_query) |
                models.Q(details__icontains=search_query) |
                models.Q(operation__icontains=search_query)
            )

        context = {
            "page_title": "定时任务管理日志",
            "logs": logs_query,
            "search_query": search_query
        }
        return render(request, "task_scheduler/management_log.html", context)