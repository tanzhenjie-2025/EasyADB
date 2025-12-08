# script_center/forms.py
from django import forms
from .models import ScriptTask
import os

class ScriptTaskForm(forms.ModelForm):
    """脚本任务表单"""
    class Meta:
        model = ScriptTask
        fields = ["task_name", "task_desc", "python_path", "script_path",
                 "airtest_mode", "log_path", "status"]
        widgets = {
            "task_desc": forms.Textarea(attrs={"rows": 3}),
            "python_path": forms.TextInput(attrs={"class": "form-control"}),
            "script_path": forms.TextInput(attrs={"class": "form-control"}),
            "log_path": forms.TextInput(attrs={"class": "form-control"}),
        }

    def clean_script_path(self):
        """校验脚本路径是否存在"""
        script_path = self.cleaned_data.get("script_path")
        if script_path and not os.path.exists(script_path):
            raise forms.ValidationError(f"脚本文件不存在：{script_path}")
        return script_path

    def clean_python_path(self):
        """校验Python路径是否存在"""
        python_path = self.cleaned_data.get("python_path")
        if python_path and not os.path.exists(python_path):
            raise forms.ValidationError(f"Python解释器不存在：{python_path}")
        return python_path