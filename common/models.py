from django.db import models

# Create your models here.

class BaseModel(models.Model):
    """通用基础模型（抽象类）"""
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True
