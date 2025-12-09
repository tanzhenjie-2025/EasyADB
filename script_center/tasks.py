import subprocess
import sys
import time
import psutil
import os
import redis
import json
from celery import shared_task
from django.utils import timezone
from .models import ScriptTask, TaskExecutionLog
from adb_manager.models import ADBDevice
import logging

logger = logging.getLogger(__name__)


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


def _terminate_process(pid: int):
    """彻底终止进程及所有子进程"""
    try:
        parent = psutil.Process(pid)
        # 终止所有子进程（递归）
        for child in parent.children(recursive=True):
            try:
                child.terminate()
            except Exception as e:
                logger.warning(f"终止子进程{child.pid}失败：{str(e)}")
        # 终止主进程
        parent.terminate()
        time.sleep(1)
        # 强制杀死未终止的进程
        if parent.is_running():
            parent.kill()
        logger.info(f"进程{pid}及其子进程已彻底终止")
    except Exception as e:
        logger.error(f"终止进程{pid}失败：{str(e)}")


@shared_task(bind=True, max_retries=0, time_limit=3600)  # 最大执行时间1小时
def execute_script_task(self, task_id, device_id, log_id):
    """Celery异步执行单个设备的脚本任务（支持超时/停止/Redis信号）"""
    try:
        # 1. 获取基础数据
        task = ScriptTask.objects.get(id=task_id)
        device = ADBDevice.objects.get(id=device_id)
        log = TaskExecutionLog.objects.get(id=log_id)
        device_serial = device.adb_connect_str

        # 2. 处理Python路径（兼容原有WindowsApps逻辑）
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

        # 4. 启动子进程
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

        # 5. 保存进程信息到Redis（用于停止功能）
        r = get_redis_conn()
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

        # 7. 执行监控（支持超时/Redis停止信号）
        start_time = time.time()
        log.stdout = f"""【执行环境信息】
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
        log.save()

        try:
            # 循环检测停止信号和超时
            while True:
                # 检查Redis停止信号
                if r and r.get(f"airtest_stop_flag_{device_serial}") == "True":
                    raise Exception("收到手动停止信号")

                # 检查超时（5分钟）
                if time.time() - start_time > 300:
                    raise subprocess.TimeoutExpired(command, 300)

                # 非阻塞读取输出（实时更新日志）
                stdout_chunk = process.stdout.readline()
                stderr_chunk = process.stderr.readline()
                if stdout_chunk:
                    log.stdout += stdout_chunk
                    log.save()
                if stderr_chunk:
                    log.stderr += stderr_chunk
                    log.save()

                # 进程已结束
                if process.poll() is not None:
                    break

                time.sleep(0.1)

            # 8. 处理执行结果
            return_code = process.poll()
            log.return_code = return_code
            log.exec_duration = time.time() - start_time

            if return_code == 0:
                log.exec_status = "success"
                log.stdout += f"\n\n【执行完成】返回码：0，耗时：{log.exec_duration:.2f}秒"
            else:
                log.exec_status = "failed"
                log.stdout += f"\n\n【执行失败】返回码：{return_code}，耗时：{log.exec_duration:.2f}秒"

        except subprocess.TimeoutExpired:
            # 超时处理
            _terminate_process(process.pid)
            log.exec_status = "timeout"
            log.stderr += f"\n\n【执行超时】超过5分钟，已强制终止进程{process.pid}"
            log.exec_duration = 300

        except Exception as e:
            # 手动停止/其他异常
            _terminate_process(process.pid)
            log.exec_status = "stopped"
            log.stderr += f"\n\n【任务停止】{str(e)}，已终止进程{process.pid}"
            log.exec_duration = time.time() - start_time

        # 9. 最终更新日志
        log.end_time = timezone.now()
        log.save()

        # 10. 清理Redis
        if r:
            r.delete(f"airtest_stop_flag_{device_serial}")
            r.hdel("script_running_processes", log_id)

        return {"status": log.exec_status, "log_id": log_id}

    except Exception as e:
        logger.error(f"Celery脚本任务执行失败：{str(e)}", exc_info=True)
        if 'log' in locals():
            log.exec_status = "error"
            log.stderr = f"{log.stderr}\n\n【系统异常】{type(e).__name__}：{str(e)}"
            log.end_time = timezone.now()
            log.save()
        return {"status": "error", "msg": str(e)}