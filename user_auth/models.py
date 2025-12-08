
# user_auth/models.py
from django.contrib.auth.models import AbstractUser
from django.db import models

class CustomUser(AbstractUser):
    """扩展用户模型，增加变现相关字段"""
    phone = models.CharField("手机号", max_length=11, unique=True, blank=True, null=True)  # 允许空（注册时可先不填）
    device_limit = models.IntegerField("设备数量限制", default=3)  # 免费版默认3台
    is_vip = models.BooleanField("是否VIP", default=False)
    vip_expire_at = models.DateTimeField("VIP过期时间", null=True, blank=True)

    class Meta:
        verbose_name = "用户"
        verbose_name_plural = "用户管理"

    def __str__(self):
        return self.username + (f"（VIP）" if self.is_vip else "（普通用户）")
