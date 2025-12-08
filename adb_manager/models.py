# adb_manager/models.py
from common.models import BaseModel
from user_auth.models import CustomUser
from django.db import models
# 新增Redis导入（和adb_manager/views.py保持一致）
from django.conf import settings
import redis

# 初始化Redis（和views.py保持一致）
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
    """ADB设备配置表（支持IP+端口/序列号两种连接方式）"""
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
        """获取连接标识（优先序列号，其次IP:端口）"""
        if self.device_serial and self.device_serial.strip():
            return self.device_serial.strip()
        if self.device_ip and self.device_port:
            return f"{self.device_ip}:{self.device_port}"
        return ""  # 修复：返回空字符串而非None

    @property
    def adb_connect_str(self):
        """兼容旧逻辑的连接字符串（IP:端口/序列号）"""
        # 优先用序列号，无则用IP+端口，避免返回None
        if self.device_serial and self.device_serial.strip():
            return self.device_serial.strip()
        if self.device_ip and self.device_port:
            return f"{self.device_ip}:{self.device_port}"
        return ""  # 修复：返回空字符串而非None

    @property
    def device_status(self):
        """统一获取设备连接状态（从Redis）"""
        connect_id = self.connect_identifier
        if not connect_id:
            return "invalid"  # 配置无效
        return r.get(f"adb:device:{connect_id}") or "offline"  # 兼容Redis未初始化的情况