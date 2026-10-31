"""图片+配置复制粘贴（Ctrl+C / Ctrl+V）回归测试。

纯逻辑用例覆盖改名/配置迁移/写入；GUI 用例离屏跑一遍
「选图复制 → 切模式粘贴 → 文件与配置都落地」的完整链路。
"""
import json
import os
import tempfile
import unittest
import unittest.mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from OAT.tools.edit_mode_and_img import (
    _IMAGE_CLIPBOARD,
    _insert_pasted_image,
    _pipeline_task_to_image_paths,
    _prepare_pasted_config,
    _unique_paste_filename,
)


class TestUniquePasteFilename(unittest.TestCase):
    def test_first_paste_uses_copy_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                _unique_paste_filename(tmp, "tiaozhan.png", set()),
                "tiaozhan_copy.png",
            )

    def test_keeps_original_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                _unique_paste_filename(tmp, "tiaozhan.jpg", set()),
                "tiaozhan_copy.jpg",
            )

    def test_no_extension_defaults_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                _unique_paste_filename(tmp, "tiaozhan", set()),
                "tiaozhan_copy.png",
            )

    def test_conflict_with_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a_copy.png"), "wb") as f:
                f.write(b"x")
            self.assertEqual(_unique_paste_filename(tmp, "a.png", set()), "a_copy2.png")

    def test_conflict_with_existing_config_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 目录里没文件，但配置里已有同名键：也不能撞键
            self.assertEqual(
                _unique_paste_filename(tmp, "a.png", {"a_copy", "a_copy2"}),
                "a_copy3.png",
            )

    def test_multiple_pastes_increment(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = set()
            names = []
            for _ in range(3):
                name = _unique_paste_filename(tmp, "a.png", keys)
                names.append(name)
                with open(os.path.join(tmp, name), "wb") as f:
                    f.write(b"x")
                keys.add(os.path.splitext(name)[0])
            self.assertEqual(names, ["a_copy.png", "a_copy2.png", "a_copy3.png"])


class TestPreparePastedConfig(unittest.TestCase):
    def test_path_and_self_next_remapped(self):
        cfg = {"path": "a.png", "next_image": "a", "ocr_target_text": "退治"}
        out = _prepare_pasted_config(cfg, "a", "a_copy", "a_copy.png")
        self.assertEqual(out["path"], "a_copy.png")
        self.assertEqual(out["next_image"], "a_copy")
        self.assertEqual(out["ocr_target_text"], "退治")

    def test_other_next_reference_preserved(self):
        cfg = {"path": "a.png", "next_image": "b"}
        out = _prepare_pasted_config(cfg, "a", "a_copy", "a_copy.png")
        self.assertEqual(out["next_image"], "b")

    def test_original_config_not_mutated(self):
        cfg = {"path": "a.png", "next_image": "a"}
        _prepare_pasted_config(cfg, "a", "a_copy", "a_copy.png")
        self.assertEqual(cfg, {"path": "a.png", "next_image": "a"})

    def test_empty_config_gets_path(self):
        self.assertEqual(
            _prepare_pasted_config({}, "a", "a_copy", "a_copy.png"),
            {"path": "a_copy.png"},
        )


class TestInsertPastedImage(unittest.TestCase):
    def test_writes_image_paths_and_pipeline(self):
        config = {"image_paths": {}, "pipeline": {"tasks": []}}
        cfg = {"path": "a_copy.png", "ocr_enabled": True, "ocr_target_text": "挑战"}
        _insert_pasted_image(config, "a_copy", cfg)
        self.assertEqual(config["image_paths"]["a_copy"], cfg)
        tasks = config["pipeline"]["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "a_copy")
        self.assertEqual(tasks[0]["template"], "a_copy.png")
        self.assertEqual(tasks[0]["ocr_text"], "挑战")

    def test_legacy_config_without_pipeline_stays_legacy(self):
        config = {"image_paths": {}}
        _insert_pasted_image(config, "a_copy", {"path": "a_copy.png"})
        self.assertNotIn("pipeline", config)
        self.assertIn("a_copy", config["image_paths"])

    def test_existing_same_name_task_replaced(self):
        config = {"image_paths": {}, "pipeline": {"tasks": [
            {"name": "a_copy", "template": "old.png"}]}}
        _insert_pasted_image(config, "a_copy", {"path": "a_copy.png"})
        self.assertEqual(len(config["pipeline"]["tasks"]), 1)
        self.assertEqual(config["pipeline"]["tasks"][0]["template"], "a_copy.png")

    def test_malformed_image_paths_raises(self):
        # 配置损坏时宁可不粘贴，也不静默覆盖用户数据
        with self.assertRaises(ValueError):
            _insert_pasted_image({"image_paths": []}, "a", {"path": "a.png"})

    def test_malformed_pipeline_raises(self):
        with self.assertRaises(ValueError):
            _insert_pasted_image(
                {"image_paths": {}, "pipeline": []}, "a", {"path": "a.png"})


class TestPasteRoundtrip(unittest.TestCase):
    def test_pipeline_config_survives_copy_to_another_mode(self):
        """源配置（pipeline 优先）→ 复制 → 改键 → 写入 → 读回，关键字段不丢"""
        source_task = {
            "name": "tiaozhan",
            "template": "tiaozhan.png",
            "click_wait_min": 1.0,
            "click_wait_max": 2.0,
            "is_start": True,
            "recognition": "template+ocr",
            "ocr_text": "挑战",
            "ocr_confidence": 0.9,
            "ocr_region": [10, 20, 30, 40],
            "action": {"type": "click", "target": "fixed_coord",
                       "x_range": [100, 200], "y_range": [200, 400]},
        }
        legacy_overrides = {"message": "开始挑战", "ocr_action": "点击文字所在区域",
                            "ocr_region": [10, 20, 30, 40]}
        # 与编辑器一致的读取顺序：pipeline 转 image_paths，再叠加 legacy 覆盖
        cfg = _pipeline_task_to_image_paths(source_task)
        for k, v in legacy_overrides.items():
            if v:
                cfg[k] = v

        pasted = _prepare_pasted_config(cfg, "tiaozhan", "tiaozhan_copy", "tiaozhan_copy.png")
        config = {"image_paths": {}, "pipeline": {"tasks": []}}
        _insert_pasted_image(config, "tiaozhan_copy", pasted)

        task = config["pipeline"]["tasks"][0]
        self.assertEqual(task["name"], "tiaozhan_copy")
        self.assertEqual(task["template"], "tiaozhan_copy.png")
        self.assertEqual(task["ocr_text"], "挑战")
        self.assertEqual(task["ocr_confidence"], 0.9)
        self.assertEqual(task["click_wait_min"], 1.0)
        self.assertEqual(task["click_wait_max"], 2.0)
        self.assertTrue(task["is_start"])
        self.assertEqual(task["action"]["target"], "fixed_coord")
        self.assertEqual(config["image_paths"]["tiaozhan_copy"]["path"], "tiaozhan_copy.png")


class TestCopyPasteInEditor(unittest.TestCase):
    """离屏 GUI：选中图片 Ctrl+C → 切换模式 Ctrl+V，文件+配置一起落地"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _make_editor(self, tmp):
        from OAT.tools.edit_mode_and_img import ModeEditorDialog
        dlg = ModeEditorDialog()
        # 用临时目录替换真实 source，避免碰到项目里的模式数据
        dlg.source_dir = tmp
        dlg.temp_mode_config = {"甲模式": "mode_a", "乙模式": "mode_b"}
        dlg.load_mode_tree()
        os.makedirs(os.path.join(tmp, "mode_a"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "mode_b"), exist_ok=True)
        return dlg

    def test_copy_then_paste_between_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            dlg = self._make_editor(tmp)
            try:
                # 源模式：一张图 + 完整配置
                with open(os.path.join(tmp, "mode_a", "btn.png"), "wb") as f:
                    f.write(b"PNGDATA")
                source_cfg = {
                    "path": "btn.png", "ocr_enabled": True, "ocr_target_text": "挑战",
                    "ocr_confidence_threshold": 0.85, "click_type": "image",
                    "action_type": "click", "is_challenge_start": True,
                    "next_image": "",
                }
                with open(os.path.join(tmp, "mode_a", "config.json"), "w", encoding="utf-8") as f:
                    json.dump({"image_paths": {"btn": source_cfg},
                               "pipeline": {"tasks": []}}, f, ensure_ascii=False)

                dlg.mode_tree.setCurrentItem(dlg.mode_tree.topLevelItem(0))
                dlg.load_images(["甲模式"])
                dlg.img_list.setCurrentRow(0)

                dlg.copy_selected_image()
                self.assertEqual(_IMAGE_CLIPBOARD["image_name"], "btn.png")
                self.assertEqual(_IMAGE_CLIPBOARD["image_bytes"], b"PNGDATA")
                self.assertEqual(_IMAGE_CLIPBOARD["config"]["ocr_target_text"], "挑战")

                # 目标模式：空目录
                dlg.mode_tree.setCurrentItem(dlg.mode_tree.topLevelItem(1))
                dlg.paste_image()

                dest_img = os.path.join(tmp, "mode_b", "btn_copy.png")
                self.assertTrue(os.path.exists(dest_img))
                with open(dest_img, "rb") as f:
                    self.assertEqual(f.read(), b"PNGDATA")

                with open(os.path.join(tmp, "mode_b", "config.json"), "r", encoding="utf-8") as f:
                    target = json.load(f)
                self.assertIn("btn_copy", target["image_paths"])
                self.assertEqual(target["image_paths"]["btn_copy"]["path"], "btn_copy.png")
                self.assertEqual(target["image_paths"]["btn_copy"]["ocr_target_text"], "挑战")
                self.assertTrue(target["image_paths"]["btn_copy"]["is_challenge_start"])
                # 目标原本有 pipeline（空），粘贴后应同步出任务
                self.assertEqual(len(target["pipeline"]["tasks"]), 1)
                self.assertEqual(target["pipeline"]["tasks"][0]["name"], "btn_copy")

                # 再粘一次 → 自动避让重名
                dlg.paste_image()
                self.assertTrue(os.path.exists(os.path.join(tmp, "mode_b", "btn_copy2.png")))
            finally:
                _IMAGE_CLIPBOARD.update(
                    {"image_name": "", "image_bytes": b"", "config": {},
                     "source_mode_dir": ""})
                dlg.deleteLater()

    def test_paste_without_copy_is_noop(self):
        _IMAGE_CLIPBOARD.update(
            {"image_name": "", "image_bytes": b"", "config": {}, "source_mode_dir": ""})
        with tempfile.TemporaryDirectory() as tmp:
            dlg = self._make_editor(tmp)
            try:
                dlg.mode_tree.setCurrentItem(dlg.mode_tree.topLevelItem(1))
                # 空剪贴板会弹提示框；离屏测试里打桩避免阻塞
                with unittest.mock.patch(
                        "OAT.tools.edit_mode_and_img.QMessageBox.information"):
                    dlg.paste_image()
                self.assertEqual(os.listdir(os.path.join(tmp, "mode_b")), [])
            finally:
                dlg.deleteLater()

    def test_ctrl_c_ctrl_v_shortcuts_are_wired(self):
        """真正的快捷键路径：图列表焦点 Ctrl+C → 模式树焦点 Ctrl+V"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        _IMAGE_CLIPBOARD.update(
            {"image_name": "", "image_bytes": b"", "config": {}, "source_mode_dir": ""})
        with tempfile.TemporaryDirectory() as tmp:
            dlg = self._make_editor(tmp)
            try:
                with open(os.path.join(tmp, "mode_a", "hotkey.png"), "wb") as f:
                    f.write(b"IMG")
                with open(os.path.join(tmp, "mode_a", "config.json"), "w",
                          encoding="utf-8") as f:
                    json.dump({"image_paths": {"hotkey": {
                        "path": "hotkey.png", "ocr_enabled": True,
                        "ocr_target_text": "结算"}},
                        "pipeline": {"tasks": []}}, f, ensure_ascii=False)

                dlg.mode_tree.setCurrentItem(dlg.mode_tree.topLevelItem(0))
                dlg.load_images(["甲模式"])
                dlg.img_list.setCurrentRow(0)
                dlg.show()
                dlg.activateWindow()
                self.app.processEvents()

                QTest.keyClick(dlg.img_list, Qt.Key.Key_C,
                               Qt.KeyboardModifier.ControlModifier)
                self.app.processEvents()
                self.assertEqual(_IMAGE_CLIPBOARD["image_name"], "hotkey.png")

                dlg.mode_tree.setCurrentItem(dlg.mode_tree.topLevelItem(1))
                self.app.processEvents()
                QTest.keyClick(dlg.mode_tree, Qt.Key.Key_V,
                               Qt.KeyboardModifier.ControlModifier)
                self.app.processEvents()

                self.assertTrue(os.path.exists(
                    os.path.join(tmp, "mode_b", "hotkey_copy.png")))
                with open(os.path.join(tmp, "mode_b", "config.json"), "r",
                          encoding="utf-8") as f:
                    target = json.load(f)
                self.assertEqual(
                    target["image_paths"]["hotkey_copy"]["ocr_target_text"], "结算")
            finally:
                _IMAGE_CLIPBOARD.update(
                    {"image_name": "", "image_bytes": b"", "config": {},
                     "source_mode_dir": ""})
                dlg.close()
                dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
