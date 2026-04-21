import subprocess
import shlex
import redis
import threading
import logging
from celery import shared_task, group
from django.conf import settings
from django.db import close_old_connections

logger = logging.getLogger(__name__)

# ====================== Redis 初始化（保持不变） ======================
r = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    decode_responses=True
)


def safe_adb_connect(device_connect_str):
    """安全执行ADB连接命令（彻底防注入）"""
    try:
        cmd = ["adb", "connect"] + shlex.split(device_connect_str.strip())
        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            encoding="utf-8",
            timeout=10,
            errors="ignore"
        )
        return {
            "success": "connected to" in result.stdout.lower(),
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": "连接超时（10秒）"}
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": f"连接失败：{str(e)}"}


# ====================== 核心：抽离单个设备检查逻辑（兼容异步/同步） ======================
def _check_and_reconnect_device_core(device_id):
    """检查并重连单个设备的核心逻辑（不依赖Celery）"""
    try:
        from .models import ADBDevice
        close_old_connections()

        device = ADBDevice.objects.get(id=device_id, is_active=True)
        connect_str = device.connect_identifier.strip()  # 【修改】统一使用 connect_identifier
        if not connect_str:
            raise ValueError("设备连接字符串为空")

        res = safe_adb_connect(connect_str)

        r.set(f"adb:device:{connect_str}", "online" if res["success"] else "offline")
        r.set(f"adb:device:{connect_str}:stdout", res["stdout"])
        r.set(f"adb:device:{connect_str}:stderr", res["stderr"])

        if res["success"]:
            devices_cmd = ["adb", "devices"]
            devices_result = subprocess.run(
                devices_cmd,
                shell=False,
                capture_output=True,
                encoding="utf-8",
                timeout=5,
                errors="ignore"
            )
            device_list = [line.strip() for line in devices_result.stdout.splitlines() if line.strip()]
            if any(connect_str in line for line in device_list):
                device.device_serial = connect_str
                device.save(update_fields=["device_serial"])

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
        logger.error(f"设备检查异常：{str(e)}", exc_info=True)
        return {"device_id": device_id, "success": False, "error": f"未知错误：{str(e)}"}
    finally:
        close_old_connections()


# ====================== Celery 异步任务（调用核心逻辑） ======================
@shared_task(name="adb_manager.check_and_reconnect_device")
def check_and_reconnect_device(device_id):
    """Celery异步检查并重连单个设备"""
    return _check_and_reconnect_device_core(device_id)


# ====================== 后台线程同步执行（优雅降级） ======================
def check_and_reconnect_device_sync(device_id):
    """后台线程同步检查并重连单个设备（不阻塞HTTP请求）"""
    thread = threading.Thread(
        target=_check_and_reconnect_device_core,
        args=(device_id,),
        daemon=True
    )
    thread.start()
    logger.info(f"已启动后台同步线程检查设备 - 设备ID：{device_id}")


# ====================== 核心：抽离所有设备检查逻辑（兼容异步/同步） ======================
def _check_all_devices_core():
    """检查所有启用设备的核心逻辑（兼容Celery/线程）"""
    from .models import ADBDevice
    close_old_connections()

    active_devices = ADBDevice.objects.filter(is_active=True)
    total = active_devices.count()
    if total == 0:
        return {"total": 0, "results": [], "message": "无启用的设备"}

    results = []
    if hasattr(settings, 'USE_CELERY') and settings.USE_CELERY:  # 【修改】安全判断 settings.USE_CELERY
        # Celery 模式：并行执行
        task_group = group(
            check_and_reconnect_device.s(device.id) for device in active_devices
        )
        task_results = task_group.apply_async()
        results = [res.get(timeout=15) for res in task_results]
    else:
        # 线程模式：并行执行（每个设备一个线程）
        threads = []
        results_dict = {}

        def thread_worker(device_id):
            result = _check_and_reconnect_device_core(device_id)
            results_dict[device_id] = result

        for device in active_devices:
            t = threading.Thread(
                target=thread_worker,
                args=(device.id,),
                daemon=True
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=15)

        results = [
            results_dict.get(
                device.id,
                {"device_id": device.id, "success": False, "error": "线程执行超时"}
            )
            for device in active_devices
        ]

    success_count = sum(1 for res in results if res.get("success", False))
    fail_count = total - success_count

    return {
        "total": total,
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results
    }


# ====================== Celery 异步任务（所有设备） ======================
@shared_task(name="adb_manager.check_all_devices")
def check_all_devices():
    """Celery异步检查所有启用的设备"""
    return _check_all_devices_core()


# ====================== 后台线程同步执行（所有设备） ======================
def check_all_devices_sync():
    """后台线程同步检查所有启用的设备（优雅降级）"""
    thread = threading.Thread(
        target=_check_all_devices_core,
        daemon=True
    )
    thread.start()
    logger.info("已启动后台同步线程检查所有设备")