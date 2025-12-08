# adb_manager/tasks.py
import subprocess
import redis
from celery import shared_task
from django.conf import settings
from .models import ADBDevice

# 初始化Redis
r = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    decode_responses=True
)

def safe_adb_connect(device_connect_str):
    """安全执行ADB连接命令（防注入）"""
    # 过滤危险字符
    safe_str = device_connect_str.replace(";", "").replace("|", "").replace("&", "").replace("$", "")
    try:
        result = subprocess.run(
            f"adb connect {safe_str}",
            shell=True,
            capture_output=True,
            encoding="utf-8",
            timeout=10
        )
        return {
            "success": "connected to" in result.stdout,
            "stdout": result.stdout,
            "stderr": result.stderr
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": "连接超时"}
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": str(e)}

@shared_task(name="adb_manager.check_and_reconnect_device")
def check_and_reconnect_device(device_id):
    """检查并重连单个设备"""
    try:
        device = ADBDevice.objects.get(id=device_id, is_active=True)
        connect_str = device.adb_connect_str
        # 执行ADB连接
        res = safe_adb_connect(connect_str)
        # 更新Redis状态
        r.set(f"adb:device:{connect_str}", "online" if res["success"] else "offline")
        r.set(f"adb:device:{connect_str}:stdout", res["stdout"])
        r.set(f"adb:device:{connect_str}:stderr", res["stderr"])
        # 更新设备序列号（如果连接成功）
        if res["success"]:
            devices_result = subprocess.run(
                "adb devices", shell=True, capture_output=True, encoding="utf-8"
            )
            if connect_str in devices_result.stdout:
                device.device_serial = connect_str
                device.save()
        return {"device_id": device_id, "success": res["success"], "connect_str": connect_str}
    except ADBDevice.DoesNotExist:
        return {"device_id": device_id, "success": False, "error": "设备不存在/已禁用"}

@shared_task(name="adb_manager.check_all_devices")
def check_all_devices():
    """检查所有启用的设备"""
    active_devices = ADBDevice.objects.filter(is_active=True)
    results = []
    for device in active_devices:
        res = check_and_reconnect_device(device.id)
        results.append(res)
    return {"total": active_devices.count(), "results": results}