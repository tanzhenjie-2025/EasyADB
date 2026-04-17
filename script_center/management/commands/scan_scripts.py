# script_center/management/commands/scan_scripts.py
import os
import re
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings
from script_center.models import BuiltinScript, ScriptParameter


class Command(BaseCommand):
    help = '扫描内置脚本目录，自动更新数据库'

    def handle(self, *args, **options):
        root_dir = Path(settings.BUILTIN_SCRIPTS_ROOT)
        if not root_dir.exists():
            self.stdout.write(self.style.ERROR(f"目录不存在: {root_dir}"))
            return

        scanned_count = 0

        # 递归查找所有 .air 文件夹
        for air_dir in root_dir.rglob("*.air"):
            if not air_dir.is_dir():
                continue

            py_file = air_dir / f"{air_dir.stem}.py"
            if not py_file.exists():
                continue

            # 计算相对路径
            rel_path = py_file.relative_to(root_dir)

            # 解析注释
            metadata = self._parse_script_header(py_file)
            if not metadata:
                self.stdout.write(f"跳过 (无有效注释): {py_file.name}")
                continue

            # 更新或创建数据库记录
            self._update_db(str(rel_path), metadata, air_dir.stem)
            scanned_count += 1
            self.stdout.write(self.style.SUCCESS(f"已处理: {metadata.get('ScriptName', py_file.stem)}"))

        self.stdout.write(self.style.SUCCESS(f"扫描完成！共处理 {scanned_count} 个脚本"))

    def _parse_script_header(self, py_file):
        """解析文件头部的特定格式注释"""
        metadata = {}
        try:
            with open(py_file, 'r', encoding='utf-8') as f:
                content = f.read()

            # 匹配三引号之间的内容
            docstring_match = re.search(r'"""(.*?)"""', content, re.DOTALL)
            if not docstring_match:
                return None

            docstring = docstring_match.group(1)

            # 提取 @ScriptName, @Description
            metadata['ScriptName'] = re.search(r'@ScriptName:\s*(.*)', docstring)
            metadata['Description'] = re.search(r'@Description:\s*(.*)', docstring)

            # 提取所有 @Param
            # 格式: @Param: loop_count|int|10|循环次数|True
            params = re.findall(r'@Param:\s*(.*)', docstring)

            # 清理数据
            if metadata['ScriptName']: metadata['ScriptName'] = metadata['ScriptName'].group(1).strip()
            if metadata['Description']: metadata['Description'] = metadata['Description'].group(1).strip()
            metadata['Params'] = params

            return metadata if metadata['ScriptName'] else None

        except Exception as e:
            self.stdout.write(self.style.WARNING(f"解析失败 {py_file}: {e}"))
            return None

    def _update_db(self, rel_path, metadata, stem):
        """更新数据库"""
        # 1. 更新或创建 Script
        script, created = BuiltinScript.objects.update_or_create(
            identifier=stem,  # 用文件夹名作为唯一标识
            defaults={
                'name': metadata['ScriptName'],
                'description': metadata.get('Description', ''),
                'file_path': rel_path,
                'category': 'AD' if '广告' in metadata['ScriptName'] else 'UTIL',  # 简单自动分类
                'is_active': True
            }
        )

        # 2. 清理旧参数
        if not created:
            script.parameters.all().delete()

        # 3. 创建新参数
        param_list = metadata.get('Params', [])
        for idx, param_line in enumerate(param_list):
            parts = [p.strip() for p in param_line.split('|')]
            if len(parts) < 4:
                continue

            p_name, p_type, p_default, p_label = parts[0], parts[1], parts[2], parts[3]
            p_required = len(parts) > 4 and parts[4].lower() == 'true'

            # 类型映射
            type_map = {'int': 'integer', 'str': 'string', 'float': 'float', 'bool': 'boolean'}

            ScriptParameter.objects.create(
                script=script,
                name=p_name,
                param_type=type_map.get(p_type, 'string'),
                label=p_label,
                default_value=p_default if p_default else None,
                help_text=p_label,
                required=p_required,
                order=idx
            )