# -*- encoding=utf8 -*-
__author__ = "TanZhenJie"

import sys
import os
import subprocess
import redis
from airtest.core.api import *
from airtest.core.error import TargetNotFoundError, NoDeviceError
from airtest.core.android.android import Android

# ===================== 1. 全局配置 =====================
# Redis配置（和Web端保持一致）
REDIS_CONFIG = {
    "host": "127.0.0.1",
    "port": 6379,
    "db": 0,
    "decode_responses": True
}

# 定义停止标志
stop_flag = False

# ===================== 2. 设备检测工具函数（核心：避免NoDeviceError） =====================
def check_adb_env():
    """检查ADB环境是否可用"""
    try:
        # 执行adb version，检测adb是否在环境变量中
        result = subprocess.run(
            ["adb", "version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            timeout=10
        )
        if result.returncode != 0:
            raise Exception(f"ADB执行失败：{result.stderr}")
        print("✅ ADB环境检测正常")
        return True
    except FileNotFoundError:
        # 尝试使用Airtest自带的ADB（避免系统无ADB环境）
        airtest_adb_path = os.path.join(os.path.dirname(Android.adb_path), "adb.exe")
        if os.path.exists(airtest_adb_path):
            Android.adb_path = airtest_adb_path
            print(f"✅ 系统无ADB，已切换为Airtest自带ADB：{airtest_adb_path}")
            return True
        else:
            raise Exception("❌ 未找到ADB环境！请确保ADB已加入系统环境变量，或安装AirtestIDE")
    except Exception as e:
        raise Exception(f"❌ ADB环境检测失败：{str(e)}")

def check_device_online(device_serial):
    """检查指定设备是否在线（避免连接离线/不存在的设备）"""
    try:
        # 执行adb devices，获取在线设备列表
        result = subprocess.run(
            ["adb", "devices"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            timeout=10
        )
        if result.returncode != 0:
            raise Exception(f"ADB获取设备列表失败：{result.stderr}")
        
        # 解析设备列表（过滤掉标题行和空行）
        device_lines = [line.strip() for line in result.stdout.split("\n") if line.strip()]
        online_devices = []
        for line in device_lines[1:]:  # 跳过第一行"List of devices attached"
            if "\t" in line:
                dev_serial, dev_status = line.split("\t", 1)
                if dev_status == "device":  # 仅保留在线设备
                    online_devices.append(dev_serial.strip())
        
        # 检查目标设备是否在在线列表中
        if device_serial not in online_devices:
            raise Exception(
                f"❌ 设备{device_serial}未在线！\n"
                f"当前在线设备列表：{online_devices}\n"
                f"请检查：1.设备USB调试已开启 2.设备已连接电脑 3.设备未被占用"
            )
        print(f"✅ 设备{device_serial}检测在线，状态正常")
        return True
    except Exception as e:
        raise Exception(f"❌ 设备检测失败：{str(e)}")

def safe_connect_device(device_serial):
    """安全连接设备（捕获异常，避免NoDeviceError）"""
    try:
        # 显式指定设备连接，避免auto_setup的隐式逻辑
        dev = connect_device(f"Android:///{device_serial}")
        # 验证设备是否真的连接成功（仅用基础属性，规避get_device_info）
        if not dev:
            raise NoDeviceError(f"设备{device_serial}连接返回空")
        # 验证设备分辨率（替代get_device_info，避免属性报错）
        try:
            width, height = dev.get_current_resolution()
            print(f"✅ 设备{device_serial}分辨率：{width}x{height}")
        except Exception as e:
            print(f"⚠️ 无法获取设备分辨率（不影响执行）：{str(e)}")
        return dev
    except NoDeviceError as e:
        raise Exception(f"❌ 设备连接失败：{str(e)}")
    except Exception as e:
        raise Exception(f"❌ 设备连接异常：{str(e)}")

# ===================== 3. 优雅退出函数 =====================
def graceful_exit():
    """优雅退出清理函数"""
    print(f"\n📢 开始优雅退出流程...")
    # 清理Redis停止信号（避免残留）
    try:
        r = redis.Redis(**REDIS_CONFIG)
        r.delete(f"airtest_stop_flag_{device_serial}")
        print(f"✅ 已清理Redis停止信号：airtest_stop_flag_{device_serial}")
    except:
        pass  # Redis不可用时忽略
    
    # 打印退出统计
    print(f"🎉 脚本已优雅退出！")
    sys.exit(0)

# ===================== 4. 主流程 =====================
if __name__ == "__main__":
    # -------------------- 步骤1：参数校验 --------------------
    if len(sys.argv) < 2:
        print("❌ 请传入设备序列号！示例：python Welfare_Box.py 10AF5E1AKU003NR")
        sys.exit(1)
    device_serial = sys.argv[1].strip()
    print(f"📌 目标设备序列号：{device_serial}")

    # -------------------- 步骤2：初始化Redis --------------------
    try:
        r = redis.Redis(**REDIS_CONFIG)
        r.ping()
        print(f"✅ Redis连接成功")
    except Exception as e:
        print(f"⚠️ Redis连接失败（优雅退出功能将受限）：{str(e)}")
        r = None  # 置空避免后续调用报错

    # -------------------- 步骤3：设备检测与连接（核心：规避NoDeviceError） --------------------
    try:
        # 1. 检查ADB环境
        check_adb_env()
        # 2. 检查设备是否在线
        check_device_online(device_serial)
        # 3. 安全连接设备
        dev = safe_connect_device(device_serial)
        # 4. 初始化auto_setup（确保日志/截图路径正常）
        auto_setup(__file__, logdir=True, devices=[f"Android:///{device_serial}"])
        print(f"✅ 设备{device_serial}连接成功，脚本初始化完成")

    except Exception as e:
        print(f"\n❌ 设备初始化失败：{str(e)}")
        sys.exit(1)

    # -------------------- 步骤4：核心业务逻辑 + 退出检测 --------------------
    try:
        # 等待目标元素加载
        print(f"🔍 等待目标元素加载（设备：{device_serial}）...")
        kuaishou_pos = exists(Template(r"tpl1765285831946.png", record_pos=(-0.346, -0.628), resolution=(720, 1612)))


        if kuaishou_pos:
            touch(kuaishou_pos)

        make_money_pos = exists(Template(r"tpl1765285700753.png", record_pos=(0.197, 1.051), resolution=(720, 1612)))
        if make_money_pos:
            touch(make_money_pos)


        welfare_box_pos = exists(Template(r"tpl1765285756343.png", record_pos=(0.36, 0.992), resolution=(720, 1612)))
        
        if welfare_box_pos:
            touch(welfare_box_pos)
            keyevent("BACK")
            sleep(1)
            keyevent("BACK")
            
            
            
        

    # -------------------- 步骤5：键盘中断捕获（Ctrl+C） --------------------
    except KeyboardInterrupt:
        print(f"\n📢 收到键盘中断信号（Ctrl+C），设备：{device_serial}")
        graceful_exit()

    # -------------------- 步骤6：通用异常处理 --------------------
    except Exception as e:
        print(f"\n❌ 脚本执行出错：{str(e)}")
        # 出错时截图保存
        try:
            snapshot(msg=f"error_{device_serial}_{str(e)[:50]}")
        except:
            pass
        # 清理Redis信号
        if r:
            try:
                r.delete(f"airtest_stop_flag_{device_serial}")
            except:
                pass
        sys.exit(1)

    # -------------------- 步骤7：正常退出 --------------------
    finally:
        if not stop_flag:
            print(f"\n📢 脚本正常结束")
        graceful_exit()



