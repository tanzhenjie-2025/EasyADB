import subprocess
import redis
from django.conf import settings
from celery import shared_task
from adb_manager.models import ADBDevice

# 初始化Redis（复用Django配置）
r = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    decode_responses=True  # 自动解码为字符串，避免b''前缀
)


def check_and_reconnect(device: ADBDevice):
    """
    检查单个设备的ADB连接状态，并尝试重连
    :param device: ADBDevice模型实例
    :return: 连接状态（True/False）
    """
    connect_str = device.adb_connect_str
    # 防命令注入：过滤特殊字符（关键！）
    safe_connect_str = connect_str.replace(";", "").replace("|", "").replace("&", "")

    try:
        # 执行ADB连接命令
        result = subprocess.run(
            f"adb connect {safe_connect_str}",
            shell=True,
            capture_output=True,
            encoding="utf-8",
            timeout=10  # 超时保护，避免卡壳
        )

        # 更新设备状态到Redis
        if "connected to" in result.stdout:
            # 同步设备号（serial）：执行adb devices获取真实serial
            devices_result = subprocess.run(
                "adb devices",
                shell=True,
                capture_output=True,
                encoding="utf-8"
            )
            if safe_connect_str in devices_result.stdout:
                device.device_serial = safe_connect_str
                device.save()  # 更新到Django模型

            r.set(f"adb:device:{safe_connect_str}", "online")
            r.set(f"adb:device:{safe_connect_str}:stdout", result.stdout)
            print(f"设备 {safe_connect_str} 连接成功")
            return True
        else:
            r.set(f"adb:device:{safe_connect_str}", "offline")
            r.set(f"adb:device:{safe_connect_str}:stderr", result.stderr)
            print(f"设备 {safe_connect_str} 连接失败：{result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        r.set(f"adb:device:{safe_connect_str}", "offline")
        r.set(f"adb:device:{safe_connect_str}:stderr", "ADB连接超时")
        print(f"设备 {safe_connect_str} 连接超时")
        return False
    except Exception as e:
        r.set(f"adb:device:{safe_connect_str}", "error")
        r.set(f"adb:device:{safe_connect_str}:stderr", str(e))
        print(f"设备 {safe_connect_str} 连接异常：{str(e)}")
        return False


@shared_task(name="check_and_reconnect_all_devices")
def check_and_reconnect_all_devices():
    """
    Celery定时任务：检查所有启用的ADB设备，尝试重连
    """
    # 从Django模型获取所有启用的设备
    active_devices = ADBDevice.objects.filter(is_active=True)
    for device in active_devices:
        check_and_reconnect(device)
    return f"已检查 {active_devices.count()} 台设备"


@shared_task(name="connect_specified_device")
def connect_specified_device(device_id: int):
    """
    手动触发：连接指定ID的设备（供Web界面调用）
    :param device_id: ADBDevice的主键ID
    """
    try:
        device = ADBDevice.objects.get(id=device_id)
        result = check_and_reconnect(device)
        return {
            "success": result,
            "device": device.adb_connect_str,
            "status": r.get(f"adb:device:{device.adb_connect_str}")
        }
    except ADBDevice.DoesNotExist:
        return {"success": False, "error": "设备不存在"}