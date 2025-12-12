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
from celery.result import AsyncResult

# 配置日志
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('orchestration_execution.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# 修复Windows编码
os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['PYTHONLEGACYWINDOWSSTDIO'] = 'utf-8'

# 全局存储（兼容原有+新增Celery任务跟踪）
running_processes = {}  # 保留原有进程存储（兼容历史逻辑）
process_lock = threading.Lock()
running_tasks = {}  # 新增：记录Celery任务ID
task_lock = threading.Lock()


# ===================== Redis进程存储函数（和tasks保持一致，避免循环导入） =====================
def get_redis_conn():
    try:
        r = redis.Redis(
            host="127.0.0.1",
            port=6379,
            db=0,
            decode_responses=True,
            socket_timeout=5
        )
        r.ping()
        return r
    except Exception as e:
        logger.error(f"Redis连接失败：{str(e)}")
        return None


def save_running_process(process_key, process_info):
    """保存进程信息到Redis"""
    r = get_redis_conn()
    if r:
        try:
            r.hset("orch_running_processes", process_key, json.dumps(process_info))
        except Exception as e:
            logger.error(f"Redis保存进程信息失败：{str(e)}")


def get_running_process(process_key):
    """从Redis获取进程信息"""
    r = get_redis_conn()
    if r:
        try:
            data = r.hget("orch_running_processes", process_key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"Redis获取进程信息失败：{str(e)}")
    return None


def remove_running_process(process_key):
    """从Redis删除进程信息"""
    r = get_redis_conn()
    if r:
        try:
            r.hdel("orch_running_processes", process_key)
            logger.info(f"Redis中进程信息已删除，KEY={process_key}")
        except Exception as e:
            logger.error(f"Redis删除进程信息失败：{str(e)}")


# ===================== Redis进程存储函数结束 =====================

# 模型和表单导入
from adb_manager.models import ADBDevice
from .models import OrchestrationTask, TaskStep, OrchestrationLog, StepExecutionLog
from .forms import OrchestrationTaskForm, TaskStepForm, TaskStepEditForm
from script_center.models import ScriptTask, TaskExecutionLog
from .tasks import execute_step_task  # 导入Celery任务


# 编排任务列表（完全保留原有代码）
class OrchestrationListView(View):
    def get(self, request):
        tasks = OrchestrationTask.objects.all()
        return render(request, "task_orchestration/list.html", {
            "tasks": tasks,
            "page_title": "编排任务管理"
        })


# 创建编排任务（完全保留原有代码）
class OrchestrationCreateView(View):
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
            return redirect(reverse("task_orchestration:edit_steps", args=[task.id]))
        return render(request, "task_orchestration/form.html", {"form": form})


# 编辑子任务步骤（合并后完整版本，包含编辑超时时间功能）
class StepEditView(View):
    def get(self, request, task_id):
        orchestration = get_object_or_404(OrchestrationTask, id=task_id)
        steps = orchestration.steps.all().order_by("execution_order")
        task_form = OrchestrationTaskForm(instance=orchestration)
        step_form = TaskStepForm()
        # 初始化编辑表单（每个步骤对应一个编辑表单）
        edit_forms = {step.id: TaskStepEditForm(instance=step) for step in steps}

        return render(request, "task_orchestration/edit_steps.html", {
            "orchestration": orchestration,
            "steps": steps,
            "task_form": task_form,
            "step_form": step_form,
            "edit_forms": edit_forms,  # 传递编辑表单到模板
            "page_title": f"编辑步骤 - {orchestration.name}"
        })

    def post(self, request, task_id):
        orchestration = get_object_or_404(OrchestrationTask, id=task_id)
        steps = orchestration.steps.all().order_by("execution_order")

        # 处理任务信息更新（如名称、状态、描述）
        if "action" in request.POST and request.POST["action"] == "update_task":
            task_form = OrchestrationTaskForm(request.POST, instance=orchestration)
            step_form = TaskStepForm()
            if task_form.is_valid():
                task_form.save()
                return redirect(f"{reverse('task_orchestration:edit_steps', args=[task_id])}?msg=状态修改成功")

        # 处理步骤添加
        elif "action" not in request.POST or request.POST["action"] == "add_step":
            task_form = OrchestrationTaskForm(instance=orchestration)
            step_form = TaskStepForm(request.POST)
            if step_form.is_valid():
                step = step_form.save(commit=False)
                step.orchestration = orchestration
                step.save()
                return redirect(f"{reverse('task_orchestration:edit_steps', args=[task_id])}?msg=步骤添加成功")

        # 处理步骤编辑（超时时间）
        elif "action" in request.POST and request.POST["action"] == "edit_step":
            step_id = request.POST.get("step_id")
            step = get_object_or_404(TaskStep, id=step_id, orchestration=orchestration)
            form = TaskStepEditForm(request.POST, instance=step)

            if form.is_valid():
                form.save()
                return redirect(f"{reverse('task_orchestration:edit_steps', args=[task_id])}?msg=步骤更新成功")
            else:
                # 表单验证失败，返回错误
                return render(request, "task_orchestration/edit_steps.html", {
                    "orchestration": orchestration,
                    "steps": steps,
                    "task_form": OrchestrationTaskForm(instance=orchestration),
                    "step_form": TaskStepForm(),
                    "edit_forms": {step.id: form for step in steps},
                    "page_title": f"编辑步骤 - {orchestration.name}",
                    "error_msg": "表单填写有误，请检查（运行时长最小10秒）"
                })

        # 表单验证失败的默认返回
        return render(request, "task_orchestration/edit_steps.html", {
            "orchestration": orchestration,
            "steps": steps,
            "task_form": task_form if 'task_form' in locals() else OrchestrationTaskForm(instance=orchestration),
            "step_form": step_form if 'step_form' in locals() else TaskStepForm(),
            "edit_forms": {step.id: TaskStepEditForm(instance=step) for step in steps},
            "page_title": f"编辑步骤 - {orchestration.name}",
            "error_msg": "表单填写有误，请检查"
        })


# 旧版执行接口（修改为Celery异步执行，保留原有入参和返回格式）
class ExecuteOrchestrationAPIView(View):
    def post(self, request, task_id):
        orchestration = get_object_or_404(OrchestrationTask, id=task_id, status="active")
        steps = orchestration.steps.order_by("execution_order").all()
        if not steps:
            return JsonResponse({
                "status": "error",
                "msg": "该编排任务没有子任务步骤"
            })

        # ========== 核心修复：替换device_status的ORM过滤逻辑 ==========
        # 先查is_active的设备，再遍历判断device_status属性（动态属性不能用于ORM过滤）
        device = None
        active_devices = ADBDevice.objects.filter(is_active=True)
        for dev in active_devices:
            if dev.device_status == "online":  # 这里用实例属性判断，不是ORM过滤
                device = dev
                break
        # ========== 核心修复结束 ==========

        if not device:
            return JsonResponse({
                "status": "error",
                "msg": "无可用在线设备"
            })

        # 创建编排日志（保留原有逻辑）
        orch_log = OrchestrationLog.objects.create(
            orchestration=orchestration,
            device=device,
            total_steps=steps.count(),
            exec_status="running",
            exec_command=f"编排任务启动：{orchestration.name} - 设备：{device.adb_connect_str}",
            stdout=f"编排任务启动中 - 时间：{timezone.now()}",
            start_time=timezone.now()
        )

        # 启动执行线程（改为管理Celery任务）
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
        """执行编排任务核心逻辑（改为Celery异步执行，支持超时停止+异常捕获）"""
        start_total_time = time.time()
        orch_log_stdout = [orch_log.stdout]
        orch_log_stderr = []
        task_ids = []  # 记录Celery任务ID

        try:
            for step in steps:
                # 检查编排任务是否已被手动停止
                current_log = OrchestrationLog.objects.get(id=orch_log.id)
                if current_log.exec_status == "stopped":
                    logger.info(f"编排任务{orch_log.id}已被手动停止，终止后续步骤执行")
                    break

                # 启动Celery异步任务执行当前步骤 → 增加异常捕获
                device_data = {
                    'id': device.id,
                    'adb_connect_str': device.adb_connect_str,
                    'device_name': device.device_name
                }
                try:
                    # 提交Celery任务 → 捕获连接错误（如Redis未启动）
                    task = execute_step_task.delay(step.id, orch_log.id, device_data)
                    task_id = task.id
                except Exception as celery_e:
                    # 记录Celery提交失败的错误，不中断整个编排任务
                    error_msg = f"提交Celery任务失败（步骤{step.execution_order}）：{str(celery_e)}"
                    logger.error(error_msg, exc_info=True)
                    orch_log_stderr.append(error_msg)
                    orch_log_stdout.append(f"\n=== 步骤{step.execution_order}执行失败 ===")
                    orch_log_stdout.append(error_msg)

                    # 标记该步骤为错误状态
                    step_log = StepExecutionLog.objects.create(
                        orchestration_log=orch_log,
                        step=step,
                        exec_status="error",
                        error_msg=error_msg,
                        start_time=timezone.now(),
                        end_time=timezone.now()
                    )

                    # 实时更新日志
                    orch_log.stdout = "\n".join(orch_log_stdout)
                    orch_log.stderr = "\n".join(orch_log_stderr)
                    orch_log.save()
                    continue  # 继续执行下一个步骤，不中断

                # 记录Celery任务ID（用于后续停止）
                with task_lock:
                    running_tasks[f"{orch_log.id}_{step.execution_order}"] = task_id
                task_ids.append((step.execution_order, task_id))

                # 生成进程KEY（和tasks保持一致）
                process_key = f"{orch_log.id}_{step.execution_order}"

                # 更新日志（保留原有输出格式）
                orch_log_stdout.append(f"\n=== 步骤{step.execution_order}开始执行 ===")
                orch_log_stdout.append(
                    f"执行命令：{step.script_task.python_path} {step.script_task.script_path} {device.adb_connect_str}")
                orch_log_stdout.append(f"工作目录：{os.path.dirname(step.script_task.script_path)}")
                orch_log_stdout.append(f"开始时间：{timezone.now()}")
                orch_log_stdout.append(f"Celery任务ID：{task_id}")
                orch_log_stdout.append(f"超时设置：{step.run_duration}秒")
                orch_log_stdout.append(f"Redis进程KEY：{process_key}")  # 新增：便于调试

                # 实时更新编排日志
                orch_log.stdout = "\n".join(orch_log_stdout)
                orch_log.save(update_fields=['stdout'])

                # 等待任务完成或超时（额外+10秒缓冲，确保超时后能及时处理）
                max_wait_time = step.run_duration + 10
                start_time = time.time()
                task_completed = False

                while time.time() - start_time < max_wait_time:
                    result = AsyncResult(task_id)
                    if result.ready():
                        # 任务已完成，获取执行结果
                        task_result = result.get()
                        step_log = StepExecutionLog.objects.filter(
                            orchestration_log=orch_log,
                            step=step
                        ).first()

                        if step_log:
                            # 保留原有日志输出格式
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
                        task_completed = True
                        break
                    time.sleep(1)

                # 处理任务超时未完成的情况
                if not task_completed:
                    orch_log_stdout.append(f"步骤{step.execution_order}任务未在规定时间内响应，标记为超时")
                    orch_log_stderr.append(f"步骤{step.execution_order}超时未响应：{step.run_duration + 10}秒内未完成")

                    # 强制标记步骤为超时
                    step_log = StepExecutionLog.objects.filter(
                        orchestration_log=orch_log,
                        step=step
                    ).first()
                    if step_log:
                        step_log.exec_status = "timeout"
                        step_log.error_msg = f"任务未在规定时间内完成（{step.run_duration}秒+10秒缓冲）"
                        step_log.end_time = timezone.now()
                        step_log.exec_duration = step.run_duration + 10
                        step_log.save()
                    else:
                        # 兼容步骤日志未创建的情况
                        step_log = StepExecutionLog.objects.create(
                            orchestration_log=orch_log,
                            step=step,
                            exec_status="timeout",
                            error_msg=f"任务未在规定时间内完成（{step.run_duration}秒+10秒缓冲）",
                            start_time=timezone.now(),
                            end_time=timezone.now(),
                            exec_duration=step.run_duration + 10
                        )

                    # 终止超时的Celery任务
                    with task_lock:
                        process_key = f"{orch_log.id}_{step.execution_order}"
                        if process_key in running_tasks:
                            try:
                                AsyncResult(running_tasks[process_key]).revoke(terminate=True)
                                # 从Redis终止进程并删除记录
                                self._kill_redis_process(process_key)
                                remove_running_process(process_key)
                            except Exception as revoke_e:
                                logger.error(f"终止Celery任务失败：{str(revoke_e)}")
                            del running_tasks[process_key]

                # 实时更新编排日志
                orch_log.stdout = "\n".join(orch_log_stdout)
                if orch_log_stderr:
                    orch_log.stderr = "\n".join(orch_log_stderr)
                orch_log.save()

            # 所有步骤处理完成（保留原有逻辑）
            orch_log.exec_duration = time.time() - start_total_time
            orch_log.end_time = timezone.now()

            # 检查失败步骤（保留原有逻辑）
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
            # 全局错误处理（保留原有逻辑）
            orch_log.exec_status = "failed"
            orch_log.error_msg = str(e)
            orch_log.stderr = "\n".join(orch_log_stderr) + f"\n\n编排任务全局错误：{str(e)}"
            orch_log.exec_duration = time.time() - start_total_time
            orch_log.end_time = timezone.now()

        finally:
            # 最终保存日志（保留原有逻辑）
            orch_log.stdout = "\n".join(orch_log_stdout)
            orch_log.stderr = "\n".join(orch_log_stderr)
            orch_log.save()

    def _kill_redis_process(self, process_key):
        """从Redis获取进程并终止"""
        process_info = get_running_process(process_key)
        if not process_info or not process_info.get("pid"):
            logger.warning(f"Redis中无进程信息，KEY={process_key}")
            return
        try:
            pid = process_info["pid"]
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                child.terminate()
            parent.terminate()
            time.sleep(1)
            if parent.is_running():
                parent.kill()
            logger.info(f"已终止Redis中记录的进程{pid}（KEY：{process_key}）")
        except Exception as e:
            logger.error(f"终止Redis进程失败（KEY：{process_key}）：{str(e)}")


# 新版执行页面（完全保留原有代码，仅修改_run_orchestration调用）
class OrchestrationExecuteView(View):
    def get(self, request):
        devices = ADBDevice.objects.filter(is_active=True)
        device_list = []
        for dev in devices:
            dev.status = dev.device_status
            device_list.append(dev)

        recent_logs = OrchestrationLog.objects.all().order_by("-id")[:10]
        for log in recent_logs:
            log.is_running = log.exec_status == "running"

        context = {
            "page_title": "执行编排任务",
            "devices": device_list,
            "orchestrations": OrchestrationTask.objects.filter(status="active"),
            "recent_logs": recent_logs
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

            # 校验设备（保留原有逻辑，这里是实例属性判断，不是ORM过滤，无需修改）
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

            # 执行每个设备（保留原有逻辑）
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
        """复用修改后的执行逻辑（Celery异步+异常捕获）"""
        ExecuteOrchestrationAPIView()._run_orchestration(orch_log, steps, device)


# 停止编排任务（增强版：支持Celery+Redis进程双终止）
class StopOrchestrationView(View):
    def get(self, request, log_id):
        try:
            orch_log = get_object_or_404(OrchestrationLog, id=log_id)
            if orch_log.exec_status != "running":
                error_msg = quote(f"编排任务未在运行中！当前状态：{orch_log.exec_status}")
                return redirect(f"{reverse('task_orchestration:execute_orchestration')}?msg={error_msg}")

            # 1. 停止所有相关Celery任务
            with task_lock:
                for key in list(running_tasks.keys()):
                    if key.startswith(f"{log_id}_"):
                        task_id = running_tasks[key]
                        try:
                            AsyncResult(task_id).revoke(terminate=True)  # 强制终止Celery任务
                            logger.info(f"已终止Celery任务{task_id}（编排日志{log_id}，步骤{key.split('_')[1]}）")
                        except Exception as e:
                            logger.error(f"终止Celery任务{task_id}失败：{str(e)}")
                        del running_tasks[key]

            # 2. 终止Redis中记录的进程（核心：解决异步进程无法终止问题）
            r = get_redis_conn()
            if r:
                for key in r.hkeys("orch_running_processes"):
                    if key.startswith(f"{log_id}_"):
                        self._kill_redis_process(key)
                        remove_running_process(key)

            # 3. 清理原有本地进程（兼容历史逻辑）
            with process_lock:
                for key in list(running_processes.keys()):
                    if key.startswith(f"{log_id}_"):
                        process_info = running_processes[key]
                        pid = process_info["pid"]
                        process = process_info["process"]

                        # 终止进程
                        try:
                            parent = psutil.Process(pid)
                            for child in parent.children(recursive=True):
                                child.terminate()
                            parent.terminate()
                            time.sleep(1)
                            if parent.is_running():
                                parent.kill()
                            logger.info(f"已终止本地进程{pid}（编排日志{log_id}，步骤{key.split('_')[1]}）")
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
            error_msg = quote(f"停止失败：{str(e)}")
            return redirect(f"{reverse('task_orchestration:execute_orchestration')}?msg={error_msg}")

    def _kill_redis_process(self, process_key):
        """从Redis获取进程并终止"""
        process_info = get_running_process(process_key)
        if not process_info or not process_info.get("pid"):
            logger.warning(f"Redis中无进程信息，KEY={process_key}")
            return
        try:
            pid = process_info["pid"]
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                child.terminate()
            parent.terminate()
            time.sleep(1)
            if parent.is_running():
                parent.kill()
            logger.info(f"已终止Redis中记录的进程{pid}（KEY：{process_key}）")
        except Exception as e:
            logger.error(f"终止Redis进程失败（KEY：{process_key}）：{str(e)}")


# 删除步骤（完全保留原有代码）
class StepDeleteView(View):
    def get(self, request, step_id):
        step = get_object_or_404(TaskStep, id=step_id)
        task_id = step.orchestration.id
        step.delete()
        return redirect(reverse("task_orchestration:edit_steps", args=[task_id]))


# 编排日志详情（完全保留原有代码）
class OrchestrationLogDetailView(View):
    def get(self, request, log_id):
        orch_log = get_object_or_404(OrchestrationLog, id=log_id)
        step_logs = orch_log.step_logs.all().order_by("step__execution_order")

        # 计算总耗时（保留原有逻辑）
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


# AJAX获取日志状态（完全保留原有代码）
# AJAX获取日志状态（修改step_data返回结构）
class OrchestrationLogStatusView(View):
    def get(self, request, log_id):
        try:
            orch_log = get_object_or_404(OrchestrationLog, id=log_id)
            step_logs = orch_log.step_logs.all().order_by("step__execution_order")
            step_data = []
            for sl in step_logs:
                step_data.append({
                    "step_log_id": sl.id,  # 新增：步骤日志ID（匹配前端元素ID）
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


# 以下为原有测试相关代码（保留）
from rest_framework.decorators import api_view
from rest_framework.response import Response
from mycelery.email.tasks import send_email, send_email2
from mycelery.main import app


def send_sms(request):
    return render(request, 'sms_send.html')


# 修正：兼容JSON和表单请求，加固参数校验
def send_sms_view(request):
    if request.method == 'POST':
        # 第一步：优先解析JSON请求体（axios发送的格式）
        try:
            # 解析JSON请求体
            request_data = json.loads(request.body)
            mobile = request_data.get('mobile', '').strip()
        except json.JSONDecodeError:
            # 解析失败则用表单格式
            mobile = request.POST.get('mobile', '').strip()

        # 加固：手机号校验
        if not mobile:
            return JsonResponse({'code': 400, 'msg': '手机号不能为空'})
        if not mobile.isdigit() or len(mobile) != 11:  # 新增11位校验
            return JsonResponse({'code': 400, 'msg': '手机号格式错误（需11位数字）'})

        # 异步调用任务（确保任务正常生成ID）
        try:
            task1 = send_email.delay(mobile)
            task2 = send_email2.delay(mobile)
        except Exception as e:
            return JsonResponse({'code': 500, 'msg': f'任务提交失败：{str(e)}'})

        # 确保返回的JSON包含task1_id/task2_id
        return JsonResponse({
            'code': 200,
            'msg': '任务已提交',
            'task1_id': task1.id,  # 明确返回task1_id
            'task2_id': task2.id,
            'mobile': mobile  # 新增：返回手机号，方便前端验证
        })
    return JsonResponse({'code': 405, 'msg': '仅支持POST请求'})


# 保持查询接口不变
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


# 在现有视图下方添加
class OrchestrationCloneView(View):
    def get(self, request, task_id):
        """显示克隆任务确认页面"""
        original_task = get_object_or_404(OrchestrationTask, id=task_id)
        return render(request, "task_orchestration/clone.html", {
            "original_task": original_task,
            "page_title": f"克隆任务 - {original_task.name}"
        })

    def post(self, request, task_id):
        """执行任务克隆操作"""
        original_task = get_object_or_404(OrchestrationTask, id=task_id)

        # 获取新任务名称
        new_task_name = request.POST.get("new_task_name")
        if not new_task_name:
            return render(request, "task_orchestration/clone.html", {
                "original_task": original_task,
                "page_title": f"克隆任务 - {original_task.name}",
                "error_msg": "新任务名称不能为空"
            })

        # 检查名称唯一性
        if OrchestrationTask.objects.filter(name=new_task_name).exists():
            return render(request, "task_orchestration/clone.html", {
                "original_task": original_task,
                "page_title": f"克隆任务 - {original_task.name}",
                "error_msg": "任务名称已存在，请使用其他名称"
            })

        # 创建新任务
        new_task = OrchestrationTask.objects.create(
            name=new_task_name,
            description=original_task.description,
            status="draft",  # 克隆任务默认为草稿状态
            create_time=timezone.now(),
            update_time=timezone.now()
        )

        # 复制所有子任务步骤
        original_steps = original_task.steps.all().order_by("execution_order")
        for step in original_steps:
            TaskStep.objects.create(
                orchestration=new_task,
                script_task=step.script_task,
                execution_order=step.execution_order,
                run_duration=step.run_duration,
                create_time=timezone.now()
            )

        # 克隆成功后跳转到编辑页面
        return redirect(reverse("task_orchestration:edit_steps", args=[new_task.id]) + "?msg=任务克隆成功")