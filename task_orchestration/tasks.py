import subprocess
import sys
import threading
import time
import psutil
import os
import json
from celery import shared_task
from django.utils import timezone
from .models import OrchestrationLog, StepExecutionLog, TaskStep
from script_center.models import ScriptTask
from adb_manager.models import ADBDevice
import logging
import redis

logger = logging.getLogger(__name__)


# ===================== Redis操作工具函数（支持本地降级） =====================
def get_redis_conn():
    """获取Redis连接（失败返回None）"""
    try:
        from django.conf import settings
        r = redis.Redis(
            host=getattr(settings, 'REDIS_HOST', '127.0.0.1'),
            port=getattr(settings, 'REDIS_PORT', 6379),
            db=getattr(settings, 'REDIS_DB', 0),
            decode_responses=True,
            socket_timeout=5
        )
        r.ping()
        return r
    except Exception as e:
        logger.warning(f"Redis连接失败，将使用本地存储：{str(e)}")
        return None


# 本地备份存储（Redis不可用时使用）
_local_process_store = {}
_local_store_lock = threading.Lock()


def save_running_process(process_key, process_info):
    """保存进程信息（优先Redis，失败则本地）"""
    r = get_redis_conn()
    if r:
        try:
            r.hset("orch_running_processes", process_key, json.dumps(process_info))
            logger.info(f"Redis已存储进程信息：KEY={process_key}, PID={process_info.get('pid')}")
            return
        except Exception as e:
            logger.warning(f"Redis保存失败，切换本地存储：{str(e)}")

    # 本地降级存储
    with _local_store_lock:
        _local_process_store[process_key] = process_info
        logger.info(f"本地已存储进程信息：KEY={process_key}, PID={process_info.get('pid')}")


def get_running_process(process_key):
    """获取进程信息（优先Redis，失败则本地）"""
    r = get_redis_conn()
    if r:
        try:
            data = r.hget("orch_running_processes", process_key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.warning(f"Redis获取失败，尝试本地存储：{str(e)}")

    # 本地降级获取
    with _local_store_lock:
        return _local_process_store.get(process_key)


def remove_running_process(process_key):
    """删除进程信息（同时清理Redis和本地）"""
    r = get_redis_conn()
    if r:
        try:
            r.hdel("orch_running_processes", process_key)
            logger.info(f"Redis中进程信息已删除，KEY={process_key}")
        except Exception as e:
            logger.warning(f"Redis删除失败：{str(e)}")

    # 清理本地存储
    with _local_store_lock:
        if process_key in _local_process_store:
            del _local_process_store[process_key]
            logger.info(f"本地进程信息已删除，KEY={process_key}")


# ===================== 核心执行逻辑（Celery和本地共用） =====================
def _execute_step_core(step_id, orch_log_id, device_data, task_id=None):
    """
    执行单个步骤的核心逻辑（无Celery依赖）
    :param task_id: Celery任务ID（本地执行时为None）
    """
    try:
        # 获取任务实例
        step = TaskStep.objects.get(id=step_id)
        orch_log = OrchestrationLog.objects.get(id=orch_log_id)
        device = ADBDevice.objects.get(id=device_data['id'])
        script_task = step.script_task

        # 创建步骤日志
        step_log = StepExecutionLog.objects.create(
            orchestration_log=orch_log,
            step=step,
            exec_status="running",
            start_time=timezone.now()
        )

        # 更新编排日志进度
        orch_log.completed_steps += 1
        orch_log.save()

        # 校验脚本是否存在
        if not hasattr(script_task, 'is_script_exists') or not script_task.is_script_exists():
            error_msg = f"脚本文件不存在：{script_task.script_path if script_task else '未知路径'}"
            step_log.exec_status = "error"
            step_log.error_msg = error_msg
            step_log.end_time = timezone.now()
            step_log.save()
            return {"status": "error", "msg": error_msg}

        # 处理Python路径
        real_python_path = _get_real_python_path(script_task)

        # 构建执行命令
        script_dir = os.path.dirname(script_task.script_path)
        command = f'"{real_python_path}" -X utf8 "{script_task.script_path}" "{device.adb_connect_str}"'
        step_log.exec_command = command
        step_log.save()

        # 构建环境变量
        env = os.environ.copy()
        env.update({
            'PYTHONIOENCODING': 'utf-8',
            'PYTHONLEGACYWINDOWSSTDIO': 'utf-8',
            'LC_ALL': 'en_US.UTF-8',
            'LANG': 'en_US.UTF-8'
        })

        # 启动进程
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=script_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            universal_newlines=True
        )

        # 存储进程信息
        process_key = f"{orch_log_id}_{step.execution_order}"
        process_info = {
            "pid": process.pid,
            "step_order": step.execution_order,
            "log_id": orch_log_id,
            "device_serial": device.adb_connect_str,
            "task_id": task_id,
            "command": command,
            "is_local": task_id is None  # 标记是否为本地执行
        }
        save_running_process(process_key, process_info)
        logger.info(f"进程{process.pid}已存储，KEY={process_key}，本地执行={task_id is None}")

        # 实时读取输出
        step_start_time = time.time()
        stdout_buffer = []
        stderr_buffer = []
        return_code = None

        try:
            # 并行读取stdout和stderr
            import threading
            stdout_thread = threading.Thread(
                target=_read_stream,
                args=(process.stdout, stdout_buffer, step_log, orch_log, 'stdout'),
                daemon=True
            )
            stderr_thread = threading.Thread(
                target=_read_stream,
                args=(process.stderr, stderr_buffer, step_log, orch_log, 'stderr'),
                daemon=True
            )

            stdout_thread.start()
            stderr_thread.start()

            # 等待进程结束或超时
            while process.poll() is None:
                if time.time() - step_start_time > step.run_duration:
                    raise subprocess.TimeoutExpired(command, step.run_duration)
                time.sleep(0.1)

            # 等待输出线程结束
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)

            return_code = process.returncode

            # 更新步骤日志最终状态
            step_log.stdout = ''.join(stdout_buffer)
            step_log.stderr = ''.join(stderr_buffer)
            step_log.return_code = return_code
            step_log.exec_duration = time.time() - step_start_time

            if return_code == 0:
                step_log.exec_status = "completed"
                step_log.error_msg = ""
            else:
                step_log.exec_status = "failed"
                step_log.error_msg = f"执行失败，返回码：{return_code}"

        except subprocess.TimeoutExpired:
            # 超时处理
            _terminate_process(process.pid)
            step_log.exec_status = "timeout"
            step_log.error_msg = f"执行超时（{step.run_duration}秒）"
            step_log.exec_duration = step.run_duration
            stderr_msg = f"进程超时被终止（{step.run_duration}秒）"
            stderr_buffer.append(stderr_msg)
            step_log.stderr = ''.join(stderr_buffer) + stderr_msg

        except Exception as e:
            if process:
                process.terminate()
            error_detail = f"""【异常信息】
类型：{type(e).__name__}
描述：{str(e)}"""
            step_log.exec_status = "error"
            step_log.error_msg = error_detail
            step_log.stderr = ''.join(stderr_buffer) + error_detail
            step_log.exec_duration = time.time() - step_start_time

        # 最终保存步骤日志
        step_log.end_time = timezone.now()
        step_log.save()

        # 实时更新编排日志
        orch_log.stdout = f"{orch_log.stdout}\n{step_log.stdout}"
        orch_log.stderr = f"{orch_log.stderr}\n{step_log.stderr}"
        orch_log.save()

        # 清理进程信息
        remove_running_process(process_key)

        return {"status": step_log.exec_status, "step_id": step_id, "step_log_id": step_log.id}

    except Exception as e:
        if 'step_log' in locals():
            step_log.exec_status = "error"
            step_log.error_msg = f"任务执行异常：{str(e)}"
            step_log.end_time = timezone.now()
            step_log.save()
        logger.error(f"步骤执行失败：{str(e)}", exc_info=True)
        return {"status": "error", "msg": str(e)}


# ===================== Celery任务（仅作为可选执行方式） =====================
@shared_task(bind=True, max_retries=0, time_limit=3600)
def execute_step_task(self, step_id, orch_log_id, device_data):
    """Celery异步执行任务（内部调用核心逻辑）"""
    return _execute_step_core(step_id, orch_log_id, device_data, task_id=self.request.id)


# ===================== 辅助函数（保持不变） =====================
def _read_stream(stream, buffer, step_log, orch_log, stream_type):
    """实时读取进程输出并更新日志"""
    try:
        for line in iter(stream.readline, ''):
            buffer.append(line)
            if stream_type == 'stdout':
                step_log.stdout = ''.join(buffer)
            else:
                step_log.stderr = ''.join(buffer)
            step_log.save()

            if stream_type == 'stdout':
                orch_log.stdout = f"{orch_log.stdout}\n{line}"
            else:
                orch_log.stderr = f"{orch_log.stderr}\n{line}"
            orch_log.save()
    except Exception as e:
        logger.error(f"读取{stream_type}失败：{str(e)}")


def _get_real_python_path(script_task: ScriptTask) -> str:
    """获取真实的Python路径"""
    from django.conf import settings
    warning_keyword = getattr(settings, 'SCRIPT_PYTHON_WARNING_KEYWORD', 'WindowsApps')

    if warning_keyword in script_task.python_path:
        # 优先使用settings中的fallback路径
        fallback_paths = getattr(settings, 'PYTHON_FALLBACK_PATHS', [])
        for path in fallback_paths:
            if os.path.exists(path):
                return path

        # 兼容原有逻辑
        possible_paths = [
            r"C:\Python311\python.exe",
            os.path.expanduser("~\\AppData\\Local\\Programs\\Python\\Python311\\python.exe"),
            r"C:\Program Files\Python311\python.exe"
        ]
        for path in possible_paths:
            if os.path.exists(path):
                return path
    return script_task.python_path


def _terminate_process(pid: int):
    """彻底终止进程及所有子进程"""
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            try:
                child.terminate()
            except:
                pass
        parent.terminate()
        time.sleep(1)
        if parent.is_running():
            parent.kill()
        logger.info(f"进程{pid}及其子进程已彻底终止")
    except Exception as e:
        logger.error(f"终止进程{pid}失败：{str(e)}")


def kill_redis_process(process_key):
    """终止进程（支持本地和Redis存储）"""
    process_info = get_running_process(process_key)
    if not process_info or not process_info.get("pid"):
        logger.warning(f"无进程信息，KEY={process_key}")
        return

    try:
        pid = process_info["pid"]
        _terminate_process(pid)
        logger.info(f"已终止进程{pid}（KEY：{process_key}）")
        remove_running_process(process_key)
    except Exception as e:
        logger.error(f"终止进程失败（KEY：{process_key}）：{str(e)}")