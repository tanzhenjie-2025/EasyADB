# -*- encoding=utf8 -*-
__author__ = "TanZhenJie"

import sys
import random
import time
import redis
from airtest.core.api import *
from airtest.cli.parser import cli_setup

# Redis配置（和主脚本保持一致）
REDIS_CONFIG = {
    "host": "127.0.0.1",
    "port": 6379,
    "db": 0,
    "decode_responses": True
}

# 初始化Redis连接
try:
    r = redis.Redis(**REDIS_CONFIG)
    r.ping()
except Exception as e:
    print(f"❌ Redis连接失败：{str(e)}")
    sys.exit(1)

# 关键：读取命令行传入的设备号（兼容中文路径/参数校验）
if __name__ == "__main__":
    # 1. 参数校验（提示更友好）
    if len(sys.argv) < 2:
        print("❌ 请指定设备号！")
        print("✅ 示例：python __main__.py 192.168.33.204:5555")
        sys.exit(1)
    device_serial = sys.argv[1].strip()  # 去空格，避免参数错误

    # 2. 初始化设备（指定脚本根目录，避免路径问题）
    auto_setup(
        __file__,
        devices=[f"Android:///{device_serial}"],
        logdir=False  # 关闭默认日志（批量执行时减少文件冗余，需要的话可开启）
    )
    width, height = device().get_current_resolution()
    center_x = width / 2
    center_y = height / 2
    cent = (center_x, center_y)

    # 3. 改为无限循环 + 优雅退出检测
    print(f"🚀 设备{device_serial}开始持续运行（按主进程Ctrl+C停止）...")
    loop_index = 0
    max_loop = 1000
    try:
        while loop_index<max_loop:
            # 检测停止信号（主进程通过Redis发送）
            if r.get(f"airtest_stop_flag_{device_serial}") == "True":
                print(f"\n📢 设备{device_serial}收到停止信号，准备退出...")
                break
            
            loop_index += 1
            print(f"\n===== 开始执行第{loop_index}轮操作（设备：{device_serial}） =====")
            
            # 示例操作1：随机休眠1-3秒（模拟页面加载）
            sleep_time = random.uniform(1, 3)
            sleep(sleep_time)
            print(f"✅ 休眠{sleep_time:.2f}秒完成")

            # 示例操作2：从下往上滑动（修复vector参数，原参数滑动幅度几乎为0）
            swipe(cent, vector=[0.0, -0.7])  # x不变，y轴向上滑50%屏幕高度
            print("✅ 从下往上滑动完成")

            # 每轮间隔15-20秒
            interval_time = random.uniform(15, 20)
            sleep(interval_time)
            print(f"✅ 轮次间隔{interval_time:.2f}秒完成")

    except KeyboardInterrupt:
        print(f"\n📢 设备{device_serial}收到键盘中断，退出循环")
    except Exception as e:
        # 增强异常日志（定位哪个设备/哪轮出错）
        print(f"\n❌ 设备{device_serial}第{loop_index}轮执行异常：{str(e)}")
        raise  # 抛出异常，方便主进程捕获
    
    print(f"\n🎉 设备{device_serial}：共执行{loop_index}轮，已优雅退出！")

