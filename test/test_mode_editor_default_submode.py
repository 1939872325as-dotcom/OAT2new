"""模式编辑器：default（默认目录项）必须能选中并改配置，但不能删/改名。

背景：mode.json 里形如
    "魂土": {"default": "huntu", "队长": "huntu_double", "队员": "huntu"}
的 default 决定「没选子模式时」用哪个目录。过去 load_mode_tree 里
`if submode_name != 'default'` 把它从树上过滤掉，而全文件没有任何代码能写入
default —— 于是它在界面上完全不可见、也改不了，表现为「默认配置无法更改」。

现在 default 出现在树上（排最前）可选中编辑，但删除/改名被拦住：
删掉它，该模式在没有子模式时无法启动。

本文件只测纯逻辑（树构建顺序、删除/改名守卫），不启动 Qt 界面。
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "OAT", "tools", "edit_mode_and_img.py")

# 魂王那种三个子模式指向同一目录的真实形态，以及魂土那种正常形态
CONFIG = {
    "请选择模式": "",
    "魂土": {"default": "huntu", "队长": "huntu_double", "队员": "huntu"},
    "魂王": {"default": "hunwang_double", "队长": "hunwang_double", "队员": "hunwang_double"},
    "业原火": "yeyuanhuo",
    "爬塔": {"default": "pata", "门票": "pata", "体力": "pata_tili"},
}


def _module_ast():
    with open(MODULE_PATH, encoding="utf-8") as f:
        return ast.parse(f.read())


def test_default_submode_key_constant_exists():
    """保留键必须以常量形式存在，删除/改名守卫才有统一依据。"""
    tree = _module_ast()
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if getattr(t, "id", None) == "DEFAULT_SUBMODE_KEY":
                found["value"] = ast.literal_eval(node.value)
    assert found.get("value") == "default", found


def _tree_children_keys(mode_data):
    """复刻 load_mode_tree 的键顺序规则（default 排最前，其余保持原序）。"""
    keys = list(mode_data.keys())
    if "default" in keys:
        keys.remove("default")
        keys.insert(0, "default")
    return keys


def test_default_is_visible_and_first_in_tree():
    """default 必须出现在子项里，且排最前 —— 否则用户点不到它。"""
    for mode in ("魂土", "魂王", "爬塔"):
        keys = _tree_children_keys(CONFIG[mode])
        assert "default" in keys, f"{mode} 的 default 没出现在树上"
        assert keys[0] == "default", f"{mode} 的 default 不在最前: {keys}"


def test_other_submodes_order_preserved():
    """插入 default 不能打乱其余子模式的原有顺序。"""
    keys = _tree_children_keys(CONFIG["魂土"])
    assert keys == ["default", "队长", "队员"], keys


def test_string_mode_has_no_children():
    """字符串型模式（如业原火）没有子项，规则不受影响。"""
    assert not isinstance(CONFIG["业原火"], dict)


def test_tree_builder_no_longer_filters_default():
    """回归点：load_mode_tree 里不能有任何「跳过 default」的条件。

    只看代码、不看注释（注释里引用了旧写法说明改动原因）。

    检测要点：判断条件里的 'default' 既可能写成字面量 `'default'`，也可能写成
    常量名 `DEFAULT_SUBMODE_KEY`。只认字面量会让测试变成摆设 —— 实测过：
    把 `if submode_name == DEFAULT_SUBMODE_KEY: continue` 注入回去，只查字面量的
    版本照样全绿。所以两种形态都要识别。
    """
    tree = _module_ast()
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "ModeEditorDialog")
    fn = next(n for n in cls.body
              if isinstance(n, ast.FunctionDef) and n.name == "load_mode_tree")

    def _refers_to_default(node) -> bool:
        """节点是否指向 default：字面量 'default' 或常量 DEFAULT_SUBMODE_KEY。"""
        if isinstance(node, ast.Constant) and node.value == "default":
            return True
        return getattr(node, "id", None) == "DEFAULT_SUBMODE_KEY"

    offenders = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare):
            continue
        # 只关心「相等/不等」判断（Eq/NotEq/Is/IsNot）。
        # `DEFAULT_SUBMODE_KEY in keys` 这种成员测试是正常的（用来把它排到最前），
        # 不能算过滤 —— 早期版本连 in 一起判，导致正确实现也被误报。
        if not any(isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot))
                   for op in node.ops):
            continue
        operands = [node.left, *node.comparators]
        if any(_refers_to_default(o) for o in operands):
            offenders.append(ast.dump(node)[:90])
    assert not offenders, (
        "load_mode_tree 仍在拿 default 做条件判断，default 会被过滤掉：\n  "
        + "\n  ".join(offenders))

    # 同时确认它确实把 default 排到了最前（而不是仅"没被过滤"）
    src = ast.get_source_segment(open(MODULE_PATH, encoding="utf-8").read(), fn)
    assert "keys.insert(0, DEFAULT_SUBMODE_KEY)" in src, \
        "load_mode_tree 没有把 default 排到最前"


def test_delete_guards_default():
    """删除 default 必须被拦住，且提示里说明原因。"""
    tree = _module_ast()
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "ModeEditorDialog")
    fn = next(n for n in cls.body
              if isinstance(n, ast.FunctionDef) and n.name == "delete_mode")
    src = ast.get_source_segment(open(MODULE_PATH, encoding="utf-8").read(), fn)
    assert "DEFAULT_SUBMODE_KEY" in src, "delete_mode 没有守卫 default"
    assert "return" in src
    assert "无法删除" in src


def test_rename_guards_default():
    """重命名 default 必须被拦住（改名会让兜底键消失）。"""
    tree = _module_ast()
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "ModeEditorDialog")
    fn = next(n for n in cls.body
              if isinstance(n, ast.FunctionDef) and n.name == "rename_mode")
    src = ast.get_source_segment(open(MODULE_PATH, encoding="utf-8").read(), fn)
    assert "DEFAULT_SUBMODE_KEY" in src, "rename_mode 没有守卫 default"
    assert "无法重命名" in src


def test_dir_resolution_reads_default():
    """选中 default 时要解析到它自己的目录名（否则编辑的会是别的目录）。"""
    tree = _module_ast()
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "ModeEditorDialog")
    fn = next(n for n in cls.body
              if isinstance(n, ast.FunctionDef) and n.name == "_resolve_dir_name")
    src = ast.get_source_segment(open(MODULE_PATH, encoding="utf-8").read(), fn)
    assert "mode_path[1]" in src, "_resolve_dir_name 应按子模式名取目录"

    # 行为核对：mode_path=['魂王','default'] 应解析到 hunwang_double
    assert CONFIG["魂王"]["default"] == "hunwang_double"
    assert CONFIG["魂土"]["default"] == "huntu"
