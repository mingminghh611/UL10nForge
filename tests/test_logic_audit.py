"""写回逻辑层审计（logic_audit）测试。

覆盖：逻辑敏感形态识别、写回前审计分级（只报告不阻断）、rawstr 扩容
记录、重开逻辑验证（译文存在性 / 序列对齐 / 短译文豁免 / 边界破坏
检出 / 译文缺失检出）。
"""
from __future__ import annotations

from hanhua.core.unity.logic_audit import (
    audit_entries_before_writeback,
    audit_raw_expansion,
    logic_pattern_of,
    snapshot_object_strings,
    verify_logic_layer,
)


def _ser_str(s: str) -> bytes:
    """Unity 序列化字符串：int32 长度头 + UTF-8 内容 + 4 字节对齐零填充。"""
    data = s.encode("utf-8")
    return len(data).to_bytes(4, "little") + data + b"\x00" * (-(4 + len(data)) % 4)


def _align(value: int, boundary: int = 4) -> int:
    return value + (-value % boundary)


def _patch_first(raw: bytearray, translation: str) -> bytearray:
    """模拟 _patch_serialized_string 对 offset=0 字符串的原位替换。"""
    old_length = int.from_bytes(raw[0:4], "little")
    old_end = _align(4 + old_length)
    payload = translation.encode("utf-8")
    new_end = _align(4 + len(payload))
    raw[0:old_end] = (
        len(payload).to_bytes(4, "little")
        + payload
        + b"\x00" * (new_end - 4 - len(payload))
    )
    return raw


# ── 形态识别 ──────────────────────────────────────────────────────

def test_logic_pattern_of_identifies_identifier_shapes():
    assert logic_pattern_of("fieldtrigger") == "lowercode_word"
    assert logic_pattern_of("doPunch") == "camel_case"
    assert logic_pattern_of("enemy_spawner") == "snake_case"
    assert logic_pattern_of("MENU_PLAY") == "uppercase_const"
    assert logic_pattern_of("player2") == "numeric_mix"
    assert logic_pattern_of(
        "UnityEngine.UI.Text, UnityEngine") == "type_descriptor"
    assert logic_pattern_of("Doctor, Doctor") is None   # 对话文本不误报
    assert logic_pattern_of("dEad") == "camel_case"  # 游戏 stylization 大小写
    assert logic_pattern_of("settings") == "lowercode_word"  # 长纯小写词
    assert logic_pattern_of("WASD") is None        # 全大写短词——合法显示文本
    assert logic_pattern_of("Hello World") is None  # 正常句子
    assert logic_pattern_of("点 击") is None
    assert logic_pattern_of("") is None


def test_audit_before_writeback_reports_without_blocking():
    entries = [
        {"key_path": "a", "original": "back", "translation": "返回"},
        {"key_path": "b", "original": "doPunch", "translation": "出拳"},
        {"key_path": "c", "original": "Hello World", "translation": "你好世界"},
        {"key_path": "d", "original": "back", "translation": "back"},  # 回显不审
        {"key_path": "e", "original": "Settings", "translation": "设置"},
    ]
    audit = audit_entries_before_writeback(entries)
    by_loc = {a["locator"]: a for a in audit}
    # 只报告不阻断：正常句子不产生记录，回显跳过
    assert "c" not in by_loc and "d" not in by_loc
    # 代码标识符形态 → warn
    assert by_loc["b"]["pattern"] == "camel_case"
    assert by_loc["b"]["severity"] == "warn"
    # 常见按钮文本短词 → note
    assert by_loc["a"]["pattern"] == "short_code_word"
    assert by_loc["a"]["severity"] == "note"


def test_audit_raw_expansion_records_only_growth():
    entry = {"key_path": "k"}
    growth = audit_raw_expansion(entry, {"obj": 7},
                                 "Start", "开始游戏")
    assert growth is not None
    assert growth["src_bytes"] == 5 and growth["dst_bytes"] == 12
    shrink = audit_raw_expansion(entry, {"obj": 7},
                                 "Settings", "设置")
    assert shrink is None
    same = audit_raw_expansion(entry, {"obj": 7},
                               "Hello", "Hello")
    assert same is None


# ── 快照与重开验证 ────────────────────────────────────────────────

def test_snapshot_object_strings_lists_all_visible():
    raw = _ser_str("Start") + _ser_str("Welcome")
    assert snapshot_object_strings(raw) == ["Start", "Welcome"]


def test_verify_logic_layer_ok_when_translation_expands():
    """中文译文扩容（插入字节后移后续字段）——序列对齐后必须通过。"""
    raw = bytearray(_ser_str("Start") + _ser_str("Welcome"))
    expected = snapshot_object_strings(bytes(raw))
    _patch_first(raw, "开始游戏")          # 5 字节 → 12 字节（扩容）
    ok, problems = verify_logic_layer(
        bytes(raw), expected, {"Start": "开始游戏"})
    assert ok, problems


def test_verify_logic_layer_short_translation_exempt():
    """短译文（2 中文字符 < 扫描 min_len=3）——扫描不可见是预期行为。"""
    raw = bytearray(_ser_str("Settings") + _ser_str("Welcome"))
    expected = snapshot_object_strings(bytes(raw))
    _patch_first(raw, "设置")
    ok, problems = verify_logic_layer(
        bytes(raw), expected, {"Settings": "设置"})
    assert ok, problems


def test_verify_logic_layer_detects_broken_boundary():
    """后续字符串长度头被破坏 → 序列缺失 → 失败（写回必须拒绝）。"""
    raw = bytearray(_ser_str("Settings") + _ser_str("Welcome"))
    expected = snapshot_object_strings(bytes(raw))
    _patch_first(raw, "设置")
    # 破坏第二个字符串的长度头（把边界写坏）
    second_offset = 4 + 6 + 2  # 第一个字段补丁后 12 字节
    raw[second_offset:second_offset + 4] = b"\xff\xff\xff\xff"
    ok, problems = verify_logic_layer(
        bytes(raw), expected, {"Settings": "设置"})
    assert not ok
    assert any("Welcome" in p for p in problems)


def test_verify_logic_layer_detects_missing_translation():
    """译文字节未出现在写回后数据 → 失败。"""
    raw = _ser_str("Settings") + _ser_str("Welcome")
    expected = snapshot_object_strings(raw)
    ok, problems = verify_logic_layer(
        raw, expected, {"Settings": "设置"})   # 没补丁，译文不存在
    assert not ok
    assert any("未出现" in p for p in problems)


# ── 知识库案例转规则（2026-08-11）──

class TestUnityEventRule:
    """案例「UnityEvent 事件绑定断裂按钮无反应」→ 对象信号判定。"""

    def test_object_is_unityevent_detects_signals(self):
        from hanhua.core.unity.logic_audit import object_is_unityevent
        assert object_is_unityevent(["m_PersistentCalls", "SomeMethod"])
        assert object_is_unityevent(["persistentCalls", "OnClick"])
        assert object_is_unityevent(["m_Target", "m_MethodName"])
        assert not object_is_unityevent(["Settings", "Welcome"])
        assert not object_is_unityevent([])


class TestLogicKeyEvidence:
    """统一逻辑键判定（识别层与写回后反向审计共用）。"""

    def test_type_descriptor_reverts(self):
        from hanhua.core.unity.logic_audit import logic_key_evidence
        verdict = logic_key_evidence("System.String, mscorlib", {})
        assert verdict and verdict[0] == "revert"
        assert verdict[1] == "type_descriptor"

    def test_unityevent_object_binding_reverts(self):
        from hanhua.core.unity.logic_audit import logic_key_evidence
        obj_strings = ["m_PersistentCalls", "OnClick", "Play"]
        verdict = logic_key_evidence("OnClick", {}, obj_strings)
        assert verdict and verdict[0] == "revert"
        assert verdict[1] == "unityevent_binding"

    def test_code_object_compare_word_reverts(self):
        from hanhua.core.unity.logic_audit import logic_key_evidence
        # 代码对象（结构跳过身份）中的比较词 = 代码按字符串分发键。
        # W1 回归：权威键是 reason（extractor 分类链写入），旧的
        # structural_reason 已被 pop——必须断言 reason 路径触发。
        verdict = logic_key_evidence(
            "Continue", {"reason": "code_heavy_identifier",
                         "role": "structural"})
        assert verdict and verdict[0] == "revert"
        assert verdict[1].startswith("logic_key_in_code_object")

    def test_input_binding_object_camel_reverts(self):
        from hanhua.core.unity.logic_audit import logic_key_evidence
        verdict = logic_key_evidence(
            "moveForward", {"reason": "input_binding", "role": "structural"})
        assert verdict and verdict[0] == "revert"

    def test_unityevent_object_reason_reverts(self):
        from hanhua.core.unity.logic_audit import logic_key_evidence
        # W1 回归：unityevent_object 分支此前因值域不匹配从不触发
        verdict = logic_key_evidence(
            "OnClick", {"reason": "unityevent_object", "role": "structural"})
        assert verdict and verdict[0] == "revert"
        assert verdict[1] == "unityevent_binding"

    def test_obj_is_key_list_signal_reverts_missed_display(self):
        from hanhua.core.unity.logic_audit import logic_key_evidence
        # W1 补强：识别层漏放（判成 display）但对象是键清单时，写回侧
        # 仍按键处置——防「结构串洗白」裸奔
        verdict = logic_key_evidence(
            "moveForward", {"reason": "natural_language",
                            "role": "display", "obj_is_key_list": True})
        assert verdict and verdict[0] == "revert"
        assert verdict[1].startswith("logic_key_in_code_object")

    def test_identifier_without_context_reports(self):
        from hanhua.core.unity.logic_audit import logic_key_evidence
        verdict = logic_key_evidence("isReady", {})
        assert verdict and verdict[0] == "report"
        assert verdict[1] == "camel_case"

    def test_button_word_reports_but_not_reverts(self):
        from hanhua.core.unity.logic_audit import logic_key_evidence
        # 逻辑比较词无对象上下文：可能是真实按钮文本 → report 不 revert
        verdict = logic_key_evidence("Continue", {})
        assert verdict and verdict[0] == "report"
        assert verdict[1] == "logic_compare_word"

    def test_common_button_word_no_verdict(self):
        from hanhua.core.unity.logic_audit import logic_key_evidence
        # 常见按钮文本白名单（LOGIC_KEYS_COMMON）不触发短词 report；
        # 但 Back 同时是逻辑比较词（LOGIC_COMPARE_WORDS）→ report 复核
        assert logic_key_evidence("Back", {})[1] == "logic_compare_word"
        assert logic_key_evidence("Welcome", {}) is None
        assert logic_key_evidence("", {}) is None


class TestRepeatConsistency:
    """同原文互斥一致性（防「译文+原文」混排断链）。"""

    @staticmethod
    def _entry(original, translation, offset, role=None, reason=None):
        meta = {"obj": 7, "offset": offset}
        if role is not None:
            meta["role"] = role
        if reason is not None:
            meta["reason"] = reason
        return ({"original": original, "translation": translation}, meta)

    def test_structural_skip_reverts_whole_group(self):
        from hanhua.core.unity.logic_audit import audit_repeat_consistency
        items = [
            self._entry("Splash", "画面", 100),                  # 要翻译
            self._entry("Splash", "Splash", 140,
                        role="structural", reason="code_line"),  # 键身份
        ]
        records = audit_repeat_consistency(items)
        assert records and records[0]["action"] == "all_reverted"
        # 翻译条目被改回原文（保留原文防混排）
        assert items[0][0]["translation"] == "Splash"

    def test_inconsistent_translations_revert_whole_group(self):
        from hanhua.core.unity.logic_audit import audit_repeat_consistency
        items = [
            self._entry("Splash", "画面", 100),
            self._entry("Splash", "水花", 140),   # 模型波动：不同译文
        ]
        records = audit_repeat_consistency(items)
        assert records and records[0]["action"] == "all_reverted"
        assert "译文不一致" in records[0]["reason"]

    def test_consistent_group_untouched(self):
        from hanhua.core.unity.logic_audit import audit_repeat_consistency
        items = [
            self._entry("Splash", "画面", 100),
            self._entry("Splash", "画面", 140),
        ]
        assert audit_repeat_consistency(items) == []

    def test_all_reverted_notifies_on_revert_once_per_entry(self):
        """C1：全组回退必须逐条通知 on_revert（写回方记账进排除表）——
        否则保留原文不进 W3 运行时排除表，插件把键身份原文再翻译 → 断链。"""
        from hanhua.core.unity.logic_audit import audit_repeat_consistency
        items = [
            self._entry("Splash", "画面", 100),                  # 要翻译 → 被回退
            self._entry("Splash", "水花", 120),                  # 译文不一致 → 被回退
            self._entry("Splash", "Splash", 140,
                        role="structural", reason="code_line"),  # 键身份（原文→原文不通知）
        ]
        notified: list[tuple[str, str]] = []
        audit_repeat_consistency(
            items, on_revert=lambda e, r: notified.append((e["original"], r)))
        assert len(notified) == 2                       # 只通知实际被回退的条目
        assert all(orig == "Splash" for orig, _ in notified)
        assert all("consistency" in r for _, r in notified)


class TestStringLengthHeaders:
    """译文长度头自证（扩容插入后长度头同步检查）。"""

    def test_correct_header_passes(self):
        from hanhua.core.unity.logic_audit import verify_string_length_headers
        raw = bytearray(_ser_str("Settings"))
        payload = "设置".encode("utf-8")
        raw[0:4] = (len(payload)).to_bytes(4, "little")  # 长度头同步
        raw[4:4 + len(payload)] = payload
        assert verify_string_length_headers(bytes(raw), {"Settings": "设置"}) == []

    def test_stale_header_detected(self):
        from hanhua.core.unity.logic_audit import verify_string_length_headers
        raw = bytearray(_ser_str("Settings"))
        payload = "设置".encode("utf-8")
        raw[4:10] = payload
        # 长度头还是旧值 8（未同步）
        raw[0:4] = (8).to_bytes(4, "little")
        problems = verify_string_length_headers(
            bytes(raw), {"Settings": "设置"})
        assert problems and any("长度头" in p for p in problems)


class TestTypetreeLogicKeyEvidence:
    """W2：typetree 分支反向语义审计（字段路径信号 + 值形态）。"""

    def _meta(self, field_path, **extra):
        meta = {"obj": 9, "field_path": field_path}
        meta.update(extra)
        return meta

    def test_unityevent_method_name_field_path_reverts(self):
        from hanhua.core.unity.logic_audit import typetree_logic_key_evidence
        # m_MethodName 不在 _IMMUTABLE_FIELD_NAMES（W2 缺口），但字段路径
        # 信号必须拦截——反射按名绑定，翻译即断绑（按钮无反应）。
        verdict = typetree_logic_key_evidence(
            self._meta(["m_OnClick", "m_PersistentCalls", "m_Calls",
                        0, "m_MethodName"]), "OnClick")
        assert verdict and verdict[0] == "revert"
        assert verdict[1] == "unityevent_binding"

    def test_target_assembly_type_name_reverts(self):
        from hanhua.core.unity.logic_audit import typetree_logic_key_evidence
        # m_Target 下的 m_TargetAssemblyTypeName：类型引用字符串（UnityEvent
        # 持久化监听器的程序集限定类型名）——save_typetree 依赖。
        verdict = typetree_logic_key_evidence(
            self._meta(["m_OnClick", "m_PersistentCalls", "m_Calls",
                        0, "m_TargetAssemblyTypeName"]),
            "UnityEngine.UI.Button, UnityEngine.UI")
        assert verdict and verdict[0] == "revert"

    def test_type_descriptor_value_reverts(self):
        from hanhua.core.unity.logic_audit import typetree_logic_key_evidence
        # 类型描述符值经 typetree 字段写入——save_typetree 反序列化依赖，
        # 翻译即 Referenced type not found。
        verdict = typetree_logic_key_evidence(
            self._meta(["m_Type"]), "System.Boolean, mscorlib")
        assert verdict and verdict[0] == "revert"
        assert verdict[1] == "type_descriptor"

    def test_key_env_code_shape_reverts(self):
        from hanhua.core.unity.logic_audit import typetree_logic_key_evidence
        verdict = typetree_logic_key_evidence(
            self._meta(["m_Property"], obj_is_key_list=True), "moveForward")
        assert verdict and verdict[0] == "revert"
        assert verdict[1].startswith("logic_key_in_code_object")

    def test_camel_without_context_reports(self):
        from hanhua.core.unity.logic_audit import typetree_logic_key_evidence
        verdict = typetree_logic_key_evidence(
            self._meta(["m_Text"]), "isReady")
        assert verdict and verdict[0] == "report"
        assert verdict[1] == "camel_case"

    def test_display_text_passes(self):
        from hanhua.core.unity.logic_audit import typetree_logic_key_evidence
        assert typetree_logic_key_evidence(
            self._meta(["m_Text"]), "Welcome to the game") is None
        assert typetree_logic_key_evidence(self._meta([]), "") is None


class TestWritebackAuditSeverityLink:
    """W4：warn 级形态 + 键环境 → 审计报告如实标注 revert（联动跳过）。"""

    def test_warn_shape_in_key_env_marks_revert(self):
        from hanhua.core.unity.logic_audit import audit_entries_before_writeback
        records = audit_entries_before_writeback([{
            "key_path": "obj/field",
            "original": "moveForward",
            "translation": "前进",
            "meta": '{"reason": "code_line", "obj_is_key_list": true}',
        }])
        assert records and records[0]["severity"] == "revert"

    def test_warn_shape_in_plain_object_stays_warn(self):
        from hanhua.core.unity.logic_audit import audit_entries_before_writeback
        records = audit_entries_before_writeback([{
            "key_path": "obj/field",
            "original": "isReady",
            "translation": "就绪",
            "meta": '{"reason": "natural_language"}',
        }])
        assert records and records[0]["severity"] == "warn"

    def test_note_shape_never_marks_revert(self):
        from hanhua.core.unity.logic_audit import audit_entries_before_writeback
        records = audit_entries_before_writeback([{
            "key_path": "obj/field",
            "original": "continue",
            "translation": "继续",
            "meta": '{"obj_is_key_list": true}',
        }])
        assert records and records[0]["severity"] == "note"

    def test_display_word_shared_with_name_reverts(self):
        """2026-08-15 多游戏实证「写回后按键 UI 失灵」：code_heavy 对象
        里放行的白名单显示词 + 对象内同值 ≥2 处（m_Name 与 m_text 同值）
        → 写回侧确定性回退保留原文，防改对象名断查找。"""
        from hanhua.core.unity.logic_audit import logic_key_evidence
        verdict = logic_key_evidence(
            "exit", {"reason": "code_heavy_display_word",
                     "role": "display"},
            obj_strings=["exit", "exit", "Normal", "Pressed"])
        assert verdict and verdict[0] == "revert"
        assert verdict[1] == "display_word_shared_with_object_name"

    def test_display_word_single_occurrence_not_reverted(self):
        """单次出现的白名单词（静态按钮文本 hotel-paradise 场景）不回退：
        至多 report 级（照常写入 + 审计留档），绝不 revert。"""
        from hanhua.core.unity.logic_audit import logic_key_evidence
        verdict = logic_key_evidence(
            "save", {"reason": "code_heavy_display_word",
                     "role": "display"},
            obj_strings=["save", "Normal", "Pressed"])
        assert verdict is None or verdict[0] == "report"

    def test_shared_word_consistency_reverts_group(self):
        """提取层 object_name_shared_word（structural）+ 同对象同原文组内
        有翻译条目 → audit_repeat_consistency 全组回退（译文+原文混排
        防断链，写回侧兜底联动）。"""
        from hanhua.core.unity.logic_audit import audit_repeat_consistency
        items = [
            ({"original": "Exit", "translation": "Exit", "status": "skipped",
              "key_path": "str/0"},
             {"obj": 7, "role": "structural",
              "reason": "object_name_shared_word"}),
            ({"original": "Exit", "translation": "退出", "status": "translated",
              "key_path": "str/1"},
             {"obj": 7, "role": "display", "reason": "display_phrase"}),
        ]
        reverted: list[dict] = []
        records = audit_repeat_consistency(
            items, on_revert=lambda e, r: reverted.append((e, r)))
        # 全组保留原文：翻译条目被回退
        assert any(e["key_path"] == "str/1" and e["translation"] == "Exit"
                   for e, _ in reverted)
        assert records


# ── give-me-strength 按键失灵写回侧兜底（2026-08-29）──
# 提取层已把 UnityEvent 回调对象/InputManager 轴配置/FMOD 路径确定性跳过；
# 写回侧按同一规则兜底——旧库残留（本次修复前已翻译的坏条目）或识别层
# 漏判的 locator，写回时确定性回退译文（保留原文，不写补丁防断链）。

def test_meta_unityevent_signal_reverts():
    """meta 显式 obj_is_unityevent（事件绑定对象）→ 写回确定性回退。"""
    from hanhua.core.unity.logic_audit import logic_key_evidence
    verdict = logic_key_evidence(
        "Play", {"obj_is_unityevent": True, "reason": "code_heavy_display_word",
                 "role": "display"},
        obj_strings=["Play", "Normal", "UnityEngine.Object, UnityEngine"])
    assert verdict and verdict[0] == "revert"
    assert verdict[1] == "unityevent_binding"


def test_meta_input_axis_signal_reverts():
    """meta 显式 obj_is_input_axis（InputManager/Cinemachine 轴配置）→
    写回确定性回退（give-me-strength：Mouse X 相机轴断链兜底）。"""
    from hanhua.core.unity.logic_audit import logic_key_evidence
    verdict = logic_key_evidence(
        "Mouse X", {"obj_is_input_axis": True, "role": "display"},
        obj_strings=["Mouse X"])
    assert verdict and verdict[0] == "revert"
    assert verdict[1] == "input_axis_binding"


def test_fmod_event_path_reverts():
    """FMOD event:/ 路径 → 写回确定性回退（旧库残留的「事件：/音乐/…」
    坏译文回退，RuntimeManager 按名查找防断链）。"""
    from hanhua.core.unity.logic_audit import logic_key_evidence
    verdict = logic_key_evidence(
        "event:/Music/GlobalMusic", {"role": "display"},
        obj_strings=["event:/Music/GlobalMusic"])
    assert verdict and verdict[0] == "revert"
    assert verdict[1] == "fmod_event_path"
    # 大写变体同样拦截
    upper = logic_key_evidence(
        "EVENT:/Music/Menu/StartSelection", {"role": "display"}, None)
    assert upper and upper[0] == "revert"


def test_typetree_unityevent_and_fmod_revert():
    """typetree 分支：obj_is_unityevent / obj_is_input_axis / event:/ 兜底
    与 rawstr 分支同规则。"""
    from hanhua.core.unity.logic_audit import typetree_logic_key_evidence
    assert typetree_logic_key_evidence(
        {"obj_is_unityevent": True}, "Play")[0] == "revert"
    assert typetree_logic_key_evidence(
        {"obj_is_input_axis": True}, "Mouse X")[0] == "revert"
    assert typetree_logic_key_evidence(
        {}, "event:/Bank/Music")[0] == "revert"
