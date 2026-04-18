import subprocess
import sys
import time
import psutil
import os
import redis
import json
import threading
from celery import shared_task
from django.utils import timezone
from django.conf import settings  # 【新增】导入Django settings
from .models import ScriptTask, TaskExecutionLog
from adb_manager.models import ADBDevice
import logging

logger = logging.getLogger(__name__)

stdout_buffer = {}
stderr_buffer = {}

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

# ====================== 优化：全局 Redis 连接池（使用settings配置） ======================
REDIS_POOL = None


def read_stream(stream, buffer_key, log, is_stdout=True):
    """实时读取子进程输出流（线程执行）+ 推送WebSocket"""
    global stdout_buffer, stderr_buffer
    buffer = []
    channel_layer = get_channel_layer()
    try:
        for line in iter(stream.readline, ''):
            if line:
                buffer.append(line)
                if is_stdout:
                    log.stdout += line
                else:
                    log.stderr += line
                log.save()
                async_to_sync(channel_layer.group_send)(
                    f'script_log_{log.id}',
                    {
                        'type': 'log_update',
                        'data': {
                            'stdout': log.stdout,
                            'stderr': log.stderr,
                            'status': log.exec_status
                        }
                    }
                )
                sys.stdout.flush()
        if buffer:
            log.save()
            async_to_sync(channel_layer.group_send)(
                f'script_log_{log.id}',
                {
                    'type': 'log_update',
                    'data': {
                        'stdout': log.stdout,
                        'stderr': log.stderr,
                        'status': log.exec_status
                    }
                }
            )
    except Exception as e:
        logger.error(f"读取子进程{buffer_key}流失败：{str(e)}")
    finally:
        stream.close()


def get_redis_conn():
    """优化：使用连接池 + settings配置"""
    global REDIS_POOL
    if REDIS_POOL is None:
        REDIS_POOL = redis.ConnectionPool(
            host=settings.REDIS_HOST,  # 【修改】使用settings
            port=settings.REDIS_PORT,  # 【修改】使用settings
            db=settings.REDIS_DB,      # 【修改】使用settings
            decode_responses=True,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT  # 【修改】使用settings
        )
    try:
        r = redis.Redis(connection_pool=REDIS_POOL)
        r.ping()
        return r
    except Exception as e:
        logger.error(f"Redis连接失败：{str(e)}")
        return None


def _graceful_terminate_process(pid: int, wait_time: int = None):
    """优雅终止进程（使用settings默认值）"""
    wait_time = wait_time or settings.SCRIPT_GRACEFUL_TERMINATE_WAIT  # 【修改】使用settings
    try:
        parent = psutil.Process(pid)
        logger.info(f"开始优雅终止进程{pid}，等待{wait_time}秒让脚本清理并输出日志...")

        for i in range(wait_time):
            if not parent.is_running():
                logger.info(f"进程{pid}已自行退出（优雅退出成功）")
                return
            time.sleep(1)
            logger.info(f"等待脚本优雅退出中...剩余{wait_time - i - 1}秒")

        logger.warning(f"进程{pid}未自行退出，开始强制终止子进程...")
        for child in parent.children(recursive=True):
            try:
                child.terminate()
                logger.info(f"已终止子进程{child.pid}")
            except Exception as e:
                logger.warning(f"终止子进程{child.pid}失败：{str(e)}")

        parent.terminate()
        time.sleep(1)

        if parent.is_running():
            parent.kill()
            logger.warning(f"已强制杀死进程{pid}")
        else:
            logger.info(f"进程{pid}已终止")

    except Exception as e:
        logger.error(f"终止进程{pid}失败：{str(e)}")


# ====================== 核心：抽离执行逻辑（兼容异步/同步） ======================
def _execute_script_core(task_id, device_id, log_id, python_path, celery_task_id=None):
    """核心执行逻辑（被 Celery 任务 和 后台线程 共同调用）"""
    log = None
    device_serial = ""
    r = get_redis_conn()
    process = None
    stdout_thread = None
    stderr_thread = None
    try:
        task = ScriptTask.objects.get(id=task_id)
        device = ADBDevice.objects.get(id=device_id)
        log = TaskExecutionLog.objects.get(id=log_id)
        device_serial = device.adb_connect_str

        input_python_path = python_path or task.python_path

        real_python_path = input_python_path
        if settings.SCRIPT_PYTHON_WARNING_KEYWORD in input_python_path:  # 【修改】使用settings（你原有配置里有这个）
            possible_paths = settings.PYTHON_FALLBACK_PATHS  # 【修改】使用settings
            for path in possible_paths:
                if os.path.exists(path):
                    real_python_path = path
                    logger.info(f"替换Python路径：{input_python_path} → {real_python_path}")
                    break

        if not os.path.exists(real_python_path):
            raise Exception(f"Python路径无效：{real_python_path}（原始传入路径：{input_python_path}）")
        logger.info(f"使用Python路径：{real_python_path}，是否存在：{os.path.exists(real_python_path)}")

        script_dir = os.path.dirname(task.script_path)
        command = f'"{real_python_path}" -X utf8 "{task.script_path}" "{device_serial}"'
        env = os.environ.copy()
        env.update({
            'PYTHONIOENCODING': 'utf-8',
            'PYTHONLEGACYWINDOWSSTDIO': 'utf-8',
            'LC_ALL': 'en_US.UTF-8',
            'LANG': 'en_US.UTF-8'
        })

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

        if r:
            process_info = {
                "pid": process.pid,
                "device_serial": device_serial,
                "log_id": log_id,
                "task_id": task_id,
                "celery_task_id": celery_task_id  # 兼容同步（传None）
            }
            r.hset(settings.SCRIPT_REDIS_PROCESS_HASH, log_id, json.dumps(process_info))  # 【修改】使用settings
            logger.info(f"脚本任务{log_id}进程{process.pid}已存入Redis")

        # 移除 Celery 专属的 update_state（同步时不需要）
        start_time = time.time()
        log.stdout = log.stdout or ''
        log_header = f"""【执行环境信息】
工作目录：{script_dir}
Python路径：{real_python_path}
Python路径是否存在：{os.path.exists(real_python_path)}
执行命令：{command}
系统编码：{sys.getfilesystemencoding()}
Python IO编码：{os.environ.get('PYTHONIOENCODING', '未设置')}
进程ID：{process.pid}
Celery任务ID：{celery_task_id or '同步执行（无）'}

【执行日志】
任务启动时间：{timezone.now()}
"""
        log.stdout += log_header
        log.save()
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f'script_log_{log_id}',
            {
                'type': 'log_update',
                'data': {
                    'stdout': log.stdout,
                    'stderr': log.stderr,
                    'status': log.exec_status
                }
            }
        )

        stdout_thread = threading.Thread(
            target=read_stream,
            args=(process.stdout, f"stdout_{process.pid}", log, True),
            daemon=True
        )
        stderr_thread = threading.Thread(
            target=read_stream,
            args=(process.stderr, f"stderr_{process.pid}", log, False),
            daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            process.wait(timeout=settings.SCRIPT_EXECUTION_TIMEOUT)  # 【修改】使用settings
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)

            return_code = process.returncode
            log.return_code = return_code
            log.exec_duration = time.time() - start_time

            if return_code == 0:
                log.exec_status = "success"
                log.stdout += f"\n\n【执行完成】返回码：0，耗时：{log.exec_duration:.2f}秒"
            else:
                log.exec_status = "failed"
                log.stdout += f"\n\n【执行失败】返回码：{return_code}，耗时：{log.exec_duration:.2f}秒"

        except subprocess.TimeoutExpired:
            logger.info(f"任务{log_id}执行超时（{settings.SCRIPT_EXECUTION_TIMEOUT}秒），发送停止信号...")  # 【修改】使用settings
            if r:
                r.set(
                    f"{settings.SCRIPT_REDIS_STOP_FLAG_PREFIX}{device_serial}",  # 【修改】使用settings
                    "True",
                    ex=settings.SCRIPT_REDIS_STOP_FLAG_EXPIRE  # 【修改】使用你原有配置
                )
                log.stderr += f"\n\n【执行超时】超过{settings.SCRIPT_EXECUTION_TIMEOUT}秒，已发送停止信号，等待脚本优雅退出..."  # 【修改】使用settings
                log.save()

            time.sleep(settings.SCRIPT_STOP_WAIT_TIME)  # 【修改】使用你原有配置
            _graceful_terminate_process(process.pid, wait_time=settings.SCRIPT_PROCESS_TERMINATE_WAIT)  # 【修改】使用你原有配置

            log.exec_status = "timeout"
            log.stderr += f"\n\n【执行超时】进程{process.pid}已终止，总耗时：{settings.SCRIPT_EXECUTION_TIMEOUT}秒"  # 【修改】使用settings
            log.exec_duration = settings.SCRIPT_EXECUTION_TIMEOUT  # 【修改】使用settings

        except Exception as e:
            if r and r.get(f"{settings.SCRIPT_REDIS_STOP_FLAG_PREFIX}{device_serial}") == "True":  # 【修改】使用settings
                logger.info(f"任务{log_id}收到手动停止信号，等待日志输出...")
                time.sleep(settings.SCRIPT_STOP_WAIT_TIME)  # 【修改】使用你原有配置
                stdout_thread.join(timeout=3)
                stderr_thread.join(timeout=3)

                _graceful_terminate_process(process.pid, wait_time=settings.SCRIPT_PROCESS_TERMINATE_WAIT)  # 【修改】使用你原有配置

                log.exec_status = "stopped"
                log.stderr += f"\n\n【任务停止】收到手动停止信号，进程{process.pid}已终止，设备：{device_serial}"
            else:
                logger.error(f"任务{log_id}执行异常：{str(e)}")
                _graceful_terminate_process(process.pid, wait_time=1)  # 紧急终止保持1秒（可根据需要新增配置）
                log.exec_status = "error"
                log.stderr += f"\n\n【执行异常】{type(e).__name__}：{str(e)}，已终止进程{process.pid}"
            log.exec_duration = time.time() - start_time

        log.end_time = timezone.now()
        log.save()

        if r:
            r.delete(f"{settings.SCRIPT_REDIS_STOP_FLAG_PREFIX}{device_serial}")  # 【修改】使用settings
            r.hdel(settings.SCRIPT_REDIS_PROCESS_HASH, log_id)  # 【修改】使用settings

        return {"status": log.exec_status, "log_id": log_id}

    except Exception as e:
        logger.error(f"脚本任务执行失败：{str(e)}", exc_info=True)
        if log:
            log.exec_status = "error"
            current_stderr = log.stderr or ''
            log.stderr = current_stderr + f"\n\n【系统异常】{type(e).__name__}：{str(e)}"
            log.end_time = timezone.now()
            log.save()
        if r:
            r.delete(f"{settings.SCRIPT_REDIS_STOP_FLAG_PREFIX}{device_serial}")  # 【修改】使用settings
            r.hdel(settings.SCRIPT_REDIS_PROCESS_HASH, log_id)  # 【修改】使用settings
        if process and process.poll() is None:
            _graceful_terminate_process(process.pid, wait_time=1)  # 紧急终止保持1秒
        return {"status": "error", "msg": str(e)}


# ====================== Celery 异步任务（调用核心逻辑） ======================
@shared_task(bind=True, max_retries=0, time_limit=3600)
def execute_script_task(self, task_id, device_id, log_id, python_path):
    """Celery 异步执行入口"""
    return _execute_script_core(
        task_id=task_id,
        device_id=device_id,
        log_id=log_id,
        python_path=python_path,
        celery_task_id=self.request.id
    )


# ====================== 新增：后台线程同步执行（优雅降级） ======================
def execute_script_sync(task_id, device_id, log_id, python_path):
    """后台线程同步执行入口（不阻塞 HTTP 请求）"""
    thread = threading.Thread(
        target=_execute_script_core,
        args=(task_id, device_id, log_id, python_path),
        kwargs={"celery_task_id": None},
        daemon=True
    )
    thread.start()
    logger.info(f"已启动后台同步执行线程 - 日志ID：{log_id}")