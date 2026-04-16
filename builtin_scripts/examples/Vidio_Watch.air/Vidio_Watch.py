"""
@ScriptName: 123
@Description: 123
@Param: loop_count|int|10|循环次数|True
@Param: sleep_time|int|2|等待秒数|False
"""
__author__ = "TanZhenJie"

import sys
import random
import time
import redis
from airtest.core.api import *
from airtest.cli.parser import cli_setup

# Redis配置（和Web端保持一致）
REDIS_CONFIG = {
    "host": "127.0.0.1",
    "port": 6379,
    "db": 0,
    "decode_responses": True
}

# 全局停止标志（避免重复检测）
stop_flag = False

def get_redis_conn():
    """安全获取Redis连接（失败不直接退出）"""
    try:
        r = redis.Redis(**REDIS_CONFIG)
        r.ping()
        print(f"✅ Redis连接成功")
        sys.stdout.flush()  # 强制刷新输出
        return r
    except Exception as e:
        print(f"⚠️ Redis连接失败（优雅退出功能受限）：{str(e)}")
        sys.stdout.flush()
        return None

def check_stop_signal(r, device_serial):
    """检测停止信号（全局标志+Redis）"""
    global stop_flag
    if stop_flag:
        return True
    if not r:
        return False
    try:
        stop_signal = r.get(f"airtest_stop_flag_{device_serial}")
        if stop_signal == "True":
            print(f"\n📢 设备{device_serial}收到停止信号，准备退出...")
            sys.stdout.flush()  # 强制刷新
            stop_flag = True
            # 额外输出：确保信号检测日志被捕获
            print(f"📌 停止信号检测时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
            sys.stdout.flush()
            return True
    except Exception as e:
        print(f"⚠️ 检测停止信号失败：{str(e)}")
        sys.stdout.flush()
    return False

def safe_sleep(seconds, r, device_serial):
    """分段sleep，每0.5秒检测一次停止信号（核心优化）"""
    total_slept = 0
    print(f"\n⏳ 开始分段休眠{seconds:.2f}秒（每0.5秒检测一次停止信号）")
    sys.stdout.flush()
    while total_slept < seconds and not stop_flag:
        time.sleep(0.5)
        total_slept += 0.5
        # 输出休眠进度（确保日志有内容）
        if total_slept % 1 == 0:  # 每1秒输出一次进度
            print(f"🔄 已休眠{total_slept:.2f}秒，剩余{seconds - total_slept:.2f}秒...")
            sys.stdout.flush()
        if check_stop_signal(r, device_serial):  # 高频检测
            print(f"\n🛑 休眠中检测到停止信号，提前退出休眠")
            sys.stdout.flush()
            break
    return stop_flag

def graceful_exit(r, device_serial, loop_index):
    """优雅退出清理（核心：清理Redis信号+刷新日志）"""
    # 1. 输出退出开始日志（确保被捕获）
    print(f"\n📢 开始执行优雅退出流程（设备：{device_serial}）")
    sys.stdout.flush()
    # 2. 清理Redis停止信号（关键：避免残留）
    if r:
        try:
            r.delete(f"airtest_stop_flag_{device_serial}")
            print(f"✅ 已清理Redis停止信号：airtest_stop_flag_{device_serial}")
            sys.stdout.flush()
        except Exception as e:
            print(f"⚠️ 清理Redis信号失败：{str(e)}")
            sys.stdout.flush()
    # 3. 打印退出日志（强制刷新）
    exit_msg = f"\n🎉 设备{device_serial}：共执行{loop_index}轮，已优雅退出！"
    print(exit_msg)
    sys.stdout.flush()
    # 4. 延迟1秒退出（确保日志被Web端读取）
    time.sleep(1)
    # 5. 正常退出
    sys.exit(0)

# 关键：读取命令行传入的设备号（兼容中文路径/参数校验）
if __name__ == "__main__":
    # 强制设置行缓冲（确保实时输出）
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    
    # 1. 参数校验（提示更友好）
    if len(sys.argv) < 2:
        print("❌ 请指定设备号！")
        print("✅ 示例：python __main__.py 192.168.33.204:5555")
        sys.stdout.flush()
        sys.exit(1)
    device_serial = sys.argv[1].strip()  # 去空格，避免参数错误
    print(f"📌 脚本启动，目标设备：{device_serial}")
    sys.stdout.flush()

    # 2. 初始化Redis（失败不直接退出）
    r = get_redis_conn()

    # 3. 初始化设备（指定脚本根目录，避免路径问题）
    try:
        print(f"\n🔧 开始初始化设备{device_serial}...")
        sys.stdout.flush()
        auto_setup(
            __file__,
            devices=[f"Android:///{device_serial}"],
            logdir=False  # 关闭默认日志（批量执行时减少文件冗余）
        )
        width, height = device().get_current_resolution()
        center_x = width / 2
        center_y = height / 2
        cent = (center_x, center_y)
        print(f"✅ 设备{device_serial}初始化成功，分辨率：{width}x{height}")
        sys.stdout.flush()
    except Exception as e:
        print(f"❌ 设备{device_serial}初始化失败：{str(e)}")
        sys.stdout.flush()
        graceful_exit(r, device_serial, 0)

    # 4. 循环执行 + 高频停止检测
    print(f"\n🚀 设备{device_serial}开始持续运行（最大{500}轮）...")
    sys.stdout.flush()
    loop_index = 0
    max_loop = 500
    try:
        while loop_index < max_loop and not stop_flag:
            # 优先检测停止信号（每轮开头+sleep中都检测）
            if check_stop_signal(r, device_serial):
                break
            
            loop_index += 1
            print(f"\n===== 开始执行第{loop_index}轮操作（设备：{device_serial}） =====")
            sys.stdout.flush()
            
            # 示例操作1：随机休眠1-3秒（分段sleep，支持中途停止）
            sleep_time = random.uniform(1, 3)
            print(f"⏳ 准备休眠{sleep_time:.2f}秒...")
            sys.stdout.flush()
            if safe_sleep(sleep_time, r, device_serial):
                break
            print(f"✅ 休眠{sleep_time:.2f}秒完成")
            sys.stdout.flush()

            # 示例操作2：从下往上滑动
            print(f"📱 执行从下往上滑动操作（中心坐标：{cent}）")
            sys.stdout.flush()
            swipe(cent, vector=[0.0, -0.7])  # x不变，y轴向上滑70%屏幕高度
            print("✅ 从下往上滑动完成")
            sys.stdout.flush()

            # 每轮间隔2-4秒（分段sleep）
            interval_time = random.uniform(2, 4)
            print(f"⏳ 准备间隔{interval_time:.2f}秒...")
            sys.stdout.flush()
            if safe_sleep(interval_time, r, device_serial):
                break
            print(f"✅ 轮次间隔{interval_time:.2f}秒完成")
            sys.stdout.flush()

    except KeyboardInterrupt:
        print(f"\n📢 设备{device_serial}收到键盘中断，退出循环")
        sys.stdout.flush()
    except Exception as e:
        # 增强异常日志（定位设备/轮次）
        print(f"\n❌ 设备{device_serial}第{loop_index}轮执行异常：{str(e)}")
        sys.stdout.flush()
    
    # 5. 最终优雅退出
    graceful_exit(r, device_serial, loop_index)