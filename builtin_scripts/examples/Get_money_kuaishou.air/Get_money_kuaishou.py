"""
@ScriptName: 123
@Description: 123
@Param: loop_count|int|10|循环次数|True
@Param: sleep_time|int|2|等待秒数|False
"""
__author__ = "tanzhenjie"

import sys
import os
# 解决基类和Simple脚本的导入路径问题
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.append(parent_dir)

# 导入父类和Simple的核心函数
from EasyADB_Base import EasyADB_Base
from Get_money_kuaishou_Simple import kuaishou_get_money_core  # 导入Simple的核心逻辑

class KuaishouSpeedScript(EasyADB_Base):
    """快手极速版业务脚本（继承父类 + 复用Simple核心逻辑）"""
    def __init__(self, device_serial):
        super().__init__(device_serial)
        self.template_resolution = (720, 1612)  # 模板分辨率

    def run_business_logic(self):
        """核心业务逻辑：直接调用Simple的核心函数，仅适配父类能力"""
        self.logger.info(f"📌 设备{self.device_serial}开始执行快手极速版领钱逻辑")
        
        # 调用Simple的核心函数，传递父类的logger和safe_sleep
        kuaishou_get_money_core(
            logger=self.logger.info,  # 传递父类的日志方法
            sleep_func=self.safe_sleep  # 传递父类的安全休眠方法（支持停止信号）
        )
        
        # 补充子类特有的逻辑（如返回键、循环计数，可选）
        keyevent("BACK")
        self.safe_sleep(2)
        self.loop_count = 1  # 标记执行次数（适配父类退出逻辑）

# ===================== 子类执行入口 =====================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("❌ 请传入设备序列号！", flush=True)
        print("✅ 示例：python Get_money_kuaishou.py 192.168.33.204:5555", flush=True)
        sys.exit(1)
    device_serial = sys.argv[1].strip()
    
    try:
        script = KuaishouSpeedScript(device_serial)
        script.run()
    except Exception as e:
        print(f"❌ 脚本执行失败：{str(e)}", flush=True)
        sys.exit(1)