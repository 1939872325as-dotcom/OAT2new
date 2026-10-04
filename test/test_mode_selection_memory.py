"""模式 / 子模式 / 挑战次数的记忆：一变就存，下次打开或刷新后沿用，不自动重置。

背景：过去 refresh_window()（刷新窗口按钮 / Ctrl+R）会把模式拨回「请选择模式」、
挑战次数拨回 1；编辑模式后重载下拉也会把选择丢掉。现在这三项写进 settings.json，
启动与重载后按记忆恢复。

这里不启动真正的 Qt 界面：用 MainWindow.__new__ 造一个壳，把 ui 换成一小组假控件，
只驱动被测的那几个方法（与 test_client_combo.py 同一套做法）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from OAT.app import main_window as mw


# ---------- 假控件 ----------

class _FakeCombo:
    def __init__(self, items, on_index_changed=None):
        self._items = list(items)
        self._index = -1
        self._on_index_changed = on_index_changed

    def count(self):
        return len(self._items)

    def itemText(self, i):
        return self._items[i]

    def currentText(self):
        if 0 <= self._index < len(self._items):
            return self._items[self._index]
        return ""

    def setCurrentIndex(self, i):
        self._index = i
        # 真界面里 setCurrentIndex 会同步触发 on_mode_selected（子模式按钮文字随之就位），
        # 这里用回调模拟这步，否则「按文字还原勾选」的顺序就测不到。
        if self._on_index_changed is not None:
            self._on_index_changed(i)


class _FakeSpin:
    def __init__(self, value=1):
        self._value = value

    def value(self):
        return self._value

    def setValue(self, v):
        self._value = v


class _FakeRadio:
    def __init__(self, text="", checked=False, hidden=True):
        self._text = text
        self._checked = checked
        self._hidden = hidden

    def text(self):
        return self._text

    def isChecked(self):
        return self._checked

    def setChecked(self, v):
        self._checked = v

    def isHidden(self):
        return self._hidden


class _FakeBox:
    def __init__(self, hidden=True):
        self._hidden = hidden

    def isHidden(self):
        return self._hidden


class _FakeTextBrowser:
    def clear(self):
        pass

    def append(self, *a):
        pass


class _FakeLogRedirect:
    def get_timestamp(self):
        return "00:00:00"

    def print(self, *a):
        pass


# ---------- 组装 ----------

MODES = ["请选择模式", "魂土", "魂王", "绘卷刷分"]
# 各模式的子模式（对应 mode.json 里除 default 外的键）
SUB_MODES = {"魂土": ["队长", "队员"], "魂王": ["队长", "队员"]}


class _UI:
    def __init__(self, index=-1, times=1):
        self.radioButton1 = _FakeRadio(hidden=True)
        self.radioButton2 = _FakeRadio(hidden=True)
        self.soul_land_options = _FakeBox(hidden=True)
        self.spinBox = _FakeSpin(times)
        self.textBrowser = _FakeTextBrowser()
        self.find_mode_combo = _FakeCombo(MODES, on_index_changed=self._on_mode_selected)
        if index >= 0:
            self.find_mode_combo.setCurrentIndex(index)

    def _on_mode_selected(self, index):
        """模拟 home_page.on_mode_selected：按当前模式重设子模式按钮文字与可见性。"""
        name = self.find_mode_combo.currentText()
        subs = SUB_MODES.get(name, [])
        self.radioButton1.setChecked(False)
        self.radioButton2.setChecked(False)
        self.radioButton1._hidden = True
        self.radioButton2._hidden = True
        self.soul_land_options._hidden = True
        if not subs:
            return
        self.radioButton1._text = subs[0]
        self.radioButton1._hidden = False
        if len(subs) > 1:
            self.radioButton2._text = subs[1]
            self.radioButton2._hidden = False
        self.soul_land_options._hidden = False


def _window(ui):
    win = mw.MainWindow.__new__(mw.MainWindow)
    win.ui = ui
    win.log_redirect = _FakeLogRedirect()
    return win


class _SettingsSandbox:
    """把 settings_data / update_settings 换成进程内的替身，别碰用户的 settings.json。"""

    def __enter__(self):
        self._data = dict(mw.settings_data)
        self._update = mw.update_settings
        self.writes = {}

        def fake_update(key, value):
            self.writes[key] = value
            mw.settings_data[key] = value

        mw.update_settings = fake_update
        return self

    def __exit__(self, *exc):
        mw.settings_data.clear()
        mw.settings_data.update(self._data)
        mw.update_settings = self._update
        return False


# ---------- 保存 ----------

def test_save_writes_mode_sub_mode_and_times():
    ui = _UI(index=MODES.index("魂土"))
    ui.spinBox.setValue(25)
    ui.radioButton1.setChecked(True)  # 子模式「队长」
    with _SettingsSandbox() as sb:
        _window(ui)._save_mode_selection()
    assert sb.writes == {'last_mode': '魂土', 'last_sub_mode': '队长', 'last_times': 25}, sb.writes


def test_placeholder_is_not_saved_so_memory_survives():
    """停在「请选择模式」（或下拉被清空的瞬间）不能把上一次的记忆冲掉。"""
    ui = _UI(index=MODES.index("请选择模式"))
    with _SettingsSandbox() as sb:
        _window(ui)._save_mode_selection()
    assert sb.writes == {}, sb.writes


def test_empty_text_is_not_saved():
    """下拉被 clear() 的瞬间 currentText() 是空串，同样不该写。"""
    ui = _UI()
    ui.find_mode_combo._index = -1
    with _SettingsSandbox() as sb:
        _window(ui)._save_mode_selection()
    assert sb.writes == {}, sb.writes


# ---------- 恢复 ----------

def test_restore_applies_mode_sub_mode_and_times():
    ui = _UI()  # 什么都没选，模拟刚启动
    with _SettingsSandbox():
        mw.settings_data.update({'last_mode': '魂土', 'last_sub_mode': '队员', 'last_times': 30})
        _window(ui)._restore_mode_selection()

    assert ui.find_mode_combo.currentText() == "魂土"
    assert ui.radioButton1.isChecked() is False
    assert ui.radioButton2.isChecked() is True, "应按记忆还原勾选第二个子模式"
    assert ui.spinBox.value() == 30


def test_restore_keeps_default_when_mode_is_gone():
    """记忆里的模式已被删除时，不报错、不硬选，退回默认。"""
    ui = _UI()
    with _SettingsSandbox():
        mw.settings_data.update({'last_mode': '已删除的模式', 'last_sub_mode': '队长', 'last_times': 7})
        _window(ui)._restore_mode_selection()

    assert ui.find_mode_combo.currentText() == ""      # 仍停在未选择
    assert ui.radioButton1.isChecked() is False
    assert ui.radioButton2.isChecked() is False


def test_restore_times_without_mode_still_applies():
    """只记得次数（模式被删）时，次数仍应恢复。"""
    ui = _UI()
    with _SettingsSandbox():
        mw.settings_data.update({'last_mode': '', 'last_sub_mode': '', 'last_times': 12})
        _window(ui)._restore_mode_selection()
    assert ui.spinBox.value() == 12


def test_restore_ignores_sub_mode_when_container_hidden():
    """模式本身没有子模式（容器隐藏）时，不能凭空勾上某个按钮。"""
    # 反例：绘卷刷分没有子模式，即使记忆里存着子模式也不该被勾上
    ui = _UI()
    with _SettingsSandbox():
        mw.settings_data.update({'last_mode': '绘卷刷分', 'last_sub_mode': '队长', 'last_times': 1})
        _window(ui)._restore_mode_selection()
    assert ui.radioButton1.isChecked() is False, "绘卷刷分没有子模式，不该被勾上"
    assert ui.radioButton2.isChecked() is False

    # 正例：魂王有子模式，应按记忆还原
    ui2 = _UI()
    with _SettingsSandbox():
        mw.settings_data.update({'last_mode': '魂王', 'last_sub_mode': '队员', 'last_times': 1})
        _window(ui2)._restore_mode_selection()
    assert ui2.radioButton2.isChecked() is True


def test_restore_is_idempotent():
    ui = _UI()
    with _SettingsSandbox():
        mw.settings_data.update({'last_mode': '魂土', 'last_sub_mode': '队长', 'last_times': 9})
        win = _window(ui)
        win._restore_mode_selection()
        win._restore_mode_selection()
    assert ui.radioButton1.isChecked() is True
    assert ui.spinBox.value() == 9


# ---------- 不自动重置 ----------

def test_refresh_window_does_not_reset_mode_or_times():
    """刷新窗口只清日志，不能把模式拨回「请选择模式」、次数拨回 1。"""
    ui = _UI(index=MODES.index("魂土"), times=42)
    ui.radioButton1.setChecked(True)
    _window(ui).refresh_window()

    assert ui.find_mode_combo.currentText() == "魂土", "刷新不该重置模式"
    assert ui.spinBox.value() == 42, "刷新不该重置挑战次数"
    assert ui.radioButton1.isChecked() is True
