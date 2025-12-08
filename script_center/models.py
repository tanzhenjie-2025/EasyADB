# script_center/models.py
from django.db import models
from adb_manager.models import ADBDevice  # 关联已有的设备模型
from django.utils import timezone

class ScriptTask(models.Model):
    """脚本任务模型（存储任务信息和执行文件路径）"""
    TASK_STATUS = (
        ("draft", "草稿"),
        ("active", "已激活"),
        ("disabled", "已禁用"),
    )
    task_name = models.CharField("任务名称", max_length=100, unique=True, help_text="如：自动化测试任务1")
    task_desc = models.TextField("任务描述", blank=True, null=True, help_text="任务详细说明")
    python_path = models.CharField("Python解释器路径", max_length=500, 
                                  default=r"C:\Users\TanZhenJie\AppData\Local\Microsoft\WindowsApps\python3.11.exe",
                                  help_text="Python.exe的完整路径")
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

    def __str__(self):
        return f"{self.task_name} ({self.status})"

    def is_script_exists(self):
        """校验脚本文件是否存在"""
        import os
        return os.path.exists(self.script_path)

    def is_python_exists(self):
        """校验Python解释器是否存在"""
        import os
        return os.path.exists(self.python_path)

class TaskExecutionLog(models.Model):
    """任务执行日志模型"""
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
    exec_command = models.TextField("执行命令", blank=True, null=True)
    stdout = models.TextField("标准输出", blank=True, null=True)
    stderr = models.TextField("错误输出", blank=True, null=True)
    start_time = models.DateTimeField("开始时间", default=timezone.now)
    end_time = models.DateTimeField("结束时间", blank=True, null=True)
    exec_duration = models.FloatField("执行耗时(秒)", blank=True, null=True)

    class Meta:
        verbose_name = "执行日志"
        verbose_name_plural = "执行日志管理"
        ordering = ["-start_time"]

    def __str__(self):
        return f"{self.task.task_name} - {self.device.adb_connect_str} - {self.exec_status}"