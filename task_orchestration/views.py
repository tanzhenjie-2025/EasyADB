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

# 全局进程存储
running_processes = {}
process_lock = threading.Lock()


# Redis连接（和脚本保持一致）
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


from adb_manager.models import ADBDevice
from .models import OrchestrationTask, TaskStep, OrchestrationLog, StepExecutionLog
from .forms import OrchestrationTaskForm, TaskStepForm
from script_center.models import ScriptTask, TaskExecutionLog
from script_center.views import running_processes as script_running_processes, process_lock as script_process_lock


# 编排任务列表
class OrchestrationListView(View):
    def get(self, request):
        tasks = OrchestrationTask.objects.all()
        return render(request, "task_orchestration/list.html", {
            "tasks": tasks,
            "page_title": "编排任务管理"
        })


# 创建编排任务
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


# 编辑子任务步骤
class StepEditView(View):
    def get(self, request, task_id):
        orchestration = get_object_or_404(OrchestrationTask, id=task_id)
        steps = orchestration.steps.all().order_by("execution_order")
        task_form = OrchestrationTaskForm(instance=orchestration)
        step_form = TaskStepForm()

        return render(request, "task_orchestration/edit_steps.html", {
            "orchestration": orchestration,
            "steps": steps,
            "task_form": task_form,
            "step_form": step_form,
            "page_title": f"编辑步骤 - {orchestration.name}"
        })

    def post(self, request, task_id):
        orchestration = get_object_or_404(OrchestrationTask, id=task_id)
        steps = orchestration.steps.all().order_by("execution_order")

        if "action" in request.POST and request.POST["action"] == "update_task":
            task_form = OrchestrationTaskForm(request.POST, instance=orchestration)
            step_form = TaskStepForm()
            if task_form.is_valid():
                task_form.save()
                return redirect(f"{reverse('task_orchestration:edit_steps', args=[task_id])}?msg=状态修改成功")
        else:
            task_form = OrchestrationTaskForm(instance=orchestration)
            step_form = TaskStepForm(request.POST)
            if step_form.is_valid():
                step = step_form.save(commit=False)
                step.orchestration = orchestration
                step.save()
                return redirect(f"{reverse('task_orchestration:edit_steps', args=[task_id])}?msg=步骤添加成功")

        return render(request, "task_orchestration/edit_steps.html", {
            "orchestration": orchestration,
            "steps": steps,
            "task_form": task_form,
            "step_form": step_form,
            "page_title": f"编辑步骤 - {orchestration.name}",
            "error_msg": "表单填写有误，请检查"
        })


# 旧版执行接口
class ExecuteOrchestrationAPIView(View):
    def post(self, request, task_id):
        orchestration = get_object_or_404(OrchestrationTask, id=task_id, status="active")
        steps = orchestration.steps.order_by("execution_order").all()
        if not steps:
            return JsonResponse({
                "status": "error",
                "msg": "该编排任务没有子任务步骤"
            })

        device = ADBDevice.objects.filter(device_status="online").first()
        if not device:
            return JsonResponse({
                "status": "error",
                "msg": "无可用在线设备"
            })

        # 创建编排日志（补充初始命令）
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
        """执行编排任务核心逻辑（带详细日志）"""
        start_total_time = time.time()
        orch_log_stdout = [orch_log.stdout]
        orch_log_stderr = []

        try:
            for step in steps:
                # 创建步骤日志
                step_log = StepExecutionLog.objects.create(
                    orchestration_log=orch_log,
                    step=step,
                    exec_status="running",
                    start_time=timezone.now()
                )
                orch_log.completed_steps += 1
                orch_log.save()

                # 获取脚本任务
                script_task = step.script_task
                if not hasattr(script_task, 'is_script_exists') or not script_task.is_script_exists():
                    error_msg = f"脚本文件不存在：{script_task.script_path if script_task else '未知路径'}"
                    step_log.exec_status = "error"
                    step_log.error_msg = error_msg
                    step_log.end_time = timezone.now()
                    step_log.save()
                    orch_log_stderr.append(f"步骤{step.execution_order}：{error_msg}")
                    continue

                # 处理Python路径（兼容WindowsApps）
                real_python_path = None
                if "WindowsApps" in script_task.python_path:
                    possible_paths = [
                        r"C:\Python311\python.exe",
                        r"C:\Users\TanZhenJie\AppData\Local\Programs\Python\Python311\python.exe",
                        os.path.expanduser("~\\AppData\\Local\\Programs\\Python\\Python311\\python.exe"),
                        r"C:\Users\谭振捷\AppData\Local\Programs\Python\Python311\python.exe",
                        r"C:\Program Files\Python311\python.exe"
                    ]
                    for path in possible_paths:
                        if os.path.exists(path):
                            real_python_path = path
                            break
                    if not real_python_path:
                        real_python_path = script_task.python_path

                # 构建执行命令
                script_dir = os.path.dirname(script_task.script_path)
                command = f'"{real_python_path}" -X utf8 "{script_task.script_path}" "{device.adb_connect_str}"'
                step_log.exec_command = command
                step_log.save()

                orch_log_stdout.append(f"\n=== 步骤{step.execution_order}开始执行 ===")
                orch_log_stdout.append(f"执行命令：{command}")
                orch_log_stdout.append(f"工作目录：{script_dir}")
                orch_log_stdout.append(f"开始时间：{timezone.now()}")

                # 构建环境变量
                env = os.environ.copy()
                env.update({
                    'PYTHONIOENCODING': 'utf-8',
                    'PYTHONLEGACYWINDOWSSTDIO': 'utf-8',
                    'LC_ALL': 'en_US.UTF-8',
                    'LANG': 'en_US.UTF-8'
                })

                process = None
                step_start_time = time.time()
                try:
                    # 启动进程
                    process = subprocess.Popen(
                        command,
                        shell=True,
                        cwd=script_dir,
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        encoding="utf-8",
                        errors="replace"
                    )

                    # 记录进程
                    with process_lock:
                        running_processes[f"{orch_log.id}_{step.execution_order}"] = {
                            "pid": process.pid,
                            "device_serial": device.adb_connect_str,
                            "process": process,
                            "step": step.execution_order,
                            "log_id": orch_log.id
                        }

                    # 等待执行/超时
                    stdout, stderr = process.communicate(timeout=step.run_duration)
                    return_code = process.returncode

                    # 记录步骤日志
                    step_log.stdout = stdout
                    step_log.stderr = stderr
                    step_log.return_code = return_code
                    step_log.exec_duration = time.time() - step_start_time

                    if return_code == 0:
                        step_log.exec_status = "completed"
                        orch_log_stdout.append(f"步骤{step.execution_order}执行成功，返回码：{return_code}")
                    else:
                        step_log.exec_status = "failed"
                        step_log.error_msg = f"执行失败，返回码：{return_code}"
                        orch_log_stderr.append(f"步骤{step.execution_order}执行失败：{stderr}")

                except subprocess.TimeoutExpired:
                    # 超时处理
                    if process:
                        process.terminate()
                    step_log.exec_status = "timeout"
                    step_log.error_msg = f"执行超时（{step.run_duration}秒）"
                    step_log.exec_duration = step.run_duration
                    orch_log_stdout.append(f"步骤{step.execution_order}执行超时（{step.run_duration}秒）")
                    orch_log_stderr.append(f"步骤{step.execution_order}超时：强制终止进程")

                except Exception as e:
                    # 其他错误
                    if process:
                        process.terminate()
                    error_detail = f"""【异常信息】
类型：{type(e).__name__}
描述：{str(e)}
完整栈：{logging.Formatter().formatException(sys.exc_info())}"""
                    step_log.exec_status = "error"
                    step_log.error_msg = error_detail
                    step_log.stderr = error_detail
                    orch_log_stderr.append(f"步骤{step.execution_order}系统错误：{str(e)}")

                finally:
                    # 清理进程
                    with process_lock:
                        key = f"{orch_log.id}_{step.execution_order}"
                        if key in running_processes:
                            del running_processes[key]

                    # 更新步骤日志
                    step_log.end_time = timezone.now()
                    step_log.save()

                    # 更新编排日志实时输出
                    orch_log.stdout = "\n".join(orch_log_stdout)
                    if orch_log_stderr:
                        orch_log.stderr = "\n".join(orch_log_stderr)
                    orch_log.save()

            # 所有步骤完成
            orch_log.exec_status = "completed"
            orch_log.exec_duration = time.time() - start_total_time
            orch_log.end_time = timezone.now()

            # 检查失败步骤
            failed_steps = StepExecutionLog.objects.filter(
                orchestration_log=orch_log,
                exec_status__in=["failed", "timeout", "error"]
            ).count()
            if failed_steps > 0:
                orch_log.exec_status = "part_failed"
                orch_log.error_msg = f"{failed_steps}个步骤执行异常（超时/失败/错误）"
                orch_log.stderr = "\n".join(orch_log_stderr)

        except Exception as e:
            # 编排任务级错误
            orch_log.exec_status = "failed"
            orch_log.error_msg = str(e)
            orch_log.stderr = "\n".join(orch_log_stderr) + f"\n\n编排任务全局错误：{str(e)}"
            orch_log.exec_duration = time.time() - start_total_time
            orch_log.end_time = timezone.now()

        finally:
            # 最终保存编排日志
            orch_log.save()


# 新版执行页面
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
        """复用旧版的详细执行逻辑"""
        ExecuteOrchestrationAPIView()._run_orchestration(orch_log, steps, device)


# 停止编排任务
class StopOrchestrationView(View):
    def get(self, request, log_id):
        try:
            orch_log = get_object_or_404(OrchestrationLog, id=log_id)
            if orch_log.exec_status != "running":
                error_msg = quote(f"编排任务未在运行中！当前状态：{orch_log.exec_status}")
                return redirect(f"{reverse('task_orchestration:execute_orchestration')}?msg={error_msg}")

            # 停止所有步骤进程
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
                        except:
                            pass

                        if process and process.poll() is None:
                            process.kill()
                        del running_processes[key]

            # 更新日志
            orch_log.exec_status = "stopped"
            orch_log.stderr = f"{orch_log.stderr}\n\n任务已手动停止 - 时间：{timezone.now()}"
            orch_log.end_time = timezone.now()
            orch_log.save()

            # 更新步骤日志
            StepExecutionLog.objects.filter(
                orchestration_log=orch_log,
                exec_status="running"
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


# 删除步骤
class StepDeleteView(View):
    def get(self, request, step_id):
        step = get_object_or_404(TaskStep, id=step_id)
        task_id = step.orchestration.id
        step.delete()
        return redirect(reverse("task_orchestration:edit_steps", args=[task_id]))


# 编排日志详情（带详细输出）
class OrchestrationLogDetailView(View):
    def get(self, request, log_id):
        orch_log = get_object_or_404(OrchestrationLog, id=log_id)
        step_logs = orch_log.step_logs.all().order_by("step__execution_order")

        # 计算总耗时
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


# AJAX获取日志状态
class OrchestrationLogStatusView(View):
    def get(self, request, log_id):
        try:
            orch_log = get_object_or_404(OrchestrationLog, id=log_id)
            step_logs = orch_log.step_logs.all().order_by("step__execution_order")
            step_data = []
            for sl in step_logs:
                step_data.append({
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