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

from .tasks import (
    execute_script_task,
    _graceful_terminate_process,
    execute_script_sync
)
from .models import ScriptTask, TaskExecutionLog, ScriptTaskManagementLog
from .forms import ScriptTaskForm
from adb_manager.models import ADBDevice

from django.conf import settings

import mimetypes
from django.http import FileResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt


# === 内置脚本库相关视图 ===
from django import forms
from .models import BuiltinScript, ScriptParameter, ScriptTask, TaskExecutionLog
from adb_manager.models import ADBDevice
from django.utils import timezone
import sys

# 在文件顶部的 import 区域添加
from django.views.decorators.http import etag
from django.utils.cache import patch_response_headers
import hashlib

from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
# ====================== 优化：全局 Redis 连接池 ======================
REDIS_POOL = None


def scan_builtin_scripts():
    """扫描内置脚本目录，返回脚本列表 [(脚本名称, 脚本绝对路径), ...]"""
    # 新增：检查 settings 配置，如果设置为不显示，直接返回空列表
    if not getattr(settings, 'SHOW_BUILTIN_SCRIPTS', True):
        return []

    scripts = []
    builtin_dir = Path(settings.BUILTIN_SCRIPTS_DIR)

    if not builtin_dir.exists():
        return scripts

    for air_dir in builtin_dir.iterdir():
        if air_dir.is_dir() and air_dir.suffix == '.air':
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
    """优化：使用连接池"""
    global REDIS_POOL
    if REDIS_POOL is None:
        REDIS_POOL = redis.ConnectionPool(
            host=get_env_config("REDIS_HOST", "127.0.0.1"),
            port=get_env_config("REDIS_PORT", 6379, int),
            db=get_env_config("REDIS_DB", 0, int),
            decode_responses=True,
            socket_timeout=2
        )
    try:
        r = redis.Redis(connection_pool=REDIS_POOL)
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
    """优化：select_related 解决 N+1"""
    limit = get_env_config("SCRIPT_RECENT_LOGS_LIMIT", 10, int)
    return TaskExecutionLog.objects.select_related('task', 'device').order_by("-id")[:limit]


class TaskListView(View):
    def get(self, request):
        search_query = request.GET.get('search', '').strip()
        # 优化：only() 只查需要的字段
        tasks_query = ScriptTask.objects.only('id', 'task_name', 'status', 'script_path', 'python_path', 'create_time')
        if search_query:
            tasks_query = tasks_query.filter(task_name__icontains=search_query)

        # ====================== 新增：分页逻辑 ======================
        page_size = get_env_config("SCRIPT_TASK_LIST_PAGE_SIZE", 10, int)
        paginator = Paginator(tasks_query, page_size)
        page = request.GET.get('page')

        try:
            tasks = paginator.page(page)
        except PageNotAnInteger:
            # 如果 page 不是整数，显示第一页
            tasks = paginator.page(1)
        except EmptyPage:
            # 如果 page 超出范围，显示最后一页
            tasks = paginator.page(paginator.num_pages)

        context = {
            "page_title": "脚本任务管理",
            "tasks": tasks,
            "search_query": search_query,
            # 新增：分页专用变量
            "page_obj": tasks,
            "is_paginated": tasks.has_other_pages(),
            "paginator": paginator
        }
        return render(request, "script_center/task_list.html", context)

class TaskAddView(View):
    """新增任务：默认填充当前Django Python解释器路径"""

    def get(self, request):
        form = ScriptTaskForm(initial={'python_path': sys.executable})
        builtin_scripts = scan_builtin_scripts()
        context = {
            "page_title": "新增脚本任务",
            "form": form,
            "default_python_path": sys.executable,
            "builtin_scripts": builtin_scripts
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
            "builtin_scripts": scan_builtin_scripts(),
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
        builtin_scripts = scan_builtin_scripts()
        context = {
            "page_title": f"编辑任务 - {task.task_name}",
            "form": form,
            "task": task,
            "default_python_path": sys.executable,
            "builtin_scripts": builtin_scripts
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
                details.append(
                    f"Python路径从 '{old_python_path or '默认路径'}' 修改为 '{updated_task.python_path or '默认路径'}'")
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
            "builtin_scripts": scan_builtin_scripts(),
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
        # 优化：select_related 解决 N+1
        logs_query = ScriptTaskManagementLog.objects.select_related('task').all().order_by("-operation_time")
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
    """执行任务：优先 Celery 异步，失败则降级到后台线程同步"""

    def get(self, request):
        search_query = request.GET.get('search', '').strip()

        # 修复：only() 中只包含真实的数据库字段，去掉 'device_status'
        # 因为 device_status 是 @property，不是数据库列
        devices = ADBDevice.objects.filter(is_active=True).only('id', 'device_name', 'device_ip', 'device_port',
                                                                'device_serial')
        device_list = []
        for dev in devices:
            # 这里可以正常调用 @property dev.device_status
            dev.status = dev.device_status
            device_list.append(dev)

        # ====================== 新增：分页获取最近执行日志 ======================
        page = request.GET.get('page', 1)
        page_size = get_env_config("SCRIPT_RECENT_LOGS_LIMIT", 10, int)
        logs_queryset = TaskExecutionLog.objects.select_related('task', 'device').order_by("-id")
        paginator = Paginator(logs_queryset, page_size)

        try:
            recent_logs = paginator.page(page)
        except PageNotAnInteger:
            # 如果 page 不是整数，显示第一页
            recent_logs = paginator.page(1)
        except EmptyPage:
            # 如果 page 超出范围，显示最后一页
            recent_logs = paginator.page(paginator.num_pages)

        logger.info(f"最近执行日志总数：{logs_queryset.count()}，当前页：{recent_logs.number}")

        # 优化：only() 只查需要的字段
        tasks_query = ScriptTask.objects.filter(status="active").only('id', 'task_name', 'script_path', 'python_path')
        if search_query:
            tasks_query = tasks_query.filter(task_name__icontains=search_query)
        tasks = tasks_query

        # 修改：兼容同步：只要状态是 running 就算运行中
        for log in recent_logs:
            log.is_running = log.exec_status == "running"
            logger.info(f"日志ID：{log.id}，状态：{log.exec_status}，是否运行中：{log.is_running}")

        context = {
            "page_title": "执行脚本任务",
            "devices": device_list,
            "tasks": tasks,
            "recent_logs": recent_logs,  # 现在是 Page 对象
            "search_query": search_query,
            # 新增：分页专用变量
            "page_obj": recent_logs,
            "is_paginated": recent_logs.has_other_pages(),
            "paginator": paginator
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
                # 修复：这里直接调用 @property
                current_status = device.device_status
                logger.info(f"检查设备状态 - ID：{device_id}，名称：{device.device_name}，状态：{current_status}")

                if current_status != "online":
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
                    stdout=f"任务启动中（{'Celery异步' if settings.USE_CELERY else '后台线程同步'}执行）{python_warning}",
                    start_time=timezone.now()
                )
                logger.info(f"创建执行日志 - ID：{log.id}，设备：{device.device_name}")

                try:
                    # ====================== 核心：异步/同步降级逻辑 ======================
                    if settings.USE_CELERY:
                        # 优先尝试 Celery 异步
                        celery_task = execute_script_task.delay(task.id, device.id, log.id, python_path)
                        save_celery_task(log.id, celery_task.id)
                        logger.info(f"提交Celery任务 - 日志ID：{log.id}，任务ID：{celery_task.id}")
                    else:
                        # 配置关闭 Celery，直接用后台线程
                        logger.info(f"配置USE_CELERY=False，使用后台线程同步执行 - 日志ID：{log.id}")
                        execute_script_sync(task.id, device.id, log.id, python_path)
                except Exception as celery_err:
                    # Celery 提交失败，优雅降级到后台线程
                    logger.warning(f"Celery任务提交失败，优雅降级到后台线程 - 日志ID：{log.id}，错误：{str(celery_err)}")
                    execute_script_sync(task.id, device.id, log.id, python_path)

            success_msg = quote(
                f"任务【{task.task_name}】已启动！共{len(valid_device_ids)}个在线设备执行中{python_warning}"
            )
            if offline_devices:
                success_msg = quote(f"{success_msg}（离线设备已过滤：{','.join(offline_devices)}）")
            logger.info(success_msg)
            return redirect(f"{reverse('script_center:execute_task')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"执行任务失败：{str(e)}", exc_info=True)
            error_msg = quote(f"执行任务失败：{str(e)}")
            return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")


class StopTaskView(View):
    """停止任务：兼容异步/同步（通过 Redis 信号停止）"""

    def get(self, request, log_id):
        try:
            logger.info(f"接收到停止任务请求 - 日志ID：{log_id}")
            log = get_object_or_404(TaskExecutionLog.objects.select_related('task', 'device'), id=log_id)
            if log.exec_status != "running":
                error_msg = quote(f"任务【{log.task.task_name}】未在运行中！当前状态：{log.exec_status}")
                logger.warning(error_msg)
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            celery_task_id = get_celery_task(log_id)
            r = get_redis_conn()
            device_serial = None

            # 1. 无论异步/同步，优先发送 Redis 停止信号（脚本会检测）
            if r:
                process_info_str = r.hget("script_running_processes", log_id)
                if process_info_str:
                    process_info = json.loads(process_info_str)
                    device_serial = process_info.get("device_serial")
                    send_redis_stop_signal(device_serial, log_id)

            # 2. 如果是 Celery 异步，撤销任务
            if celery_task_id:
                from celery.result import AsyncResult
                AsyncResult(celery_task_id).revoke(terminate=False)
                delete_celery_task(log_id)
                logger.info(f"已发送Celery停止信号 - ID：{celery_task_id}，日志ID：{log_id}")
            else:
                logger.info(f"未找到Celery任务ID（同步执行模式），仅依赖Redis信号停止 - 日志ID：{log_id}")

            stop_wait_time = get_env_config("SCRIPT_STOP_WAIT_TIME", 8, int)
            time.sleep(stop_wait_time)

            # 3. 强制清理进程（兜底）
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
- Celery任务ID：{celery_task_id or '同步执行（无）'}
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
        # 优化：select_related
        log = get_object_or_404(TaskExecutionLog.objects.select_related('task', 'device'), id=log_id)
        log.exec_duration_str = format_duration(log.exec_duration)
        context = {
            "page_title": f"执行日志 - {log.task.task_name}",
            "log": log
        }
        return render(request, "script_center/log_detail.html", context)


class LogStatusView(View):
    def get(self, request, log_id):
        try:
            # 优化：只查 id, exec_status, exec_duration，不查巨大的 stdout/stderr
            log = get_object_or_404(
                TaskExecutionLog.objects.select_related('task').only('id', 'exec_status', 'exec_duration'),
                id=log_id
            )

            # 修改：兼容同步：只要状态是 running 就算运行中
            is_running = log.exec_status == "running"

            # 优化：生成 ETag，如果内容没变，返回 304 Not Modified
            content_hash = hashlib.md5(f"{log.exec_status}-{log.exec_duration}".encode()).hexdigest()

            # 检查 If-None-Match 头
            if_none_match = request.META.get('HTTP_IF_NONE_MATCH')
            if if_none_match == f'"{content_hash}"':
                return HttpResponse(status=304)

            response = JsonResponse({
                "code": 200,
                "status": log.exec_status,
                "duration": log.exec_duration,
                "is_running": is_running
            })

            # 设置 ETag 和缓存头（缓存 1 秒）
            response['ETag'] = f'"{content_hash}"'
            patch_response_headers(response, cache_timeout=1)
            return response

        except Exception as e:
            return JsonResponse({
                "code": 500,
                "msg": str(e)
            })


def get_airtest_log_dir(script_path):
    try:
        script_path_obj = Path(script_path)
        air_dir = script_path_obj.parent
        if air_dir.suffix != '.air':
            return None
        log_dir = air_dir / 'log'
        return log_dir if log_dir.exists() else None
    except Exception:
        return None


class AirtestLogImagesView(View):
    """获取指定日志关联的 Airtest log 图片列表（优化：分页）"""

    def get(self, request, log_id):
        # 优化：select_related 只查 task 里的 script_path
        log = get_object_or_404(TaskExecutionLog.objects.select_related('task').only('id', 'task__script_path'),
                                id=log_id)
        log_dir = get_airtest_log_dir(log.task.script_path)

        if not log_dir:
            return JsonResponse({"code": 404, "msg": "未找到对应的 Airtest log 目录"})

        image_extensions = {'.png', '.jpg', '.jpeg', '.bmp', '.gif'}
        images = []
        try:
            # 1. 先收集所有文件
            all_files = []
            for file_path in log_dir.iterdir():
                if file_path.is_file() and file_path.suffix.lower() in image_extensions:
                    stat = file_path.stat()
                    all_files.append({
                        "name": file_path.name,
                        "size": f"{stat.st_size / 1024:.2f} KB",
                        "url": reverse('script_center:serve_airtest_image', args=[log_id, file_path.name]),
                        "modified_time": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        "timestamp": stat.st_mtime  # 用于排序
                    })

            # 2. 排序
            all_files.sort(key=lambda x: x["timestamp"], reverse=True)

            # 3. 分页 (简单的分页逻辑)
            page = int(request.GET.get('page', 1))
            page_size = int(request.GET.get('page_size', 10))  # 默认每页10张
            start = (page - 1) * page_size
            end = start + page_size

            paginated_images = all_files[start:end]

            # 移除 timestamp，不返回给前端
            for img in paginated_images:
                del img['timestamp']

            return JsonResponse({
                "code": 200,
                "images": paginated_images,
                "total": len(all_files),
                "page": page,
                "page_size": page_size,
                "has_next": end < len(all_files)
            })
        except Exception as e:
            logger.error(f"扫描 Airtest log 图片失败：{str(e)}")
            return JsonResponse({"code": 500, "msg": f"扫描失败：{str(e)}"})


class ServeAirtestLogImageView(View):
    """直接返回 Airtest log 图片文件（带缓存）"""

    def get(self, request, log_id, image_name):
        # 优化：select_related 确保安全，但只查必要字段
        log_full = get_object_or_404(TaskExecutionLog.objects.select_related('task').only('id', 'task__script_path'),
                                     id=log_id)
        log_dir = get_airtest_log_dir(log_full.task.script_path)

        if not log_dir:
            return HttpResponse("未找到 log 目录", status=404)

        image_path = (log_dir / image_name).resolve()
        if log_dir.resolve() not in image_path.parents:
            return HttpResponse("非法访问", status=403)

        if not image_path.exists():
            return HttpResponse("图片不存在", status=404)

        content_type, _ = mimetypes.guess_type(image_name)
        response = FileResponse(open(image_path, 'rb'), content_type=content_type or 'image/png')

        # 优化：添加缓存头，让浏览器缓存图片 1 年
        patch_response_headers(response, cache_timeout=31536000)
        return response


@method_decorator(csrf_exempt, name='dispatch')
class ClearAirtestLogImagesView(View):
    """清理指定日志关联的 Airtest log 图片"""

    def post(self, request, log_id):
        # 优化：select_related
        log = get_object_or_404(TaskExecutionLog.objects.select_related('task'), id=log_id)
        log_dir = get_airtest_log_dir(log.task.script_path)

        if not log_dir:
            return JsonResponse({"code": 404, "msg": "未找到对应的 Airtest log 目录"})

        try:
            image_extensions = {'.png', '.jpg', '.jpeg', '.bmp', '.gif'}
            deleted_count = 0
            for file_path in log_dir.iterdir():
                if file_path.is_file() and file_path.suffix.lower() in image_extensions:
                    file_path.unlink()
                    deleted_count += 1

            ScriptTaskManagementLog.objects.create(
                task=log.task,
                operation="clear_images",
                operator=request.user.username if request.user.is_authenticated else "匿名用户",
                details=f"在日志详情页清理了 Airtest log 图片，共删除 {deleted_count} 张"
            )

            return JsonResponse({"code": 200, "msg": f"清理成功，共删除 {deleted_count} 张图片"})
        except Exception as e:
            logger.error(f"清理 Airtest log 图片失败：{str(e)}")
            return JsonResponse({"code": 500, "msg": f"清理失败：{str(e)}"})


class BuiltinScriptListView(View):
    """内置脚本列表页（带Tab栏）"""

    def get(self, request):
        # 如果设置关闭了，直接重定向回脚本中心
        if not getattr(settings, 'SHOW_BUILTIN_SCRIPTS', True):
            return redirect(reverse('script_center:task_list'))

        categories = BuiltinScript.CATEGORY_CHOICES
        scripts_by_category = {}

        for cat_key, cat_label in categories:
            scripts = BuiltinScript.objects.filter(category=cat_key, is_active=True)
            if scripts.exists():
                scripts_by_category[cat_label] = scripts

        context = {
            "page_title": "内置脚本库",
            "scripts_by_category": scripts_by_category
        }
        return render(request, "script_center/builtin_list.html", context)


class BuiltinScriptDetailView(View):
    """脚本详情页 + 动态表单 + 一键执行"""

    def get(self, request, script_id):
        # 如果设置关闭了，直接重定向
        if not getattr(settings, 'SHOW_BUILTIN_SCRIPTS', True):
            return redirect(reverse('script_center:task_list'))

        script = get_object_or_404(BuiltinScript, id=script_id, is_active=True)

        # 1. 动态构建表单
        class DynamicScriptForm(forms.Form):
            # 额外添加设备选择
            device = forms.ModelChoiceField(
                queryset=ADBDevice.objects.filter(is_active=True),
                label="选择设备",
                required=True,
                widget=forms.Select(attrs={'class': 'form-control'})
            )

        for param in script.parameters.all():
            field_args = {
                'label': param.label,
                'required': param.required,
                'initial': param.default_value,
                'help_text': param.help_text,
            }

            if param.param_type == 'integer':
                field_class = forms.IntegerField
            elif param.param_type == 'float':
                field_class = forms.FloatField
            elif param.param_type == 'boolean':
                field_class = forms.BooleanField
                field_args['required'] = False
            else:
                field_class = forms.CharField

            DynamicScriptForm.base_fields[param.name] = field_class(**field_args)

        form = DynamicScriptForm()

        # 2. 读取源码用于展示
        source_code = ""
        try:
            with open(script.get_absolute_path(), 'r', encoding='utf-8') as f:
                source_code = f.read()
        except:
            source_code = "无法读取源码文件"

        context = {
            "page_title": f"脚本详情 - {script.name}",
            "script": script,
            "form": form,
            "source_code": source_code
        }
        return render(request, "script_center/builtin_detail.html", context)

    def post(self, request, script_id):
        """执行脚本：复用你现有的 Celery 逻辑"""
        # POST 也要检查
        if not getattr(settings, 'SHOW_BUILTIN_SCRIPTS', True):
            return redirect(reverse('script_center:task_list'))

        script = get_object_or_404(BuiltinScript, id=script_id, is_active=True)

        # 简单处理：直接获取数据
        device_id = request.POST.get('device')
        device = get_object_or_404(ADBDevice, id=device_id)

        # 1. 创建一个临时的 ScriptTask
        temp_task = ScriptTask.objects.create(
            task_name=f"[内置] {script.name} - {timezone.now().strftime('%H:%M:%S')}",
            task_desc=script.description,
            script_path=script.get_absolute_path(),
            status="active",
            airtest_mode=True
        )
        # 2. 复用现有的 ExecuteTaskView 里的逻辑
        log = TaskExecutionLog.objects.create(
            task=temp_task,
            device=device,
            exec_status="running",
            exec_command=f"准备执行内置脚本: {script.name}",
            stdout="任务启动中...",
            start_time=timezone.now()
        )

        # 3. 调用 Celery
        from .tasks import execute_script_task
        celery_task = execute_script_task.delay(temp_task.id, device.id, log.id, sys.executable)

        # 4. 保存 Redis
        try:
            save_celery_task(log.id, celery_task.id)
        except:
            pass

        return redirect(reverse('script_center:log_detail', args=[log.id]))
