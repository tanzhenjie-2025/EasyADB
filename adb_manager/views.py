from django.conf import settings
import redis
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.urls import reverse
from urllib.parse import quote
from .models import ADBDevice
from .forms import ADBDeviceForm
import logging
import subprocess
import os

logger = logging.getLogger(__name__)

# 初始化Redis
try:
    r = redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        decode_responses=True,
        socket_timeout=2,
        retry_on_timeout=True
    )
    r.ping()
    logger.info("Redis连接成功")
except Exception as e:
    logger.error(f"Redis连接失败：{e}")
    class EmptyRedis:
        def get(self, key, *args, **kwargs):
            return None
        def set(self, key, value, *args, **kwargs):
            return None
        def delete(self, key, *args, **kwargs):
            return None
    r = EmptyRedis()


def index(request):
    """易控ADB首页"""
    devices = ADBDevice.objects.all()
    logger.info(f"数据库查询到的设备数量：{devices.count()}")
    logger.info(f"数据库设备详情：{[str(dev) for dev in devices]}")

    device_list = []
    for dev in devices:
        try:
            connect_id = dev.connect_identifier
            logger.info(f"处理设备：{connect_id}")

            status = r.get(f"adb:device:{connect_id}") or "offline"
            stdout = r.get(f"adb:device:{connect_id}:stdout") or ""
            stderr = r.get(f"adb:device:{connect_id}:stderr") or ""

            device_item = {
                "id": dev.id,
                "name": dev.device_name,
                "ip": dev.device_ip,
                "port": dev.device_port,
                "serial": dev.device_serial,
                "connect_id": connect_id,
                "is_active": dev.is_active,
                "status": status,
                "stdout": stdout,
                "stderr": stderr
            }
            device_list.append(device_item)
            logger.info(f"设备{connect_id}组装后数据：{device_item}")
        except Exception as dev_err:
            logger.error(f"处理设备{dev}失败：{dev_err}")
            continue

    context = {
        "csrf_token": get_token(request),
        "page_title": "易控ADB - 设备管控中心",
        "devices": device_list
    }
    return render(request, "adb_manager/index.html", context)


class ADBDeviceStatusView(View):
    """获取所有设备状态接口"""
    def get(self, request):
        try:
            devices = ADBDevice.objects.all()
            device_list = []
            for dev in devices:
                connect_id = dev.connect_identifier
                status = r.get(f"adb:device:{connect_id}") or "unknown"
                stdout = r.get(f"adb:device:{connect_id}:stdout") or ""
                stderr = r.get(f"adb:device:{connect_id}:stderr") or ""
                dev_dict = {
                    "id": dev.id,
                    "device_name": dev.device_name,
                    "device_ip": dev.device_ip,
                    "device_port": dev.device_port,
                    "device_serial": dev.device_serial,
                    "connect_id": connect_id,
                    "is_active": dev.is_active,
                    "status": status,
                    "stdout": stdout,
                    "stderr": stderr
                }
                device_list.append(dev_dict)
            return JsonResponse({
                "code": 200,
                "msg": "获取成功",
                "data": device_list
            })
        except Exception as e:
            return JsonResponse({
                "code": 500,
                "msg": f"获取失败：{str(e)}",
                "data": []
            })


class ADBDeviceConnectView(View):
    """手动连接指定设备（支持序列号/IP+端口）"""
    def post(self, request):
        try:
            device_id = request.POST.get("device_id")
            if not device_id or not device_id.isdigit():
                error_msg = quote("参数错误：device_id必须为数字")
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            device = get_object_or_404(ADBDevice, id=device_id)
            connect_id = device.connect_identifier
            if not connect_id:
                error_msg = quote("设备未配置序列号/IP+端口，无法连接")
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            # ADB路径配置
            adb_path = r"C:\Users\谭振捷\AppData\Local\Android\Sdk\platform-tools\adb.exe"
            if not os.path.exists(adb_path):
                adb_path = "adb"

            # 核心：支持序列号连接的ADB命令
            if ":" in connect_id:
                # IP:端口格式 - 用connect命令
                cmd = [adb_path, "connect", connect_id]
            else:
                # 纯序列号格式 - 先检查设备是否在线，再连接（USB/无线）
                cmd = [adb_path, "-s", connect_id, "wait-for-device", "shell", "echo", "connected"]

            logger.info(f"执行ADB连接命令：{' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                encoding="utf-8",
                timeout=10
            )

            # 结果判断
            success_keywords = ["connected to", "connected", "echo connected"]
            if any(kw in result.stdout for kw in success_keywords) or result.returncode == 0:
                r.set(f"adb:device:{connect_id}", "online")
                r.set(f"adb:device:{connect_id}:stdout", result.stdout or f"设备{connect_id}连接成功")
                r.set(f"adb:device:{connect_id}:stderr", "")
                success_msg = quote(f"设备{connect_id}连接成功！")
            else:
                r.set(f"adb:device:{connect_id}", "offline")
                r.set(f"adb:device:{connect_id}:stderr", result.stderr or result.stdout or "连接失败")
                success_msg = quote(f"设备{connect_id}连接失败：{result.stderr or result.stdout}")

            return redirect(f"{reverse('adb_manager:index')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"连接设备失败：{str(e)}", exc_info=True)
            error_msg = quote(f"连接失败：{str(e)}")
            return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")


class ADBDeviceDisconnectView(View):
    """手动断开ADB设备（支持序列号）"""
    def post(self, request):
        try:
            device_id = request.POST.get("device_id")
            if not device_id or not device_id.isdigit():
                error_msg = quote("参数错误：device_id必须为数字")
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            device = get_object_or_404(ADBDevice, id=device_id)
            connect_id = device.connect_identifier
            if not connect_id:
                error_msg = quote("设备未配置序列号/IP+端口，无法断开")
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            adb_path = r"C:\Users\谭振捷\AppData\Local\Android\Sdk\platform-tools\adb.exe"
            if not os.path.exists(adb_path):
                adb_path = "adb"

            # 断开命令（兼容序列号/IP+端口）
            if ":" in connect_id:
                cmd = [adb_path, "disconnect", connect_id]
            else:
                cmd = [adb_path, "-s", connect_id, "disconnect"]

            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                encoding="utf-8",
                timeout=10
            )

            # 更新状态
            if "disconnected" in result.stdout or result.returncode == 0:
                r.set(f"adb:device:{connect_id}", "offline")
                r.set(f"adb:device:{connect_id}:stdout", result.stdout)
                r.set(f"adb:device:{connect_id}:stderr", "")
                success_msg = quote(f"设备{connect_id}断开连接成功！")
            else:
                r.set(f"adb:device:{connect_id}", "error")
                r.set(f"adb:device:{connect_id}:stderr", result.stderr or result.stdout)
                success_msg = quote(f"设备{connect_id}断开连接失败：{result.stderr or result.stdout}")

            return redirect(f"{reverse('adb_manager:index')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"断开设备失败：{str(e)}", exc_info=True)
            error_msg = quote(f"断开连接失败：{str(e)}")
            return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")


class RefreshAllDevicesView(View):
    """刷新所有设备状态（支持序列号）"""
    def post(self, request):
        try:
            devices = ADBDevice.objects.filter(is_active=True)
            if not devices.exists():
                error_msg = quote("暂无启用的设备，无需刷新！")
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            adb_path = r"C:\Users\谭振捷\AppData\Local\Android\Sdk\platform-tools\adb.exe"
            if not os.path.exists(adb_path):
                adb_path = "adb"

            # 获取已连接的设备列表
            devices_cmd = [adb_path, "devices"]
            devices_result = subprocess.run(
                devices_cmd,
                shell=True,
                capture_output=True,
                encoding="utf-8",
                timeout=10
            )
            connected_devices = []
            for line in devices_result.stdout.splitlines():
                line = line.strip()
                if line and not line.startswith(("List of devices", "adb:")):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == "device":
                        connected_devices.append(parts[0].strip())

            # 遍历更新设备状态
            success_count = 0
            fail_count = 0
            for device in devices:
                connect_id = device.connect_identifier
                if not connect_id:
                    fail_count += 1
                    continue

                try:
                    if connect_id in connected_devices:
                        r.set(f"adb:device:{connect_id}", "online")
                        r.set(f"adb:device:{connect_id}:stdout", f"设备{connect_id}已连接（自动检测）")
                        r.set(f"adb:device:{connect_id}:stderr", "")
                        success_count += 1
                    else:
                        # 尝试重新连接
                        if ":" in connect_id:
                            cmd = [adb_path, "connect", connect_id]
                        else:
                            cmd = [adb_path, "-s", connect_id, "wait-for-device", "shell", "echo", "connected"]

                        result = subprocess.run(
                            cmd,
                            shell=True,
                            capture_output=True,
                            encoding="utf-8",
                            timeout=10
                        )

                        if any(kw in result.stdout for kw in ["connected to", "connected", "echo connected"]) or result.returncode == 0:
                            r.set(f"adb:device:{connect_id}", "online")
                            r.set(f"adb:device:{connect_id}:stdout", result.stdout)
                            r.set(f"adb:device:{connect_id}:stderr", "")
                            success_count += 1
                        else:
                            r.set(f"adb:device:{connect_id}", "offline")
                            r.set(f"adb:device:{connect_id}:stderr", result.stderr or result.stdout)
                            fail_count += 1
                except Exception as dev_err:
                    logger.error(f"处理设备{connect_id}失败：{dev_err}", exc_info=True)
                    r.set(f"adb:device:{connect_id}", "error")
                    r.set(f"adb:device:{connect_id}:stderr", str(dev_err))
                    fail_count += 1

            success_msg = quote(f"刷新完成！成功更新{success_count}台设备，失败{fail_count}台设备")
            return redirect(f"{reverse('adb_manager:index')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"刷新所有设备状态失败：{str(e)}", exc_info=True)
            error_msg = quote(f"刷新失败：{str(e)}")
            return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")


class AddDeviceView(View):
    """添加ADB设备（支持序列号）"""
    def get(self, request):
        form = ADBDeviceForm()
        if request.user.is_authenticated:
            form.fields["user"].initial = request.user.id

        context = {
            "page_title": "添加ADB设备 - 易控ADB",
            "form": form,
            "csrf_token": get_token(request)
        }
        return render(request, "adb_manager/add_device.html", context)

    def post(self, request):
        form = ADBDeviceForm(request.POST)
        if form.is_valid():
            device = form.save(commit=False)
            if request.user.is_authenticated and not device.user:
                device.user = request.user
            device.save()

            success_msg = quote(f"设备【{device.device_name}】添加成功！")
            return redirect(f"{reverse('adb_manager:index')}?msg={success_msg}")
        else:
            context = {
                "page_title": "添加ADB设备 - 易控ADB",
                "form": form,
                "csrf_token": get_token(request),
                "error_msg": "表单填写有误，请检查！"
            }
            return render(request, "adb_manager/add_device.html", context)


class EditDeviceView(View):
    """编辑ADB设备（支持序列号）"""
    def get(self, request, device_id):
        device = get_object_or_404(ADBDevice, id=device_id)
        form = ADBDeviceForm(instance=device)

        context = {
            "page_title": f"编辑设备 - {device.device_name}",
            "form": form,
            "device": device,
            "csrf_token": get_token(request)
        }
        return render(request, "adb_manager/edit_device.html", context)

    def post(self, request, device_id):
        device = get_object_or_404(ADBDevice, id=device_id)
        old_connect_id = device.connect_identifier
        form = ADBDeviceForm(request.POST, instance=device)

        if form.is_valid():
            updated_device = form.save()
            new_connect_id = updated_device.connect_identifier

            # 迁移Redis状态
            if old_connect_id != new_connect_id and old_connect_id:
                status = r.get(f"adb:device:{old_connect_id}") or "offline"
                stdout = r.get(f"adb:device:{old_connect_id}:stdout") or ""
                stderr = r.get(f"adb:device:{old_connect_id}:stderr") or ""

                if new_connect_id:
                    r.set(f"adb:device:{new_connect_id}", status)
                    r.set(f"adb:device:{new_connect_id}:stdout", stdout)
                    r.set(f"adb:device:{new_connect_id}:stderr", stderr)

                r.delete(f"adb:device:{old_connect_id}")
                r.delete(f"adb:device:{old_connect_id}:stdout")
                r.delete(f"adb:device:{old_connect_id}:stderr")

            success_msg = quote(f"设备【{updated_device.device_name}】修改成功！")
            return redirect(f"{reverse('adb_manager:index')}?msg={success_msg}")
        else:
            context = {
                "page_title": f"编辑设备 - {device.device_name}",
                "form": form,
                "device": device,
                "csrf_token": get_token(request),
                "error_msg": "表单填写有误，请检查！"
            }
            return render(request, "adb_manager/edit_device.html", context)


class DeleteDeviceView(View):
    """删除ADB设备"""
    def post(self, request, device_id):
        try:
            device = get_object_or_404(ADBDevice, id=device_id)
            device_name = device.device_name
            connect_id = device.connect_identifier

            # 断开设备
            adb_path = r"C:\Users\谭振捷\AppData\Local\Android\Sdk\platform-tools\adb.exe"
            if not os.path.exists(adb_path):
                adb_path = "adb"

            if connect_id:
                if ":" in connect_id:
                    cmd = [adb_path, "disconnect", connect_id]
                else:
                    cmd = [adb_path, "-s", connect_id, "disconnect"]
                subprocess.run(cmd, shell=True, capture_output=True, timeout=5)

                # 删除Redis状态
                r.delete(f"adb:device:{connect_id}")
                r.delete(f"adb:device:{connect_id}:stdout")
                r.delete(f"adb:device:{connect_id}:stderr")

            # 删除数据库记录
            device.delete()

            success_msg = quote(f"设备【{device_name}】删除成功！")
            return redirect(f"{reverse('adb_manager:index')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"删除设备失败：{str(e)}", exc_info=True)
            error_msg = quote(f"删除失败：{str(e)}")
            return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")


class ConnectAllDevicesView(View):
    """一键连接所有设备（支持序列号）"""
    def post(self, request):
        try:
            devices = ADBDevice.objects.filter(is_active=True)
            if not devices.exists():
                error_msg = quote("暂无启用的设备，无需连接！")
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            adb_path = r"C:\Users\谭振捷\AppData\Local\Android\Sdk\platform-tools\adb.exe"
            if not os.path.exists(adb_path):
                adb_path = "adb"

            success_count = 0
            fail_count = 0
            result_logs = []

            for device in devices:
                connect_id = device.connect_identifier
                if not connect_id:
                    fail_count += 1
                    result_logs.append(f"⚠️ {device.device_name}：未配置序列号/IP+端口")
                    continue

                try:
                    if ":" in connect_id:
                        cmd = [adb_path, "connect", connect_id]
                    else:
                        cmd = [adb_path, "-s", connect_id, "wait-for-device", "shell", "echo", "connected"]

                    result = subprocess.run(
                        cmd,
                        shell=True,
                        capture_output=True,
                        encoding="utf-8",
                        timeout=10
                    )

                    if any(kw in result.stdout for kw in ["connected to", "connected", "echo connected"]) or result.returncode == 0:
                        r.set(f"adb:device:{connect_id}", "online")
                        r.set(f"adb:device:{connect_id}:stdout", result.stdout)
                        r.set(f"adb:device:{connect_id}:stderr", "")
                        success_count += 1
                        result_logs.append(f"✅ {connect_id}：连接成功")
                    else:
                        r.set(f"adb:device:{connect_id}", "offline")
                        r.set(f"adb:device:{connect_id}:stderr", result.stderr or result.stdout)
                        fail_count += 1
                        result_logs.append(f"❌ {connect_id}：连接失败 - {result.stderr or result.stdout}")

                except Exception as e:
                    logger.error(f"一键连接 - 设备{connect_id}异常：{str(e)}")
                    r.set(f"adb:device:{connect_id}", "error")
                    r.set(f"adb:device:{connect_id}:stderr", str(e))
                    fail_count += 1
                    result_logs.append(f"⚠️ {connect_id}：操作异常 - {str(e)}")

            success_msg = quote(f"一键连接完成！成功{success_count}台，失败{fail_count}台。详情：{' | '.join(result_logs)}")
            return redirect(f"{reverse('adb_manager:index')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"一键连接全部设备失败：{str(e)}", exc_info=True)
            error_msg = quote(f"一键连接失败：{str(e)}")
            return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")


class DisconnectAllDevicesView(View):
    """一键断开所有设备（支持序列号）"""
    def post(self, request):
        try:
            devices = ADBDevice.objects.all()
            if not devices.exists():
                error_msg = quote("暂无设备，无需断开！")
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            adb_path = r"C:\Users\谭振捷\AppData\Local\Android\Sdk\platform-tools\adb.exe"
            if not os.path.exists(adb_path):
                adb_path = "adb"

            success_count = 0
            fail_count = 0
            result_logs = []

            for device in devices:
                connect_id = device.connect_identifier
                if not connect_id:
                    fail_count += 1
                    result_logs.append(f"⚠️ {device.device_name}：未配置序列号/IP+端口")
                    continue

                try:
                    if ":" in connect_id:
                        cmd = [adb_path, "disconnect", connect_id]
                    else:
                        cmd = [adb_path, "-s", connect_id, "disconnect"]

                    result = subprocess.run(
                        cmd,
                        shell=True,
                        capture_output=True,
                        encoding="utf-8",
                        timeout=10
                    )

                    if "disconnected" in result.stdout or result.returncode == 0:
                        r.set(f"adb:device:{connect_id}", "offline")
                        r.set(f"adb:device:{connect_id}:stdout", result.stdout)
                        r.set(f"adb:device:{connect_id}:stderr", "")
                        success_count += 1
                        result_logs.append(f"✅ {connect_id}：断开成功")
                    else:
                        r.set(f"adb:device:{connect_id}", "error")
                        r.set(f"adb:device:{connect_id}:stderr", result.stderr or result.stdout)
                        fail_count += 1
                        result_logs.append(f"❌ {connect_id}：断开失败 - {result.stderr or result.stdout}")

                except Exception as e:
                    logger.error(f"一键断开 - 设备{connect_id}异常：{str(e)}")
                    r.set(f"adb:device:{connect_id}", "error")
                    r.set(f"adb:device:{connect_id}:stderr", str(e))
                    fail_count += 1
                    result_logs.append(f"⚠️ {connect_id}：操作异常 - {str(e)}")

            success_msg = quote(f"一键断开完成！成功{success_count}台，失败{fail_count}台。详情：{' | '.join(result_logs)}")
            return redirect(f"{reverse('adb_manager:index')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"一键断开全部设备失败：{str(e)}", exc_info=True)
            error_msg = quote(f"一键断开失败：{str(e)}")
            return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")


class CSRFTokenView(View):
    """获取CSRF Token接口"""
    def get(self, request):
        return JsonResponse({
            "code": 200,
            "csrf_token": get_token(request)
        })