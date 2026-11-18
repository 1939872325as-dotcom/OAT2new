# -*- coding: utf-8 -*-
"""启动时自动提权（auto-elevate at startup）

背景：游戏客户端（onmyoji.exe）常以管理员运行（由 requireAdministrator 的
启动器拉起）。Windows UIPI 规定低完整性进程无法对高完整性进程的窗口执行
SetWindowPos —— 表现为「窗口检测自动调整大小失效」，日志刷 winerror 5
（访问被拒绝）。见 WindowChecker 的 access_denied 分支。

判定标准（重要）：看【完整性级别】，不看 IsUserAnAdmin。
- 本机 UAC 关闭时 IsUserAnAdmin 恒为 True，会误判"已是管理员"直接跳过（实测踩坑）；
- exe 文件上若带强制完整性标签（Mandatory Label），进程令牌会被压到 Low，
  与是否在管理员组无关（实测踩坑：OAT2.exe 曾被打了 Low 标签）；
- UIPI 的真正判据是 TokenIntegrityLevel：≥ High 才能操作 High 的游戏窗口。

方案：启动时查自身 IL；低于 High 则用 ShellExecuteW 的 "runas" 动词重启自身
（触发一次 UAC 弹窗），原进程直接退出。用户可在设置里关闭
（settings.json 的 auto_elevate = false）。

注意：
- 打包后 sys.executable 是 OAT2.exe，直接重启 exe；
- 源码运行（python main.py）时用 sys.executable + sys.argv 重建命令行；
- 用户点 UAC「否」→ ShellExecuteW 返回拒绝 → 按未提权继续运行，
  不阻塞、不循环弹窗（每次启动最多请求一次）。
"""

import ctypes
import os
import sys
from typing import Optional

from OAT.utils.logging import logger


def is_running_as_admin() -> bool:
    """当前进程是否以管理员令牌运行（保留语义，供外部调用；提权判定不用它）"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# 完整性级别 RID（S-1-16-<RID>）
_IL_RID = {'Low': 0x1000, 'Medium': 0x2000, 'Medium Plus': 0x2100,
           'High': 0x3000, 'System': 0x4000}


def _open_self_token():
    """打开当前进程令牌（TOKEN_QUERY）；失败返回 None"""
    import ctypes.wintypes as wt
    k32 = ctypes.windll.kernel32
    adv = ctypes.windll.advapi32
    tok = wt.HANDLE()
    # GetCurrentProcess() 返回伪句柄 -1；TOKEN_QUERY = 0x0008
    if not adv.OpenProcessToken(ctypes.c_void_p(k32.GetCurrentProcess()),
                                0x0008, ctypes.byref(tok)):
        return None
    return tok


def get_self_integrity_level() -> Optional[int]:
    """取当前进程令牌的完整性级别 RID（0x2000=Medium、0x3000=High…）；失败返回 None

    不能用 IsUserAnAdmin 判断"权限够不够"：UAC 关闭时它恒为 True，而令牌仍可能
    因 exe 文件上的 Mandatory Label 被压到 Low。UIPI 判据是完整性级别本身。
    缓冲区是 TOKEN_MANDATORY_LABEL{ PSID; DWORD }：前 8 字节是指向内部 SID 的指针，
    SID 的 RID 用 GetSidSubAuthority 读末个 SubAuthority。
    """
    import ctypes.wintypes as wt
    try:
        adv = ctypes.windll.advapi32
        k32 = ctypes.windll.kernel32
        adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        adv.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.c_uint32)

        tok = _open_self_token()
        if not tok:
            return None
        try:
            buf = ctypes.create_string_buffer(64)
            rl = wt.DWORD()
            # TokenIntegrityLevel = 25
            if not adv.GetTokenInformation(tok, 25, buf, 64, ctypes.byref(rl)):
                return None
            # 结构体按指针解析：LABEL.Sid 即缓冲区内的 SID 地址（self-referential）
            sid = ctypes.cast(ctypes.byref(buf), ctypes.POINTER(ctypes.c_void_p))
            sid_ptr = ctypes.cast(sid.contents, ctypes.c_void_p)
            if not sid_ptr:
                return None
            cnt = adv.GetSidSubAuthorityCount(sid_ptr)
            if not cnt:
                return None
            return adv.GetSidSubAuthority(sid_ptr, cnt.contents.value - 1).contents.value
        finally:
            k32.CloseHandle(tok)
    except Exception:
        return None


def integrity_level_name(rid: Optional[int]) -> str:
    """RID 转可读名，用于日志"""
    if rid is None:
        return '未知'
    for name, v in _IL_RID.items():
        if v == rid:
            return name
    return f'S-1-16-{rid}'


def has_sufficient_integrity() -> bool:
    """当前完整性级别是否足以操作 High IL 的游戏窗口（≥ High）

    查不到 IL（异常环境）时按"有权限"处理，避免误触发提权循环。
    """
    rid = get_self_integrity_level()
    if rid is None:
        return True
    return rid >= _IL_RID['High']


def _is_elevation_denied(code: int) -> bool:
    """ShellExecuteW 的失败码里哪些代表用户拒绝 UAC / 无同意提示"""
    # ERROR_CANCELLED = 1223
    return code in (1223, 5)


def relaunch_as_admin() -> bool:
    """以管理员身份重启自身。

    返回 True 表示已发起提权重启（调用方应尽快退出当前进程）；
    返回 False 表示未发起（用户拒绝 UAC 或执行失败），调用方按原样继续。
    """
    params = None
    workdir = None
    try:
        if getattr(sys, 'frozen', False) or '__compiled__' in globals():
            exe = sys.executable
            params = None  # 打包版无需参数
        else:
            # 源码运行：python main.py ...，把脚本路径作为参数传回
            exe = sys.executable
            script = os.path.abspath(sys.argv[0]) if sys.argv else ''
            main_py = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), 'main.py')
            # 优先定位 main.py（__file__ 在包内，向上两级即项目根）
            if os.path.isfile(main_py):
                script = main_py
            params = f'"{script}"' if script else None

        # 工作目录必须是 exe 所在目录：ShellExecuteW 传 None 时新进程 CWD 落到
        # System32，Nuitka standalone 的 python312.dll 等依赖都在 exe 旁边，
        # 找不到就直接弹「无法继续执行代码」系统错误（实测踩坑）。
        workdir = os.path.dirname(os.path.abspath(exe)) or None

        r = ctypes.windll.shell32.ShellExecuteW(
            None, 'runas', exe, params, workdir, 1)  # SW_SHOWNORMAL
        if r > 32:
            logger.info('已请求以管理员身份重启程序（UAC 确认后新窗口生效）')
            return True
        if _is_elevation_denied(r):
            logger.warning(f'用户取消了管理员授权（ShellExecuteW 返回 {r}），按普通权限继续运行')
        else:
            logger.warning(f'提权重启失败（ShellExecuteW 返回 {r}），按普通权限继续运行')
        return False
    except Exception as e:
        logger.warning(f'提权重启异常: {e}，按普通权限继续运行')
        return False


def maybe_elevate() -> bool:
    """启动入口调用：完整性级别不足且设置允许时，提权重启并返回 True（调用方退出）。

    设置开关：settings.json 的 auto_elevate（缺省 true = 开启）。
    提权重启只尝试一次；拒绝/失败都按普通权限继续，不影响可用性。
    """
    rid = get_self_integrity_level()
    if has_sufficient_integrity():
        logger.info(f'当前进程完整性级别 {integrity_level_name(rid)}，权限足够，无需提权')
        return False

    # 读取设置开关（读失败按开启处理，与其它设置项的缺省策略一致）
    allow = True
    try:
        from OAT.tools import settings as _s
        sd = getattr(_s, 'settings_data', {}) or {}
        allow = bool(sd.get('auto_elevate', True))
    except Exception:
        allow = True

    if not allow:
        logger.info(f'当前完整性级别 {integrity_level_name(rid)} 不足'
                    f'（auto_elevate=false，跳过自动提权）；'
                    f'游戏以管理员运行时，窗口自动调整会被系统拒绝')
        return False

    logger.info(f'检测到完整性级别 {integrity_level_name(rid)} 不足（需要 High）；'
                f'游戏客户端常以管理员权限运行，低完整性下窗口自动调整会被 Windows '
                f'拒绝（UIPI）——正在请求提权…')
    if relaunch_as_admin():
        return True
    return False
