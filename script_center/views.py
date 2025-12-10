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

# 导入Celery任务
from .tasks import execute_script_task, _graceful_terminate_process  # 替换为新的优雅终止函数
from .models import ScriptTask, TaskExecutionLog
from .forms import ScriptTaskForm
from adb_manager.models import ADBDevice

# 配置详细日志
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('script_execution.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# 修复Windows编码问题
os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['PYTHONLEGACYWINDOWSSTDIO'] = 'utf-8'


# ===================== Redis连接及任务ID操作函数 =====================
def get_redis_conn():
    """获取Redis连接"""
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


def save_celery_task(log_id, celery_task_id):
    """保存Celery任务ID到Redis"""
    r = get_redis_conn()
    if r:
        try:
            r.hset("script_running_tasks", log_id, celery_task_id)
            logger.info(f"已保存Celery任务ID到Redis - 日志ID：{log_id}，任务ID：{celery_task_id}")
        except Exception as e:
            logger.error(f"保存Celery任务ID失败：{str(e)}")


def get_celery_task(log_id):
    """从Redis获取Celery任务ID"""
    r = get_redis_conn()
    if r:
        try:
            return r.hget("script_running_tasks", log_id)
        except Exception as e:
            logger.error(f"获取Celery任务ID失败：{str(e)}")
    return None


def delete_celery_task(log_id):
    """从Redis删除Celery任务ID"""
    r = get_redis_conn()
    if r:
        try:
            r.hdel("script_running_tasks", log_id)
            logger.info(f"已删除Redis中的Celery任务ID - 日志ID：{log_id}")
        except Exception as e:
            logger.error(f"删除Celery任务ID失败：{str(e)}")


# ===================== 任务管理视图 =====================
class TaskListView(View):
    """任务列表页"""

    def get(self, request):
        tasks = ScriptTask.objects.all()
        context = {
            "page_title": "脚本任务管理",
            "tasks": tasks
        }
        return render(request, "script_center/task_list.html", context)


class TaskAddView(View):
    """新增任务"""

    def get(self, request):
        form = ScriptTaskForm()
        context = {
            "page_title": "新增脚本任务",
            "form": form
        }
        return render(request, "script_center/task_form.html", context)

    def post(self, request):
        form = ScriptTaskForm(request.POST)
        if form.is_valid():
            task = form.save()
            success_msg = quote(f"任务【{task.task_name}】创建成功！")
            return redirect(f"{reverse('script_center:task_list')}?msg={success_msg}")
        context = {
            "page_title": "新增脚本任务",
            "form": form,
            "error_msg": "表单填写有误，请检查！"
        }
        return render(request, "script_center/task_form.html", context)


class TaskEditView(View):
    """编辑任务"""

    def get(self, request, task_id):
        task = get_object_or_404(ScriptTask, id=task_id)
        form = ScriptTaskForm(instance=task)
        context = {
            "page_title": f"编辑任务 - {task.task_name}",
            "form": form,
            "task": task
        }
        return render(request, "script_center/task_form.html", context)

    def post(self, request, task_id):
        task = get_object_or_404(ScriptTask, id=task_id)
        form = ScriptTaskForm(request.POST, instance=task)
        if form.is_valid():
            task = form.save()
            success_msg = quote(f"任务【{task.task_name}】修改成功！")
            return redirect(f"{reverse('script_center:task_list')}?msg={success_msg}")
        context = {
            "page_title": f"编辑任务 - {task.task_name}",
            "form": form,
            "task": task,
            "error_msg": "表单填写有误，请检查！"
        }
        return render(request, "script_center/task_form.html", context)


class TaskDeleteView(View):
    """删除任务"""

    def post(self, request, task_id):
        try:
            task = get_object_or_404(ScriptTask, id=task_id)
            task_name = task.task_name
            task.delete()
            success_msg = quote(f"任务【{task_name}】删除成功！")
        except Exception as e:
            logger.error(f"删除任务失败：{str(e)}")
            success_msg = quote(f"删除失败：{str(e)}")
        return redirect(f"{reverse('script_center:task_list')}?msg={success_msg}")


# ===================== 任务执行/停止核心逻辑 =====================
class ExecuteTaskView(View):
    """执行任务页面 + Celery异步执行逻辑"""

    def get(self, request):
        # 获取所有可用设备和任务
        devices = ADBDevice.objects.filter(is_active=True)
        device_list = []
        for dev in devices:
            dev.status = dev.device_status
            device_list.append(dev)

        # 按创建时间倒序获取日志
        recent_logs = TaskExecutionLog.objects.all().order_by("-id")[:10]
        logger.info(f"最近执行日志数量：{recent_logs.count()}")
        for log in recent_logs:
            # 标记是否正在运行（基于Redis中的Celery任务）
            celery_task_id = get_celery_task(log.id)
            log.is_running = celery_task_id is not None and log.exec_status == "running"
            logger.info(f"日志ID：{log.id}，状态：{log.exec_status}，是否运行中：{log.is_running}")

        context = {
            "page_title": "执行脚本任务",
            "devices": device_list,
            "tasks": ScriptTask.objects.filter(status="active"),
            "recent_logs": recent_logs
        }
        return render(request, "script_center/execute_task.html", context)

    def post(self, request):
        try:
            # 1. 获取并校验请求参数
            device_ids = request.POST.getlist("device_ids")
            task_id = request.POST.get("task_id")

            logger.info(f"接收到执行请求 - 设备ID：{device_ids}，任务ID：{task_id}")

            if not device_ids or not task_id:
                error_msg = quote("请选择至少一个设备和一个任务！")
                logger.warning(error_msg)
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            # 2. 校验任务是否有效
            task = get_object_or_404(ScriptTask, id=task_id, status="active")
            logger.info(f"获取到任务：{task.task_name}，脚本路径：{task.script_path}，Python路径：{task.python_path}")

            if not task.is_script_exists():
                error_msg = quote(f"任务脚本不存在：{task.script_path}")
                logger.error(error_msg)
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            # 3. 校验设备状态
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

            # 4. 为每个有效设备创建日志并提交Celery任务
            python_warning = ""
            if "WindowsApps" in task.python_path:
                python_warning = "（注意：Python路径为WindowsApps快捷方式，已自动替换为真实路径）"

            for device_id in valid_device_ids:
                device = get_object_or_404(ADBDevice, id=device_id)
                # 创建执行日志
                log = TaskExecutionLog.objects.create(
                    task=task,
                    device=device,
                    exec_status="running",
                    exec_command=f"准备执行：{task.python_path} {task.script_path} {device.adb_connect_str}",
                    stdout=f"任务启动中（Celery异步执行）{python_warning}",
                    start_time=timezone.now()
                )
                logger.info(f"创建执行日志 - ID：{log.id}，设备：{device.device_name}")

                # 提交Celery异步任务
                try:
                    celery_task = execute_script_task.delay(task.id, device.id, log.id)
                    # 记录Celery任务ID到Redis
                    save_celery_task(log.id, celery_task.id)
                    logger.info(f"提交Celery任务 - 日志ID：{log.id}，任务ID：{celery_task.id}")
                except Exception as celery_err:
                    log.exec_status = "error"
                    log.stderr = f"Celery任务提交失败：{str(celery_err)}"
                    log.end_time = timezone.now()
                    log.save()
                    logger.error(f"Celery任务提交失败 - 日志ID：{log.id}，错误：{str(celery_err)}")

            # 5. 返回执行成功提示
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
    """停止Celery异步任务（兼容Redis进程终止）"""

    def get(self, request, log_id):
        try:
            logger.info(f"接收到停止任务请求 - 日志ID：{log_id}")

            # 1. 获取日志对象
            log = get_object_or_404(TaskExecutionLog, id=log_id)
            if log.exec_status != "running":
                error_msg = quote(f"任务【{log.task.task_name}】未在运行中！当前状态：{log.exec_status}")
                logger.warning(error_msg)
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            # 2. 终止Celery任务（从Redis获取任务ID）
            celery_task_id = get_celery_task(log_id)
            if not celery_task_id:
                error_msg = quote(f"未找到任务【{log.task.task_name}】的Celery执行记录！")
                logger.warning(error_msg)
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            # 发送停止信号（先不立即终止，让脚本优雅退出）
            r = get_redis_conn()
            device_serial = None
            if r:
                process_info_str = r.hget("script_running_processes", log_id)
                if process_info_str:
                    process_info = json.loads(process_info_str)
                    device_serial = process_info.get("device_serial")
                    # 发送停止信号（有效期60秒）
                    r.set(f"airtest_stop_flag_{device_serial}", "True", ex=60)
                    logger.info(f"已发送Redis停止信号 - 设备：{device_serial}，日志ID：{log_id}")

            # 温柔终止Celery任务（不立即kill，给脚本响应时间）
            AsyncResult(celery_task_id).revoke(terminate=False)  # 关键：terminate=False 不立即终止
            delete_celery_task(log_id)
            logger.info(f"已发送Celery停止信号 - ID：{celery_task_id}，日志ID：{log_id}")

            # 延长等待时间至8秒，确保脚本输出优雅退出日志
            time.sleep(8)

            # 3. 从Redis获取进程并优雅终止（如果仍在运行）
            if r and process_info_str:
                process_info = json.loads(process_info_str)
                pid = process_info.get("pid")
                if pid:
                    try:
                        # 检查进程是否仍在运行，再终止
                        if psutil.pid_exists(pid):
                            _graceful_terminate_process(pid, wait_time=3)
                            logger.info(f"已优雅终止进程{pid} - 设备：{device_serial}")
                        else:
                            logger.info(f"进程{pid}已自行退出（优雅退出成功）")
                    except Exception as e:
                        logger.error(f"终止进程{pid}失败：{str(e)}")

                    # 清理Redis
                    r.hdel("script_running_processes", log_id)
                    r.delete(f"airtest_stop_flag_{device_serial}")

            # 4. 更新日志状态（修复：标记为stopped，而非error）
            log.exec_status = "stopped"
            log.stderr = f"""任务已手动停止（优雅退出）
- 停止时间：{timezone.now()}
- 设备序列号：{device_serial or '未知'}
- Celery任务ID：{celery_task_id}
- 终止日志ID：{log_id}
- 已等待8秒让脚本完成清理和日志输出"""
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
    """查看执行日志详情"""

    def get(self, request, log_id):
        log = get_object_or_404(TaskExecutionLog, id=log_id)
        # 格式化耗时
        if log.exec_duration:
            log.exec_duration_str = f"{log.exec_duration:.2f}秒"
        else:
            log.exec_duration_str = "未知"
        context = {
            "page_title": f"执行日志 - {log.task.task_name}",
            "log": log
        }
        return render(request, "script_center/log_detail.html", context)


class LogStatusView(View):
    """获取日志执行状态（AJAX用）"""

    def get(self, request, log_id):
        try:
            log = get_object_or_404(TaskExecutionLog, id=log_id)
            # 从Redis判断是否运行中
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


