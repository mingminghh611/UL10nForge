"""B15/B16 回归测试（snowday_汉化 实证 2026-09-05）。

B15（按键失灵根因）：InputActionAsset 的 m_ExpectedControlType
（'Button'）与 m_Groups（'XR'/'Joystick'/'Touch'）被 typetree 路径当
显示文本翻译写回 → Input System 绑定解析失败 → 全部按键失灵。
四层防线：对象级 config 类整跳（提取）+ 字段黑名单（提取）+
writer._IMMUTABLE_FIELD_NAMES（写回 L2）+ logic_audit 字段路径回退
（写回 L3）+ meta.script_class 透传（写回端兜底证据）。

B16（漏提根因）：UTF-16LE TextAsset（双 BOM 头 b'\xef\xbb\xbf\xff\xfe'）
被 textasset_binary 二进制过滤整文件跳过（NUL 占比 ~0.5 > 0.05）→
134 条西班牙语对话全部漏提。修复：UTF-16 BOM 探测先于二进制过滤，
解码进既有格式链；写回侧 _patch_textasset 对称支持并保持原 BOM 形态。
"""
import json

from hanhua.core.unity.extractor import (
    _TYPETREE_IMMUTABLE_FIELD_NAMES, _textasset_entries,
    _typetree_string_entries)
from hanhua.core.unity.logic_audit import typetree_logic_key_evidence
from hanhua.core.unity.writer import (
    WriteResult, _IMMUTABLE_FIELD_NAMES, _patch_textasset)


# ── B15：提取端字段黑名单 ───────────────────────────────────────

def test_b15_immutable_field_names_contain_input_binding():
    """提取端字段黑名单含 m_ExpectedControlType/m_Groups（casefold 化）。"""
    for name in ("m_ExpectedControlType", "m_Groups",
                 "mExpectedControlType", "mGroups"):
        assert name.casefold() in _TYPETREE_IMMUTABLE_FIELD_NAMES


def test_b15_writer_immutable_field_names_contain_input_binding():
    """写回 L2 不可变字段清单含 m_ExpectedControlType/m_Groups。"""
    assert "m_ExpectedControlType" in _IMMUTABLE_FIELD_NAMES
    assert "m_Groups" in _IMMUTABLE_FIELD_NAMES


def test_b15_typetree_input_binding_fields_blocked():
    """InputActionAsset 树：m_ExpectedControlType='Button'、
    m_Groups='XR' 是机器标识 → 不进 display（字段黑名单拦截）。"""
    tree = {
        "m_Name": "NewControls",
        "m_ActionMaps": [{
            "m_Name": "Player",
            "m_Bindings": [
                {"m_Name": "", "m_Action": "Move",
                 "m_Groups": "Gamepad", "m_ExpectedControlType": "Button"},
                {"m_Name": "", "m_Action": "Fire",
                 "m_Groups": "XR", "m_ExpectedControlType": "Button"},
            ],
        }],
    }
    display, candidates = _typetree_string_entries(
        "f", 7, tree, "resources.assets", script_class="InputActionAsset")
    display_texts = {e.original for e in display}
    # 控件类型/组名绝不进 display（B15 核心：'Button'→'按钮' 曾漏网）
    assert "Button" not in display_texts
    assert "XR" not in display_texts
    # meta 透传 script_class（写回端 _config_class_of 兜底证据）
    for e in display:
        assert e.meta["script_class"] == "InputActionAsset"


def test_b15_script_class_stamped_into_meta():
    """script_class 参数透传进条目 meta（含 display 与 candidate）。"""
    tree = {
        "m_Name": "SomeTable",
        "m_Description": "A snow day story.",
        "m_ExtraKey": "some_key_style",
    }
    display, candidates = _typetree_string_entries(
        "f", 7, tree, "t.assets", script_class="DialogueDatabase")
    for e in display + candidates:
        assert e.meta.get("script_class") == "DialogueDatabase"


def test_b15_script_class_default_empty_not_stamped():
    """缺省 script_class=''：不写 meta（与既有行为一致，测试位置调用
    兼容）。"""
    tree = {"m_Description": "A snow day story."}
    display, _ = _typetree_string_entries("f", 7, tree, "t.assets")
    assert "script_class" not in display[0].meta


def test_b15_logic_audit_reverts_input_binding_fields():
    """写回 L3：输入绑定字段路径 → ('revert', 'input_binding_field')。"""
    meta = {"field_path": ["m_ActionMaps", 0, "m_Bindings", 0,
                           "m_ExpectedControlType"]}
    assert typetree_logic_key_evidence(meta, "Button") == \
        ("revert", "input_binding_field")
    meta_groups = {"field_path": ["m_ControlSchemes", 0, "m_Groups"]}
    assert typetree_logic_key_evidence(meta_groups, "Joystick") == \
        ("revert", "input_binding_field")


# ── B16：UTF-16 TextAsset ───────────────────────────────────────

def _utf16le_csv_bytes() -> bytes:
    """snowday DialogueStructure 形态：UTF-8 BOM + UTF-16LE 双 BOM 的
    分号 CSV 对话表。Text 列放中间保证 source_col 命中对话列
    （并列填充数取首个语言列）。"""
    csv_text = (
        "ID;Text;Name\n"
        "1;Hola, ¿cómo estás?;Mika\n"
        "2;Bien, gracias por preguntar.;Mika\n"
        "3;Vamos a jugar en la nieve.;Tomás\n"
    )
    return b"\xef\xbb\xbf" + csv_text.encode("utf-16")


def test_b16_utf16le_csv_extracted_not_binary_skipped():
    """双 BOM UTF-16LE CSV：不再被 textasset_binary 整文件跳过，
    对话行经 csv 分支产出条目。"""
    skipped: dict[str, int] = {}
    display = _textasset_entries(
        "f", 46, _utf16le_csv_bytes(), "resources.assets", skipped)
    assert display, "UTF-16 对话表必须产出条目"
    assert skipped.get("textasset_utf16_detected") == 1
    assert "textasset_binary" not in skipped
    originals = {e.original.strip() for e in display}
    assert any("jugar en la nieve" in t for t in originals)
    # BOM 形态留档（写回侧对称依据）
    assert display[0].meta["textasset_encoding"] == "utf-16-le-bom8"


def test_b16_utf16le_bare_bom_csv_extracted():
    """裸 UTF-16LE BOM（无 UTF-8 BOM 前缀）同样放行。"""
    csv_text = "ID;Text\n1;Hola mundo\n"
    display = _textasset_entries(
        "f", 9, csv_text.encode("utf-16"), "table.assets", None)
    assert display
    assert display[0].meta["textasset_encoding"] == "utf-16-le"


def test_b16_real_binary_still_skipped():
    """对照组：无 BOM 的高 NUL 二进制内容保持既有跳过。"""
    skipped: dict[str, int] = {}
    display = _textasset_entries(
        "f", 10, b"ABC\x00\x01\x02\x03DEF\x00\x04\x05" * 40,
        "bin.assets", skipped)
    assert not display
    assert skipped.get("textasset_binary", 0) >= 1


def _entry_tuple(file_id: str, key_path: str, original: str,
                 translation: str, meta: dict) -> tuple[dict, dict]:
    return ({
        "file_id": file_id, "key_path": key_path,
        "original": original, "translation": translation,
        "status": "translated", "meta": json.dumps(meta),
    }, meta)


def test_b16_utf16_writeback_preserves_bom_form():
    """写回对称：UTF-16 CSV 翻译写回 → 重编码保持原 BOM 形态（双 BOM）。"""
    raw = _utf16le_csv_bytes()
    display = _textasset_entries(
        "f", 46, raw, "resources.assets", None)
    assert display
    items = []
    for e in display:
        if e.original.strip() == "Vamos a jugar en la nieve.":
            items.append(_entry_tuple(
                e.file_id, e.key_path, e.original,
                "Vamos a jugar en la nieve.(中)", e.meta))
    assert items, "需命中至少一条可写条目"
    result = WriteResult()
    # 与真实 writer 调用点同构：textasset_format 条目走 structured_items
    # （writer.py 按行级/结构化分槽，CSV 条目属结构化）
    out = _patch_textasset(raw, [], items, result)
    assert out != raw, "译文必须写回"
    assert out.startswith(b"\xef\xbb\xbf\xff\xfe"), "双 BOM 形态保持"
    # encode("utf-16") 自带 LE BOM 前缀，文件内是裸 LE 载荷 → 用 utf-16-le
    assert "jugar en la nieve.(中)".encode("utf-16-le") in out
    assert not result.rejected


def test_b16_utf16_writeback_no_translation_returns_original():
    """无译文写回：原字节原样返回（含原 BOM）。"""
    raw = _utf16le_csv_bytes()
    result = WriteResult()
    out = _patch_textasset(raw, [], [], result)
    assert out == raw


def test_b16_utf16_be_writeback_preserves_bom():
    """UTF-16BE（裸 BOM）：写回后保持 BE BOM。"""
    csv_text = "ID;Text\n1;Hola mundo\n"
    raw = b"\xfe\xff" + csv_text.encode("utf-16-be")
    display = _textasset_entries("f", 5, raw, "t.assets", None)
    assert display, "UTF-16BE 也必须被提取"
    items = []
    for e in display:
        if e.original.strip() == "Hola mundo":
            items.append(_entry_tuple(
                e.file_id, e.key_path, e.original, "Hola mundo.(中)",
                e.meta))
    assert items
    result = WriteResult()
    out = _patch_textasset(raw, [], items, result)
    assert out.startswith(b"\xfe\xff")
    assert "Hola mundo.(中)".encode("utf-16-be") in out
