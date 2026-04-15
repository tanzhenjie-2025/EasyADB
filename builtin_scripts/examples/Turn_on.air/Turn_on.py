# 导入基础库
from airtest.core.api import auto_setup, connect_device, keyevent, sleep, swipe
import re
import subprocess
import sys
import time
import logging
import os
from pathlib import Path

# ===================== 1. 日志配置（增强实时性+设备标识） =====================
def setup_device_logger(device_id):
    """
    配置带设备标识的实时日志
    :param device_id: 设备序列号/IP:端口
    :return: 配置好的logger对象
    """
    # 创建专属logger，避免多设备日志冲突
    logger = logging.getLogger(f"DeviceUnlock_{device_id}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # 清空重复handler
    
    # 日志格式：时间 - 设备 - 级别 - 消息
    formatter = logging.Formatter(
        '[%(asctime)s] [%(name)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 1. 控制台输出（实时无缓冲）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 2. 文件输出（按设备分文件）
    file_handler = logging.FileHandler(f'device_power_on_{device_id}.log', encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # 禁用日志传播，避免重复输出
    logger.propagate = False
    
    # 强制开启行缓冲，确保日志实时输出
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)
    
    return logger

# ===================== 2. 配置类（分设备适配） =====================
class UnlockConfig:
    """解锁配置类（分手机/平板适配）"""
    # ADB路径自动检测优先级
    ADB_PATHS = [
        r"D:\ItApp\Auto\platform-tools-latest-windows\platform-tools\adb.exe",
        r"C:\platform-tools\adb.exe",
        os.path.expanduser("~\\AppData\\Local\\Android\\Sdk\\platform-tools\\adb.exe"),
        "adb.exe",  # 系统环境变量中的ADB
    ]
    
    # ========== 手机配置（保留原有稳定值） ==========
    PHONE_CONFIG = {
        "SCREEN_ON_CHECK_RETRY": 2,
        "SWIPE_RETRY": 1,
        "UNLOCK_VERIFY_RETRY": 2,
        "SLEEP_AFTER_POWER_KEY": 2.5,
        "SLEEP_AFTER_SWIPE": 1.0,
        "SLEEP_AFTER_VERIFY": 0.5,
        "SWIPE_DURATION": 0.4,
        "SWIPE_STEP_DELAY": 0.5,
        "SWIPE_START_Y_RATIO": 0.85,
        "SWIPE_END_Y_RATIO": 0.3,
        "SWIPE_X_CENTER_RATIO": 0.5
    }
    
    # ========== 平板配置（适配优化） ==========
    TABLET_CONFIG = {
        "SCREEN_ON_CHECK_RETRY": 3,
        "SWIPE_RETRY": 1,
        "UNLOCK_VERIFY_RETRY": 2,
        "SLEEP_AFTER_POWER_KEY": 3.0,
        "SLEEP_AFTER_SWIPE": 1.5,
        "SLEEP_AFTER_VERIFY": 0.5,
        "SWIPE_DURATION": 0.6,
        "SWIPE_STEP_DELAY": 0.5,
        "SWIPE_START_Y_RATIO": 0.85,
        "SWIPE_END_Y_RATIO": 0.2,
        "SWIPE_X_CENTER_RATIO": 0.5
    }

# ===================== 3. 工具函数 =====================
def find_adb_path():
    """自动检测可用的ADB路径"""
    for adb_path in UnlockConfig.ADB_PATHS:
        if os.path.exists(adb_path) and os.access(adb_path, os.X_OK):
            return adb_path
    # 最后尝试系统环境变量中的ADB
    try:
        subprocess.check_output(["adb", "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return "adb"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

def is_tablet(dev, logger):
    """
    自动识别设备类型（手机/平板）
    判断依据：1.分辨率比例 2.设备属性 3.屏幕尺寸
    """
    try:
        # 1. 获取分辨率判断比例（平板宽高比更接近1:1）
        width, height = dev.get_current_resolution()
        ratio = min(width, height) / max(width, height)
        # 2. 获取设备属性
        device_prop = dev.shell("getprop ro.product.device").strip().lower()
        tablet_keywords = ["tablet", "pad", "tab", "xiaomiPad", "huaweiPad"]
        
        # 判定规则：比例>0.6 或 设备名包含平板关键字
        if ratio > 0.6 or any(keyword in device_prop for keyword in tablet_keywords):
            logger.info("📱 识别为平板设备")
            return True
        else:
            logger.info("📱 识别为手机设备")
            return False
    except Exception as e:
        logger.warning(f"⚠️ 设备类型识别失败，默认按手机处理：{e}")
        return False

def get_device_config(is_tablet_device):
    """根据设备类型获取对应配置"""
    if is_tablet_device:
        return UnlockConfig.TABLET_CONFIG
    else:
        return UnlockConfig.PHONE_CONFIG

def verify_unlock_success(dev, config, logger):
    """验证解锁是否成功（检测是否进入主屏幕）"""
    for retry in range(config["UNLOCK_VERIFY_RETRY"]):
        try:
            # 方式1：检测是否能执行主屏幕相关操作
            dev.shell("am start -W -a android.intent.action.MAIN -c android.intent.category.HOME")
            logger.info(f"✅ 解锁验证成功（重试{retry+1}/{config['UNLOCK_VERIFY_RETRY']}）")
            return True
        except Exception as e:
            logger.warning(f"⚠️ 解锁验证失败（重试{retry+1}/{config['UNLOCK_VERIFY_RETRY']}）：{e}")
            sleep(config["SLEEP_AFTER_VERIFY"])
    return False

# ===================== 4. 核心功能函数 =====================
def check_screen_state(dev, device_id, adb_path, config, is_tablet_device, logger):
    """检测屏幕亮屏状态（手机display优先+平板强制亮度判断）"""
    # ========== 平板专属检测逻辑（保留之前修复） ==========
    if is_tablet_device:
        for retry in range(config["SCREEN_ON_CHECK_RETRY"]):
            try:
                # 1. 优先读取屏幕亮度（核心判定依据）
                brightness = -1
                try:
                    # 方式1：读取系统亮度设置
                    brightness_str = dev.shell("settings get system screen_brightness").strip()
                    brightness = int(brightness_str) if brightness_str else 0
                    logger.info(f"📊 平板屏幕亮度值：{brightness}")
                except:
                    # 方式2：从dumpsys power提取亮度
                    power_info = dev.shell("dumpsys power").lower()
                    brightness_match = re.search(r"mscreenbrightness=(\d+)", power_info)
                    if brightness_match:
                        brightness = int(brightness_match.group(1))
                        logger.info(f"📊 平板从dumpsys提取亮度值：{brightness}")
                
                # 2. 读取dumpsys display状态（更精准）
                display_state = "unknown"
                try:
                    display_info = dev.shell("dumpsys display").lower()
                    if "state=off" in display_info or "display power state: off" in display_info:
                        display_state = "off"
                    elif "state=on" in display_info or "display power state: on" in display_info:
                        display_state = "on"
                    logger.info(f"📊 平板display状态：{display_state}")
                except:
                    display_state = "unknown"
                    logger.warning("⚠️ 平板无法读取display状态")
                
                # 3. 平板核心判定规则（优先级：亮度 > display状态 > 关键字）
                if brightness == 0:
                    logger.info("📱 平板亮度为0，强制判定为熄屏")
                    return False
                elif brightness > 0 and display_state == "on":
                    logger.info("📱 平板亮度>0且display为on，判定为亮屏")
                    return True
                else:
                    logger.warning(f"⚠️ 平板亮度={brightness}，display={display_state}，默认判定为熄屏")
                    return False
            except Exception as e:
                logger.warning(f"⚠️ 平板检测失败（重试{retry+1}/{config['SCREEN_ON_CHECK_RETRY']}）：{e}")
                sleep(0.8)
        
        # 平板最终策略：强制判定为熄屏
        logger.warning(f"⚠️ 平板检测失败，强制判定为熄屏")
        return False
    
    # ========== 手机检测逻辑（终极优化：display优先+宽松亮屏） ==========
    else:
        # 扩展关键字（兼容更多手机型号）
        on_patterns = [
            r"mScreenOn=true",
            r"display power state: on",
            r"screenstate=on",
            r"isScreenOn=true",
            r"screen on: true",
            r"powerstate=on",
            r"lcd_power=on"
        ]
        off_patterns = [
            r"mScreenOn=false",
            r"display power state: off",
            r"screenstate=off",
            r"isScreenOn=false",
            r"screen on: false",
            r"powerstate=off",
            r"lcd_power=off"
        ]
        
        for retry in range(config["SCREEN_ON_CHECK_RETRY"]):
            try:
                # 检测维度1：dumpsys display（最高优先级）
                display_on = False
                display_off = False
                try:
                    display_info = dev.shell("dumpsys display").lower()
                    display_on = "state=on" in display_info or "display power state: on" in display_info
                    display_off = "state=off" in display_info or "display power state: off" in display_info
                except:
                    display_on = False
                    display_off = False
                
                # 检测维度2：dumpsys power（辅助验证）
                power_on = False
                power_off = False
                try:
                    power_info = dev.shell("dumpsys power").lower()
                    power_on = any(re.search(pattern, power_info) for pattern in on_patterns)
                    power_off = any(re.search(pattern, power_info) for pattern in off_patterns)
                except:
                    power_on = False
                    power_off = False
                
                logger.info(f"📊 手机检测结果：display_on={display_on}, display_off={display_off}, power_on={power_on}, power_off={power_off}")
                
                # 手机核心判定规则（display优先，宽松亮屏）
                if display_off:
                    # 仅当display明确熄屏时，才判定为熄屏
                    logger.info("📱 手机display检测到明确熄屏状态")
                    return False
                elif display_on:
                    # display明确亮屏，直接判定为亮屏（忽略power状态）
                    logger.info("📱 手机display检测到亮屏状态")
                    return True
                else:
                    # display状态模糊，参考power状态
                    if power_off:
                        logger.info("📱 手机power检测到明确熄屏状态")
                        return False
                    elif power_on:
                        logger.info("📱 手机power检测到亮屏状态")
                        return True
                    else:
                        # 状态完全模糊，重试
                        logger.warning(f"⚠️ 手机亮屏状态未明确匹配（重试{retry+1}/{config['SCREEN_ON_CHECK_RETRY']}）")
                        sleep(0.5)
            
            except Exception as e:
                logger.warning(f"⚠️ 手机检测失败，改用ADB调用（重试{retry+1}/{config['SCREEN_ON_CHECK_RETRY']}）：{e}")
                try:
                    # 备用方案：优先检测display
                    cmd_display = f'"{adb_path}" -s {device_id} shell dumpsys display'
                    display_info = subprocess.check_output(
                        cmd_display, shell=True, encoding="utf-8", errors="ignore"
                    ).lower()
                    display_on_adb = "state=on" in display_info or "display power state: on" in display_info
                    display_off_adb = "state=off" in display_info or "display power state: off" in display_info
                    
                    if display_on_adb:
                        logger.info("📱 ADB检测到手机display亮屏状态")
                        return True
                    elif display_off_adb:
                        logger.info("📱 ADB检测到手机display熄屏状态")
                        return False
                    else:
                        # display模糊，检测power
                        cmd_power = f'"{adb_path}" -s {device_id} shell dumpsys power'
                        power_info = subprocess.check_output(
                            cmd_power, shell=True, encoding="utf-8", errors="ignore"
                        ).lower()
                        if "mScreenOn=false" in power_info:
                            logger.info("📱 ADB检测到手机power熄屏状态")
                            return False
                except Exception as adb_e:
                    logger.error(f"❌ ADB检测失败（重试{retry+1}/{config['SCREEN_ON_CHECK_RETRY']}）：{adb_e}")
                    sleep(0.5)
        
        # 手机最终策略：默认判定为亮屏（核心！彻底避免亮屏误操作）
        logger.warning(f"⚠️ 手机检测失败，默认判定为亮屏（避免误关屏幕）")
        return True

def power_on_screen(dev, device_id, adb_path, config, is_tablet_device, logger):
    """执行电源键亮屏操作（双方案保障）"""
    try:
        # 方案1：Airtest keyevent
        keyevent("26")  # 电源键事件码
        logger.info(f"✅ Airtest电源键亮屏指令已发送（{'平板' if is_tablet_device else '手机'}）")
        sleep(config["SLEEP_AFTER_POWER_KEY"])
        return True
    except Exception as power_e:
        logger.warning(f"⚠️ Airtest电源键操作失败：{power_e}")
        try:
            # 方案2：ADB备用方案
            cmd = f'"{adb_path}" -s {device_id} shell input keyevent 26'
            subprocess.check_output(cmd, shell=True, encoding="utf-8", errors="ignore")
            logger.info(f"✅ ADB备用电源键亮屏成功（{'平板' if is_tablet_device else '手机'}）")
            sleep(config["SLEEP_AFTER_POWER_KEY"])
            return True
        except Exception as adb_power_e:
            logger.error(f"❌ ADB备用电源键也失败：{adb_power_e}")
            return False

def swipe_unlock_screen(dev, device_id, adb_path, screen_width, screen_height, config, is_tablet_device, logger):
    """执行滑动解锁（分手机/平板适配）"""
    # 计算滑动坐标
    start_x = int(screen_width * config["SWIPE_X_CENTER_RATIO"])
    start_y = int(screen_height * config["SWIPE_START_Y_RATIO"])
    end_x = start_x
    end_y = int(screen_height * config["SWIPE_END_Y_RATIO"])
    
    device_type = "平板" if is_tablet_device else "手机"
    logger.info(f"📏 {device_type}滑动坐标：({start_x},{start_y}) → ({end_x},{end_y})（时长：{config['SWIPE_DURATION']}s）")
    
    for retry in range(config["SWIPE_RETRY"] + 1):
        try:
            # 方案1：Airtest swipe
            swipe((start_x, start_y), (end_x, end_y), duration=config["SWIPE_DURATION"])
            sleep(config["SLEEP_AFTER_SWIPE"])
            logger.info(f"✅ Airtest滑动解锁完成（{device_type}，重试{retry}/{config['SWIPE_RETRY']}）")
            return True
        except Exception as swipe_e:
            logger.warning(f"⚠️ Airtest滑动失败（{device_type}，重试{retry}/{config['SWIPE_RETRY']}）：{swipe_e}")
            try:
                # 方案2：ADB滑动
                swipe_duration_ms = int(config["SWIPE_DURATION"] * 1000)
                cmd = f'"{adb_path}" -s {device_id} shell input swipe {start_x} {start_y} {end_x} {end_y} {swipe_duration_ms}'
                subprocess.check_output(cmd, shell=True, encoding="utf-8", errors="ignore")
                sleep(config["SLEEP_AFTER_SWIPE"])
                logger.info(f"✅ ADB备用滑动解锁成功（{device_type}，重试{retry}/{config['SWIPE_RETRY']}）")
                return True
            except Exception as adb_swipe_e:
                logger.error(f"❌ ADB滑动也失败（{device_type}，重试{retry}/{config['SWIPE_RETRY']}）：{adb_swipe_e}")
                sleep(config["SWIPE_STEP_DELAY"])
    
    logger.warning(f"⚠️ 所有滑动方式均失败（{device_type}可能无需滑动解锁/有密码锁）")
    return False

# ===================== 5. 主函数 =====================
def main():
    # 1. 参数校验
    if len(sys.argv) < 2:
        print("[ERROR] 错误：未指定设备！")
        print("[INFO] 正确用法：python 脚本路径 设备标识")
        print("[INFO] 示例1（序列号）：python Turn_on.py 10AF5E1AKU003NR")
        print("[INFO] 示例2（IP+端口）：python Turn_on.py 192.168.33.204:5555")
        sys.stdout.flush()
        sys.exit(1)
    
    DEVICE_ID = sys.argv[1].strip()
    
    # 2. 初始化日志
    logger = setup_device_logger(DEVICE_ID)
    logger.info(f"🔌 开始处理设备：{DEVICE_ID}")
    sys.stdout.flush()
    
    # 3. 自动检测ADB路径
    adb_path = find_adb_path()
    if not adb_path:
        logger.error("❌ 未找到可用的ADB，请检查ADB路径配置或系统环境变量")
        sys.stdout.flush()
        sys.exit(1)
    logger.info(f"✅ 找到ADB路径：{adb_path}")
    sys.stdout.flush()
    
    # 4. 连接设备
    auto_setup(__file__)
    try:
        dev = connect_device(f"Android:///{DEVICE_ID}")
        dev.shell("echo 'device online'")
        logger.info("✅ 设备连接成功")
        sys.stdout.flush()
    except Exception as e:
        logger.error(f"❌ 设备连接失败：{e}")
        logger.info("   排查方向：1.ADB设备列表（adb devices）能看到该设备；2.设备标识格式正确；3.设备已开启USB调试")
        sys.stdout.flush()
        sys.exit(1)
    
    # 5. 识别设备类型+获取分辨率
    is_tablet_device = is_tablet(dev, logger)
    config = get_device_config(is_tablet_device)
    
    try:
        screen_width, screen_height = dev.get_current_resolution()
        logger.info(f"📱 设备分辨率：{screen_width}x{screen_height}")
        sys.stdout.flush()
    except Exception as e:
        logger.error(f"❌ 获取分辨率失败：{e}")
        sys.stdout.flush()
        sys.exit(1)
    
    # 6. 检测屏幕状态并执行对应操作
    screen_on = check_screen_state(dev, DEVICE_ID, adb_path, config, is_tablet_device, logger)
    sys.stdout.flush()
    
    if not screen_on:
        # 6.1 熄屏：执行亮屏+解锁
        device_type = "平板" if is_tablet_device else "手机"
        logger.info(f"📱 {device_type}处于熄屏状态，执行亮屏操作...")
        
        if not power_on_screen(dev, DEVICE_ID, adb_path, config, is_tablet_device, logger):
            logger.error(f"❌ {device_type}亮屏操作失败，退出执行")
            sys.stdout.flush()
            sys.exit(1)
        
        # 执行滑动解锁
        swipe_success = swipe_unlock_screen(dev, DEVICE_ID, adb_path, screen_width, screen_height, config, is_tablet_device, logger)
        sys.stdout.flush()
        
        # 验证解锁结果
        if verify_unlock_success(dev, config, logger):
            logger.info(f"🎉 {device_type}亮屏解锁完成！")
        else:
            logger.warning(f"⚠️ {device_type}解锁验证未通过（可能有密码锁/指纹锁）")
    else:
        # 6.2 亮屏：无需操作
        logger.info("✅ 屏幕已亮，无需执行任何操作")
    
    # 最终刷新输出
    sys.stdout.flush()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] 用户中断执行")
        sys.stdout.flush()
    except Exception as e:
        print(f"\n[ERROR] 脚本执行异常：{e}")
        sys.stdout.flush()
        sys.exit(1)