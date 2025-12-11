# task_scheduler/forms.py
from django import forms
from .models import ScheduleTask
from task_orchestration.models import OrchestrationTask
from adb_manager.models import ADBDevice
import croniter

class ScheduleTaskForm(forms.ModelForm):
    # 常用Cron模板（方便非技术用户选择）
    CRON_TEMPLATES = [
        ("", "—— 选择常用模板（可选） ——"),
        ("0 8 * * *", "每天8点执行"),
        ("0 0 * * *", "每天0点执行"),
        ("0 0 * * 1", "每周一0点执行"),
        ("0 0 * * 1,3,5", "每周一/三/五0点执行"),
        ("0 12 1 * *", "每月1号12点执行"),
        ("0 12 1,15 * *", "每月1/15号12点执行"),
        ("*/30 * * * *", "每30分钟执行一次"),
        ("*/60 * * * *", "每小时执行一次"),
    ]
    cron_template = forms.ChoiceField(
        choices=CRON_TEMPLATES,
        required=False,
        widget=forms.Select(attrs={'class': 'form-select mb-2'})
    )

    class Meta:
        model = ScheduleTask
        fields = ['name', 'orchestration', 'device', 'cron_expression', 'is_active']
        widgets = {
            'cron_expression': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '例如：0 8 * * *'
            }),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'orchestration': forms.Select(attrs={'class': 'form-select'}),
            'device': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 只显示激活状态的编排任务和设备
        self.fields['orchestration'].queryset = OrchestrationTask.objects.filter(status='active')
        self.fields['device'].queryset = ADBDevice.objects.filter(is_active=True)

    def clean_cron_expression(self):
        """验证Cron表达式格式"""
        cron_expr = self.cleaned_data.get('cron_expression')
        try:
            # 验证Cron表达式有效性
            croniter.croniter(cron_expr)
            return cron_expr
        except Exception as e:
            raise forms.ValidationError(f"无效的Cron表达式：{str(e)}")

    def clean(self):
        """如果选择了模板，自动填充Cron表达式"""
        cleaned_data = super().clean()
        template = cleaned_data.get('cron_template')
        if template:
            cleaned_data['cron_expression'] = template
        return cleaned_data