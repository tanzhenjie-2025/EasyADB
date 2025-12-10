from django import forms
from .models import OrchestrationTask, TaskStep

class OrchestrationTaskForm(forms.ModelForm):
    class Meta:
        model = OrchestrationTask
        fields = ["name", "description", "status"]
        widgets = {
            "description": forms.Textarea(attrs={
                "rows": 3,
                "class": "form-control",
                "placeholder": "请输入任务描述（可选）"
            }),
            "name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "请输入唯一的任务名称"
            }),
            "status": forms.Select(attrs={
                "class": "form-control"
            }),
        }

class TaskStepForm(forms.ModelForm):
    class Meta:
        model = TaskStep
        fields = ["script_task", "execution_order", "run_duration"]
        widgets = {
            "script_task": forms.Select(attrs={"class": "form-control"}),
            "execution_order": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 1,
                "placeholder": "输入数字，越小越先执行"
            }),
            "run_duration": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 10,
                "placeholder": "单位：秒，最小10秒"
            }),
        }

# 新增：步骤编辑表单（仅编辑超时时间）
class TaskStepEditForm(forms.ModelForm):
    class Meta:
        model = TaskStep
        fields = ["run_duration"]
        widgets = {
            "run_duration": forms.NumberInput(attrs={
                "class": "form-control",
                "min": 10,  # 和添加时保持一致的最小值
                "step": 1,
                "placeholder": "单位：秒，最小10秒"
            }),
        }
        labels = {
            "run_duration": "运行时长(秒)",
        }
        help_texts = {
            "run_duration": "指定该子任务运行多久后自动停止（最小10秒）",
        }