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

            adb_path = r"C:\Users\TanZhenJie\AppData\Local\Android\Sdk\platform-tools\adb.exe"
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


# 原有代码...（保持不变）

class ADBDevicesListView(View):
    """执行adb devices命令，返回所有已连接设备"""
    def get(self, request):
        try:
            # 复用现有ADB路径配置
            adb_path = r"C:\Users\TanZhenJie\AppData\Local\Android\Sdk\platform-tools\adb.exe"
            if not os.path.exists(adb_path):
                adb_path = "adb"

            # 执行adb devices命令
            cmd = [adb_path, "devices", "-l"]  # -l参数显示详细信息
            logger.info(f"执行命令：{' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                encoding="utf-8",
                timeout=10
            )

            # 解析命令结果
            output = result.stdout.strip()
            error = result.stderr.strip()
            connected_devices = []

            if output:
                # 按行解析结果（跳过首行"List of devices attached"）
                lines = output.splitlines()[1:]
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith("adb:"):
                        # 分割设备信息（格式：序列号 状态 详细信息）
                        parts = line.split(maxsplit=2)
                        if len(parts) >= 2:
                            device_info = {
                                "serial": parts[0].strip(),
                                "status": parts[1].strip(),
                                "details": parts[2].strip() if len(parts) >=3 else ""
                            }
                            connected_devices.append(device_info)

            # 返回结果
            return JsonResponse({
                "code": 200,
                "msg": "执行成功",
                "data": {
                    "raw_output": output,
                    "error": error,
                    "connected_devices": connected_devices,
                    "device_count": len(connected_devices)
                }
            })

        except subprocess.TimeoutExpired:
            logger.error("adb devices命令执行超时")
            return JsonResponse({
                "code": 500,
                "msg": "命令执行超时",
                "data": {"raw_output": "", "error": "执行超时", "connected_devices": [], "device_count": 0}
            })
        except Exception as e:
            logger.error(f"执行adb devices失败：{str(e)}", exc_info=True)
            return JsonResponse({
                "code": 500,
                "msg": f"执行失败：{str(e)}",
                "data": {"raw_output": "", "error": str(e), "connected_devices": [], "device_count": 0}
            })


import re  # 确保已导入re模块


# 原有代码保留，仅替换电池解析部分

class ADBDeviceDetailView(View):
    """获取指定设备的详细信息（厂商、型号、系统版本、电量、WiFi IP等）"""

    def get(self, request):
        try:
            device_id = request.GET.get("device_id")
            if not device_id or not device_id.isdigit():
                return JsonResponse({
                    "code": 400,
                    "msg": "参数错误：device_id必须为数字",
                    "data": {}
                })

            # 获取设备信息
            device = get_object_or_404(ADBDevice, id=device_id)
            connect_id = device.connect_identifier
            if not connect_id:
                return JsonResponse({
                    "code": 400,
                    "msg": "设备未配置序列号/IP+端口，无法获取详情",
                    "data": {}
                })

            # ADB路径配置（复用现有配置）
            adb_path = r"C:\Users\TanZhenJie\AppData\Local\Android\Sdk\platform-tools\adb.exe"
            if not os.path.exists(adb_path):
                adb_path = "adb"

            # 定义需要执行的ADB命令列表（新增电池备用命令）
            commands = {
                "brand": [adb_path, "-s", connect_id, "shell", "getprop", "ro.product.brand"],  # 厂商
                "model": [adb_path, "-s", connect_id, "shell", "getprop", "ro.product.model"],  # 型号
                "system_version": [adb_path, "-s", connect_id, "shell", "getprop", "ro.build.version.release"],  # 系统版本
                "serial": [adb_path, "-s", connect_id, "get-serialno"],  # 设备序列号
                "battery": [adb_path, "-s", connect_id, "shell", "dumpsys", "battery"],  # 电池信息
                "battery_level_backup": [adb_path, "-s", connect_id, "shell", "getprop", "status.battery.level"],
                # 电量备用命令
                "battery_health_backup": [adb_path, "-s", connect_id, "shell", "getprop", "status.battery.health"],
                # 健康度备用
                "battery_status_backup": [adb_path, "-s", connect_id, "shell", "getprop", "status.battery.state"],
                # 状态备用
                "wifi_ip": [adb_path, "-s", connect_id, "shell", "ip", "addr", "show", "wlan0"]  # WiFi IP
            }

            # 执行所有命令并解析结果
            result_data = {
                "device_name": device.device_name,
                "connect_id": connect_id,
                "brand": "",
                "model": "",
                "system_version": "",
                "serial": "",
                "battery_level": "",
                "battery_health": "",
                "battery_status": "",
                "wifi_ip": "",
                "raw_commands": {}  # 保存原始命令输出（用于调试）
            }

            # 执行每个命令
            for cmd_key, cmd in commands.items():
                try:
                    logger.info(f"执行设备详情命令：{' '.join(cmd)}")
                    result = subprocess.run(
                        cmd,
                        shell=True,
                        capture_output=True,
                        encoding="utf-8",
                        timeout=15  # 延长超时时间（部分命令执行较慢）
                    )
                    stdout = result.stdout.strip()
                    stderr = result.stderr.strip()
                    result_data["raw_commands"][cmd_key] = {"stdout": stdout, "stderr": stderr}

                    # 解析不同命令的结果
                    if cmd_key == "brand":
                        result_data["brand"] = stdout or "未知"
                    elif cmd_key == "model":
                        result_data["model"] = stdout or "未知"
                    elif cmd_key == "system_version":
                        result_data["system_version"] = stdout or "未知"
                    elif cmd_key == "serial":
                        result_data["serial"] = stdout or "未知"
                    elif cmd_key == "battery":
                        # 解析电池信息（适配数字/字符串两种格式）
                        battery_info = stdout
                        if not battery_info:
                            result_data["battery_level"] = "未知"
                            result_data["battery_health"] = "未知"
                            result_data["battery_status"] = "未知"
                            continue

                        try:
                            # 正则匹配（兼容数字/字符串格式）
                            level_match = re.search(r"level:\s*(\d+)", battery_info, re.IGNORECASE)
                            health_match = re.search(r"health:\s*(\d+|\w+)", battery_info, re.IGNORECASE)  # 匹配数字或字符串
                            status_match = re.search(r"status:\s*(\d+|\w+)", battery_info, re.IGNORECASE)  # 匹配数字或字符串

                            # 电量解析
                            if level_match and level_match.group(1):
                                result_data["battery_level"] = f"{level_match.group(1)}%"
                            else:
                                result_data["battery_level"] = "未知"

                            # 健康度解析（同时兼容数字/字符串）
                            health_val = health_match.group(1).upper() if (
                                        health_match and health_match.group(1)) else ""
                            # 数字映射表（优先）
                            health_num_map = {
                                "1": "未知",
                                "2": "良好",
                                "3": "过热",
                                "4": "损坏",
                                "5": "过压",
                                "6": "未知故障",
                                "7": "过冷"
                            }
                            # 字符串映射表（兼容其他设备）
                            health_str_map = {
                                "GOOD": "良好",
                                "OVERHEAT": "过热",
                                "DEAD": "损坏",
                                "OVER_VOLTAGE": "过压",
                                "UNSPECIFIED_FAILURE": "未知故障",
                                "COLD": "过冷",
                                "UNKNOWN": "未知"
                            }
                            # 优先按数字匹配，再按字符串匹配
                            result_data["battery_health"] = health_num_map.get(health_val,
                                                                               health_str_map.get(health_val, "未知"))

                            # 状态解析（同时兼容数字/字符串）
                            status_val = status_match.group(1).upper() if (
                                        status_match and status_match.group(1)) else ""
                            # 数字映射表（优先）
                            status_num_map = {
                                "1": "未知",
                                "2": "充电中",
                                "3": "放电中",
                                "4": "未充电",
                                "5": "已充满"
                            }
                            # 字符串映射表（兼容其他设备）
                            status_str_map = {
                                "CHARGING": "充电中",
                                "DISCHARGING": "放电中",
                                "NOT_CHARGING": "未充电",
                                "FULL": "已充满",
                                "UNKNOWN": "未知",
                                "CONNECTED": "已连接电源（未充电）"
                            }
                            # 优先按数字匹配，再按字符串匹配
                            result_data["battery_status"] = status_num_map.get(status_val,
                                                                               status_str_map.get(status_val, "未知"))

                        except Exception as e:
                            logger.error(f"解析电池信息失败：{str(e)}")
                            result_data["battery_level"] = "未知"
                            result_data["battery_health"] = "未知"
                            result_data["battery_status"] = "未知"
                    # 备用电池信息解析（主命令失败时生效）
                    elif cmd_key == "battery_level_backup":
                        if not result_data["battery_level"] or result_data["battery_level"] == "未知":
                            result_data["battery_level"] = f"{stdout}%" if stdout else "未知"
                    elif cmd_key == "battery_health_backup":
                        if not result_data["battery_health"] or result_data["battery_health"] == "未知":
                            health_map = {"good": "良好", "bad": "损坏", "unknown": "未知"}
                            result_data["battery_health"] = health_map.get(stdout.lower(), stdout or "未知")
                    elif cmd_key == "battery_status_backup":
                        if not result_data["battery_status"] or result_data["battery_status"] == "未知":
                            status_map = {"charging": "充电中", "discharging": "放电中", "full": "已充满",
                                          "not_charging": "未充电"}
                            result_data["battery_status"] = status_map.get(stdout.lower(), stdout or "未知")
                    elif cmd_key == "wifi_ip":
                        # 解析WiFi IP（安卓10+）
                        ip_lines = stdout.splitlines()
                        for line in ip_lines:
                            if "inet " in line and not "127.0.0.1" in line:
                                ip_part = line.split("inet ")[1].split("/")[0].strip()
                                result_data["wifi_ip"] = ip_part
                                break
                        if not result_data["wifi_ip"]:
                            result_data["wifi_ip"] = "未连接WiFi/无IP"

                except subprocess.TimeoutExpired:
                    result_data["raw_commands"][cmd_key] = {"stdout": "", "stderr": "命令执行超时"}
                    logger.error(f"设备{connect_id}执行{cmd_key}命令超时")
                except Exception as e:
                    result_data["raw_commands"][cmd_key] = {"stdout": "", "stderr": str(e)}
                    logger.error(f"设备{connect_id}执行{cmd_key}命令失败：{str(e)}")

            return JsonResponse({
                "code": 200,
                "msg": "获取设备详情成功",
                "data": result_data
            })

        except ADBDevice.DoesNotExist:
            return JsonResponse({
                "code": 404,
                "msg": "设备不存在",
                "data": {}
            })
        except Exception as e:
            logger.error(f"获取设备详情失败：{str(e)}", exc_info=True)
            return JsonResponse({
                "code": 500,
                "msg": f"获取失败：{str(e)}",
                "data": {}
            })


class ADBDeviceEnableWirelessView(View):
    """开启设备无线ADB功能（先查IP→查数据库→再执行端口操作，已有信息则提示不保存）"""

    def post(self, request):
        try:
            device_id = request.POST.get("device_id")
            if not device_id or not device_id.isdigit():
                error_msg = quote("参数错误：device_id必须为数字")
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            device = get_object_or_404(ADBDevice, id=device_id)
            connect_id = device.connect_identifier
            if not connect_id:
                error_msg = quote("设备未配置序列号/IP+端口，无法操作")
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            # 检查设备是否在线
            status = r.get(f"adb:device:{connect_id}") or "offline"
            if status != "online":
                error_msg = quote(f"设备{connect_id}当前不在线，无法开启无线ADB")
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            # 检查是否已经是无线连接（通过IP:端口形式判断）
            if ":" in connect_id:
                adb_path = r"C:\Users\谭振捷\AppData\Local\Android\Sdk\platform-tools\adb.exe"
                if not os.path.exists(adb_path):
                    adb_path = "adb"

                # 先获取WiFi IP（统一路径+兼容解析）
                wifi_ip = self._get_wifi_ip(adb_path, connect_id)
                success_msg = quote(f"设备{connect_id}已是无线连接状态，IP地址：{wifi_ip}")
                return redirect(f"{reverse('adb_manager:index')}?msg={success_msg}")

            # ========== 核心调整：先获取IP，再查数据库，最后执行tcpip命令 ==========
            # 1. 统一ADB路径（使用正确的"谭振捷"路径）
            adb_path = r"C:\Users\谭振捷\AppData\Local\Android\Sdk\platform-tools\adb.exe"
            if not os.path.exists(adb_path):
                adb_path = "adb"

            # 2. 先获取设备WiFi IP（提前获取，失败则直接提示）
            wifi_ip = self._get_wifi_ip(adb_path, connect_id)
            if wifi_ip == "获取IP失败":
                error_msg = quote(f"设备{connect_id}获取WiFi IP失败，无法开启无线ADB，请检查设备网络连接！")
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            # 3. 【关键】先查数据库：检查当前设备/其他设备是否已有该无线信息
            db_check_msg = ""
            need_save = True  # 是否需要保存数据库的标记

            # 3.1 检查当前设备是否已有IP+端口
            if device.device_ip and device.device_port:
                db_check_msg = f"该设备已配置无线信息（IP：{device.device_ip}，端口：{device.device_port}）"
                need_save = False
            # 3.2 检查其他设备是否占用该IP+端口（避免唯一约束冲突）
            elif ADBDevice.objects.filter(device_ip=wifi_ip, device_port=5555).exclude(id=device.id).exists():
                db_check_msg = f"IP：{wifi_ip} 端口：5555 已被其他设备占用"
                need_save = False

            # 4. 执行tcpip命令开启无线ADB（无论是否保存数据库，都执行端口操作）
            cmd = [adb_path, "-s", connect_id, "tcpip", "5555"]
            logger.info(f"执行开启无线ADB命令：{' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                encoding="utf-8",
                timeout=15
            )

            # 5. 验证tcpip命令执行结果
            success_keywords = ["restarting in TCP mode port: 5555", "already in TCP mode", "restarting TCP port 5555"]
            if any(kw in result.stdout for kw in success_keywords) or result.returncode == 0:
                # 更新Redis状态
                r.set(f"adb:device:{connect_id}:stdout", result.stdout or f"设备{connect_id}开启无线ADB成功")
                r.set(f"adb:device:{connect_id}:stderr", "")

                # 6. 根据数据库检查结果决定是否保存
                if need_save:
                    # 无冲突，保存到数据库
                    device.device_ip = wifi_ip
                    device.device_port = 5555
                    device.save()
                    success_msg = quote(
                        f"设备{connect_id}已成功开启无线ADB！IP地址：{wifi_ip}，可通过 {wifi_ip}:5555 连接"
                    )
                else:
                    # 已有信息，仅提示不保存
                    success_msg = quote(
                        f"设备{connect_id}已成功开启无线ADB！{db_check_msg}，本次未更新数据库，可通过 {wifi_ip}:5555 连接"
                    )
            else:
                # 端口操作失败
                r.set(f"adb:device:{connect_id}:stderr", result.stderr or result.stdout or "开启无线ADB失败")
                success_msg = quote(f"设备{connect_id}开启无线ADB失败：{result.stderr or result.stdout}")

            return redirect(f"{reverse('adb_manager:index')}?msg={success_msg}")

        except Exception as e:
            logger.error(f"开启无线ADB失败：{str(e)}", exc_info=True)
            error_msg = quote(f"开启无线ADB失败：{str(e)}")
            return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

    def _get_wifi_ip(self, adb_path, connect_id):
        """封装IP获取逻辑（复用详情页解析规则，增加备选命令）"""
        # 定义多套IP获取命令（兼容不同安卓版本）
        ip_commands = [
            [adb_path, "-s", connect_id, "shell", "ip", "addr", "show", "wlan0"],  # 优先
            [adb_path, "-s", connect_id, "shell", "getprop", "dhcp.wlan0.ipaddress"],  # 备选1
            [adb_path, "-s", connect_id, "shell", "getprop", "wifi.ip.address"],  # 备选2
            [adb_path, "-s", connect_id, "shell", "ifconfig", "wlan0"],  # 备选3（旧安卓）
        ]

        for cmd in ip_commands:
            try:
                logger.info(f"执行IP获取命令：{' '.join(cmd)}")
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    encoding="utf-8",
                    timeout=15
                )

                if result.returncode == 0 and result.stdout:
                    # 复用详情页的IP解析逻辑（确保和详情页结果一致）
                    ip_lines = result.stdout.splitlines()
                    for line in ip_lines:
                        line = line.strip()
                        if "inet " in line and not "127.0.0.1" in line and not "::" in line:
                            ip_part = line.split("inet ")[1].split("/")[0].strip()
                            if ip_part and "." in ip_part:  # 验证是IPv4地址
                                logger.info(f"成功获取IP：{ip_part}")
                                return ip_part
                        # 兼容getprop直接返回IP的情况
                        elif "." in line and len(line.split(".")) == 4:
                            logger.info(f"成功获取IP（getprop）：{line.strip()}")
                            return line.strip()
            except Exception as e:
                logger.warning(f"IP获取命令执行失败：{str(e)}")
                continue

        logger.warning(f"所有IP获取命令均失败，设备：{connect_id}")
        return "获取IP失败"