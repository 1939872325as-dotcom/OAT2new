"""
挑战函数，用for循环构建资源路径，在用if来控制自加。
"""

import os
import time
import random

from OAT.tools.OnmyojiAuto import OnmyojiAutomation
from OAT.tools import settings
from OAT.utils.do_after_challenge import do_after_challenge
from OAT.utils.logging import logger
from OAT.utils.pause_state import is_stale, pause_aware_sleep, wait_if_paused
from OAT.utils.warning_box import warning_box
from OAT.utils.error_handler import log_error


# 「一张图都没匹配到」时的节流（秒）。匹配到图时，节奏由该图的
# 「点击后随机等待」(click_wait_min/max) 接管，主循环不再额外等待。
#
# 这里替代的是过去写死在主循环末尾的 random.uniform(1.0, 2.0)：
# 那段原本想解决的是「两次识别之间别太密」，但那件事现在交给每张图各自的配置来做；
# 而「一张都没匹配到」这个场景没有图可以负责，若也一并去掉就会全速空转截图，
# 所以保留一段固定节流兜底（值按需调整，与按图配置无关）。
NO_MATCH_WAIT_RANGE = (0.7, 2.0)


class CommonChallenge:
    """
    通用挑战类，用于执行自动化挑战操作
    
    Args:
        times: 挑战次数
        config: 配置字典
        script_dir: 脚本目录
        window_title: 窗口标题
        hidden_window: 是否隐藏窗口，默认为False
        sync_mode: 是否同步模式，默认为False
        synchronizer: 同步器实例，默认为None
        sync_mode_value: 同步模式值，默认为"exactly_sync"
        threshold: 识别阈值，默认为None（使用设置中的默认值）
        find_mode: 识别模式，默认为None（使用设置中的默认值）
    """
    
    def __init__(self,
                 times: int,
                 config: dict,
                 script_dir: str,
                 window_title: str,
                 hidden_window: bool=False,
                 sync_mode: bool=False,
                 synchronizer=None,  # 同步器实例
                 sync_mode_value: str = "exactly_sync",  # 同步模式值
                 threshold: int = None,
                 find_mode: str = None,
                 window_hwnd: int = None):
        self.times = times
        self.config = config
        self.script_dir = script_dir
        self.window_title = window_title
        self.hidden_window = hidden_window
        self.sync_mode = sync_mode
        self.synchronizer = synchronizer
        self.sync_mode_value = sync_mode_value
        self.threshold = threshold
        self.find_mode = find_mode
        self.window_hwnd = window_hwnd
        self.automation_obj = None
        self.image_paths = {}
        self.image_info = {}
        # 识别顺序（默认按 config 中 image_paths 的书写顺序）
        self.ordered_keys = []
        
        # 识别模式取设置默认；阈值不在此固化——传 None 给自动化对象，
        # 让它每次识别现读设置，运行中拖「识别阈值」滑块即可实时生效
        if self.find_mode is None:
            self.find_mode = settings.FIND_MODE
        
        # 初始化自动化对象
        self.automation_obj = OnmyojiAutomation(self.window_title, self.synchronizer, self.sync_mode_value, self.find_mode, self.threshold, hwnd=self.window_hwnd)
        
        # 预加载图像
        self._preload_images()
    
    def _preload_images(self):
        """预加载所有图像模板"""
        logger.info(f"当前使用的识别模式: {self.find_mode}")
        
        for k, v in self.config['image_paths'].items():
            # 支持两种配置格式：
            # 1. 旧格式：v 是字符串路径
            # 2. 新格式：v 是包含 path、message、is_challenge_start 等信息的字典
            if isinstance(v, dict):
                path = os.path.join(self.script_dir, v['path'])
                self.image_info[k] = {
                    'message': v.get('message', ''),
                    'is_challenge_start': v.get('is_challenge_start', False),
                    'click_type': v.get('click_type') or ('coordinate' if v.get('click_area') else 'image'),
                    'click_area': v.get('click_area', None),
                    'next_image': v.get('next_image', None),
                    'is_global': v.get('is_global', False),
                    'ocr_enabled': v.get('ocr_enabled', False),
                    'ocr_target_text': v.get('ocr_target_text', ''),
                    'ocr_action': v.get('ocr_action') or '点击文字所在区域',
                    'ocr_confidence_threshold': v.get('ocr_confidence_threshold', 0.8),
                    'ocr_region': v.get('ocr_region', None),
                    # 点击后随机等待区间（秒）：每张图可单独配置，None 表示用默认 0.8~5
                    'click_wait_min': v.get('click_wait_min', None),
                    'click_wait_max': v.get('click_wait_max', None),
                    # 停止点击（出现图片停止点击）：命中任一停止图则本轮不点击该图
                    'stop_click_enabled': v.get('stop_click_enabled', False),
                    'stop_images': v.get('stop_images', []) or [],
                    'stop_time': v.get('stop_time', None)
                }
            else:
                # 兼容旧格式
                path = os.path.join(self.script_dir, v)
                self.image_info[k] = {
                    'message': '',
                    'is_challenge_start': k in ['tiaozhan', 'kaishi'],
                    'click_type': 'image',
                    'click_area': None,
                    'next_image': None,
                    'is_global': False,
                    'ocr_enabled': False,
                    'ocr_target_text': '',
                    'ocr_action': '点击文字所在区域',
                    'ocr_confidence_threshold': 0.8,
                    'ocr_region': None,
                    'click_wait_min': None,
                    'click_wait_max': None,
                    'stop_click_enabled': False,
                    'stop_images': [],
                    'stop_time': None
                }
            
            self.image_paths[k] = path

        # 配置文件里可能残留已删除图片的条目（旧版编辑器只删 png 不删配置）。
        # 这类死条目一旦进入识别序列，每轮都会拿不存在的路径去匹配，只刷错误日志、
        # 永远不命中，还会拖慢整轮。这里按文件是否存在做一次过滤，把它们剔出序列。
        # 必须在 preload 之前过滤：否则每轮启动都白读一次不存在的文件。
        missing = [k for k, p in self.image_paths.items() if not os.path.exists(p)]
        for k in missing:
            del self.image_paths[k]
            self.image_info.pop(k, None)
        if missing:
            logger.warning(
                f"配置中有 {len(missing)} 个图像条目对应的文件不存在，已跳过: {', '.join(missing)}"
            )
            # 悬空引用清理：next_image 指向被剔除的图时清空，避免流程等一个永不命中的图
            for info in self.image_info.values():
                if info.get('next_image') in missing:
                    info['next_image'] = None

        # 解析停止点击配置：把停止图相对路径解析成绝对路径，缺文件的剔除并告警；
        # 一条可用都没有就按未配置处理。必须在 preload 循环前完成，停止图也一并预加载。
        for k, info in self.image_info.items():
            info['stop_images_abs'] = None
            if not info.get('stop_click_enabled') or not info.get('stop_images'):
                continue
            _abs_list = []
            for rel in info['stop_images']:
                _p = os.path.join(self.script_dir, rel)
                if os.path.exists(_p):
                    _abs_list.append(_p)
                else:
                    logger.warning(f"停止图文件不存在，已忽略: {_p}（图 {k}）")
            if _abs_list:
                info['stop_images_abs'] = _abs_list
                try:
                    info['stop_time'] = float(
                        info.get('stop_time') if info.get('stop_time') is not None else 5.0)
                except (ValueError, TypeError):
                    info['stop_time'] = 5.0
                for _p in _abs_list:
                    self.automation_obj.preload_image(_p)
            else:
                logger.warning(f"图 {k} 配置了停止点击但没有可用的停止图，已按未配置处理")

        # 预加载存活下来的模板
        for path in self.image_paths.values():
            self.automation_obj.preload_image(path)

        self._build_recognition_order()
        logger.info(f"已预加载 {len(self.image_paths)} 张图像模板")

    def _build_recognition_order(self):
        """确定识别顺序

        config 中的 recognition_order 是作者显式声明的识别优先级；此前该字段
        没有任何代码读取，实际顺序退化为 JSON 键的书写顺序。未声明时仍按
        image_paths 的书写顺序（Python 3.7+ dict 保序）。
        """
        declared = self.config.get('recognition_order') or []
        rank = {name: i for i, name in enumerate(declared)}
        insertion = {name: i for i, name in enumerate(self.image_paths)}
        self.ordered_keys = sorted(
            self.image_paths,
            key=lambda name: (rank.get(name, len(rank)), insertion[name]),
        )
        if declared:
            logger.info(f"识别顺序: {' -> '.join(self.ordered_keys)}")
    
    def _ensure_window_size(self, tol: int = 2, warn_every_rounds: int = 20):
        """窗口尺寸守卫：客户区偏离设置目标超过 tol 像素时自动调整回目标尺寸。

        模板图按设置里的「自定义分辨率」截取，客户区一变所有模板按比例失配。
        常见成因：手动拖拽、游戏/模拟器自适配缩放。调整失败（权限不足）只告警不阻塞。
        """
        try:
            from OAT.tools.WindowChecker import WindowChecker
            target_w = int(settings.settings_data.get('custom_res_width', 1441))
            target_h = int(settings.settings_data.get('custom_res_height', 812))
            hwnd = self.automation_obj.hwnd if self.automation_obj else self.window_hwnd
            if not hwnd:
                return
            wc = WindowChecker()
            cw, ch = wc._get_client_size(hwnd)
            if cw <= 0 or ch <= 0:
                return
            # 物理 vs 逻辑：游戏是 DPI-UNAWARE 时，实际应有的客户区是「逻辑×系统缩放」
            # （125% 屏幕上 1417x798 的逻辑目标 → 1771x998 物理），
            # 用逻辑值直接比会永远差 25%、每轮都触发一次无谓 resize。
            phys_w, phys_h = wc._physical_target_size(hwnd, target_w, target_h)
            # 容差随 DPI 系数放大（1.25 倍换算 + 边框取整会带来 2~3px 偏差）
            _sc = wc._dpi_scale_for_window(hwnd) or 1.0
            _tol = max(tol, int(round(tol * _sc)) + 1)
            if abs(cw - phys_w) <= _tol and abs(ch - phys_h) <= _tol:
                self._size_guard_warned = False
                return
            # 尺寸漂了：恢复 + 告警（每 warn_every_rounds 轮提醒一次，避免刷屏）
            self._size_guard_count = getattr(self, '_size_guard_count', 0) + 1
            should_warn = (self._size_guard_count == 1
                           or self._size_guard_count % warn_every_rounds == 0)
            if should_warn:
                logger.warning(
                    f'窗口客户区 {cw}x{ch} 偏离目标 逻辑 {target_w}x{target_h}'
                    f'（物理 {phys_w}x{phys_h}；模板按逻辑尺寸截取，偏离会导致全部识别失败）'
                    f'——正在自动恢复')
            wc.resize_window(target_w, target_h, hwnd=hwnd)
            # 等待生效（最多 1.5s），失败则下轮再试
            import time as _t
            deadline = _t.time() + 1.5
            while _t.time() < deadline:
                if wc._size_close(wc._get_client_size(hwnd), (phys_w, phys_h)):
                    if should_warn:
                        logger.info('窗口尺寸已恢复到目标，识别继续')
                    self._size_guard_count = 0
                    break
                _t.sleep(0.15)
        except Exception as e:
            # 守卫绝不能打断挑战主流程
            logger.warning(f'窗口尺寸守卫执行失败（忽略）: {e}')

    def run(self) -> bool:
        """
        执行挑战操作
        
        Returns:
            bool: 挑战是否成功完成
        """
        try:
            i = 0
            current_next_image = None
            # 跟踪图片连续出现次数
            consecutive_count = {}
            # 跟踪开始识别下一个图片的时间
            next_image_start_time = None
            # 超时时间（秒）
            NEXT_IMAGE_TIMEOUT = 15

            while i < self.times:
                # 本轮是否真的点到过图：点到过则由该图的「点击后随机等待」主管节奏，
                # 一张都没点到才走末尾那段轻量节流（替代了原来写死的 1.0~2.0 秒）
                acted = False
                # 窗口尺寸守卫：客户区偏离设置目标超过容差时，模板全部匹配不上，
                # 循环会变成【静默空转】（每轮节流 0.7~2s，日志一条不出，看起来像卡死）。
                # 这里每轮检查并自动恢复尺寸 + 告警，把这种状态显性化。（实测踩坑：
                # 游戏窗口被缩到基准的 80% 后，挑战线程无声空转了数分钟毫无日志。）
                self._ensure_window_size()
                # 全局协同暂停：暂停时阻塞等待；收到停止请求则干净退出
                try:
                    if wait_if_paused() < 0:
                        try:
                            logger.info("挑战已停止")
                        except Exception:
                            pass
                        return False
                except Exception:
                    pass
                # 代际过期：双起旧线程即使错过 join 也必须退出（不替代上方停止检查）
                try:
                    try:
                        _stale = is_stale()
                    except Exception:
                        _stale = False
                    if _stale:
                        try:
                            logger.info("挑战已停止")
                        except Exception:
                            pass
                        return False
                except Exception:
                    pass
                # 优先检测全局图片
                global_images = [k for k in self.ordered_keys if self.image_info[k]['is_global']]
                for key in global_images:
                    img_path = self.image_paths[key]
                    try:
                        info = self.image_info[key]
                        if self.automation_obj.perform_action(
                            img_path, 
                            hidden_window=self.hidden_window, 
                            sync_mode=self.sync_mode,
                            click_type=info['click_type'],
                            click_area=info['click_area'],
                            ocr_enabled=info['ocr_enabled'],
                            ocr_target_text=info['ocr_target_text'],
                            ocr_action=info['ocr_action'],
                            click_wait_min=info['click_wait_min'],
                            click_wait_max=info['click_wait_max'],
                            stop_images=info.get('stop_images_abs'),
                            stop_time=info.get('stop_time')
                        ):
                            acted = True
                            if info['message']:
                                logger.info(info['message'])
                            # 重置连续出现次数，因为全局图片处理不影响正常流程
                            consecutive_count = {}
                            # 全局图片处理后继续循环，不改变current_next_image
                            continue
                    except Exception as e:
                        pass

                # 处理指定的下一个图片
                if current_next_image:
                    # 记录开始时间
                    if next_image_start_time is None:
                        next_image_start_time = time.time()
                        # print(f"开始识别下一个图片: {current_next_image}")
                    
                    # 检查是否超时
                    if time.time() - next_image_start_time > NEXT_IMAGE_TIMEOUT:
                        logger.warning(f"识别超时：{current_next_image} 在 {NEXT_IMAGE_TIMEOUT} 秒内未找到，回到默认识别模式")
                        current_next_image = None
                        next_image_start_time = None
                        consecutive_count = {}
                    else:
                        if current_next_image in self.image_paths:
                            matched_key = current_next_image
                            img_path = self.image_paths[matched_key]
                            try:
                                info = self.image_info[matched_key]
                                if self.automation_obj.perform_action(
                                    img_path, 
                                    hidden_window=self.hidden_window, 
                                    sync_mode=self.sync_mode,
                                    click_type=info['click_type'],
                                    click_area=info['click_area'],
                                    ocr_enabled=info['ocr_enabled'],
                                    ocr_target_text=info['ocr_target_text'],
                                    ocr_action=info['ocr_action'],
                                    ocr_region=info['ocr_region'],
                                    ocr_confidence_threshold=info['ocr_confidence_threshold'],
                                    click_wait_min=info['click_wait_min'],
                                    click_wait_max=info['click_wait_max'],
                                    stop_images=info.get('stop_images_abs'),
                                    stop_time=info.get('stop_time')
                                ):
                                    acted = True
                                    # 增加连续出现次数（重试次数由同一张图的连续命中次数推导）
                                    count = consecutive_count.get(matched_key, 0) + 1
                                    consecutive_count[matched_key] = count

                                    # 检查连续出现次数
                                    if count == 2:
                                        logger.warning(f"警告：图片 {matched_key} 已连续出现2次，开始计数重试")
                                    elif count >= 5:
                                        logger.error(f"错误：图片 {matched_key} 已连续出现5次，强制停止挑战")
                                        return False

                                    # 重试提示：从第2次连续命中起计数，第5次在上方强制停止
                                    if count >= 2:
                                        logger.info(f"重试次数：{count - 1}/5")
                                    
                                    if info['message']:
                                        logger.info(info['message'])
                                    if info['is_challenge_start']:
                                        i += 1
                                        logger.info(f"还剩{self.times - i}次挑战")
                                        try:
                                            logger.progress(i, self.times)
                                        except Exception:
                                            pass
                                        # 挑战开始时重置连续出现次数
                                        consecutive_count = {}
                                    # 更新下一个图片
                                    current_next_image = info['next_image']
                                    # 重置开始时间
                                    next_image_start_time = None
                                    # 切换到不同图片时重置连续出现次数（原实现引用了未定义的 key）
                                    if current_next_image != matched_key:
                                        consecutive_count = {}
                            except Exception as e:
                                logger.warning(f"处理图片 {matched_key} 失败: {e}")
                else:
                    # 没有指定下一个图片时，尝试识别所有非全局图片
                    # 过滤出非全局图片
                    non_global_images = [k for k in self.ordered_keys if not self.image_info[k]['is_global']]
                    if non_global_images:
                        # 按顺序尝试识别所有非全局图片
                        for key in non_global_images:
                            if key in self.image_paths:
                                matched_key = key
                                img_path = self.image_paths[key]
                                try:
                                    info = self.image_info[key]
                                    if self.automation_obj.perform_action(
                                        img_path, 
                                        hidden_window=self.hidden_window, 
                                        sync_mode=self.sync_mode,
                                        click_type=info['click_type'],
                                        click_area=info['click_area'],
                                        ocr_enabled=info['ocr_enabled'],
                                        ocr_target_text=info['ocr_target_text'],
                                        ocr_action=info['ocr_action'],
                                        ocr_region=info['ocr_region'],
                                        ocr_confidence_threshold=info['ocr_confidence_threshold'],
                                        click_wait_min=info['click_wait_min'],
                                        click_wait_max=info['click_wait_max']
                                    ):
                                        acted = True
                                        # 增加连续出现次数（重试次数由同一张图的连续命中次数推导）
                                        count = consecutive_count.get(key, 0) + 1
                                        consecutive_count[key] = count

                                        # 检查连续出现次数
                                        if count == 2:
                                            logger.warning(f"警告：图片 {key} 已连续出现2次，开始计数重试")
                                        elif count >= 5:
                                            logger.error(f"错误：图片 {key} 已连续出现5次，强制停止挑战")
                                            return False

                                        # 重试提示：从第2次连续命中起计数，第5次在上方强制停止
                                        if count >= 2:
                                            logger.info(f"重试次数：{count - 1}/5")
                                        
                                        if info['message']:
                                            logger.info(info['message'])
                                        if info['is_challenge_start']:
                                            i += 1
                                            logger.info(f"还剩{self.times - i}次挑战")
                                            try:
                                                logger.progress(i, self.times)
                                            except Exception:
                                                pass
                                            # 挑战开始时重置连续出现次数
                                            consecutive_count = {}
                                        # 更新下一个图片
                                        current_next_image = info['next_image']
                                        # 重置开始时间
                                        next_image_start_time = None
                                        # 切换到不同图片时重置连续出现次数
                                        if current_next_image and current_next_image != key:
                                            consecutive_count = {}
                                        # 识别到图片后跳出循环
                                        break
                                except Exception as e:
                                    logger.warning(f"处理图片 {matched_key} 失败: {e}")

                # 只有「一张图都没匹配到」时才在这里节流（暂停不计入，停止则干净退出）。
                # 匹配到图时节奏已由该图的「点击后随机等待」负责，这里不再叠加 ——
                # 过去写死的 random.uniform(1.0, 2.0) 就是被这一段取代的。
                if not acted:
                    try:
                        _sleep_target = random.uniform(*NO_MATCH_WAIT_RANGE)
                    except Exception:
                        _sleep_target = NO_MATCH_WAIT_RANGE[0]
                    try:
                        _sleep_done = pause_aware_sleep(_sleep_target, 0.1)
                    except Exception:
                        _sleep_done = True
                    if not _sleep_done:
                        try:
                            logger.info("挑战已停止")
                        except Exception:
                            pass
                        return False

            logger.info(f"挑战完成！共执行{self.times}次挑战")
            
            # 挑战完成后执行后续操作
            do_after_challenge(self.automation_obj.hwnd, self.synchronizer, self.sync_mode)
            
            return True
        except Exception as e:
            error_msg = f"错误：挑战过程中发生致命错误：{str(e)}"
            # 使用warning_box显示错误信息
            warning_box(error_msg)
            # 写入日志文件
            log_error(error_msg)
            return False


def common_challenge(
        times: int, config: dict,
        script_dir: str, window_title: str,
        hidden_window: bool=False, sync_mode: bool=False,
        synchronizer=None, # 同步器实例
        sync_mode_value: str = "exactly_sync", # 同步模式值
        threshold: int = None,
        find_mode: str = None,
        window_hwnd: int = None
) -> bool:
    """
    兼容原有函数接口的封装函数
    
    Args:
        times: 挑战次数
        config: 配置字典
        script_dir: 脚本目录
        window_title: 窗口标题
        hidden_window: 是否隐藏窗口，默认为False
        sync_mode: 是否同步模式，默认为False
        synchronizer: 同步器实例，默认为None
        sync_mode_value: 同步模式值，默认为"exactly_sync"
        threshold: 识别阈值，默认为None（使用设置中的默认值）
        find_mode: 识别模式，默认为None（使用设置中的默认值）
    
    Returns:
        bool: 挑战是否成功完成
    """
    challenge = CommonChallenge(
        times=times,
        config=config,
        script_dir=script_dir,
        window_title=window_title,
        hidden_window=hidden_window,
        sync_mode=sync_mode,
        synchronizer=synchronizer,
        sync_mode_value=sync_mode_value,
        threshold=threshold,
        find_mode=find_mode,
        window_hwnd=window_hwnd
    )
    return challenge.run()