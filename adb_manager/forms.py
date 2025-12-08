from django import forms
from .models import ADBDevice
from user_auth.models import CustomUser

class ADBDeviceForm(forms.ModelForm):
    """ADB设备添加/编辑表单（支持序列号）"""
    device_port = forms.IntegerField(
        label="ADB端口",
        initial=5555,
        min_value=1,
        max_value=65535,
        required=False,
        help_text="默认5555（序列号连接时可不填）"
    )
    device_serial = forms.CharField(
        label="设备序列号",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-input", "placeholder": "如10AF5E1AKU003NR或192.168.3.100:5555"}),
        help_text="ADB设备序列号（优先使用），可通过adb devices命令查看"
    )

    class Meta:
        model = ADBDevice
        fields = ["device_name", "device_ip", "device_port", "device_serial", "is_active", "user"]
        labels = {
            "device_name": "设备名称",
            "device_ip": "设备IP",
            "device_port": "ADB端口",
            "device_serial": "设备序列号",
            "is_active": "启用监控",
            "user": "关联用户"
        }
        widgets = {
            "device_name": forms.TextInput(attrs={"class": "form-input", "placeholder": "如：游戏手机1"}),
            "device_ip": forms.TextInput(attrs={"class": "form-input", "placeholder": "如：192.168.3.100（可选）"}),
            "device_port": forms.NumberInput(attrs={"class": "form-input"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-checkbox"}),
            "user": forms.Select(attrs={"class": "form-select"})
        }

    # 验证：序列号和IP+端口至少填一个
    def clean(self):
        cleaned_data = super().clean()
        serial = cleaned_data.get("device_serial", "").strip()
        ip = cleaned_data.get("device_ip")
        port = cleaned_data.get("device_port")

        if not serial and (not ip or not port):
            raise forms.ValidationError("必须填写【设备序列号】或【设备IP+端口】其中一项")
        return cleaned_data

    def clean_device_name(self):
        name = self.cleaned_data.get("device_name")
        if not name:
            raise forms.ValidationError("设备名称不能为空")
        if len(name) > 50:
            raise forms.ValidationError("设备名称长度不能超过50个字符")
        return name

    def clean_device_ip(self):
        ip = self.cleaned_data.get("device_ip")
        # IP可选（序列号存在时）
        if ip is None and not self.cleaned_data.get("device_serial", "").strip():
            raise forms.ValidationError("设备IP不能为空（未填写序列号时）")
        return ip