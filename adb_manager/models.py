# adb_manager/models.py
from common.models import BaseModel
from user_auth.models import CustomUser
from django.db import models
from django.conf import settings
import redis

# 初始化Redis（保持不变）
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
except Exception as e:
    class EmptyRedis:
        def get(self, key, *args, **kwargs):
            return None


    r = EmptyRedis()


class ADBDevice(BaseModel):
    """ADB设备配置表（保持不变）"""
    # 原有代码保持不变
    device_name = models.CharField(
        "设备名称",
        max_length=50,
        help_text="如：游戏手机1"
    )
    device_ip = models.GenericIPAddressField(
        "设备IP",
        blank=True,
        null=True,
        help_text="局域网IP（序列号连接时可不填），如192.168.3.100"
    )
    device_port = models.IntegerField(
        "ADB端口",
        default=5555,
        blank=True,
        null=True,
        help_text="默认5555（序列号连接时可不填）"
    )
    device_serial = models.CharField(
        "设备序列号",
        max_length=100,
        blank=True,
        null=True,
        help_text="ADB设备序列号（优先使用），如10AF5E1AKU003NR或192.168.3.100:5555"
    )
    is_active = models.BooleanField("是否启用监控", default=True)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="关联用户",
        help_text="绑定该设备的用户（为空则所有用户可操作）"
    )

    class Meta:
        verbose_name = "ADB设备"
        verbose_name_plural = "ADB设备管理"
        unique_together = ("device_ip", "device_port")

    def __str__(self):
        if self.device_serial:
            return f"{self.device_name} ({self.device_serial})"
        return f"{self.device_name} ({self.device_ip}:{self.device_port})"

    @property
    def connect_identifier(self):
        if self.device_serial and self.device_serial.strip():
            return self.device_serial.strip()
        if self.device_ip and self.device_port:
            return f"{self.device_ip}:{self.device_port}"
        return ""

    @property
    def adb_connect_str(self):
        if self.device_serial and self.device_serial.strip():
            return self.device_serial.strip()
        if self.device_ip and self.device_port:
            return f"{self.device_ip}:{self.device_port}"
        return ""

    @property
    def device_status(self):
        connect_id = self.connect_identifier
        if not connect_id:
            return "invalid"
        return r.get(f"adb:device:{connect_id}") or "offline"


# 新增操作日志模型
class ADBDeviceOperationLog(BaseModel):
    """ADB设备操作日志记录"""
    OPERATION_TYPES = (
        ('connect', '连接设备'),
        ('disconnect', '断开设备'),
        ('add', '添加设备'),
        ('edit', '编辑设备'),
        ('delete', '删除设备'),
        ('enable_wireless', '开启无线ADB'),
        ('connect_all', '一键连接所有'),
        ('disconnect_all', '一键断开所有'),
        ('refresh_all', '刷新所有状态'),
    )

    device = models.ForeignKey(
        ADBDevice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="操作设备"
    )
    operation_type = models.CharField(
        "操作类型",
        max_length=20,
        choices=OPERATION_TYPES
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="操作人"
    )
    operation_result = models.BooleanField("操作结果", default=True)
    operation_details = models.TextField("操作详情", blank=True, default="")

    class Meta:
        verbose_name = "设备操作日志"
        verbose_name_plural = "设备操作日志管理"
        ordering = ["-created_at"]

    def __str__(self):
        username = self.user.username if self.user else "未知用户"
        device_name = self.device.device_name if self.device else "无特定设备"
        return f"{username} {self.get_operation_type_display()} {device_name} {'成功' if self.operation_result else '失败'}"