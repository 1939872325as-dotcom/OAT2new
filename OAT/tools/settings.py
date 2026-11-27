APP_VERSION = "2.3.8"

import json
import os
import threading
from typing import Optional

# 加载设置配置
current_dir = os.path.dirname(os.path.abspath(__file__))
settings_file_path = os.path.join(current_dir, 'settings.json')
from ..utils.logging import logger

# 读取配置文件
try:
    with open(settings_file_path, 'r', encoding='utf-8') as f:
        settings_data = json.load(f)
except Exception as e:
    logger.error(f"加载配置文件失败: {str(e)}")
    # 使用默认配置
    settings_data = {
        'theme': 'light',
        'transparency': 50,
        'close_program_after_challenge': False,
        'close_game_after_challenge': False,
        'sync_mode': 'exactly_sync',
        'find_value': 85,
        'find_mode': 'opencv',
        'use_grayscale': True,
        'custom_res_width': 1441,
        'custom_res_height': 812,
        'window_arrange_mode': 'diagonal',  # 窗口排列方式：diagonal（对角线）或 tile（平铺）
        'windows_per_row': 3,  # 平铺排列时一行的窗口数量
        'capture_window_mode': 'PrintWindow'  # 窗口捕获模式(PrintWindow/BitBlt)
    }

# 导出配置变量
FIND_MODE = settings_data.get('find_mode', 'opencv')
FIND_THRESHOLD = settings_data.get('find_value', 85)
FIND_THRESHOLD_VALUE = FIND_THRESHOLD / 100.0  # 转换为0-1之间的值

# 模板匹配是否走灰度（默认开）。
# 灰度把三通道合成单通道，matchTemplate 耗时约为彩色的 1/6（1416x798 实测
# 142ms→21ms），现有模板的分数与彩色一致；代价是丢失色度信息——「形状/亮度
# 相同、只有颜色不同」的两个状态会变得不可区分（御魂稀有度边框、元素球、
# 技能可用/冷却等）。识别率下降时可在设置里关掉，运行中切换立即生效。
USE_GRAYSCALE = bool(settings_data.get('use_grayscale', True))

# 其他配置变量
# 窗口排列相关设置
WINDOW_ARRANGE_MODE = settings_data.get('window_arrange_mode', 'diagonal')  # 窗口排列方式
WINDOWS_PER_ROW = settings_data.get('windows_per_row', 3)  # 平铺排列时一行的窗口数量
# 后台获取图像模式
BACKEND_GET_IMG_MODE = settings_data.get('capture_window_mode', 'PrintWindow')  # 后台获取图像模式(PrintWindow/BitBlt)
# 模拟器后台模式（MuMu 免 ADB）
EMULATOR_TYPE = settings_data.get('emulator_type', 'pc')

HANDLE_SPEC = settings_data.get('handle_spec', 'auto')
SCREENSHOT_METHOD = settings_data.get('screenshot_method', 'nemu_ipc')
CONTROL_METHOD = settings_data.get('control_method', 'window_message')
DEFAULT_MUMU_FOLDER = 'E:\\MuMuPlayer'
MUMU_FOLDER = settings_data.get('mumu_folder', DEFAULT_MUMU_FOLDER)

# 提供更新配置的函数
# 写盘模型：内存中的 settings_data 与导出变量【立即】生效（所有读方无感知），
# 磁盘写入经 0.3 秒合并窗口延后——滑条拖动/连敲键盘每次 tick 都调 update_settings，
# 原来每次都整文件 json.dump（拖一次滑条写盘几十次）。写盘跨线程（GUI 线程 +
# 截图工作线程切模式 + MuMu 目录回写），必须有锁；临时文件 + os.replace 保证
# 崩溃/并发不会留下半截 JSON（原来截断一次就丢全部用户设置）。
_settings_lock = threading.Lock()
_flush_timer = None


def _write_settings_file() -> None:
    """把 settings_data 原子写入 settings.json（调用方须已持锁）。"""
    tmp_path = settings_file_path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(settings_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, settings_file_path)


def _flush_settings() -> None:
    """定时合并写盘（Timer 线程）。"""
    global _flush_timer
    with _settings_lock:
        _flush_timer = None
        try:
            _write_settings_file()
        except Exception as e:
            logger.error(f"保存配置文件失败: {str(e)}")


def _apply_export(key, value) -> None:
    """同步更新模块级导出变量（立即生效，不等写盘）。"""
    global FIND_MODE, FIND_THRESHOLD, FIND_THRESHOLD_VALUE, USE_GRAYSCALE
    global WINDOW_ARRANGE_MODE, WINDOWS_PER_ROW, BACKEND_GET_IMG_MODE
    global EMULATOR_TYPE, HANDLE_SPEC, SCREENSHOT_METHOD, CONTROL_METHOD, MUMU_FOLDER
    if key == 'find_mode':
        FIND_MODE = value
    elif key == 'find_value':
        FIND_THRESHOLD = value
        FIND_THRESHOLD_VALUE = value / 100.0
    elif key == 'use_grayscale':
        USE_GRAYSCALE = bool(value)
    elif key == 'window_arrange_mode':
        WINDOW_ARRANGE_MODE = value
    elif key == 'windows_per_row':
        WINDOWS_PER_ROW = value
    elif key == 'capture_window_mode':
        BACKEND_GET_IMG_MODE = value
    elif key == 'emulator_type':
        EMULATOR_TYPE = value
    elif key == 'handle_spec':
        HANDLE_SPEC = value
    elif key == 'screenshot_method':
        SCREENSHOT_METHOD = value
    elif key == 'control_method':
        CONTROL_METHOD = value
    elif key == 'mumu_folder':
        MUMU_FOLDER = value


def update_settings(key, value):
    """
    更新配置并保存到文件（内存/导出变量立即生效；磁盘写入合并延后 ≤0.3s）

    Args:
        key: 配置键名
        value: 配置值
    """
    global _flush_timer
    with _settings_lock:
        settings_data[key] = value
        try:
            _apply_export(key, value)
        except Exception as e:
            logger.error(f"更新配置导出变量失败: {key}={value}: {str(e)}")
        # 已有待触发的合并写盘就不再新起定时器：连续调用自动合并成一次写盘
        t = _flush_timer
        if t is not None:
            try:
                if t.is_alive():
                    return True
            except Exception:
                pass
        try:
            nt = threading.Timer(0.3, _flush_settings)
            nt.daemon = True
            _flush_timer = nt
        except Exception as e:
            # 定时器创建失败：退回同步写盘，保证最终一致
            try:
                _write_settings_file()
            except Exception as e2:
                logger.error(f"保存配置文件失败: {str(e2)}")
                return False
        nt.start()
        return True


def _is_valid_mumu_folder(folder) -> bool:
    """配置目录是否为有效 MuMu 根（惰性导入避免启动加载 win32/psutil）。"""
    try:
        from OAT.tools.emulator.mumu_handle import is_mumu_root
        return is_mumu_root(folder)
    except Exception:
        return False


def _detect_mumu_folder_safe() -> Optional[str]:
    """进程反推 MuMu 安装目录；任何异常按未找到处理。"""
    try:
        from OAT.tools.emulator.mumu_handle import detect_mumu_folder
        return detect_mumu_folder()
    except Exception:
        return None


def resolve_mumu_folder() -> str:
    """生效的 MuMu 安装目录：配置有效→配置；否则进程反推并回写；再失败→默认。"""
    if _is_valid_mumu_folder(MUMU_FOLDER):
        return MUMU_FOLDER
    try:
        detected = _detect_mumu_folder_safe()
    except Exception:
        detected = None
    if detected:
        update_settings('mumu_folder', detected)
        return detected
    return MUMU_FOLDER or DEFAULT_MUMU_FOLDER

