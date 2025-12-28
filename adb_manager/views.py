"""ADB设备管理视图（添加日志记录功能）"""
from django.conf import settings
import redis
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.urls import reverse
from urllib.parse import quote
from .models import ADBDevice, ADBDeviceOperationLog  # 导入新模型
from .forms import ADBDeviceForm
import logging
import subprocess
import os
import re

# 初始化日志
logger = logging.getLogger(__name__)


# ===================== 公共配置与工具函数 =====================
def get_adb_path():
    """获取ADB路径（从环境变量读取，不存在则使用系统默认）"""
    adb_path = os.getenv("ADB_PATH", "adb")
    if os.path.exists(adb_path):
        return adb_path
    return "adb"


def get_redis_client():
    """获取Redis客户端"""
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
        return r
    except Exception as e:
        logger.error(f"Redis连接失败：{e}")

        # 空实现，保证代码不报错
        class EmptyRedis:
            def get(self, key, *args, **kwargs):
                return None

            def set(self, key, value, *args, **kwargs):
                return None

            def delete(self, key, *args, **kwargs):
                return None

        return EmptyRedis()


def execute_adb_command(cmd, timeout=None):
    """执行ADB命令的公共方法"""
    if timeout is None:
        timeout = int(os.getenv("ADB_COMMAND_TIMEOUT", 10))

    logger.info(f"执行ADB命令：{' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout
        )
        return result
    except subprocess.TimeoutExpired:
        logger.error(f"ADB命令执行超时：{' '.join(cmd)}")
        raise
    except Exception as e:
        logger.error(f"ADB命令执行失败：{str(e)}", exc_info=True)
        raise


def get_wifi_ip(connect_id):
    """封装WiFi IP获取逻辑"""
    adb_path = get_adb_path()
    # 定义多套IP获取命令（兼容不同安卓版本）
    ip_commands = [
        [adb_path, "-s", connect_id, "shell", "ip", "addr", "show", "wlan0"],  # 优先
        [adb_path, "-s", connect_id, "shell", "getprop", "dhcp.wlan0.ipaddress"],  # 备选1
        [adb_path, "-s", connect_id, "shell", "getprop", "wifi.ip.address"],  # 备选2
        [adb_path, "-s", connect_id, "shell", "ifconfig", "wlan0"],  # 备选3（旧安卓）
    ]

    for cmd in ip_commands:
        try:
            result = execute_adb_command(cmd)
            if result.returncode == 0 and result.stdout:
                # 解析IP地址
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


# 新增：日志记录辅助函数
def log_device_operation(request, device, operation_type, result=True, details=""):
    """
    记录设备操作日志
    :param request: 请求对象
    :param device: 操作的设备对象，可为None（如一键操作）
    :param operation_type: 操作类型
    :param result: 操作结果，布尔值
    :param details: 操作详情
    """
    user = request.user if request.user.is_authenticated else None

    ADBDeviceOperationLog.objects.create(
        device=device,
        operation_type=operation_type,
        user=user,
        operation_result=result,
        operation_details=details
    )

    # 同时记录到系统日志
    username = user.username if user else "匿名用户"
    device_name = device.device_name if device else "无特定设备"
    logger.info(f"{username} {operation_type} {device_name} {'成功' if result else '失败'}: {details}")


# 初始化Redis客户端
r = get_redis_client()


# ===================== 视图函数/类 =====================
def index(request):
    """易控ADB首页（保持不变）"""
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
    """获取所有设备状态接口（保持不变）"""
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
    """手动连接指定设备（添加日志记录）"""
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
                log_device_operation(
                    request, device, 'connect', False,
                    f"连接失败：{error_msg}"
                )
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            # 从环境变量获取ADB路径
            adb_path = get_adb_path()

            # 核心：支持序列号连接的ADB命令
            if ":" in connect_id:
                # IP:端口格式 - 用connect命令
                cmd = [adb_path, "connect", connect_id]
            else:
                # 纯序列号格式 - 先检查设备是否在线，再连接（USB/无线）
                cmd = [adb_path, "-s", connect_id, "wait-for-device", "shell", "echo", "connected"]

            result = execute_adb_command(cmd)

            # 结果判断
            success_keywords = ["connected to", "connected", "echo connected"]
            if any(kw in result.stdout for kw in success_keywords) or result.returncode == 0:
                r.set(f"adb:device:{connect_id}", "online")
                r.set(f"adb:device:{connect_id}:stdout", result.stdout or f"设备{connect_id}连接成功")
                r.set(f"adb:device:{connect_id}:stderr", "")
                success_msg = f"设备{connect_id}连接成功！"
                log_device_operation(
                    request, device, 'connect', True,
                    success_msg + f" 输出: {result.stdout[:100]}"
                )
            else:
                r.set(f"adb:device:{connect_id}", "offline")
                r.set(f"adb:device:{connect_id}:stderr", result.stderr or result.stdout or "连接失败")
                success_msg = f"设备{connect_id}连接失败：{result.stderr or result.stdout}"
                log_device_operation(
                    request, device, 'connect', False,
                    success_msg
                )

            return redirect(f"{reverse('adb_manager:index')}?msg={quote(success_msg)}")

        except Exception as e:
            logger.error(f"连接设备失败：{str(e)}", exc_info=True)
            error_msg = f"连接失败：{str(e)}"
            # 尝试获取设备对象用于日志记录
            try:
                device = ADBDevice.objects.get(id=device_id) if device_id else None
            except:
                device = None
            log_device_operation(
                request, device, 'connect', False,
                error_msg
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(error_msg)}")


class ADBDeviceDisconnectView(View):
    """手动断开ADB设备（添加日志记录）"""
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
                log_device_operation(
                    request, device, 'disconnect', False,
                    f"断开失败：{error_msg}"
                )
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            adb_path = get_adb_path()

            # 断开命令（兼容序列号/IP+端口）
            if ":" in connect_id:
                cmd = [adb_path, "disconnect", connect_id]
            else:
                cmd = [adb_path, "-s", connect_id, "disconnect"]

            result = execute_adb_command(cmd)

            # 更新状态
            if "disconnected" in result.stdout or result.returncode == 0:
                r.set(f"adb:device:{connect_id}", "offline")
                r.set(f"adb:device:{connect_id}:stdout", result.stdout)
                r.set(f"adb:device:{connect_id}:stderr", "")
                success_msg = f"设备{connect_id}断开连接成功！"
                log_device_operation(
                    request, device, 'disconnect', True,
                    success_msg + f" 输出: {result.stdout[:100]}"
                )
            else:
                r.set(f"adb:device:{connect_id}", "error")
                r.set(f"adb:device:{connect_id}:stderr", result.stderr or result.stdout)
                success_msg = f"设备{connect_id}断开连接失败：{result.stderr or result.stdout}"
                log_device_operation(
                    request, device, 'disconnect', False,
                    success_msg
                )

            return redirect(f"{reverse('adb_manager:index')}?msg={quote(success_msg)}")

        except Exception as e:
            logger.error(f"断开设备失败：{str(e)}", exc_info=True)
            error_msg = f"断开连接失败：{str(e)}"
            # 尝试获取设备对象用于日志记录
            try:
                device = ADBDevice.objects.get(id=device_id) if device_id else None
            except:
                device = None
            log_device_operation(
                request, device, 'disconnect', False,
                error_msg
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(error_msg)}")


class RefreshAllDevicesView(View):
    """刷新所有设备状态（添加日志记录）"""
    def post(self, request):
        try:
            devices = ADBDevice.objects.filter(is_active=True)
            if not devices.exists():
                error_msg = quote("暂无启用的设备，无需刷新！")
                log_device_operation(
                    request, None, 'refresh_all', True,
                    "刷新所有设备：暂无启用的设备"
                )
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            adb_path = get_adb_path()

            # 获取已连接的设备列表
            devices_cmd = [adb_path, "devices"]
            devices_result = execute_adb_command(devices_cmd)
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

                        result = execute_adb_command(cmd)

                        if any(kw in result.stdout for kw in
                               ["connected to", "connected", "echo connected"]) or result.returncode == 0:
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

            success_msg = f"刷新完成！成功更新{success_count}台设备，失败{fail_count}台设备"
            log_device_operation(
                request, None, 'refresh_all', True,
                success_msg
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(success_msg)}")

        except Exception as e:
            logger.error(f"刷新所有设备状态失败：{str(e)}", exc_info=True)
            error_msg = f"刷新失败：{str(e)}"
            log_device_operation(
                request, None, 'refresh_all', False,
                error_msg
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(error_msg)}")


class AddDeviceView(View):
    """添加ADB设备（添加日志记录）"""
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

            success_msg = f"设备【{device.device_name}】添加成功！"
            log_device_operation(
                request, device, 'add', True,
                f"{success_msg} 序列号: {device.device_serial}, IP: {device.device_ip}, 端口: {device.device_port}"
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(success_msg)}")
        else:
            error_details = "; ".join([f"{field}: {', '.join(errors)}" for field, errors in form.errors.items()])
            log_device_operation(
                request, None, 'add', False,
                f"添加设备失败：{error_details}"
            )
            context = {
                "page_title": "添加ADB设备 - 易控ADB",
                "form": form,
                "csrf_token": get_token(request),
                "error_msg": "表单填写有误，请检查！"
            }
            return render(request, "adb_manager/add_device.html", context)


class EditDeviceView(View):
    """编辑ADB设备（添加日志记录）"""
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

            success_msg = f"设备【{updated_device.device_name}】修改成功！"
            log_device_operation(
                request, updated_device, 'edit', True,
                f"{success_msg} 旧标识: {old_connect_id}, 新标识: {new_connect_id}"
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(success_msg)}")
        else:
            error_details = "; ".join([f"{field}: {', '.join(errors)}" for field, errors in form.errors.items()])
            log_device_operation(
                request, device, 'edit', False,
                f"编辑设备失败：{error_details}"
            )
            context = {
                "page_title": f"编辑设备 - {device.device_name}",
                "form": form,
                "device": device,
                "csrf_token": get_token(request),
                "error_msg": "表单填写有误，请检查！"
            }
            return render(request, "adb_manager/edit_device.html", context)


class DeleteDeviceView(View):
    """删除ADB设备（添加日志记录）"""
    def post(self, request, device_id):
        try:
            device = get_object_or_404(ADBDevice, id=device_id)
            device_name = device.device_name
            connect_id = device.connect_identifier
            device_details = f"名称: {device_name}, 标识: {connect_id}"

            # 断开设备
            adb_path = get_adb_path()

            if connect_id:
                if ":" in connect_id:
                    cmd = [adb_path, "disconnect", connect_id]
                else:
                    cmd = [adb_path, "-s", connect_id, "disconnect"]
                execute_adb_command(cmd, timeout=5)

                # 删除Redis状态
                r.delete(f"adb:device:{connect_id}")
                r.delete(f"adb:device:{connect_id}:stdout")
                r.delete(f"adb:device:{connect_id}:stderr")

            # 删除数据库记录
            device.delete()

            success_msg = f"设备【{device_name}】删除成功！"
            log_device_operation(
                request, None, 'delete', True,
                f"{success_msg} {device_details}"
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(success_msg)}")

        except Exception as e:
            logger.error(f"删除设备失败：{str(e)}", exc_info=True)
            error_msg = f"删除失败：{str(e)}"
            # 尝试获取设备对象用于日志记录
            try:
                device = ADBDevice.objects.get(id=device_id) if device_id else None
                device_name = device.device_name if device else "未知设备"
            except:
                device = None
                device_name = "未知设备"

            log_device_operation(
                request, device, 'delete', False,
                f"删除设备【{device_name}】失败：{error_msg}"
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(error_msg)}")


class ConnectAllDevicesView(View):
    """一键连接所有设备（添加日志记录）"""
    def post(self, request):
        try:
            devices = ADBDevice.objects.filter(is_active=True)
            if not devices.exists():
                error_msg = quote("暂无启用的设备，无需连接！")
                log_device_operation(
                    request, None, 'connect_all', True,
                    "一键连接所有设备：暂无启用的设备"
                )
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            adb_path = get_adb_path()

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

                    result = execute_adb_command(cmd)

                    if any(kw in result.stdout for kw in
                           ["connected to", "connected", "echo connected"]) or result.returncode == 0:
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

            success_msg = f"一键连接完成！成功{success_count}台，失败{fail_count}台。详情：{' | '.join(result_logs)}"
            log_device_operation(
                request, None, 'connect_all', True,
                success_msg
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(success_msg)}")

        except Exception as e:
            logger.error(f"一键连接全部设备失败：{str(e)}", exc_info=True)
            error_msg = f"一键连接失败：{str(e)}"
            log_device_operation(
                request, None, 'connect_all', False,
                error_msg
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(error_msg)}")


class DisconnectAllDevicesView(View):
    """一键断开所有设备（添加日志记录）"""
    def post(self, request):
        try:
            devices = ADBDevice.objects.all()
            if not devices.exists():
                error_msg = quote("暂无设备，无需断开！")
                log_device_operation(
                    request, None, 'disconnect_all', True,
                    "一键断开所有设备：暂无设备"
                )
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            adb_path = get_adb_path()

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

                    result = execute_adb_command(cmd)

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

            success_msg = f"一键断开完成！成功{success_count}台，失败{fail_count}台。详情：{' | '.join(result_logs)}"
            log_device_operation(
                request, None, 'disconnect_all', True,
                success_msg
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(success_msg)}")

        except Exception as e:
            logger.error(f"一键断开全部设备失败：{str(e)}", exc_info=True)
            error_msg = f"一键断开失败：{str(e)}"
            log_device_operation(
                request, None, 'disconnect_all', False,
                error_msg
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(error_msg)}")


class CSRFTokenView(View):
    """获取CSRF Token接口（保持不变）"""
    def get(self, request):
        return JsonResponse({
            "code": 200,
            "csrf_token": get_token(request)
        })


class ADBDevicesListView(View):
    """执行adb devices命令（保持不变）"""
    def get(self, request):
        try:
            adb_path = get_adb_path()

            # 执行adb devices命令
            cmd = [adb_path, "devices", "-l"]  # -l参数显示详细信息
            result = execute_adb_command(cmd)

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
                                "details": parts[2].strip() if len(parts) >= 3 else ""
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


class ADBDeviceDetailView(View):
    """获取指定设备的详细信息（保持不变）"""
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

            adb_path = get_adb_path()
            default_timeout = int(os.getenv("ADB_COMMAND_TIMEOUT", 15))

            # 定义需要执行的ADB命令列表
            commands = {
                "brand": [adb_path, "-s", connect_id, "shell", "getprop", "ro.product.brand"],  # 厂商
                "model": [adb_path, "-s", connect_id, "shell", "getprop", "ro.product.model"],  # 型号
                "system_version": [adb_path, "-s", connect_id, "shell", "getprop", "ro.build.version.release"],  # 系统版本
                "serial": [adb_path, "-s", connect_id, "get-serialno"],  # 设备序列号
                "battery": [adb_path, "-s", connect_id, "shell", "dumpsys", "battery"],  # 电池信息
                "battery_level_backup": [adb_path, "-s", connect_id, "shell", "getprop", "status.battery.level"],
                "battery_health_backup": [adb_path, "-s", connect_id, "shell", "getprop", "status.battery.health"],
                "battery_status_backup": [adb_path, "-s", connect_id, "shell", "getprop", "status.battery.state"],
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
                    result = execute_adb_command(cmd, timeout=default_timeout)
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
                        # 解析电池信息
                        battery_info = stdout
                        if not battery_info:
                            result_data["battery_level"] = "未知"
                            result_data["battery_health"] = "未知"
                            result_data["battery_status"] = "未知"
                            continue

                        try:
                            # 正则匹配
                            level_match = re.search(r"level:\s*(\d+)", battery_info, re.IGNORECASE)
                            health_match = re.search(r"health:\s*(\d+|\w+)", battery_info, re.IGNORECASE)
                            status_match = re.search(r"status:\s*(\d+|\w+)", battery_info, re.IGNORECASE)

                            # 电量解析
                            if level_match and level_match.group(1):
                                result_data["battery_level"] = f"{level_match.group(1)}%"
                            else:
                                result_data["battery_level"] = "未知"

                            # 健康度解析
                            health_val = health_match.group(1).upper() if (health_match and health_match.group(1)) else ""
                            health_num_map = {
                                "1": "未知", "2": "良好", "3": "过热", "4": "损坏",
                                "5": "过压", "6": "未知故障", "7": "过冷"
                            }
                            health_str_map = {
                                "GOOD": "良好", "OVERHEAT": "过热", "DEAD": "损坏",
                                "OVER_VOLTAGE": "过压", "UNSPECIFIED_FAILURE": "未知故障",
                                "COLD": "过冷", "UNKNOWN": "未知"
                            }
                            result_data["battery_health"] = health_num_map.get(health_val,
                                                                               health_str_map.get(health_val, "未知"))

                            # 状态解析
                            status_val = status_match.group(1).upper() if (status_match and status_match.group(1)) else ""
                            status_num_map = {
                                "1": "未知", "2": "充电中", "3": "放电中",
                                "4": "未充电", "5": "已充满"
                            }
                            status_str_map = {
                                "CHARGING": "充电中", "DISCHARGING": "放电中",
                                "NOT_CHARGING": "未充电", "FULL": "已充满",
                                "UNKNOWN": "未知", "CONNECTED": "已连接电源（未充电）"
                            }
                            result_data["battery_status"] = status_num_map.get(status_val,
                                                                               status_str_map.get(status_val, "未知"))

                        except Exception as e:
                            logger.error(f"解析电池信息失败：{str(e)}")
                            result_data["battery_level"] = "未知"
                            result_data["battery_health"] = "未知"
                            result_data["battery_status"] = "未知"
                    # 备用电池信息解析
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
                        # 解析WiFi IP
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
    """开启设备无线ADB功能（添加日志记录）"""
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
                log_device_operation(
                    request, device, 'enable_wireless', False,
                    f"开启无线ADB失败：{error_msg}"
                )
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            # 检查设备是否在线
            status = r.get(f"adb:device:{connect_id}") or "offline"
            if status != "online":
                error_msg = quote(f"设备{connect_id}当前不在线，无法开启无线ADB")
                log_device_operation(
                    request, device, 'enable_wireless', False,
                    error_msg
                )
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            # 检查是否已经是无线连接
            if ":" in connect_id:
                # 获取WiFi IP
                wifi_ip = get_wifi_ip(connect_id)
                success_msg = f"设备{connect_id}已是无线连接状态，IP地址：{wifi_ip}"
                log_device_operation(
                    request, device, 'enable_wireless', True,
                    success_msg
                )
                return redirect(f"{reverse('adb_manager:index')}?msg={quote(success_msg)}")

            # 获取ADB路径
            adb_path = get_adb_path()

            # 获取设备WiFi IP
            wifi_ip = get_wifi_ip(connect_id)
            if wifi_ip == "获取IP失败":
                error_msg = quote(f"设备{connect_id}获取WiFi IP失败，无法开启无线ADB，请检查设备网络连接！")
                log_device_operation(
                    request, device, 'enable_wireless', False,
                    error_msg
                )
                return redirect(f"{reverse('adb_manager:index')}?msg={error_msg}")

            # 检查数据库中是否已有该无线信息
            db_check_msg = ""
            need_save = True
            default_port = int(os.getenv("ADB_DEFAULT_WIRELESS_PORT", 5555))

            # 检查当前设备是否已有IP+端口
            if device.device_ip and device.device_port:
                db_check_msg = f"该设备已配置无线信息（IP：{device.device_ip}，端口：{device.device_port}）"
                need_save = False
            # 检查其他设备是否占用该IP+端口
            elif ADBDevice.objects.filter(device_ip=wifi_ip, device_port=default_port).exclude(id=device.id).exists():
                db_check_msg = f"IP：{wifi_ip} 端口：{default_port} 已被其他设备占用"
                need_save = False

            # 执行tcpip命令开启无线ADB
            cmd = [adb_path, "-s", connect_id, "tcpip", str(default_port)]
            result = execute_adb_command(cmd)

            # 验证tcpip命令执行结果
            success_keywords = [
                f"restarting in TCP mode port: {default_port}",
                "already in TCP mode",
                f"restarting TCP port {default_port}"
            ]
            if any(kw in result.stdout for kw in success_keywords) or result.returncode == 0:
                # 更新Redis状态
                r.set(f"adb:device:{connect_id}:stdout", result.stdout or f"设备{connect_id}开启无线ADB成功")
                r.set(f"adb:device:{connect_id}:stderr", "")

                # 根据数据库检查结果决定是否保存
                if need_save:
                    # 无冲突，保存到数据库
                    device.device_ip = wifi_ip
                    device.device_port = default_port
                    device.save()
                    success_msg = f"设备{connect_id}已成功开启无线ADB！IP地址：{wifi_ip}，可通过 {wifi_ip}:{default_port} 连接"
                else:
                    # 已有信息，仅提示不保存
                    success_msg = f"设备{connect_id}已成功开启无线ADB！{db_check_msg}，本次未更新数据库，可通过 {wifi_ip}:{default_port} 连接"

                log_device_operation(
                    request, device, 'enable_wireless', True,
                    success_msg + f" 输出: {result.stdout[:100]}"
                )
            else:
                # 端口操作失败
                r.set(f"adb:device:{connect_id}:stderr", result.stderr or result.stdout or "开启无线ADB失败")
                success_msg = f"设备{connect_id}开启无线ADB失败：{result.stderr or result.stdout}"
                log_device_operation(
                    request, device, 'enable_wireless', False,
                    success_msg
                )

            return redirect(f"{reverse('adb_manager:index')}?msg={quote(success_msg)}")

        except Exception as e:
            logger.error(f"开启无线ADB失败：{str(e)}", exc_info=True)
            error_msg = f"开启无线ADB失败：{str(e)}"
            # 尝试获取设备对象用于日志记录
            try:
                device = ADBDevice.objects.get(id=device_id) if device_id else None
            except:
                device = None
            log_device_operation(
                request, device, 'enable_wireless', False,
                error_msg
            )
            return redirect(f"{reverse('adb_manager:index')}?msg={quote(error_msg)}")

class ADBOperationLogView(View):
    """设备操作日志列表视图（支持筛选、分页）"""
    def get(self, request):
        try:
            # 获取筛选/分页参数
            device_id = request.GET.get("device_id")
            operation_type = request.GET.get("operation_type")
            start_date = request.GET.get("start_date")
            end_date = request.GET.get("end_date")
            page = int(request.GET.get("page", 1))
            page_size = int(request.GET.get("page_size", 20))

            # 构建查询条件
            queryset = ADBDeviceOperationLog.objects.all().order_by("-created_at")

            # 设备筛选
            if device_id and device_id.isdigit():
                queryset = queryset.filter(device_id=device_id)
            # 操作类型筛选
            if operation_type:
                queryset = queryset.filter(operation_type=operation_type)
            # 时间范围筛选
            if start_date:
                queryset = queryset.filter(created_at__gte=start_date)
            if end_date:
                queryset = queryset.filter(created_at__lte=end_date)

            # 分页计算
            total = queryset.count()
            total_pages = (total + page_size - 1) // page_size
            logs = queryset[(page-1)*page_size : page*page_size]

            # 格式化日志数据
            log_list = []
            for log in logs:
                log_list.append({
                    "id": log.id,
                    "device_name": log.device.device_name if log.device else "无特定设备",
                    "device_id": log.device.id if log.device else "",
                    "operation_type": log.operation_type,
                    "operation_type_display": log.get_operation_type_display(),
                    "username": log.user.username if log.user else "未知用户",
                    "operation_result": log.operation_result,
                    "operation_result_display": "成功" if log.operation_result else "失败",
                    "operation_details": log.operation_details,
                    "created_at": log.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                })

            return JsonResponse({
                "code": 200,
                "msg": "获取日志成功",
                "data": {
                    "logs": log_list,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                }
            })

        except Exception as e:
            logger.error(f"获取操作日志失败：{str(e)}", exc_info=True)
            return JsonResponse({
                "code": 500,
                "msg": f"获取日志失败：{str(e)}",
                "data": {}
            })