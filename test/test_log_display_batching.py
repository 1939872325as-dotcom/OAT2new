"""日志窗口刷新策略回归测试。

背景：原先每条日志都在 GUI 线程 insertHtml + 滚动到底，文档越大单条越慢
（实测运行数小时后单条 ~0.7s），点界面会卡。现在改为批量缓冲、文末插入、
自适应刷新间隔。这里锁住这些行为。
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from OAT.utils import logging as logmod
from OAT.utils.logging import LogRedirect


class TestDisplayBatching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication, QTextBrowser
        cls.app = QApplication.instance() or QApplication([])
        cls.QTextBrowser = QTextBrowser

    def setUp(self):
        self.browser = self.QTextBrowser()
        self.browser.resize(600, 300)
        self.browser.document().setMaximumBlockCount(2000)
        self.browser.show()
        self.app.processEvents()
        self.lr = LogRedirect(self.browser)

    def tearDown(self):
        self.browser.close()
        self.browser.deleteLater()
        self.lr.deleteLater()

    def _html(self, i):
        return (f"<span title=\"INFO\"><font color='#22C55E'>●</font> "
                f"12:00:00 第 {i} 行日志</span>")

    def test_lines_are_buffered_until_flush(self):
        for i in range(30):
            self.lr._safe_append(self._html(i))
        # 未刷新前不应触碰文档（GUI 线程不被日志占住）
        self.assertEqual(self.browser.toPlainText().strip(), "")
        self.lr._flush_pending()
        text = self.browser.toPlainText()
        for i in range(30):
            self.assertIn(f"第 {i} 行日志", text)
        # 顺序保持：0 在 29 前面
        self.assertLess(text.index("第 0 行"), text.index("第 29 行"))

    def test_flush_inserts_at_end_even_after_user_clicks_log(self):
        """用户点过日志（光标移到开头）后，新日志仍应追加到文末"""
        from PyQt6.QtGui import QTextCursor

        self.lr._safe_append(self._html(1))
        self.lr._flush_pending()

        # 模拟用户点击日志顶部：把控件光标移到文档开头
        cur = self.browser.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.Start)
        self.browser.setTextCursor(cur)
        user_pos = self.browser.textCursor().position()

        self.lr._safe_append(self._html(2))
        self.lr._flush_pending()

        text = self.browser.toPlainText()
        self.assertLess(text.index("第 1 行"), text.index("第 2 行"))
        # 用户的选区/光标不被日志追加移动
        self.assertEqual(self.browser.textCursor().position(), user_pos)

    def test_pending_cap_reports_dropped_lines(self):
        n = logmod._DISPLAY_MAX_PENDING + 10
        for i in range(n):
            self.lr._safe_append(self._html(i))
        self.lr._flush_pending()
        text = self.browser.toPlainText()
        self.assertIn("省略 10 条", text)
        self.assertIn(f"第 {n - 1} 行日志", text)
        self.assertNotIn("第 0 行日志", text)

    def test_hidden_browser_defers_flush(self):
        """窗口不可见（最小化/切页）时不排版，缓冲保留到重新可见"""
        self.browser.hide()
        self.app.processEvents()
        self.lr._safe_append(self._html(1))
        self.lr._flush_pending()
        self.assertEqual(self.browser.toPlainText().strip(), "")

        self.browser.show()
        self.app.processEvents()
        self.lr._flush_pending()
        self.assertIn("第 1 行日志", self.browser.toPlainText())

    def test_adaptive_interval_backs_off_after_slow_flush(self):
        self.lr._last_flush_ms = 0
        self.assertEqual(self.lr._next_flush_interval(),
                         logmod._DISPLAY_FLUSH_MIN_MS)
        self.lr._last_flush_ms = 500
        self.assertEqual(self.lr._next_flush_interval(),
                         logmod._DISPLAY_FLUSH_MAX_MS)

    def test_signal_emission_is_coalesced(self):
        """真实信号路径：多次 emit 只入缓冲，一次 flush 全部显示"""
        for i in range(5):
            self.lr.append_log.emit(self._html(i))
        self.app.processEvents()
        self.assertEqual(self.browser.toPlainText().strip(), "")
        self.lr._flush_pending()
        text = self.browser.toPlainText()
        for i in range(5):
            self.assertIn(f"第 {i} 行日志", text)


if __name__ == "__main__":
    unittest.main()
