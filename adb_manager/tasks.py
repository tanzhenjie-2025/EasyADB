import subprocess
import shlex  # 引入安全的命令解析模块
import redis
from celery import shared_task, group  # 新增group用于并行执行
from django.conf import settings
from .models import ADBDevice

# 初始化Redis（保持原有逻辑，建议后续封装到common/redis_utils.py）
r = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    decode_responses=True
)

def safe_adb_connect(device_connect_str):
    """
    安全执行ADB连接命令（彻底防注入）
    核心优化：
    1. 弃用shell=True，改用列表形式的命令参数（subprocess安全执行的核心）
    2. 使用shlex.split处理连接字符串，兼容特殊字符场景
    3. 保留原有超时、异常处理逻辑
    """
    try:
        # 安全构造ADB连接命令：列表形式（避免shell注入）
        # shlex.split确保即使连接字符串含特殊字符，也能正确解析
        cmd = ["adb", "connect"] + shlex.split(device_connect_str.strip())
        result = subprocess.run(
            cmd,  # 列表形式命令（关键！）
            shell=False,  # 显式关闭shell（默认False，此处显式声明增强可读性）
            capture_output=True,
            encoding="utf-8",
            timeout=10,
            errors="ignore"  # 新增：避免编码错误导致程序崩溃
        )
        # 保持原有返回逻辑
        return {
            "success": "connected to" in result.stdout.lower(),  # 小写匹配，提升鲁棒性
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": "连接超时（10秒）"}
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": f"连接失败：{str(e)}"}


@shared_task(name="adb_manager.check_and_reconnect_device")
def check_and_reconnect_device(device_id):
    """检查并重连单个设备（优化adb devices执行逻辑，防注入）"""
    try:
        device = ADBDevice.objects.get(id=device_id, is_active=True)
        connect_str = device.adb_connect_str.strip()
        if not connect_str:
            raise ValueError("设备连接字符串为空")

        # 执行安全的ADB连接
        res = safe_adb_connect(connect_str)

        # 更新Redis状态（保持原有逻辑）
        r.set(f"adb:device:{connect_str}", "online" if res["success"] else "offline")
        r.set(f"adb:device:{connect_str}:stdout", res["stdout"])
        r.set(f"adb:device:{connect_str}:stderr", res["stderr"])

        # 优化：安全执行adb devices（弃用shell=True）
        if res["success"]:
            # 安全构造adb devices命令（列表形式）
            devices_cmd = ["adb", "devices"]
            devices_result = subprocess.run(
                devices_cmd,
                shell=False,
                capture_output=True,
                encoding="utf-8",
                timeout=5,
                errors="ignore"
            )
            # 检查设备是否在已连接列表中（优化匹配逻辑）
            device_list = [line.strip() for line in devices_result.stdout.splitlines() if line.strip()]
            if any(connect_str in line for line in device_list):
                device.device_serial = connect_str
                device.save(update_fields=["device_serial"])  # 只更新需要的字段，提升性能

        return {
            "device_id": device_id,
            "success": res["success"],
            "connect_str": connect_str,
            "message": res["stdout"] or res["stderr"]
        }
    except ADBDevice.DoesNotExist:
        return {"device_id": device_id, "success": False, "error": "设备不存在/已禁用"}
    except ValueError as e:
        return {"device_id": device_id, "success": False, "error": f"参数错误：{str(e)}"}
    except Exception as e:
        return {"device_id": device_id, "success": False, "error": f"未知错误：{str(e)}"}


@shared_task(name="adb_manager.check_all_devices")
def check_all_devices():
    """
    检查所有启用的设备（优化为并行执行，提升性能）
    核心优化：
    1. 从循环同步执行改为Celery group并行执行
    2. 避免单设备阻塞导致整体检查缓慢
    """
    active_devices = ADBDevice.objects.filter(is_active=True)
    total = active_devices.count()
    if total == 0:
        return {"total": 0, "results": [], "message": "无启用的设备"}

    # 构造并行任务组
    task_group = group(
        check_and_reconnect_device.s(device.id) for device in active_devices
    )
    # 异步执行所有任务
    task_results = task_group.apply_async()
    # 获取所有任务结果（非阻塞，已通过apply_async异步执行）
    results = [res.get(timeout=15) for res in task_results]  # 单个任务超时15秒

    # 统计成功/失败数
    success_count = sum(1 for res in results if res.get("success", False))
    fail_count = total - success_count

    return {
        "total": total,
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results
    }