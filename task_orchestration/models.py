from django.db import models
from django.utils import timezone
from script_center.models import ScriptTask
from adb_manager.models import ADBDevice

class OrchestrationTask(models.Model):
    """编排任务主表（包含多个子任务步骤）"""
    TASK_STATUS = (
        ("draft", "草稿"),
        ("active", "已激活"),
        ("disabled", "已禁用"),
    )
    name = models.CharField("编排任务名称", max_length=100, unique=True)
    description = models.TextField("描述", blank=True, null=True)
    status = models.CharField("状态", max_length=20, choices=TASK_STATUS, default="draft")
    create_time = models.DateTimeField("创建时间", default=timezone.now)
    update_time = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "编排任务"
        verbose_name_plural = "编排任务管理"
        ordering = ["-create_time"]

    def __str__(self):
        return self.name

class TaskStep(models.Model):
    """子任务步骤（属于某个编排任务）"""
    orchestration = models.ForeignKey(
        OrchestrationTask,
        on_delete=models.CASCADE,
        related_name="steps",
        verbose_name="所属编排任务"
    )
    script_task = models.ForeignKey(
        ScriptTask,
        on_delete=models.CASCADE,
        verbose_name="关联脚本任务"
    )
    execution_order = models.PositiveIntegerField("执行顺序")
    run_duration = models.PositiveIntegerField("运行时长(秒)", help_text="指定该子任务运行多久后自动停止")
    create_time = models.DateTimeField("创建时间", default=timezone.now)

    class Meta:
        verbose_name = "子任务步骤"
        verbose_name_plural = "子任务步骤管理"
        ordering = ["execution_order"]
        unique_together = ["orchestration", "execution_order"]  # 确保同编排任务内顺序唯一

    def __str__(self):
        return f"{self.orchestration.name} - 步骤{self.execution_order} - {self.script_task.task_name}"

class OrchestrationLog(models.Model):
    """编排任务执行日志（补充详细字段）"""
    EXEC_STATUS = (
        ("running", "执行中"),
        ("completed", "已完成"),
        ("part_failed", "部分失败"),
        ("failed", "执行失败"),
        ("stopped", "手动停止"),
    )
    orchestration = models.ForeignKey(
        OrchestrationTask,
        on_delete=models.CASCADE,
        related_name="logs",
        verbose_name="关联编排任务"
    )
    device = models.ForeignKey(
        ADBDevice,
        on_delete=models.CASCADE,
        verbose_name="执行设备",
        default=1
    )
    exec_status = models.CharField("执行状态", max_length=20, choices=EXEC_STATUS, default="running")
    exec_command = models.TextField("执行命令", blank=True, null=True)
    stdout = models.TextField("标准输出", blank=True, null=True)
    stderr = models.TextField("错误输出", blank=True, null=True)
    exec_duration = models.FloatField("执行耗时(秒)", blank=True, null=True)
    total_steps = models.PositiveIntegerField("总步骤数")
    completed_steps = models.PositiveIntegerField("已完成步骤数", default=0)
    error_msg = models.TextField("错误信息", blank=True, null=True)
    start_time = models.DateTimeField("开始时间", default=timezone.now)
    end_time = models.DateTimeField("结束时间", blank=True, null=True)

    class Meta:
        verbose_name = "编排执行日志"
        verbose_name_plural = "编排执行日志管理"
        ordering = ["-start_time"]

    def __str__(self):
        return f"{self.orchestration.name} - {self.device.device_name} - {self.exec_status}"

class StepExecutionLog(models.Model):
    """子任务步骤执行日志（补充详细字段）"""
    EXEC_STATUS = (
        ("pending", "待执行"),
        ("running", "执行中"),
        ("completed", "已完成"),
        ("timeout", "执行超时"),
        ("failed", "执行失败"),
        ("stopped", "已停止"),
        ("error", "系统错误"),
    )
    orchestration_log = models.ForeignKey(
        OrchestrationLog,
        on_delete=models.CASCADE,
        related_name="step_logs",
        verbose_name="所属编排日志"
    )
    step = models.ForeignKey(
        TaskStep,
        on_delete=models.CASCADE,
        verbose_name="关联步骤"
    )
    exec_status = models.CharField("执行状态", max_length=20, choices=EXEC_STATUS, default="pending")
    exec_command = models.TextField("执行命令", blank=True, null=True)
    stdout = models.TextField("标准输出", blank=True, null=True)
    stderr = models.TextField("错误输出", blank=True, null=True)
    return_code = models.IntegerField("返回码", blank=True, null=True)
    exec_duration = models.FloatField("执行耗时(秒)", blank=True, null=True)
    error_msg = models.TextField("步骤错误信息", blank=True, null=True)
    start_time = models.DateTimeField("开始时间", default=timezone.now)
    end_time = models.DateTimeField("结束时间", blank=True, null=True)

    class Meta:
        verbose_name = "步骤执行日志"
        verbose_name_plural = "步骤执行日志管理"
        ordering = ["step__execution_order"]

    def __str__(self):
        return f"{self.orchestration_log.orchestration.name} - 步骤{self.step.execution_order} - {self.exec_status}"


class OrchestrationManagementLog(models.Model):
    """编排任务全局管理操作日志（记录所有任务的增删改克隆）"""
    OPERATION_TYPES = (
        ("create", "新增"),
        ("edit", "编辑"),
        ("delete", "删除"),
        ("clone", "克隆"),
    )

    orchestration = models.ForeignKey(
        OrchestrationTask,
        on_delete=models.SET_NULL,  # 任务删除后字段设为NULL
        null=True,
        blank=True,
        related_name="management_logs",
        verbose_name="关联编排任务"
    )
    original_task_name = models.CharField("原任务名称", max_length=100, blank=True, null=True)
    original_task_id = models.IntegerField("原任务ID", blank=True, null=True)  # 新增：记录任务ID
    operation_type = models.CharField("操作类型", max_length=20, choices=OPERATION_TYPES)
    operator = models.CharField("操作人", max_length=100)
    operation_time = models.DateTimeField("操作时间", default=timezone.now)
    details = models.TextField("操作详情", blank=True, null=True)

    class Meta:
        verbose_name = "编排任务管理日志"
        verbose_name_plural = "编排任务管理日志"
        ordering = ["-operation_time"]

    def __str__(self):
        task_name = self.original_task_name or "已删除任务"
        task_id = self.original_task_id or "未知ID"
        return f"{self.get_operation_type_display()} - {task_name}（{task_id}） - {self.operation_time.strftime('%Y-%m-%d %H:%M:%S')}"