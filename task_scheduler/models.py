# task_scheduler/models.py
from django.db import models
# 删掉 from django.utils import timezone
import datetime  # 新增
import croniter
from task_orchestration.models import OrchestrationTask
from adb_manager.models import ADBDevice


class ScheduleTask(models.Model):
    """定时任务模型（基于Cron表达式，适配USE_TZ=False）"""
    name = models.CharField("定时任务名称", max_length=100, unique=True)
    orchestration = models.ForeignKey(
        OrchestrationTask,
        on_delete=models.CASCADE,
        related_name="schedules",
        verbose_name="关联编排任务"
    )
    device = models.ForeignKey(
        ADBDevice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="指定执行设备",
        help_text="为空则自动选择在线设备"
    )
    cron_expression = models.CharField(
        "Cron表达式",
        max_length=100,
        default="0 0 * * *",
        help_text="格式：分 时 日 月 周，支持标准Cron语法。示例：<br/>"
                  "0 8 * * * → 每天8点执行<br/>"
                  "0 0 * * 1,3,5 → 每周一/三/五0点执行<br/>"
                  "0 12 1,15 * * → 每月1/15号12点执行<br/>"
                  "*/30 * * * * → 每30分钟执行一次"
    )
    is_active = models.BooleanField("是否启用", default=True)
    last_run_time = models.DateTimeField("最后执行时间", blank=True, null=True)
    next_run_time = models.DateTimeField("下次执行时间", blank=True, null=True)
    create_time = models.DateTimeField("创建时间", default=datetime.datetime.now)  # 改这里
    update_time = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "定时任务"
        verbose_name_plural = "定时任务管理"
        ordering = ["-create_time"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        """保存时自动计算下次执行时间（去掉时区）"""
        if self.cron_expression:
            self.next_run_time = self.calculate_next_run_time()
        super().save(*args, **kwargs)

    def calculate_next_run_time(self):
        """基于Cron表达式计算下次执行时间（生成本地无时区时间）"""
        now = datetime.datetime.now()  # 本地时间（上海），无时区标记
        cron = croniter.croniter(self.cron_expression, now)
        next_run = cron.get_next(datetime.datetime)
        return next_run  # 直接返回naive datetime，去掉timezone.make_aware

    def is_due(self):
        """检查当前是否到了执行时间（误差5分钟）"""
        now = datetime.datetime.now()
        cron = croniter.croniter(self.cron_expression, now - datetime.timedelta(minutes=5))
        prev_run = cron.get_prev(datetime.datetime)
        # 检查是否在最近5分钟内应该执行
        return prev_run >= now - datetime.timedelta(minutes=5) and self.is_active


# ScheduleExecutionLog 模型无需改字段，只需改视图/tasks中赋值的地方
class ScheduleExecutionLog(models.Model):
    EXEC_STATUS = (
        ("success", "执行成功"),
        ("failed", "执行失败"),
        ("running", "执行中"),
    )

    schedule = models.ForeignKey(
        ScheduleTask,
        on_delete=models.CASCADE,
        related_name="logs",
        verbose_name="关联定时任务"
    )
    orchestration_log = models.ForeignKey(
        "task_orchestration.OrchestrationLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="关联编排日志"
    )
    exec_status = models.CharField("执行状态", max_length=20, choices=EXEC_STATUS, default="running")
    device = models.ForeignKey(
        ADBDevice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="执行设备"
    )
    start_time = models.DateTimeField("开始时间", default=datetime.datetime.now)  # 改这里
    end_time = models.DateTimeField("结束时间", blank=True, null=True)
    error_msg = models.TextField("错误信息", blank=True, null=True)

    class Meta:
        verbose_name = "定时任务执行日志"
        verbose_name_plural = "定时任务执行日志管理"
        ordering = ["-start_time"]

    def __str__(self):
        return f"{self.schedule.name} - {self.exec_status} - {self.start_time.strftime('%Y-%m-%d %H:%M')}"