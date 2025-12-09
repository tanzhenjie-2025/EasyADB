import subprocess
import sys
import time
import psutil
import os
from celery import shared_task
from django.utils import timezone
from .models import OrchestrationLog, StepExecutionLog, TaskStep
from script_center.models import ScriptTask
from adb_manager.models import ADBDevice
import logging
import redis  # 新增Redis导入

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=0, time_limit=3600)  # 最大执行时间1小时
def execute_step_task(self, step_id, orch_log_id, device_data):
    """Celery异步执行单个步骤，支持超时自动停止+Redis连接检查"""
    try:
        # 第一步：检查Redis连接（辅助排查连接问题）
        try:
            r = redis.Redis(host='127.0.0.1', port=6379, db=0, socket_timeout=5)
            r.ping()
            logger.info(f"Celery任务{self.request.id}：Redis连接正常")
        except Exception as redis_e:
            logger.error(f"Celery任务{self.request.id}：Redis连接失败 - {str(redis_e)}", exc_info=True)
            # 不终止任务，继续执行（Redis仅用于Celery消息队列，不影响脚本执行）

        # 获取任务实例
        step = TaskStep.objects.get(id=step_id)
        orch_log = OrchestrationLog.objects.get(id=orch_log_id)
        device = ADBDevice.objects.get(id=device_data['id'])
        script_task = step.script_task

        # 创建步骤日志（和原有逻辑一致）
        step_log = StepExecutionLog.objects.create(
            orchestration_log=orch_log,
            step=step,
            exec_status="running",
            start_time=timezone.now()
        )

        # 更新编排日志进度（和原有逻辑一致）
        orch_log.completed_steps += 1
        orch_log.save()

        # 校验脚本是否存在（和原有逻辑一致）
        if not hasattr(script_task, 'is_script_exists') or not script_task.is_script_exists():
            error_msg = f"脚本文件不存在：{script_task.script_path if script_task else '未知路径'}"
            step_log.exec_status = "error"
            step_log.error_msg = error_msg
            step_log.end_time = timezone.now()
            step_log.save()
            return {"status": "error", "msg": error_msg}

        # 处理Python路径（兼容WindowsApps，保留原有逻辑）
        real_python_path = _get_real_python_path(script_task)

        # 构建执行命令（和原有逻辑一致）
        script_dir = os.path.dirname(script_task.script_path)
        command = f'"{real_python_path}" -X utf8 "{script_task.script_path}" "{device.adb_connect_str}"'
        step_log.exec_command = command
        step_log.save()

        # 构建环境变量（和原有逻辑一致）
        env = os.environ.copy()
        env.update({
            'PYTHONIOENCODING': 'utf-8',
            'PYTHONLEGACYWINDOWSSTDIO': 'utf-8',
            'LC_ALL': 'en_US.UTF-8',
            'LANG': 'en_US.UTF-8'
        })

        # 启动进程（和原有逻辑一致）
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

        # 记录进程ID（用于后续终止）
        self.update_state(state='RUNNING', meta={
            'pid': process.pid,
            'step_id': step_id,
            'log_id': orch_log_id
        })

        # 等待执行（设置超时，超时自动终止）
        step_start_time = time.time()
        try:
            stdout, stderr = process.communicate(timeout=step.run_duration)
            return_code = process.returncode

            # 更新步骤日志（和原有逻辑一致）
            step_log.stdout = stdout
            step_log.stderr = stderr
            step_log.return_code = return_code
            step_log.exec_duration = time.time() - step_start_time

            if return_code == 0:
                step_log.exec_status = "completed"
                step_log.error_msg = ""
            else:
                step_log.exec_status = "failed"
                step_log.error_msg = f"执行失败，返回码：{return_code}"

        except subprocess.TimeoutExpired:
            # 超时处理：彻底终止进程及子进程
            _terminate_process(process.pid)
            step_log.exec_status = "timeout"
            step_log.error_msg = f"执行超时（{step.run_duration}秒）"
            step_log.exec_duration = step.run_duration
            stderr = f"进程超时被终止（{step.run_duration}秒）"
            step_log.stderr = stderr

        # 通用异常处理
        except Exception as e:
            if process:
                process.terminate()
            error_detail = f"""【异常信息】
类型：{type(e).__name__}
描述：{str(e)}
完整栈：{sys.exc_info()}"""
            step_log.exec_status = "error"
            step_log.error_msg = error_detail
            step_log.stderr = error_detail
            step_log.exec_duration = time.time() - step_start_time

        # 最终保存步骤日志
        step_log.end_time = timezone.now()
        step_log.save()
        return {"status": step_log.exec_status, "step_id": step_id}

    except Exception as e:
        # 任务级异常处理
        if 'step_log' in locals():
            step_log.exec_status = "error"
            step_log.error_msg = f"任务执行异常：{str(e)}"
            step_log.end_time = timezone.now()
            step_log.save()
        logger.error(f"Celery任务执行失败：{str(e)}", exc_info=True)
        return {"status": "error", "msg": str(e)}

def _get_real_python_path(script_task: ScriptTask) -> str:
    """获取真实的Python路径（保留原有逻辑）"""
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
                return path
    return script_task.python_path

def _terminate_process(pid: int):
    """彻底终止进程及所有子进程"""
    try:
        parent = psutil.Process(pid)
        # 终止所有子进程
        for child in parent.children(recursive=True):
            try:
                child.terminate()
            except:
                pass
        # 终止主进程
        parent.terminate()
        # 等待1秒后检查，未终止则强制杀死
        time.sleep(1)
        if parent.is_running():
            parent.kill()
    except Exception as e:
        logger.error(f"终止进程{pid}失败：{str(e)}")