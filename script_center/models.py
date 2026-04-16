from django.db import models
from adb_manager.models import ADBDevice
from django.utils import timezone
from django.conf import settings
import os

class ScriptTask(models.Model):
    """脚本任务模型（存储任务信息和执行文件路径）"""
    TASK_STATUS = (
        ("draft", "草稿"),
        ("active", "已激活"),
        ("disabled", "已禁用"),
    )
    task_name = models.CharField("任务名称", max_length=100, unique=True, help_text="如：自动化测试任务1")
    task_desc = models.TextField("任务描述", blank=True, null=True, help_text="任务详细说明")

    python_path = models.CharField(
        "Python解释器路径",
        max_length=500,
        blank=True,
        null=True,
        help_text="不填写则自动使用Django当前运行的Python解释器"
    )

    script_path = models.CharField("脚本文件路径", max_length=500,
                                   help_text="如：C:\\test\\test4.py（指向.py文件）")
    airtest_mode = models.BooleanField("是否使用Airtest模式", default=False,
                                       help_text="勾选则用airtest run方式执行")
    log_path = models.CharField("日志保存路径", max_length=500, blank=True, null=True,
                                default="./logs", help_text="脚本执行日志保存路径")
    status = models.CharField("任务状态", max_length=20, choices=TASK_STATUS, default="active")
    create_time = models.DateTimeField("创建时间", default=timezone.now)
    update_time = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "脚本任务"
        verbose_name_plural = "脚本任务管理"
        ordering = ["-create_time"]
        indexes = [
            models.Index(fields=['task_name']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"{self.task_name} ({self.status})"

    def is_script_exists(self):
        import os
        return os.path.exists(self.script_path)

    def is_python_exists(self):
        import os
        if not self.python_path:
            return True
        return os.path.exists(self.python_path)


class TaskExecutionLog(models.Model):
    EXEC_STATUS = (
        ("running", "执行中"),
        ("success", "执行成功"),
        ("failed", "执行失败"),
        ("timeout", "执行超时"),
        ("error", "执行异常"),
    )
    task = models.ForeignKey(ScriptTask, on_delete=models.CASCADE, verbose_name="关联任务")
    device = models.ForeignKey(ADBDevice, on_delete=models.CASCADE, verbose_name="执行设备")
    exec_status = models.CharField("执行状态", max_length=20, choices=EXEC_STATUS, default="running")
    exec_command = models.TextField("执行命令", blank=True, default='')
    stdout = models.TextField("标准输出", blank=True, default='')
    stderr = models.TextField("错误输出", blank=True, default='')
    start_time = models.DateTimeField("开始时间", default=timezone.now)
    end_time = models.DateTimeField("结束时间", blank=True, null=True)
    exec_duration = models.FloatField("执行耗时(秒)", blank=True, null=True)

    class Meta:
        verbose_name = "执行日志"
        verbose_name_plural = "执行日志管理"
        ordering = ["-start_time"]
        indexes = [
            models.Index(fields=['task']),
            models.Index(fields=['device']),
            models.Index(fields=['exec_status']),
            models.Index(fields=['-start_time']),
        ]

    def __str__(self):
        return f"{self.task.task_name} - {self.device.adb_connect_str} - {self.exec_status}"


class ScriptTaskManagementLog(models.Model):
    OPERATION_TYPE = (
        ("create", "新增"),
        ("edit", "编辑"),
        ("delete", "删除"),
    )

    task = models.ForeignKey(
        ScriptTask,
        on_delete=models.SET_NULL,
        null=True,
        verbose_name="关联任务"
    )
    operation = models.CharField("操作类型", max_length=20, choices=OPERATION_TYPE)
    operator = models.CharField("操作人", max_length=100)
    operation_time = models.DateTimeField("操作时间", default=timezone.now)
    details = models.TextField("操作详情", blank=True, null=True)

    class Meta:
        verbose_name = "脚本任务管理日志"
        verbose_name_plural = "脚本任务管理日志"
        ordering = ["-operation_time"]
        indexes = [
            models.Index(fields=['task']),
            models.Index(fields=['operation']),
            models.Index(fields=['operator']),
            models.Index(fields=['-operation_time']),
        ]

    def __str__(self):
        task_name = self.task.task_name if self.task else "未知任务"
        return f"{self.get_operation_display()} {task_name}"

# script_center/models.py
# ... (保留你原有的 ScriptTask, TaskExecutionLog 等模型) ...



class BuiltinScript(models.Model):
    CATEGORY_CHOICES = [
        ('DEVICE', '设备控制'),
        ('AD', '广告自动化'),
        ('MONKEY', '压力测试'),
        ('UTIL', '实用工具'),
    ]

    name = models.CharField("脚本名称", max_length=100)
    identifier = models.SlugField("唯一标识", max_length=100, unique=True)
    category = models.CharField("分类", max_length=20, choices=CATEGORY_CHOICES, db_index=True)
    description = models.TextField("功能介绍", blank=True)
    file_path = models.CharField("文件相对路径", max_length=255, help_text="相对于 BUILTIN_SCRIPTS_ROOT")
    version = models.CharField("版本号", max_length=20, default="1.0.0")
    is_active = models.BooleanField("是否启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'name']
        verbose_name = "内置脚本"
        verbose_name_plural = "内置脚本库"

    def __str__(self):
        return f"[{self.get_category_display()}] {self.name}"

    def get_absolute_path(self):
        return os.path.join(settings.BUILTIN_SCRIPTS_ROOT, self.file_path)


class ScriptParameter(models.Model):
    TYPE_CHOICES = [
        ('string', '字符串'),
        ('integer', '整数'),
        ('float', '小数'),
        ('boolean', '开关'),
    ]

    script = models.ForeignKey(BuiltinScript, on_delete=models.CASCADE, related_name='parameters')
    name = models.CharField("参数名 (如: --count)", max_length=50)
    param_type = models.CharField("类型", max_length=10, choices=TYPE_CHOICES, default='string')
    label = models.CharField("显示名称", max_length=100)
    default_value = models.CharField("默认值", max_length=255, blank=True, null=True)
    help_text = models.CharField("帮助说明", max_length=200, blank=True)
    required = models.BooleanField("必填", default=False)
    order = models.IntegerField("排序", default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f"{self.script.name} - {self.name}"