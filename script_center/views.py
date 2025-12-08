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
import psutil  # 需要安装：pip install psutil
import redis
from datetime import datetime
from django.utils import timezone

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

# ===================== 全局存储（关键：记录运行中的进程） =====================
# 存储格式：{log_id: {"pid": 进程ID, "device_serial": 设备序列号, "process": 进程对象}}
running_processes = {}
# 进程锁（防止多线程竞争）
process_lock = threading.Lock()


# 初始化Redis连接（和脚本保持一致）
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
            # 修正：使用正确的命名空间+URL名称
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
            # 修正：使用正确的命名空间+URL名称
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
        # 修正：使用正确的命名空间+URL名称
        return redirect(f"{reverse('script_center:task_list')}?msg={success_msg}")


# ===================== 任务执行/停止核心逻辑 =====================
class ExecuteTaskView(View):
    """执行任务页面 + 执行逻辑"""

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
            # 标记是否正在运行
            log.is_running = log.id in running_processes and log.exec_status == "running"
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
                # 修正：使用正确的命名空间+URL名称
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            # 2. 校验任务是否有效
            task = get_object_or_404(ScriptTask, id=task_id, status="active")
            logger.info(f"获取到任务：{task.task_name}，脚本路径：{task.script_path}，Python路径：{task.python_path}")

            if not task.is_script_exists():
                error_msg = quote(f"任务脚本不存在：{task.script_path}")
                logger.error(error_msg)
                # 修正：使用正确的命名空间+URL名称
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            # 放宽Python路径校验
            real_python_path = None
            python_warning = ""
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
                        logger.info(f"找到真实Python路径：{real_python_path}")
                        break

                if not real_python_path:
                    # 改为警告，不阻断执行
                    python_warning = "（注意：Python路径为WindowsApps快捷方式，可能执行失败，请手动配置真实路径）"
                    logger.warning(f"未找到真实Python路径，使用原路径：{task.python_path}")
                    real_python_path = task.python_path

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
                # 修正：使用正确的命名空间+URL名称
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            # 4. 为每个有效设备创建日志并启动执行线程
            threads = []
            for device_id in valid_device_ids:
                device = get_object_or_404(ADBDevice, id=device_id)
                # 创建执行日志
                log = TaskExecutionLog.objects.create(
                    task=task,
                    device=device,
                    exec_status="running",
                    exec_command=f"准备执行：{task.python_path} {task.script_path} {device.adb_connect_str}",
                    stdout=f"任务启动中{python_warning}",
                    start_time=timezone.now()
                )
                logger.info(f"创建执行日志 - ID：{log.id}，设备：{device.device_name}")

                # 启动线程执行脚本
                try:
                    t = threading.Thread(
                        target=run_script_thread,
                        args=(task.id, device.id, log.id, real_python_path)
                    )
                    t.daemon = True
                    t.start()
                    threads.append(t)
                    logger.info(f"启动执行线程 - 日志ID：{log.id}，线程ID：{t.ident}")
                    time.sleep(0.5)
                except Exception as thread_err:
                    log.exec_status = "error"
                    log.stderr = f"线程启动失败：{str(thread_err)}"
                    log.end_time = timezone.now()
                    log.save()
                    logger.error(f"线程启动失败 - 日志ID：{log.id}，错误：{str(thread_err)}")

            # 5. 返回执行成功提示
            success_msg = quote(
                f"任务【{task.task_name}】已启动！共{len(valid_device_ids)}个在线设备执行中{python_warning}")
            if offline_devices:
                success_msg = quote(f"{success_msg}（离线设备已过滤：{','.join(offline_devices)}）")
            logger.info(success_msg)
            # 修正：使用正确的命名空间+URL名称
            return redirect(f"{reverse('script_center:task_execute')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"执行任务失败：{str(e)}", exc_info=True)
            error_msg = quote(f"执行任务失败：{str(e)}")
            # 修正：使用正确的命名空间+URL名称
            return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")


class StopTaskView(View):
    """停止任务执行（按日志ID）"""

    def get(self, request, log_id):
        try:
            logger.info(f"接收到停止任务请求 - 日志ID：{log_id}")

            # 1. 获取日志对象
            log = get_object_or_404(TaskExecutionLog, id=log_id)
            if log.exec_status != "running":
                error_msg = quote(f"任务【{log.task.task_name}】未在运行中！当前状态：{log.exec_status}")
                logger.warning(error_msg)
                return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

            # 2. 加锁操作进程字典
            with process_lock:
                if log_id not in running_processes:
                    error_msg = quote(f"未找到任务【{log.task.task_name}】的运行进程！")
                    logger.warning(error_msg)
                    return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")

                # 获取进程信息
                process_info = running_processes[log_id]
                pid = process_info["pid"]
                device_serial = process_info["device_serial"]
                process = process_info.get("process")

                # 3. 步骤1：发送Redis停止信号（优雅退出）
                r = get_redis_conn()
                if r:
                    r.set(f"airtest_stop_flag_{device_serial}", "True", ex=60)
                    logger.info(f"已向Redis发送停止信号 - 设备：{device_serial}，Key：airtest_stop_flag_{device_serial}")

                # 4. 步骤2：修复Windows信号发送逻辑（核心）
                try:
                    if os.name == 'nt':  # Windows系统
                        # 方案1：先尝试终止进程组（修复CREATE_NEW_PROCESS_GROUP导致的信号问题）
                        parent = psutil.Process(pid)
                        # 获取所有子进程（递归，包括ADB等）
                        children = parent.children(recursive=True)
                        logger.info(f"找到进程{pid}的子进程：{[p.pid for p in children]}")

                        # 先终止子进程
                        for child in children:
                            try:
                                child.terminate()
                                logger.info(f"已终止子进程{child.pid}")
                            except:
                                continue
                        # 等待子进程终止
                        time.sleep(1)

                        # 再终止主进程
                        if parent.is_running():
                            parent.terminate()
                            logger.info(f"已终止主进程{pid}")
                    else:  # Linux/Mac
                        os.kill(pid, signal.SIGINT)
                        logger.info(f"已向进程{pid}发送SIGINT信号")
                except Exception as e:
                    logger.warning(f"发送信号失败：{str(e)}，将强制终止进程")

                # 5. 步骤3：强制终止兜底（增强）
                time.sleep(2)
                if process and process.poll() is None:
                    process.kill()  # 直接杀死，替代terminate
                    logger.info(f"进程{pid}未响应，已强制杀死")
                # 额外兜底：通过psutil杀死进程（防止subprocess对象失效）
                try:
                    parent = psutil.Process(pid)
                    if parent.is_running():
                        parent.kill()
                        logger.info(f"通过psutil强制杀死进程{pid}")
                except:
                    pass

                # 6. 清理进程字典
                del running_processes[log_id]
                logger.info(f"已从运行列表移除日志ID：{log_id}")

                # 7. 更新日志状态
                log.exec_status = "stopped"
                log.stderr = f"任务已手动停止\n- 停止时间：{timezone.now()}\n- 设备序列号：{device_serial}\n- 终止进程ID：{pid}"
                log.end_time = timezone.now()
                log.save()

                # 8. 清理Redis停止信号
                if r:
                    r.delete(f"airtest_stop_flag_{device_serial}")

                success_msg = quote(f"任务【{log.task.task_name}】已成功停止！设备：{device_serial}")
                logger.info(success_msg)
                return redirect(f"{reverse('script_center:execute_task')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"停止任务失败：{str(e)}", exc_info=True)
            error_msg = quote(f"停止任务失败：{str(e)}")
            return redirect(f"{reverse('script_center:execute_task')}?msg={error_msg}")


def run_script_thread(task_id, device_id, log_id, real_python_path=None):
    """线程执行脚本函数（记录进程PID）"""
    logger.info(f"线程开始执行 - 任务ID：{task_id}，设备ID：{device_id}，日志ID：{log_id}")

    # 强制获取日志对象
    try:
        log = TaskExecutionLog.objects.get(id=log_id)
    except Exception as e:
        logger.error(f"获取日志对象失败：{str(e)}")
        return

    process = None
    try:
        # 1. 初始化基础信息
        task = ScriptTask.objects.get(id=task_id)
        device = ADBDevice.objects.get(id=device_id)
        device_serial = device.adb_connect_str

        logger.info(f"线程初始化完成 - 任务：{task.task_name}，设备：{device_serial}")

        # 2. 基础校验
        if not device_serial:
            log.exec_status = "error"
            log.stderr = "设备配置无效：未填写序列号/IP+端口"
            log.end_time = timezone.now()
            log.save()
            logger.error(f"设备{device.id}配置无效，无序列号/IP+端口")
            return

        # 3. 确定最终Python路径
        final_python_path = real_python_path if real_python_path else task.python_path
        logger.info(f"最终Python路径：{final_python_path}，是否存在：{os.path.exists(final_python_path)}")

        # 4. 构建执行命令
        script_dir = os.path.dirname(task.script_path)
        command = f'"{final_python_path}" -X utf8 "{task.script_path}" "{device_serial}"'
        logger.info(f"执行命令：{command}，工作目录：{script_dir}")

        # 5. 更新日志基础信息
        log.exec_command = command
        log.save()

        # 6. 执行脚本（记录进程对象和PID）
        start_time = time.time()

        # 构建环境变量
        env = os.environ.copy()
        env.update({
            'PYTHONIOENCODING': 'utf-8',
            'PYTHONLEGACYWINDOWSSTDIO': 'utf-8',
            'LC_ALL': 'en_US.UTF-8',
            'LANG': 'en_US.UTF-8'
        })

        # 关键：启动进程并记录PID（移除了Windows进程组创建参数，修复信号传递问题）
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=script_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace"
            # 已移除：creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        )

        # 记录进程信息到全局字典
        with process_lock:
            running_processes[log_id] = {
                "pid": process.pid,
                "device_serial": device_serial,
                "process": process,
                "log_id": log_id,
                "task_name": task.task_name
            }
        logger.info(f"进程已启动 - PID：{process.pid}，日志ID：{log_id}，设备：{device_serial}")

        # 7. 等待进程执行完成并获取输出
        stdout, stderr = process.communicate(timeout=300)  # 超时时间可根据需求调整
        return_code = process.returncode

        # 8. 计算耗时和更新日志
        end_time = time.time()
        log.end_time = timezone.now()
        log.exec_duration = end_time - start_time
        log.stdout = f"""【执行环境信息】
工作目录：{script_dir}
Python路径：{final_python_path}
Python路径是否存在：{os.path.exists(final_python_path)}
执行命令：{command}
系统编码：{sys.getfilesystemencoding()}
Python IO编码：{os.environ.get('PYTHONIOENCODING', '未设置')}
进程ID：{process.pid}

【标准输出】
{stdout}"""
        log.stderr = stderr

        # 9. 判断执行结果
        if return_code == 0:
            log.exec_status = "success"
            logger.info(f"设备{device_serial}执行任务{task.task_name}成功，返回码：{return_code}")
        elif return_code == -2 or return_code == 1:  # Ctrl+C或手动停止的返回码
            log.exec_status = "stopped"
            logger.info(f"设备{device_serial}任务{task.task_name}被手动停止，返回码：{return_code}")
        else:
            log.exec_status = "failed"
            logger.error(f"设备{device_serial}执行失败，返回码：{return_code}，错误：{stderr}")

        # 10. 清理进程字典
        with process_lock:
            if log_id in running_processes:
                del running_processes[log_id]

        log.save()

    except subprocess.TimeoutExpired:
        # 超时处理
        if process:
            process.terminate()
            with process_lock:
                if log_id in running_processes:
                    del running_processes[log_id]

        log.exec_status = "timeout"
        log.stderr = f"执行超时（超过5分钟）\n设备序列号：{device_serial if 'device_serial' in locals() else '未知'}\n任务名称：{task.task_name if 'task' in locals() else '未知'}\n进程ID：{process.pid if process else '未知'}"
        log.end_time = timezone.now()
        log.save()
        logger.error(f"设备{device_serial if 'device_serial' in locals() else '未知'}执行任务超时")

    except Exception as e:
        # 通用异常处理
        if process:
            try:
                process.terminate()
            except:
                pass
            with process_lock:
                if log_id in running_processes:
                    del running_processes[log_id]

        log.exec_status = "error"
        log.stderr = f"""【异常信息】
类型：{type(e).__name__}
描述：{str(e)}
设备序列号：{device_serial if 'device_serial' in locals() else '未知'}
Python路径：{final_python_path if 'final_python_path' in locals() else '未知'}
执行命令：{command if 'command' in locals() else '未生成'}
进程ID：{process.pid if process else '未知'}
完整异常栈：{logging.Formatter().formatException(sys.exc_info())}"""
        log.end_time = timezone.now()
        log.save()
        logger.error(f"执行脚本异常：{str(e)}", exc_info=True)


class LogDetailView(View):
    """查看执行日志详情"""

    def get(self, request, log_id):
        log = get_object_or_404(TaskExecutionLog, id=log_id)
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
            return JsonResponse({
                "code": 200,
                "status": log.exec_status,
                "stdout": log.stdout,
                "stderr": log.stderr,
                "duration": log.exec_duration,
                "is_running": log.id in running_processes and log.exec_status == "running"
            })
        except Exception as e:
            return JsonResponse({
                "code": 500,
                "msg": str(e)
            })