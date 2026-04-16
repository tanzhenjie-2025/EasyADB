"""
@ScriptName: 123
@Description: 123
"""
__author__ = "TanZhenJie"

import sys
import os
import subprocess
import redis
import time  # 新增
import logging  # 新增：导入日志模块
from airtest.core.api import *
from airtest.core.error import TargetNotFoundError, NoDeviceError
from airtest.core.android.android import Android

# ===================== 1. 日志配置（核心新增） =====================
def setup_logger(device_serial):
    """配置实时日志输出（无缓冲）"""
    # 创建logger
    logger = logging.getLogger(f"AirtestScript_{device_serial}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # 清空重复handler
    
    # 配置控制台输出
    handler = logging.StreamHandler(sys.stdout)
    # 日志格式：时间 - 设备 - 级别 - 消息
    formatter = logging.Formatter(
        '%(asctime)s - [%(name)s] - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    # 禁用日志传播（避免重复输出）
    logger.propagate = False
    
    # 设置无缓冲输出
    sys.stdout.reconfigure(line_buffering=True)  # Python 3.7+ 支持
    return logger

# ===================== 2. 全局配置 =====================
REDIS_CONFIG = {
    "host": "127.0.0.1",
    "port": 6379,
    "db": 0,
    "decode_responses": True
}
stop_flag = False
logger = None  # 全局logger对象

# ===================== 3. 工具函数 =====================
def check_adb_env():
    """检查ADB环境是否可用"""
    try:
        result = subprocess.run(
            ["adb", "version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            timeout=10
        )
        if result.returncode != 0:
            raise Exception(f"ADB执行失败：{result.stderr}")
        logger.info("✅ ADB环境检测正常")  # 替换print为logger.info
        return True
    except FileNotFoundError:
        airtest_adb_path = os.path.join(os.path.dirname(Android.adb_path), "adb.exe")
        if os.path.exists(airtest_adb_path):
            Android.adb_path = airtest_adb_path
            logger.info(f"✅ 系统无ADB，已切换为Airtest自带ADB：{airtest_adb_path}")
            return True
        else:
            raise Exception("❌ 未找到ADB环境！请确保ADB已加入系统环境变量，或安装AirtestIDE")
    except Exception as e:
        raise Exception(f"❌ ADB环境检测失败：{str(e)}")

def check_device_online(device_serial):
    """检查指定设备是否在线"""
    try:
        result = subprocess.run(
            ["adb", "devices"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            timeout=10
        )
        if result.returncode != 0:
            raise Exception(f"ADB获取设备列表失败：{result.stderr}")
        
        device_lines = [line.strip() for line in result.stdout.split("\n") if line.strip()]
        online_devices = []
        for line in device_lines[1:]:
            if "\t" in line:
                dev_serial, dev_status = line.split("\t", 1)
                if dev_status == "device":
                    online_devices.append(dev_serial.strip())
        
        if device_serial not in online_devices:
            raise Exception(
                f"❌ 设备{device_serial}未在线！\n"
                f"当前在线设备列表：{online_devices}\n"
                f"请检查：1.设备USB调试已开启 2.设备已连接电脑 3.设备未被占用"
            )
        logger.info(f"✅ 设备{device_serial}检测在线，状态正常")
        return True
    except Exception as e:
        raise Exception(f"❌ 设备检测失败：{str(e)}")

def safe_connect_device(device_serial):
    """安全连接设备"""
    try:
        dev = connect_device(f"Android:///{device_serial}")
        if not dev:
            raise NoDeviceError(f"设备{device_serial}连接返回空")
        try:
            width, height = dev.get_current_resolution()
            logger.info(f"✅ 设备{device_serial}分辨率：{width}x{height}")
        except Exception as e:
            logger.warning(f"⚠️ 无法获取设备分辨率（不影响执行）：{str(e)}")  # 替换为warning
        return dev
    except NoDeviceError as e:
        raise Exception(f"❌ 设备连接失败：{str(e)}")
    except Exception as e:
        raise Exception(f"❌ 设备连接异常：{str(e)}")

def check_stop_signal(r, device_serial):
    """高频检测停止信号（核心优化）"""
    global stop_flag
    if stop_flag:
        return True
    try:
        stop_signal = r.get(f"airtest_stop_flag_{device_serial}")
        if stop_signal == "True":
            logger.info(f"\n📢 检测到停止信号！")
            stop_flag = True
            return True
    except Exception as e:
        logger.warning(f"⚠️ 检测停止信号失败：{str(e)}")  # 替换为warning
    return False

def safe_sleep(seconds, r, device_serial):
    """分段sleep，每0.5秒检测一次停止信号"""
    total_slept = 0
    while total_slept < seconds and not stop_flag:
        time.sleep(0.5)
        total_slept += 0.5
        check_stop_signal(r, device_serial)  # 高频检测
    return stop_flag

def graceful_exit(loop_count=0):
    """优雅退出清理函数（优化：强制刷新输出）"""
    # 强制刷新日志缓冲区，确保日志能被捕获
    logger.info(f"\n📢 开始优雅退出流程...")
    sys.stdout.flush()
    
    # 清理Redis停止信号
    try:
        r = redis.Redis(**REDIS_CONFIG)
        r.delete(f"airtest_stop_flag_{device_serial}")
        logger.info(f"✅ 已清理Redis停止信号：airtest_stop_flag_{device_serial}")
        sys.stdout.flush()
    except:
        pass
    
    # 打印退出统计
    exit_msg = f"🎉 脚本已优雅退出！累计执行{loop_count}次循环\n"
    logger.info(exit_msg)
    sys.stdout.flush()  # 最后一次刷新
    sys.exit(0)

# ===================== 4. 主流程 =====================
if __name__ == "__main__":
    # -------------------- 步骤1：参数校验 --------------------
    if len(sys.argv) < 2:
        # 错误信息用logger.error
        logging.error("❌ 请传入设备序列号！示例：python Welfare_Box.py 10AF5E1AKU003NR")
        sys.stdout.flush()
        sys.exit(1)
    device_serial = sys.argv[1].strip()
    
    # 初始化logger（核心：传入设备序列号，确保日志带设备标识）
    logger = setup_logger(device_serial)
    
    logger.info(f"📌 目标设备序列号：{device_serial}")
    sys.stdout.flush()

    # -------------------- 步骤2：初始化Redis --------------------
    try:
        r = redis.Redis(**REDIS_CONFIG)
        r.ping()
        logger.info(f"✅ Redis连接成功")
        sys.stdout.flush()
    except Exception as e:
        logger.warning(f"⚠️ Redis连接失败（优雅退出功能将受限）：{str(e)}")  # 替换为warning
        sys.stdout.flush()
        r = None

    # -------------------- 步骤3：设备检测与连接 --------------------
    try:
        check_adb_env()
        check_device_online(device_serial)
        dev = safe_connect_device(device_serial)
        auto_setup(__file__, logdir=True, devices=[f"Android:///{device_serial}"])
        logger.info(f"✅ 设备{device_serial}连接成功，脚本初始化完成")
        sys.stdout.flush()

    except Exception as e:
        error_msg = f"\n❌ 设备初始化失败：{str(e)}\n"
        logger.error(error_msg)  # 替换为error
        sys.stdout.flush()
        sys.exit(1)

    # -------------------- 步骤4：核心业务逻辑 + 高频退出检测 --------------------
    try:
        logger.info(f"🔍 等待目标元素加载（设备：{device_serial}）...")
        sys.stdout.flush()
        loop_count = 1
        max_loop = 1
        #         此处输入代码
        kuaishou_pos = exists(Template(r"tpl1765590200738.png", record_pos=(-0.403, -0.303), resolution=(1600, 2560)))
        if kuaishou_pos:
            touch(kuaishou_pos)
            sleep(3)
        x_pos = exists(Template(r"tpl1765590588955.png", record_pos=(0.421, 0.29), resolution=(1600, 2560)))
        if x_pos:
            touch(x_pos)


        make_money_pos = exists(Template(r"tpl1765590647833.png", record_pos=(0.196, 0.699), resolution=(1600, 2560)))
        if make_money_pos:
            touch(make_money_pos)



        auto_setup(__file__)
        width, height = device().get_current_resolution()
        center_x = width / 2
        center_y = height / 2
        cent = (center_x, center_y)
        count = 0
        while count<5:
            count +=1
            advertisement_pos = exists(Template(r"tpl1765590929722.png", record_pos=(0.372, -0.312), resolution=(1600, 2560)))
            if not advertisement_pos:
                swipe(cent, vector=[-0.0000, -0.5000])
            else:
                break


        if advertisement_pos:
            touch(advertisement_pos)
            sleep(37)
            keyevent("BACK")
            sleep(5)
            keyevent("BACK")
            sleep(2)
            x_pos = exists(Template(r"tpl1765591163697.png", record_pos=(0.203, -0.207), resolution=(1600, 2560)))
            touch(x_pos)
            sleep(1)
        count = 0
        max_loop = 5
        while count<max_loop:
            touch(advertisement_pos)
            sleep(37)
            keyevent("BACK")
            sleep(5)
            keyevent("BACK")
            sleep(1)
            touch(x_pos)
            sleep(2)
            count += 1


    # -------------------- 步骤5：键盘中断捕获 --------------------
    except KeyboardInterrupt:
        logger.info(f"\n📢 收到键盘中断信号（Ctrl+C），设备：{device_serial}")
        sys.stdout.flush()
        graceful_exit(loop_count)

    # -------------------- 步骤6：通用异常处理 --------------------
    except Exception as e:
        error_msg = f"\n❌ 脚本执行出错：{str(e)}"
        logger.error(error_msg)  # 替换为error
        sys.stdout.flush()
        try:
            snapshot(msg=f"error_{device_serial}_{str(e)[:50]}")
        except:
            pass
        if r:
            try:
                r.delete(f"airtest_stop_flag_{device_serial}")
            except:
                pass
        sys.exit(1)

    # -------------------- 步骤7：正常退出 --------------------
    finally:
        if stop_flag:
            graceful_exit(loop_count)
        else:
            normal_exit_msg = f"\n📢 脚本正常结束（已执行{loop_count}次循环，达到最大次数{max_loop}）\n"
            logger.info(normal_exit_msg)
            sys.stdout.flush()
            graceful_exit(loop_count)


