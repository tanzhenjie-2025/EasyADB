from pathlib import Path

from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.urls import reverse
from urllib.parse import quote
from django.http import JsonResponse, HttpResponse
import logging
import os
import subprocess
import threading
import time
import sys
import locale
import signal
import psutil
import redis
import json
from datetime import datetime
from django.utils import timezone
from celery.result import AsyncResult
import django.db.models as models

from .tasks import execute_script_task, _graceful_terminate_process
from .models import ScriptTask, TaskExecutionLog, ScriptTaskManagementLog
from .forms import ScriptTaskForm
from adb_manager.models import ADBDevice

from django.conf import settings

def scan_builtin_scripts():
    """扫描内置脚本目录，返回脚本列表 [(脚本名称, 脚本绝对路径), ...]"""
    scripts = []
    builtin_dir = Path(settings.BUILTIN_SCRIPTS_DIR)

    if not builtin_dir.exists():
        return scripts

    # 遍历目录下所有 .air 文件夹（Airtest 脚本是文件夹结构）
    for air_dir in builtin_dir.iterdir():
        if air_dir.is_dir() and air_dir.suffix == '.air':
            # 查找 .air 目录下与文件夹同名的 .py 主文件
            py_file = air_dir / f"{air_dir.stem}.py"
            if py_file.exists():
                scripts.append((air_dir.stem, str(py_file.resolve())))

    return scripts

def get_env_config(key, default=None, cast_type=str):
    value = os.getenv(key, default)
    if value is None:
        return default
    try:
        if cast_type == int:
            return int(value)
        elif cast_type == bool:
            return value.lower() == "true"
        return cast_type(value)
    except (ValueError, TypeError):
        logger.error(f"配置 {key} 转换失败，使用默认值 {default}")
        return default


LOG_FILE = get_env_config("SCRIPT_LOG_FILE", "script_execution.log")
logger = logging.getLogger(__name__)
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


def get_redis_conn():
    try:
        r = redis.Redis(
            host=get_env_config("REDIS_HOST", "127.0.0.1"),
            port=get_env_config("REDIS_PORT", 6379, int),
            db=get_env_config("REDIS_DB", 0, int),
            decode_responses=True,
            socket_timeout=5
        )
        r.ping()
        return r
    except Exception as e:
        logger.error(f"Redis连接失败：{str(e)}")
        return None


def save_celery_task(log_id, celery_task_id):
    r = get_redis_conn()
    if r:
        try:
            r.hset("script_running_tasks", log_id, celery_task_id)
            logger.info(f"已保存Celery任务ID到Redis - 日志ID：{log_id}，任务ID：{celery_task_id}")
        except Exception as e:
            logger.error(f"保存Celery任务ID失败：{str(e)}")


def get_celery_task(log_id):
    r = get_redis_conn()
    if r:
        try:
            return r.hget("script_running_tasks", log_id)
        except Exception as e:
            logger.error(f"获取Celery任务ID失败：{str(e)}")
    return None


def delete_celery_task(log_id):
    r = get_redis_conn()
    if r:
        try:
            r.hdel("script_running_tasks", log_id)
            logger.info(f"已删除Redis中的Celery任务ID - 日志ID：{log_id}")
        except Exception as e:
            logger.error(f"删除Celery任务ID失败：{str(e)}")


def send_redis_stop_signal(device_serial, log_id):
    r = get_redis_conn()
    if r and device_serial:
        expire_seconds = get_env_config("SCRIPT_REDIS_STOP_FLAG_EXPIRE", 60, int)
        try:
            r.set(f"airtest_stop_flag_{device_serial}", "True", ex=expire_seconds)
            logger.info(f"已发送Redis停止信号 - 设备：{device_serial}，日志ID：{log_id}，有效期：{expire_seconds}秒")
        except Exception as e:
            logger.error(f"发送Redis停止信号失败：{str(e)}")


def format_duration(duration):
    if duration:
        return f"{duration:.2f}秒"
    return "未知"


def get_python_warning(python_path):
    warning_keyword = get_env_config("SCRIPT_PYTHON_WARNING_KEYWORD", "WindowsApps")
    if warning_keyword in python_path:
        return "（注意：Python路径为WindowsApps快捷方式，已自动替换为真实路径）"
    return ""


def get_recent_logs():
    limit = get_env_config("SCRIPT_RECENT_LOGS_LIMIT", 10, int)
    return TaskExecutionLog.objects.all().order_by("-id")[:limit]


class TaskListView(View):
    def get(self, request):
        search_query = request.GET.get('search', '').strip()
        tasks_query = ScriptTask.objects.all()
        if search_query:
            tasks_query = tasks_query.filter(task_name__icontains=search_query)
        tasks = tasks_query
        context = {
            "page_title": "脚本任务管理",
            "tasks": tasks,
            "search_query": search_query
        }
        return render(request, "script_center/task_list.html", context)


class TaskAddView(View):
    """新增任务：默认填充当前Django Python解释器路径"""
    def get(self, request):
        # 初始值设为当前Python解释器路径
        form = ScriptTaskForm(initial={'python_path': sys.executable})
        # 获取内置脚本列表
        builtin_scripts = scan_builtin_scripts()
        context = {
            "page_title": "新增脚本任务",
            "form": form,
            "default_python_path": sys.executable,
            "builtin_scripts": builtin_scripts  # 传给前端
        }
        return render(request, "script_center/task_form.html", context)

    def post(self, request):
        form = ScriptTaskForm(request.POST)
        if form.is_valid():
            task = form.save()
            ScriptTaskManagementLog.objects.create(
                task=task,
                operation="create",
                operator=request.user.username if request.user.is_authenticated else "匿名用户",
                details=f"创建了任务：{task.task_name}，Python路径：{task.python_path or '默认路径'}，脚本路径：{task.script_path}，任务状态：{task.status}"
            )
            success_msg = quote(f"任务【{task.task_name}】创建成功！")
            return redirect(f"{reverse('script_center:task_list')}?msg={success_msg}")
        context = {
            "page_title": "新增脚本任务",
            "form": form,
            "default_python_path": sys.executable,
            "builtin_scripts": scan_builtin_scripts(),  # 报错时重新传
            "error_msg": "表单填写有误，请检查！"
        }
        return render(request, "script_center/task_form.html", context)


class TaskEditView(View):
    """编辑任务：若原Python路径为空则填充默认值"""
    def get(self, request, task_id):
        task = get_object_or_404(ScriptTask, id=task_id)
        initial = {}
        if not task.python_path:
            initial['python_path'] = sys.executable
        form = ScriptTaskForm(instance=task, initial=initial)
        # 获取内置脚本列表
        builtin_scripts = scan_builtin_scripts()
        context = {
            "page_title": f"编辑任务 - {task.task_name}",
            "form": form,
            "task": task,
            "default_python_path": sys.executable,
            "builtin_scripts": builtin_scripts  # 传给前端
        }
        return render(request, "script_center/task_form.html", context)

    def post(self, request, task_id):
        task = get_object_or_404(ScriptTask, id=task_id)
        old_task_name = task.task_name
        old_script_path = task.script_path
        old_python_path = task.python_path
        old_status = task.status

        form = ScriptTaskForm(request.POST, instance=task)
        if form.is_valid():
            updated_task = form.save()
            details = []
            if old_task_name != updated_task.task_name:
                details.append(f"任务名称从 '{old_task_name}' 修改为 '{updated_task.task_name}'")
            if old_script_path != updated_task.script_path:
                details.append(f"脚本路径从 '{old_script_path}' 修改为 '{updated_task.script_path}'")
            if old_python_path != updated_task.python_path:
                details.append(f"Python路径从 '{old_python_path or '默认路径'}' 修改为 '{updated_task.python_path or '默认路径'}'")
            if old_status != updated_task.status:
                details.append(f"任务状态从 '{old_status}' 修改为 '{updated_task.status}'")

            ScriptTaskManagementLog.objects.create(
                task=updated_task,
                operation="edit",
                operator=request.user.username if request.user.is_authenticated else "匿名用户",
                details="；".join(details) if details else f"编辑了任务 '{updated_task.task_name}'，未修改关键信息"
            )
            success_msg = quote(f"任务【{updated_task.task_name}】修改成功！")
            return redirect(f"{reverse('script_center:task_list')}?msg={success_msg}")
        context = {
            "page_title": f"编辑任务 - {task.task_name}",
            "form": form,
            "task": task,
            "default_python_path": sys.executable,
            "builtin_scripts": scan_builtin_scripts(),  # 报错时重新传
            "error_msg": "表单填写有误，请检查！"
        }
        return render(request, "script_center/task_form.html", context)


class TaskDeleteView(View):
    def post(self, request, task_id):
        try:
            task = get_object_or_404(ScriptTask, id=task_id)
            task_name = task.task_name
            ScriptTaskManagementLog.objects.create(
                task=task,
                operation="delete",
                operator=request.user.username if request.user.is_authenticated else "匿名用户",
                details=f"删除了任务：{task_name}，脚本路径：{task.script_path}，Python路径：{task.python_path or '默认路径'}"
            )
            task.delete()
            success_msg = quote(f"任务【{task_name}】删除成功！")
        except Exception as e:
            logger.error(f"删除任务失败：{str(e)}")
            success_msg = quote(f"删除失败：{str(e)}")
        return redirect(f"{reverse('script_center:task_list')}?msg={success_msg}")


class TaskManagementLogView(View):
    def get(self, request):
        search_query = request.GET.get('search', '').strip()
        logs_query = ScriptTaskManagementLog.objects.all().order_by("-operation_time")
        if search_query:
            logs_query = logs_query.filter(
                models.Q(task__task_name__icontains=search_query) |
                models.Q(operator__icontains=search_query) |
                models.Q(details__icontains=search_query) |
                models.Q(operation__icontains=search_query)
            )
        context = {
            "page_title": "脚本任务管理日志",
            "logs": logs_query,
            "search_query": search_query
        }
        return render(request, "script_center/management_log.html", context)


class ExecuteTaskView(View):
    """执行任务：若任务未填Python路径则使用默认解释器"""
    def get(self, request):
        search_query = request.GET.get('search', '').strip()
        devices = ADBDevice.objects.filter(is_active=True)
        device_list = []
        for dev in devices:
            dev.status = dev.device_status
            device_list.append(dev)

        recent_logs = get_recent_logs()
        logger.info(f"最近执行日志数量：{recent_logs.count()}")

        tasks_query = ScriptTask.objects.filter(status="active")
        if search_query:
            tasks_query = tasks_query.filter(task_name__icontains=search_query)
        tasks = tasks_query

        for log in recent_logs:
            celery_task_id = get_celery_task(log.id)
            log.is_running = celery_task_id is not None and log.exec_status == "running"
            logger.info(f"日志ID：{log.id}，状态：{log.exec_status}，是否运行中：{log.is_running}")

        context = {
            "page_title": "执行脚本任务",
            "devices": device_list,
            "tasks": tasks,
            "recent_logs": recent_logs,
            "search_query": search_query
        }
        return render(request, "script_center/execute_task.html", context)

    def post(self, request):
        try:
            device_ids = request.POST.getlist("device_ids")
            task_id = request.POST.get("task_id")
            logger.info(f"接收到执行请求 - 设备ID：{device_ids}，任务ID：{task_id}")

            if not device_ids or not task_id:
                error_msg = quote("请选择至少一个设备和一个任务！")
                logger.warning(error_msg)
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            task = get_object_or_404(ScriptTask, id=task_id, status="active")
            # 核心修改：若任务未填Python路径，使用当前Django解释器
            python_path = task.python_path or sys.executable
            logger.info(f"获取到任务：{task.task_name}，脚本路径：{task.script_path}，Python路径：{python_path}")

            if not task.is_script_exists():
                error_msg = quote(f"任务脚本不存在：{task.script_path}")
                logger.error(error_msg)
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            offline_devices = []
            valid_device_ids = []
            for device_id in device_ids:
                device = get_object_or_404(ADBDevice, id=device_id)
                logger.info(f"检查设备状态 - ID：{device_id}，名称：{device.device_name}，状态：{device.device_status}")
                if device.device_status != "online":
                    offline_devices.append(device.device_name)
                else:
                    valid_device_ids.append(device_id)

            if not valid_device_ids:
                error_msg = quote(f"所选设备均离线！离线设备：{','.join(offline_devices)}")
                logger.error(error_msg)
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            python_warning = get_python_warning(python_path)
            for device_id in valid_device_ids:
                device = get_object_or_404(ADBDevice, id=device_id)
                log = TaskExecutionLog.objects.create(
                    task=task,
                    device=device,
                    exec_status="running",
                    exec_command=f"准备执行：{python_path} {task.script_path} {device.adb_connect_str}",
                    stdout=f"任务启动中（Celery异步执行）{python_warning}",
                    start_time=timezone.now()
                )
                logger.info(f"创建执行日志 - ID：{log.id}，设备：{device.device_name}")

                try:
                    # 注意：需确保tasks.py中的execute_script_task也接收并使用python_path参数
                    celery_task = execute_script_task.delay(task.id, device.id, log.id, python_path)
                    save_celery_task(log.id, celery_task.id)
                    logger.info(f"提交Celery任务 - 日志ID：{log.id}，任务ID：{celery_task.id}")
                except Exception as celery_err:
                    log.exec_status = "error"
                    log.stderr = f"Celery任务提交失败：{str(celery_err)}"
                    log.end_time = timezone.now()
                    log.save()
                    logger.error(f"Celery任务提交失败 - 日志ID：{log.id}，错误：{str(celery_err)}")

            success_msg = quote(
                f"任务【{task.task_name}】已启动！共{len(valid_device_ids)}个在线设备执行中{python_warning}")
            if offline_devices:
                success_msg = quote(f"{success_msg}（离线设备已过滤：{','.join(offline_devices)}）")
            logger.info(success_msg)
            return redirect(f"{reverse('script_center:execute_task')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"执行任务失败：{str(e)}", exc_info=True)
            error_msg = quote(f"执行任务失败：{str(e)}")
            return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")


class StopTaskView(View):
    def get(self, request, log_id):
        try:
            logger.info(f"接收到停止任务请求 - 日志ID：{log_id}")
            log = get_object_or_404(TaskExecutionLog, id=log_id)
            if log.exec_status != "running":
                error_msg = quote(f"任务【{log.task.task_name}】未在运行中！当前状态：{log.exec_status}")
                logger.warning(error_msg)
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            celery_task_id = get_celery_task(log_id)
            if not celery_task_id:
                error_msg = quote(f"未找到任务【{log.task.task_name}】的Celery执行记录！")
                logger.warning(error_msg)
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            r = get_redis_conn()
            device_serial = None
            if r:
                process_info_str = r.hget("script_running_processes", log_id)
                if process_info_str:
                    process_info = json.loads(process_info_str)
                    device_serial = process_info.get("device_serial")
                    send_redis_stop_signal(device_serial, log_id)

            AsyncResult(celery_task_id).revoke(terminate=False)
            delete_celery_task(log_id)
            logger.info(f"已发送Celery停止信号 - ID：{celery_task_id}，日志ID：{log_id}")

            stop_wait_time = get_env_config("SCRIPT_STOP_WAIT_TIME", 8, int)
            time.sleep(stop_wait_time)

            if r and process_info_str:
                process_info = json.loads(process_info_str)
                pid = process_info.get("pid")
                if pid:
                    try:
                        if psutil.pid_exists(pid):
                            terminate_wait = get_env_config("SCRIPT_PROCESS_TERMINATE_WAIT", 3, int)
                            _graceful_terminate_process(pid, wait_time=terminate_wait)
                            logger.info(f"已优雅终止进程{pid} - 设备：{device_serial}，等待时间：{terminate_wait}秒")
                        else:
                            logger.info(f"进程{pid}已自行退出（优雅退出成功）")
                    except Exception as e:
                        logger.error(f"终止进程{pid}失败：{str(e)}")
                    r.hdel("script_running_processes", log_id)
                    if device_serial:
                        r.delete(f"airtest_stop_flag_{device_serial}")

            log.exec_status = "stopped"
            log.stderr = f"""任务已手动停止（优雅退出）
- 停止时间：{timezone.now()}
- 设备序列号：{device_serial or '未知'}
- Celery任务ID：{celery_task_id}
- 终止日志ID：{log_id}
- 已等待{stop_wait_time}秒让脚本完成清理和日志输出"""
            log.end_time = timezone.now()
            log.save()

            success_msg = quote(f"任务【{log.task.task_name}】已发送停止信号，脚本已优雅退出！")
            logger.info(success_msg)
            return redirect(f"{reverse('script_center:execute_task')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"停止任务失败：{str(e)}", exc_info=True)
            error_msg = quote(f"停止任务失败：{str(e)}")
            return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")


class LogDetailView(View):
    def get(self, request, log_id):
        log = get_object_or_404(TaskExecutionLog, id=log_id)
        log.exec_duration_str = format_duration(log.exec_duration)
        context = {
            "page_title": f"执行日志 - {log.task.task_name}",
            "log": log
        }
        return render(request, "script_center/log_detail.html", context)


class LogStatusView(View):
    def get(self, request, log_id):
        try:
            log = get_object_or_404(TaskExecutionLog, id=log_id)
            celery_task_id = get_celery_task(log.id)
            is_running = celery_task_id is not None and log.exec_status == "running"
            return JsonResponse({
                "code": 200,
                "status": log.exec_status,
                "stdout": log.stdout,
                "stderr": log.stderr,
                "duration": log.exec_duration,
                "is_running": is_running
            })
        except Exception as e:
            return JsonResponse({
                "code": 500,
                "msg": str(e)
            })