# -*- coding: utf-8 -*-
"""把本程序的输入法切到微软拼音（中文）。

背景
----
Windows 会按 exe 记住每个程序上次用的输入法。若 OAT2.exe 记住的是
「中文(简体) - 美式键盘」（它不是 IME），窗口里打字只会得到英文字母，
输入法根本不参与组词 —— 表现就是「添加模式时无法输入中文」。
按 Win+Space 可以临时切回来，但这个记忆会被重置，于是问题反复出现。

这里在程序启动时通过 TSF 激活微软拼音的输入法配置（ITfInputProcessorProfiles
::ActivateLanguageProfile），让它对该进程的 GUI 线程生效。已经激活时是幂等的，
失败也不影响程序运行（用户仍可手动 Win+Space）。
"""
import ctypes
import uuid

from OAT.utils.logging import logger

# CLSID_TF_InputProcessorProfiles / IID_ITfInputProcessorProfiles
_CLSID_PROFILES = "{33C53A50-F456-4884-B049-85FD643ECFED}"
_IID_PROFILES = "{1F02B6C5-7842-4EE6-8A0B-9A24183A95CA}"
# 微软拼音：TextService CLSID + 语言配置 GUID
# （对应 HKCU\Control Panel\International\User Profile\zh-Hans-CN 里的 0804:{...}{...}）
_CLSID_MSPINYIN = "{81D4E9C9-1D3B-41BC-9E6C-4B40BF79E35E}"
_GUID_MSPINYIN = "{FA550B04-5AD7-411F-A5AC-CA038EC515D7}"
_LANGID_ZH_CN = 0x0804

_CLSCTX_INPROC_SERVER = 0x1
_VTBL_RELEASE = 2
_VTBL_ACTIVATE_LANGUAGE_PROFILE = 10


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _to_guid(text):
    u = uuid.UUID(text)
    g = _GUID()
    g.Data1 = u.time_low
    g.Data2 = u.time_mid
    g.Data3 = u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


def _vtable_fn(ptr_value, index, restype, *argtypes):
    """取 COM 接口虚函数表第 index 项并转成可调用函数。"""
    vtable = ctypes.cast(ptr_value, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    addr = vtable[index]
    if hasattr(addr, "value"):
        addr = addr.value
    if not addr:
        raise OSError(f"COM 虚函数表第 {index} 项为空")
    return ctypes.WINFUNCTYPE(restype, *argtypes)(addr)


def _create_profiles():
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ptr = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(_to_guid(_CLSID_PROFILES)), None, _CLSCTX_INPROC_SERVER,
        ctypes.byref(_to_guid(_IID_PROFILES)), ctypes.byref(ptr))
    if hr != 0 or not ptr.value:
        raise OSError(f"CoCreateInstance 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
    return ptr


def ensure_chinese_ime() -> bool:
    """把 GUI 线程的输入法配置激活为微软拼音；返回是否成功。

    幂等：已经是微软拼音时调用无副作用。任何异常都只记日志，不抛给调用方。
    """
    ptr = ctypes.c_void_p()
    try:
        ptr = _create_profiles()
        activate = _vtable_fn(
            ptr.value, _VTBL_ACTIVATE_LANGUAGE_PROFILE, ctypes.c_long,
            ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.c_ushort, ctypes.POINTER(_GUID))
        hr = activate(ptr, ctypes.byref(_to_guid(_CLSID_MSPINYIN)), _LANGID_ZH_CN,
                      ctypes.byref(_to_guid(_GUID_MSPINYIN)))
        ok = (hr & 0xFFFFFFFF) == 0
        if ok:
            logger.info("输入法：已切换到微软拼音")
        else:
            logger.warning(f"输入法：切换到微软拼音失败 hr=0x{hr & 0xFFFFFFFF:08X}"
                           f"（可手动按 Win+Space 切换）")
        return ok
    except Exception as e:
        logger.warning(f"输入法初始化失败（不影响使用，可手动 Win+Space 切换）：{e}")
        return False
    finally:
        if ptr.value:
            try:
                release = _vtable_fn(ptr.value, _VTBL_RELEASE, ctypes.c_ulong, ctypes.c_void_p)
                release(ptr)
            except Exception:
                pass
