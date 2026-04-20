from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.urls import reverse
from django.http import JsonResponse
from django.utils import timezone
from urllib.parse import quote
import logging
import threading
import time
import subprocess
import psutil
import os
import sys
import redis
import json
from django.conf import settings

# 导入模型、表单和核心逻辑
from adb_manager.models import ADBDevice
from .models import OrchestrationTask, TaskStep, OrchestrationLog, StepExecutionLog, OrchestrationManagementLog
from .forms import OrchestrationTaskForm, TaskStepForm, TaskStepEditForm
from script_center.models import ScriptTask, TaskExecutionLog
from .tasks import _execute_step_core, kill_redis_process, get_running_process, remove_running_process, get_redis_conn

from celery.result import AsyncResult

from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
# ===================== 公共工具函数（保持不变） =====================
def get_env_config(key, default=None, cast_type=str):
    """统一读取环境变量配置"""
    from django.conf import settings
    # 优先从settings读取（已从.env加载）
    value = getattr(settings, key, os.getenv(key, default))
    if value is None:
        return default

    try:
        if cast_type == int:
            return int(value)
        elif cast_type == bool:
            return str(value).lower() == "true"
        return cast_type(value)
    except (ValueError, TypeError):
        logger.error(f"配置 {key} 转换失败，使用默认值 {default}")
        return default

# ===================== 日志配置（保持不变） =====================
LOG_FILE = get_env_config("ORCH_LOG_FILE", "orchestration_execution.log")
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['PYTHONLEGACYWINDOWSSTDIO'] = 'utf-8'

# ===================== 全局配置（从settings读取） =====================
RECENT_LOGS_LIMIT = get_env_config("ORCH_RECENT_LOGS_LIMIT", 10, int)
STEP_TIMEOUT_BUFFER = get_env_config("ORCH_STEP_TIMEOUT_BUFFER", 10, int)
PROCESS_TERMINATE_WAIT = get_env_config("ORCH_PROCESS_TERMINATE_WAIT", 1, int)
REDIS_PROCESS_HASH = get_env_config("ORCH_REDIS_PROCESS_HASH", "orch_running_processes")
CELERY_TERMINATE_FORCE = get_env_config("ORCH_CELERY_TERMINATE_FORCE", True, bool)
MOBILE_VALID_LENGTH = get_env_config("ORCH_MOBILE_VALID_LENGTH", 11, int)
USE_CELERY = get_env_config("USE_CELERY", True, bool)

# 全局存储（兼容Celery和本地任务）
running_processes = {}
process_lock = threading.Lock()
running_tasks = {}  # 存储格式：{key: {'type': 'celery'|'local', 'id': task_id|thread_obj}}
task_lock = threading.Lock()

# ===================== 优雅降级：统一任务执行器 =====================
def is_celery_available():
    """检查Celery是否可用"""
    if not USE_CELERY:
        return False

    try:
        from celery.result import AsyncResult
        from mycelery.main import app

        # 检查Broker连接
        conn = app.connection_for_write()
        conn.ensure_connection(max_retries=1, interval_start=0.1)
        conn.release()
        return True
    except Exception as e:
        logger.warning(f"Celery不可用，将使用本地执行：{str(e)}")
        return False

def execute_task_async(step_id, orch_log_id, device_data, process_key):
    """
    统一任务执行接口（优雅降级核心）
    :return: (task_type, task_handle) - task_type: 'celery'|'local'
    """
    if is_celery_available():
        try:
            from .tasks import execute_step_task
            from celery.result import AsyncResult

            task = execute_step_task.delay(step_id, orch_log_id, device_data)
            logger.info(f"Celery任务已提交：{task.id}，KEY={process_key}")
            return 'celery', task.id
        except Exception as e:
            logger.warning(f"Celery任务提交失败，切换本地执行：{str(e)}")

    # 本地降级执行
    logger.info(f"使用本地线程执行，KEY={process_key}")
    thread = threading.Thread(
        target=_execute_step_core,
        args=(step_id, orch_log_id, device_data),
        daemon=True
    )
    thread.start()
    return 'local', thread

def wait_task_completion(task_type, task_handle, step, orch_log, process_key):
    """
    统一等待任务完成接口
    :return: task_result（如果有）
    """
    max_wait_time = step.run_duration + STEP_TIMEOUT_BUFFER
    start_time = time.time()
    task_completed = False
    task_result = None

    if task_type == 'celery':
        from celery.result import AsyncResult
        result = AsyncResult(task_handle)

        while time.time() - start_time < max_wait_time:
            if result.ready():
                task_result = result.get()
                task_completed = True
                break
            time.sleep(1)
    else:
        # 本地执行：等待线程结束或超时
        thread = task_handle
        while time.time() - start_time < max_wait_time:
            if not thread.is_alive():
                task_completed = True
                break
            time.sleep(1)

        # 本地执行结果直接从数据库读取
        if task_completed:
            step_log = StepExecutionLog.objects.filter(
                orchestration_log=orch_log,
                step=step
            ).first()
            if step_log:
                task_result = {
                    "status": step_log.exec_status,
                    "step_id": step.id,
                    "step_log_id": step_log.id
                }

    return task_completed, task_result

# ===================== 编排任务核心视图（修改执行逻辑） =====================
class OrchestrationListView(View):
    """编排任务列表（支持任务名称搜索）"""

    def get(self, request):
        # 获取搜索关键词
        search_keyword = request.GET.get('search', '').strip()
        # 基础查询
        tasks = OrchestrationTask.objects.all()

        # 搜索过滤：任务名称模糊匹配
        if search_keyword:
            tasks = tasks.filter(name__icontains=search_keyword)

        return render(request, "task_orchestration/list.html", {
            "tasks": tasks,
            "page_title": "编排任务管理",
            "current_search": search_keyword  # 把搜索词传回前端，保持输入框内容
        })

class OrchestrationCreateView(View):
    """创建编排任务（保持不变）"""
    def get(self, request):
        form = OrchestrationTaskForm()
        return render(request, "task_orchestration/form.html", {
            "form": form,
            "page_title": "创建编排任务"
        })

    def post(self, request):
        form = OrchestrationTaskForm(request.POST)
        if form.is_valid():
            task = form.save()

            operator = request.user.username if request.user.is_authenticated else "匿名用户"
            details = f"创建新编排任务【{task.name}（ID：{task.id}）】，状态：{task.get_status_display()}，描述：{task.description or '无'}"
            OrchestrationManagementLog.objects.create(
                orchestration=task,
                original_task_name=task.name,
                original_task_id=task.id,
                operation_type="create",
                operator=operator,
                details=details
            )

            return redirect(reverse("task_orchestration:edit_steps", args=[task.id]))
        return render(request, "task_orchestration/form.html", {"form": form})

class OrchestrationDeleteView(View):
    """删除编排任务（保持不变）"""
    def get(self, request, task_id):
        task = get_object_or_404(OrchestrationTask, id=task_id)

        operator = request.user.username if request.user.is_authenticated else "匿名用户"
        details = f"删除编排任务【{task.name}（ID：{task.id}）】，删除前状态：{task.get_status_display()}，包含{task.steps.count()}个子任务步骤"
        OrchestrationManagementLog.objects.create(
            orchestration=task,
            original_task_name=task.name,
            original_task_id=task.id,
            operation_type="delete",
            operator=operator,
            details=details
        )

        task_name = task.name
        task.delete()

        success_msg = quote(f"编排任务【{task_name}】已成功删除！")
        return redirect(f"{reverse('task_orchestration:list')}?msg={success_msg}")

class StepEditView(View):
    """编辑子任务步骤（保持不变）"""
    def get(self, request, task_id):
        orchestration = get_object_or_404(OrchestrationTask, id=task_id)
        steps = orchestration.steps.all().order_by("execution_order")
        task_form = OrchestrationTaskForm(instance=orchestration)
        step_form = TaskStepForm()
        edit_forms = {step.id: TaskStepEditForm(instance=step) for step in steps}

        return render(request, "task_orchestration/edit_steps.html", {
            "orchestration": orchestration,
            "steps": steps,
            "task_form": task_form,
            "step_form": step_form,
            "edit_forms": edit_forms,
            "page_title": f"编辑步骤 - {orchestration.name}"
        })

    def post(self, request, task_id):
        orchestration = get_object_or_404(OrchestrationTask, id=task_id)
        steps = orchestration.steps.all().order_by("execution_order")

        if "action" in request.POST and request.POST["action"] == "update_task":
            old_name = orchestration.name
            old_status = orchestration.status
            old_desc = orchestration.description

            task_form = OrchestrationTaskForm(request.POST, instance=orchestration)
            step_form = TaskStepForm()
            if task_form.is_valid():
                updated_task = task_form.save()

                operator = request.user.username if request.user.is_authenticated else "匿名用户"
                change_details = []
                if old_name != updated_task.name:
                    change_details.append(f"名称：{old_name} → {updated_task.name}")
                if old_status != updated_task.status:
                    change_details.append(f"状态：{old_status} → {updated_task.status}")
                if old_desc != updated_task.description:
                    change_details.append(
                        f"描述：{'无' if not old_desc else old_desc} → {'无' if not updated_task.description else updated_task.description}")
                details = f"编辑编排任务【{updated_task.name}（ID：{updated_task.id}）】，变更内容：{'; '.join(change_details)}"
                OrchestrationManagementLog.objects.create(
                    orchestration=updated_task,
                    original_task_name=updated_task.name,
                    original_task_id=updated_task.id,
                    operation_type="edit",
                    operator=operator,
                    details=details
                )

                return redirect(f"{reverse('task_orchestration:edit_steps', args=[task_id])}?msg=状态修改成功")

        elif "action" not in request.POST or request.POST["action"] == "add_step":
            task_form = OrchestrationTaskForm(instance=orchestration)
            step_form = TaskStepForm(request.POST)
            if step_form.is_valid():
                step = step_form.save(commit=False)
                step.orchestration = orchestration
                step.save()
                return redirect(f"{reverse('task_orchestration:edit_steps', args=[task_id])}?msg=步骤添加成功")

        elif "action" in request.POST and request.POST["action"] == "edit_step":
            step_id = request.POST.get("step_id")
            step = get_object_or_404(TaskStep, id=step_id, orchestration=orchestration)
            form = TaskStepEditForm(request.POST, instance=step)

            if form.is_valid():
                form.save()
                return redirect(f"{reverse('task_orchestration:edit_steps', args=[task_id])}?msg=步骤更新成功")
            else:
                return render(request, "task_orchestration/edit_steps.html", {
                    "orchestration": orchestration,
                    "steps": steps,
                    "task_form": OrchestrationTaskForm(instance=orchestration),
                    "step_form": TaskStepForm(),
                    "edit_forms": {step.id: form for step in steps},
                    "page_title": f"编辑步骤 - {orchestration.name}",
                    "error_msg": "表单填写有误，请检查（运行时长最小10秒）"
                })

        return render(request, "task_orchestration/edit_steps.html", {
            "orchestration": orchestration,
            "steps": steps,
            "task_form": task_form if 'task_form' in locals() else OrchestrationTaskForm(instance=orchestration),
            "step_form": step_form if 'step_form' in locals() else TaskStepForm(),
            "edit_forms": {step.id: TaskStepEditForm(instance=step) for step in steps},
            "page_title": f"编辑步骤 - {orchestration.name}",
            "error_msg": "表单填写有误，请检查"
        })

class OrchestrationGlobalManagementLogView(View):
    """全局编排任务管理日志（保持不变）"""
    def get(self, request):
        from django.db import models
        logs = OrchestrationManagementLog.objects.all().order_by("-operation_time")

        operation_type = request.GET.get("operation_type")
        task_name = request.GET.get("task_name")
        operator = request.GET.get("operator")

        if operation_type:
            logs = logs.filter(operation_type=operation_type)
        if task_name:
            logs = logs.filter(
                models.Q(orchestration__name__icontains=task_name) |
                models.Q(original_task_name__icontains=task_name)
            )
        if operator:
            logs = logs.filter(operator__icontains=operator)

        return render(request, "task_orchestration/global_management_logs.html", {
            "page_title": "全局编排任务管理日志",
            "logs": logs,
            "operation_types": OrchestrationManagementLog.OPERATION_TYPES,
            "current_operation_type": operation_type,
            "current_task_name": task_name,
            "current_operator": operator
        })

class ExecuteOrchestrationAPIView(View):
    """旧版执行接口（使用优雅降级执行器）"""
    def post(self, request, task_id):
        orchestration = get_object_or_404(OrchestrationTask, id=task_id, status="active")
        steps = orchestration.steps.order_by("execution_order").all()
        if not steps:
            return JsonResponse({
                "status": "error",
                "msg": "该编排任务没有子任务步骤"
            })

        # 查找可用设备
        device = None
        active_devices = ADBDevice.objects.filter(is_active=True)
        for dev in active_devices:
            if dev.device_status == "online":
                device = dev
                break

        if not device:
            return JsonResponse({
                "status": "error",
                "msg": "无可用在线设备"
            })

        # 创建编排日志
        orch_log = OrchestrationLog.objects.create(
            orchestration=orchestration,
            device=device,
            total_steps=steps.count(),
            exec_status="running",
            exec_command=f"编排任务启动：{orchestration.name} - 设备：{device.adb_connect_str}",
            stdout=f"编排任务启动中 - 时间：{timezone.now()}",
            start_time=timezone.now()
        )

        # 启动执行线程
        threading.Thread(
            target=self._run_orchestration,
            args=(orch_log, steps, device),
            daemon=True
        ).start()

        return JsonResponse({
            "status": "success",
            "msg": "编排任务已启动",
            "log_id": orch_log.id
        })

    def _run_orchestration(self, orch_log, steps, device):
        """执行编排任务核心逻辑（使用优雅降级执行器）"""
        start_total_time = time.time()
        orch_log_stdout = [orch_log.stdout]
        orch_log_stderr = []

        try:
            for step in steps:
                # 检查是否已被停止
                current_log = OrchestrationLog.objects.get(id=orch_log.id)
                if current_log.exec_status == "stopped":
                    logger.info(f"编排任务{orch_log.id}已被手动停止，终止后续步骤执行")
                    break

                # 准备数据
                device_data = {
                    'id': device.id,
                    'adb_connect_str': device.adb_connect_str,
                    'device_name': device.device_name
                }
                process_key = f"{orch_log.id}_{step.execution_order}"

                # 更新日志
                orch_log_stdout.append(f"\n=== 步骤{step.execution_order}开始执行 ===")
                orch_log_stdout.append(
                    f"执行命令：{step.script_task.python_path} {step.script_task.script_path} {device.adb_connect_str}")
                orch_log_stdout.append(f"工作目录：{os.path.dirname(step.script_task.script_path)}")
                orch_log_stdout.append(f"开始时间：{timezone.now()}")
                orch_log_stdout.append(f"执行模式：{'Celery' if is_celery_available() else '本地线程'}")
                orch_log_stdout.append(f"超时设置：{step.run_duration}秒")
                orch_log_stdout.append(f"进程KEY：{process_key}")

                orch_log.stdout = "\n".join(orch_log_stdout)
                orch_log.save(update_fields=['stdout'])

                # 使用统一执行器提交任务
                try:
                    task_type, task_handle = execute_task_async(
                        step.id, orch_log.id, device_data, process_key
                    )

                    # 记录任务
                    with task_lock:
                        running_tasks[process_key] = {
                            'type': task_type,
                            'handle': task_handle
                        }
                except Exception as e:
                    error_msg = f"提交任务失败（步骤{step.execution_order}）：{str(e)}"
                    logger.error(error_msg, exc_info=True)
                    orch_log_stderr.append(error_msg)
                    orch_log_stdout.append(f"\n=== 步骤{step.execution_order}执行失败 ===")
                    orch_log_stdout.append(error_msg)

                    StepExecutionLog.objects.create(
                        orchestration_log=orch_log,
                        step=step,
                        exec_status="error",
                        error_msg=error_msg,
                        start_time=timezone.now(),
                        end_time=timezone.now()
                    )

                    orch_log.stdout = "\n".join(orch_log_stdout)
                    orch_log.stderr = "\n".join(orch_log_stderr)
                    orch_log.save()
                    continue

                # 等待任务完成
                task_completed, task_result = wait_task_completion(
                    task_type, task_handle, step, orch_log, process_key
                )

                # 处理执行结果
                step_log = StepExecutionLog.objects.filter(
                    orchestration_log=orch_log,
                    step=step
                ).first()

                if step_log:
                    if step_log.exec_status == "completed":
                        orch_log_stdout.append(
                            f"步骤{step.execution_order}执行成功，返回码：{step_log.return_code}")
                    elif step_log.exec_status == "timeout":
                        orch_log_stdout.append(
                            f"步骤{step.execution_order}执行超时（{step.run_duration}秒），已强制终止")
                        orch_log_stderr.append(f"步骤{step.execution_order}超时：强制终止进程")
                    elif step_log.exec_status == "failed":
                        orch_log_stdout.append(
                            f"步骤{step.execution_order}执行失败，返回码：{step_log.return_code}")
                        orch_log_stderr.append(f"步骤{step.execution_order}执行失败：{step_log.stderr}")
                    elif step_log.exec_status == "error":
                        orch_log_stdout.append(f"步骤{step.execution_order}执行出错：{step_log.error_msg}")
                        orch_log_stderr.append(f"步骤{step.execution_order}系统错误：{step_log.error_msg}")
                else:
                    # 兼容未创建step_log的情况
                    orch_log_stdout.append(f"步骤{step.execution_order}执行完成（无详细日志）")

                # 处理超时未完成
                if not task_completed:
                    orch_log_stdout.append(f"步骤{step.execution_order}任务未在规定时间内响应，标记为超时")
                    orch_log_stderr.append(
                        f"步骤{step.execution_order}超时未响应：{step.run_duration + STEP_TIMEOUT_BUFFER}秒内未完成")

                    if step_log:
                        step_log.exec_status = "timeout"
                        step_log.error_msg = f"任务未在规定时间内完成（{step.run_duration}秒+{STEP_TIMEOUT_BUFFER}秒缓冲）"
                        step_log.end_time = timezone.now()
                        step_log.exec_duration = step.run_duration + STEP_TIMEOUT_BUFFER
                        step_log.save()
                    else:
                        step_log = StepExecutionLog.objects.create(
                            orchestration_log=orch_log,
                            step=step,
                            exec_status="timeout",
                            error_msg=f"任务未在规定时间内完成（{step.run_duration}秒+{STEP_TIMEOUT_BUFFER}秒缓冲）",
                            start_time=timezone.now(),
                            end_time=timezone.now(),
                            exec_duration=step.run_duration + STEP_TIMEOUT_BUFFER
                        )

                    # 终止超时任务
                    with task_lock:
                        if process_key in running_tasks:
                            task_info = running_tasks[process_key]
                            if task_info['type'] == 'celery':
                                try:
                                    from celery.result import AsyncResult
                                    AsyncResult(task_info['handle']).revoke(terminate=CELERY_TERMINATE_FORCE)
                                except Exception as e:
                                    logger.error(f"终止Celery任务失败：{str(e)}")
                            # 终止进程（无论Celery还是本地）
                            kill_redis_process(process_key)
                            del running_tasks[process_key]

                # 清理任务记录
                with task_lock:
                    if process_key in running_tasks:
                        del running_tasks[process_key]

                # 实时更新日志
                orch_log.stdout = "\n".join(orch_log_stdout)
                if orch_log_stderr:
                    orch_log.stderr = "\n".join(orch_log_stderr)
                orch_log.save()

            # 所有步骤完成
            orch_log.exec_duration = time.time() - start_total_time
            orch_log.end_time = timezone.now()

            failed_steps = StepExecutionLog.objects.filter(
                orchestration_log=orch_log,
                exec_status__in=["failed", "timeout", "error"]
            ).count()

            if failed_steps > 0:
                orch_log.exec_status = "part_failed"
                orch_log.error_msg = f"{failed_steps}个步骤执行异常（超时/失败/错误）"
                orch_log.stderr = "\n".join(orch_log_stderr)
            else:
                orch_log.exec_status = "completed"

        except Exception as e:
            orch_log.exec_status = "failed"
            orch_log.error_msg = str(e)
            orch_log.stderr = "\n".join(orch_log_stderr) + f"\n\n编排任务全局错误：{str(e)}"
            orch_log.exec_duration = time.time() - start_total_time
            orch_log.end_time = timezone.now()

        finally:
            orch_log.stdout = "\n".join(orch_log_stdout)
            orch_log.stderr = "\n".join(orch_log_stderr)
            orch_log.save()

class OrchestrationExecuteView(View):
    """新版执行页面（添加分页功能）"""
    def get(self, request):
        devices = ADBDevice.objects.filter(is_active=True)
        device_list = []
        for dev in devices:
            dev.status = dev.device_status
            device_list.append(dev)

        # 分页处理：获取所有日志并分页
        all_logs = OrchestrationLog.objects.all().order_by("-id")
        paginator = Paginator(all_logs, RECENT_LOGS_LIMIT)  # 每页显示 RECENT_LOGS_LIMIT 条
        page = request.GET.get('page')

        try:
            recent_logs_page = paginator.page(page)
        except PageNotAnInteger:
            # 如果页码不是整数，显示第一页
            recent_logs_page = paginator.page(1)
        except EmptyPage:
            # 如果页码超出范围，显示最后一页
            recent_logs_page = paginator.page(paginator.num_pages)

        # 给当前页的日志添加运行状态标记
        for log in recent_logs_page:
            log.is_running = log.exec_status == "running"

        context = {
            "page_title": "执行编排任务",
            "devices": device_list,
            "orchestrations": OrchestrationTask.objects.filter(status="active"),
            "recent_logs": recent_logs_page,  # 传递分页后的页面对象
            "paginator": paginator,
            "page_obj": recent_logs_page,
            "is_paginated": recent_logs_page.has_other_pages(),  # 是否有分页
        }
        return render(request, "task_orchestration/execute_orchestration.html", context)


    def post(self, request):
        try:
            device_ids = request.POST.getlist("device_ids")
            orch_id = request.POST.get("orch_id")

            if not device_ids or not orch_id:
                error_msg = quote("请选择至少一个设备和一个编排任务！")
                return redirect(f"{reverse('task_orchestration:execute_orchestration')}?msg={error_msg}")

            orchestration = get_object_or_404(OrchestrationTask, id=orch_id, status="active")
            steps = orchestration.steps.order_by("execution_order").all()
            if not steps:
                error_msg = quote(f"编排任务【{orchestration.name}】没有配置子任务步骤！")
                return redirect(f"{reverse('task_orchestration:execute_orchestration')}?msg={error_msg}")

            # 校验设备
            offline_devices = []
            valid_device_ids = []
            for device_id in device_ids:
                device = get_object_or_404(ADBDevice, id=device_id)
                if device.device_status != "online":
                    offline_devices.append(device.device_name)
                else:
                    valid_device_ids.append(device_id)

            if not valid_device_ids:
                error_msg = quote(f"所选设备均离线！离线设备：{','.join(offline_devices)}")
                return redirect(f"{reverse('task_orchestration:execute_orchestration')}?msg={error_msg}")

            # 执行每个设备
            for device_id in valid_device_ids:
                device = get_object_or_404(ADBDevice, id=device_id)
                orch_log = OrchestrationLog.objects.create(
                    orchestration=orchestration,
                    device=device,
                    total_steps=steps.count(),
                    exec_status="running",
                    exec_command=f"编排任务批量执行：{orchestration.name} - 设备：{device.adb_connect_str}",
                    stdout=f"批量执行启动中 - 时间：{timezone.now()}",
                    start_time=timezone.now()
                )

                threading.Thread(
                    target=self._run_orchestration,
                    args=(orch_log, steps, device),
                    daemon=True
                ).start()

            success_msg = quote(f"编排任务【{orchestration.name}】已启动！共{len(valid_device_ids)}个设备执行中")
            if offline_devices:
                success_msg = quote(f"{success_msg}（离线设备已过滤：{','.join(offline_devices)}）")
            return redirect(f"{reverse('task_orchestration:execute_orchestration')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"执行编排任务失败：{str(e)}", exc_info=True)
            error_msg = quote(f"执行失败：{str(e)}")
            return redirect(f"{reverse('task_orchestration:execute_orchestration')}?msg={error_msg}")

    def _run_orchestration(self, orch_log, steps, device):
        """复用修改后的执行逻辑"""
        ExecuteOrchestrationAPIView()._run_orchestration(orch_log, steps, device)


class StopOrchestrationView(View):
    """停止编排任务（支持Celery和本地任务）"""

    def get(self, request, log_id):
        try:
            orch_log = get_object_or_404(OrchestrationLog, id=log_id)
            if orch_log.exec_status != "running":
                error_msg = quote(f"编排任务未在运行中！当前状态：{orch_log.exec_status}")
                return redirect(f"{reverse('task_orchestration:execute_orchestration')}?msg={error_msg}")

            # 1. 停止所有相关任务
            with task_lock:
                for key in list(running_tasks.keys()):
                    if key.startswith(f"{log_id}_"):
                        task_info = running_tasks[key]
                        try:
                            if task_info['type'] == 'celery':
                                from celery.result import AsyncResult
                                AsyncResult(task_info['handle']).revoke(terminate=CELERY_TERMINATE_FORCE)
                                logger.info(f"已终止Celery任务{task_info['handle']}（编排日志{log_id}）")
                            # 本地线程会在进程终止后自动结束
                        except Exception as e:
                            logger.error(f"终止任务失败：{str(e)}")
                        del running_tasks[key]

            # 2. 终止所有相关进程（修复版）
            # 先尝试从Redis获取所有key
            r = get_redis_conn()
            all_keys = []
            if r:
                try:
                    all_keys = r.hkeys(REDIS_PROCESS_HASH)
                except Exception as e:
                    logger.warning(f"Redis获取keys失败：{str(e)}")

            # 如果Redis不可用或没获取到，尝试从本地存储获取
            if not all_keys:
                try:
                    from .tasks import _local_process_store, _local_store_lock
                    with _local_store_lock:  # 使用tasks.py中定义的锁，而不是新建锁
                        all_keys = list(_local_process_store.keys())
                except Exception as e:
                    logger.warning(f"本地存储获取keys失败：{str(e)}")

            # 终止匹配的进程
            for key in all_keys:
                if key.startswith(f"{log_id}_"):
                    kill_redis_process(key)

            # 3. 清理原有本地进程（兼容历史逻辑）
            with process_lock:
                for key in list(running_processes.keys()):
                    if key.startswith(f"{log_id}_"):
                        process_info = running_processes[key]
                        pid = process_info["pid"]
                        process = process_info["process"]

                        try:
                            parent = psutil.Process(pid)
                            for child in parent.children(recursive=True):
                                child.terminate()
                            parent.terminate()
                            time.sleep(PROCESS_TERMINATE_WAIT)
                            if parent.is_running():
                                parent.kill()
                            logger.info(f"已终止本地进程{pid}（编排日志{log_id}）")
                        except Exception as e:
                            logger.error(f"终止本地进程{pid}失败：{str(e)}")

                        if process and process.poll() is None:
                            process.kill()
                        del running_processes[key]

            # 更新日志状态
            orch_log.exec_status = "stopped"
            orch_log.stderr = f"{orch_log.stderr}\n\n任务已手动停止 - 时间：{timezone.now()}"
            orch_log.end_time = timezone.now()
            orch_log.save()

            # 更新步骤日志
            StepExecutionLog.objects.filter(
                orchestration_log=orch_log,
                exec_status__in=["running", "pending"]
            ).update(
                exec_status="stopped",
                end_time=timezone.now(),
                error_msg="任务被手动停止"
            )

            success_msg = quote(f"编排任务【{orch_log.orchestration.name}】已成功停止！")
            return redirect(f"{reverse('task_orchestration:execute_orchestration')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"停止任务失败：{str(e)}", exc_info=True)  # 加上日志输出
            error_msg = quote(f"停止失败：{str(e)}")
            return redirect(f"{reverse('task_orchestration:execute_orchestration')}?msg={error_msg}")

class StepDeleteView(View):
    """删除步骤（保持不变）"""
    def get(self, request, step_id):
        step = get_object_or_404(TaskStep, id=step_id)
        task_id = step.orchestration.id
        step.delete()
        return redirect(reverse("task_orchestration:edit_steps", args=[task_id]))

class OrchestrationLogDetailView(View):
    """编排日志详情（保持不变）"""
    def get(self, request, log_id):
        orch_log = get_object_or_404(OrchestrationLog, id=log_id)
        step_logs = orch_log.step_logs.all().order_by("step__execution_order")

        if orch_log.exec_duration:
            orch_log.exec_duration_str = f"{orch_log.exec_duration:.2f}秒"
        else:
            orch_log.exec_duration_str = "未知"

        context = {
            "page_title": f"编排执行日志 - {orch_log.orchestration.name}",
            "orch_log": orch_log,
            "step_logs": step_logs
        }
        return render(request, "task_orchestration/log_detail.html", context)

class OrchestrationLogStatusView(View):
    """AJAX获取日志状态（保持不变）"""
    def get(self, request, log_id):
        try:
            orch_log = get_object_or_404(OrchestrationLog, id=log_id)
            step_logs = orch_log.step_logs.all().order_by("step__execution_order")
            step_data = []
            for sl in step_logs:
                step_data.append({
                    "step_log_id": sl.id,
                    "order": sl.step.execution_order,
                    "status": sl.exec_status,
                    "stdout": sl.stdout,
                    "stderr": sl.stderr,
                    "duration": sl.exec_duration,
                    "return_code": sl.return_code
                })

            return JsonResponse({
                "code": 200,
                "status": orch_log.exec_status,
                "stdout": orch_log.stdout,
                "stderr": orch_log.stderr,
                "duration": orch_log.exec_duration,
                "step_data": step_data
            })
        except Exception as e:
            return JsonResponse({
                "code": 500,
                "msg": str(e)
            })

class OrchestrationCloneView(View):
    """克隆编排任务（保持不变）"""
    def get(self, request, task_id):
        original_task = get_object_or_404(OrchestrationTask, id=task_id)
        return render(request, "task_orchestration/clone.html", {
            "original_task": original_task,
            "page_title": f"克隆任务 - {original_task.name}"
        })

    def post(self, request, task_id):
        original_task = get_object_or_404(OrchestrationTask, id=task_id)

        new_task_name = request.POST.get("new_task_name")
        if not new_task_name:
            return render(request, "task_orchestration/clone.html", {
                "original_task": original_task,
                "page_title": f"克隆任务 - {original_task.name}",
                "error_msg": "新任务名称不能为空"
            })

        if OrchestrationTask.objects.filter(name=new_task_name).exists():
            return render(request, "task_orchestration/clone.html", {
                "original_task": original_task,
                "page_title": f"克隆任务 - {original_task.name}",
                "error_msg": "任务名称已存在，请使用其他名称"
            })

        new_task = OrchestrationTask.objects.create(
            name=new_task_name,
            description=original_task.description,
            status="draft",
            create_time=timezone.now(),
            update_time=timezone.now()
        )

        original_steps = original_task.steps.all().order_by("execution_order")
        for step in original_steps:
            TaskStep.objects.create(
                orchestration=new_task,
                script_task=step.script_task,
                execution_order=step.execution_order,
                run_duration=step.run_duration,
                create_time=timezone.now()
            )

        operator = request.user.username if request.user.is_authenticated else "匿名用户"
        details = f"从原任务【{original_task.name}（ID：{original_task.id}）】克隆生成新任务【{new_task.name}（ID：{new_task.id}）】，复制了{original_steps.count()}个子任务步骤"
        OrchestrationManagementLog.objects.create(
            orchestration=new_task,
            original_task_name=new_task.name,
            original_task_id=new_task.id,
            operation_type="clone",
            operator=operator,
            details=details
        )

        return redirect(reverse("task_orchestration:edit_steps", args=[new_task.id]) + "?msg=任务克隆成功")

# ===================== 原有测试相关代码（保持不变） =====================
from rest_framework.decorators import api_view
from rest_framework.response import Response
from mycelery.email.tasks import send_email, send_email2
from mycelery.main import app

def send_sms(request):
    return render(request, 'sms_send.html')

def send_sms_view(request):
    if request.method == 'POST':
        try:
            request_data = json.loads(request.body)
            mobile = request_data.get('mobile', '').strip()
        except json.JSONDecodeError:
            mobile = request.POST.get('mobile', '').strip()

        if not mobile:
            return JsonResponse({'code': 400, 'msg': '手机号不能为空'})
        if not mobile.isdigit() or len(mobile) != MOBILE_VALID_LENGTH:
            return JsonResponse({'code': 400, 'msg': f'手机号格式错误（需{MOBILE_VALID_LENGTH}位数字）'})

        try:
            task1 = send_email.delay(mobile)
            task2 = send_email2.delay(mobile)
        except Exception as e:
            return JsonResponse({'code': 500, 'msg': f'任务提交失败：{str(e)}'})

        return JsonResponse({
            'code': 200,
            'msg': '任务已提交',
            'task1_id': task1.id,
            'task2_id': task2.id,
            'mobile': mobile
        })
    return JsonResponse({'code': 405, 'msg': '仅支持POST请求'})

@api_view(['GET'])
def check_task_result(request):
    task_id = request.GET.get('task_id')
    if not task_id:
        return Response({'code': 400, 'msg': '缺少task_id'}, status=400)

    task = AsyncResult(task_id, app=app)

    return Response({
        'task_id': task_id,
        'status': task.status,
        'result': task.result if task.status == 'SUCCESS' else None,
        'error': str(task.result) if task.status == 'FAILURE' else None
    })