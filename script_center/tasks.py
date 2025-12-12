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
from .models import ScriptTask, TaskExecutionLog
from adb_manager.models import ADBDevice
import logging

logger = logging.getLogger(__name__)

# 全局变量：存储实时输出
stdout_buffer = {}
stderr_buffer = {}


# 新增导入：Channels的Channel Layer
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

# 1. 改造read_stream函数：每更新日志就推送
def read_stream(stream, buffer_key, log, is_stdout=True):
    """实时读取子进程输出流（线程执行）+ 推送WebSocket"""
    global stdout_buffer, stderr_buffer
    buffer = []
    channel_layer = get_channel_layer()  # 获取Channel Layer
    try:
        for line in iter(stream.readline, ''):
            if line:
                buffer.append(line)
                # 实时写入日志（避免丢失）
                if is_stdout:
                    log.stdout += line
                else:
                    log.stderr += line
                # 每1行保存一次（保证实时性）+ 推送WebSocket
                log.save()
                # 推送日志到WebSocket分组
                async_to_sync(channel_layer.group_send)(
                    f'script_log_{log.id}',  # 对应Consumers的分组名
                    {
                        'type': 'log_update',  # 对应Consumers的log_update方法
                        'data': {
                            'stdout': log.stdout,
                            'stderr': log.stderr,
                            'status': log.exec_status
                        }
                    }
                )
                # 强制刷新缓冲区
                sys.stdout.flush()
        # 剩余内容写入并推送
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


# Redis连接函数（和视图层保持一致）
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


def _graceful_terminate_process(pid: int, wait_time: int = 5):
    """
    优雅终止进程（兼容脚本优雅退出）
    :param pid: 进程ID
    :param wait_time: 等待脚本优雅退出的时间（秒）→ 延长至5秒，确保日志输出
    """
    try:
        parent = psutil.Process(pid)
        logger.info(f"开始优雅终止进程{pid}，等待{wait_time}秒让脚本清理并输出日志...")

        # 等待脚本优雅退出（延长等待时间）
        for i in range(wait_time):
            if not parent.is_running():
                logger.info(f"进程{pid}已自行退出（优雅退出成功）")
                return
            time.sleep(1)
            logger.info(f"等待脚本优雅退出中...剩余{wait_time - i - 1}秒")

        # 等待超时后，强制终止所有子进程（递归）
        logger.warning(f"进程{pid}未自行退出，开始强制终止子进程...")
        for child in parent.children(recursive=True):
            try:
                child.terminate()
                logger.info(f"已终止子进程{child.pid}")
            except Exception as e:
                logger.warning(f"终止子进程{child.pid}失败：{str(e)}")

        # 强制终止主进程
        parent.terminate()
        time.sleep(1)

        # 最终强制kill未终止的进程
        if parent.is_running():
            parent.kill()
            logger.warning(f"已强制杀死进程{pid}")
        else:
            logger.info(f"进程{pid}已终止")

    except Exception as e:
        logger.error(f"终止进程{pid}失败：{str(e)}")


@shared_task(bind=True, max_retries=0, time_limit=3600)  # 最大执行时间1小时
def execute_script_task(self, task_id, device_id, log_id):
    """Celery异步执行单个设备的脚本任务（支持超时/停止/Redis信号 + 实时日志）"""
    log = None  # 初始化log，防止异常时locals()无该变量
    device_serial = ""
    r = get_redis_conn()  # 提前初始化Redis连接
    process = None
    stdout_thread = None
    stderr_thread = None
    try:
        # 1. 获取基础数据
        task = ScriptTask.objects.get(id=task_id)
        device = ADBDevice.objects.get(id=device_id)
        log = TaskExecutionLog.objects.get(id=log_id)
        device_serial = device.adb_connect_str

        # 2. 处理Python路径（兼容原有WindowsApps逻辑 + 有效性检查）
        real_python_path = task.python_path
        if "WindowsApps" in task.python_path:
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
                    logger.info(f"替换Python路径：{task.python_path} → {real_python_path}")
                    break

        # 强制检查Python路径有效性
        if not os.path.exists(real_python_path):
            raise Exception(f"Python路径无效：{real_python_path}（原始路径：{task.python_path}）")
        logger.info(f"使用Python路径：{real_python_path}，是否存在：{os.path.exists(real_python_path)}")

        # 3. 构建执行命令和环境变量
        script_dir = os.path.dirname(task.script_path)
        command = f'"{real_python_path}" -X utf8 "{task.script_path}" "{device_serial}"'
        env = os.environ.copy()
        env.update({
            'PYTHONIOENCODING': 'utf-8',
            'PYTHONLEGACYWINDOWSSTDIO': 'utf-8',
            'LC_ALL': 'en_US.UTF-8',
            'LANG': 'en_US.UTF-8'
        })

        # 4. 启动子进程（修改为实时输出模式）
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=script_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # 行缓冲，实时输出
            universal_newlines=True  # 兼容换行符
        )

        # 5. 保存进程信息到Redis（用于停止功能）
        if r:
            process_info = {
                "pid": process.pid,
                "device_serial": device_serial,
                "log_id": log_id,
                "task_id": task_id,
                "celery_task_id": self.request.id
            }
            r.hset("script_running_processes", log_id, json.dumps(process_info))
            logger.info(f"脚本任务{log_id}进程{process.pid}已存入Redis")

        # 6. 更新Celery任务状态（用于后续追踪）
        self.update_state(state='RUNNING', meta={
            'pid': process.pid,
            'log_id': log_id,
            'device_serial': device_serial
        })

        # 7. 初始化日志头（确保基础信息存在）
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
Celery任务ID：{self.request.id}

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

        # 8. 启动线程实时读取stdout/stderr（核心修复：避免日志丢失）
        stdout_thread = threading.Thread(
            target=read_stream,
            args=(process.stdout, f"stdout_{process.pid}", log, True),
            daemon=True  # 守护线程，随主进程退出
        )
        stderr_thread = threading.Thread(
            target=read_stream,
            args=(process.stderr, f"stderr_{process.pid}", log, False),
            daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        # 9. 等待进程完成/超时/停止（替代原communicate）
        try:
            # 等待进程结束，同时检测超时（3000秒=50分钟）
            process.wait(timeout=3000)
            # 等待输出线程完成（确保所有输出被读取）
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)

            # 10. 处理正常执行结果
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
            # 超时处理：先发送停止信号，等待日志输出，再终止
            logger.info(f"任务{log_id}执行超时（50分钟），发送停止信号...")
            if r:
                r.set(f"airtest_stop_flag_{device_serial}", "True", ex=60)
                log.stderr += f"\n\n【执行超时】超过50分钟，已发送停止信号，等待脚本优雅退出..."
                log.save()

            # 等待5秒让脚本输出优雅退出日志
            time.sleep(5)
            # 优雅终止进程
            _graceful_terminate_process(process.pid, wait_time=3)

            log.exec_status = "timeout"
            log.stderr += f"\n\n【执行超时】进程{process.pid}已终止，总耗时：3000秒"
            log.exec_duration = 3000

        except Exception as e:
            # 优先检测手动停止信号（修复状态标记错误）
            if r and r.get(f"airtest_stop_flag_{device_serial}") == "True":
                logger.info(f"任务{log_id}收到手动停止信号，等待日志输出...")
                # 等待5秒让脚本输出优雅退出日志
                time.sleep(5)
                # 等待输出线程完成
                stdout_thread.join(timeout=3)
                stderr_thread.join(timeout=3)

                # 优雅终止进程
                _graceful_terminate_process(process.pid, wait_time=3)

                log.exec_status = "stopped"  # 修复：标记为手动停止，而非error
                log.stderr += f"\n\n【任务停止】收到手动停止信号，进程{process.pid}已终止，设备：{device_serial}"
            else:
                # 其他异常：强制终止但保留日志
                logger.error(f"任务{log_id}执行异常：{str(e)}")
                _graceful_terminate_process(process.pid, wait_time=1)
                log.exec_status = "error"
                log.stderr += f"\n\n【执行异常】{type(e).__name__}：{str(e)}，已终止进程{process.pid}"
            log.exec_duration = time.time() - start_time

        # 11. 最终更新日志（强制保存所有输出）
        log.end_time = timezone.now()
        log.save()

        # 12. 清理Redis
        if r:
            r.delete(f"airtest_stop_flag_{device_serial}")
            r.hdel("script_running_processes", log_id)

        return {"status": log.exec_status, "log_id": log_id}

    except Exception as e:
        logger.error(f"Celery脚本任务执行失败：{str(e)}", exc_info=True)
        if log:
            log.exec_status = "error"
            # 核心防护：确保stderr/stdout不是None再拼接
            current_stderr = log.stderr or ''
            log.stderr = current_stderr + f"\n\n【系统异常】{type(e).__name__}：{str(e)}"
            log.end_time = timezone.now()
            log.save()
        # 清理Redis残留
        if r:
            r.delete(f"airtest_stop_flag_{device_serial}")
            r.hdel("script_running_processes", log_id)
        # 终止未结束的进程
        if process and process.poll() is None:
            _graceful_terminate_process(process.pid, wait_time=1)
        return {"status": "error", "msg": str(e)}