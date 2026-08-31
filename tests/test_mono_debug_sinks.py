"""Mono #US 调试 sink / 结构 sink 证明测试（alisa-demo 多余识别实证）。

alisa-demo 地毯式排查（0.36.x）：Managed/Assembly-CSharp.dll 的 184 条
us# pending 全是被误放行的调试/结构串，三类根因 + 一个验证链增强：
  1) MonoBehaviour.print（旧版 Debug.Log，开发控制台）不在 _LOG_SINKS
     → ~120 条调试消息（'Video has ended.'/'Scene Loaded'）被 F33 句子
     启发式放行（多余识别根因）。
  2) AnimatorStateInfo.IsName / LayerMask.GetMask（按名运行时查找）不在
     _STRUCTURAL_SINKS → 动画状态名/图层名被放行（翻译断动画/物理）。
  3) 输入绑定按键名（JS_ButtonN / 1stAxis± / Arrow X / KeyCode 名）被验证
     为 UI 文本（流入重绑 UI 的 set_text），但翻译后绑定失效。
  4) LayerMask.GetMask(params string[]) 的数组元素（newarr + 多个
     stelem.ref + dup）此前无法证明——补 newarr/stelem 容器证明链。
"""
from __future__ import annotations

import struct
from types import SimpleNamespace

from hanhua.core.unity.mono_dll import extract_dll_user_strings


def _build_fake_pe(heap, bodies, member_refs, method_defs=None):
    class FakeUserStrings:
        def sizeof(self):
            return len(heap)

        def get_data_at_offset(self, offset, size):
            return bytes(heap)

        def get_file_offset(self, offset):
            return 100

    if method_defs is None:
        method_defs = [SimpleNamespace(Rva=rva) for rva in bodies]
    return SimpleNamespace(
        net=SimpleNamespace(
            user_strings=FakeUserStrings(),
            mdtables=SimpleNamespace(
                MemberRef=SimpleNamespace(rows=member_refs),
                MethodDef=SimpleNamespace(rows=method_defs),
            ),
        ),
        get_data=lambda rva, size: bodies.get(rva, b"")[:size],
        close=lambda: None,
    )


def _text_ref(name, ns="System", type_name="String",
              signature=b"\x00\x02\x0e\x0e\x0e"):
    declaring = SimpleNamespace(TypeName=type_name, TypeNamespace=ns)
    return SimpleNamespace(
        Name=name, Class=SimpleNamespace(row=declaring),
        Signature=SimpleNamespace(value=signature))


def _setter_ref():
    declaring = SimpleNamespace(TypeName="TMP_Text", TypeNamespace="TMPro")
    return SimpleNamespace(
        Name="set_text", Class=SimpleNamespace(row=declaring))


def _heap_of(texts):
    heap = bytearray(b"\x00")
    tokens = []
    for text in texts:
        raw = text.encode("utf-16-le") + b"\x01"
        tokens.append(len(heap))
        heap.extend((len(raw),))
        heap.extend(raw)
    return bytes(heap), tokens


def _extract(heap, bodies, member_refs, monkeypatch, tmp_path,
             method_defs=None, dll_name: str = "Assembly-CSharp.dll"):
    import dnfile
    fake_pe = _build_fake_pe(heap, bodies, member_refs, method_defs)
    monkeypatch.setattr(dnfile, "dnPE", lambda _path: fake_pe)
    parsed = extract_dll_user_strings(tmp_path / dll_name)
    return {e.original: e for e in parsed.entries
            if not e.key_path.startswith("skip/")}


def _extract_all(heap, bodies, member_refs, monkeypatch, tmp_path,
                 method_defs=None, dll_name: str = "Assembly-CSharp.dll"):
    """含 skip/ 样本条目的完整视图（input_key_label 等留档在 skip 里）。"""
    import dnfile
    fake_pe = _build_fake_pe(heap, bodies, member_refs, method_defs)
    monkeypatch.setattr(dnfile, "dnPE", lambda _path: fake_pe)
    parsed = extract_dll_user_strings(tmp_path / dll_name)
    return {e.original: e for e in parsed.entries}


_SETTER = 0x0A000001


def _ldstr(token):
    return b"\x72" + struct.pack("<I", 0x70000000 | token)


def _call(token):
    return b"\x28" + struct.pack("<I", token)


def _callvirt(token):
    return b"\x6f" + struct.pack("<I", token)


def _members():
    return [
        _setter_ref(),
        _text_ref("print", ns="UnityEngine", type_name="MonoBehaviour",
                  signature=b"\x00\x01\x01\x0e"),
        _text_ref("IsName", ns="UnityEngine", type_name="AnimatorStateInfo",
                  signature=b"\x00\x01\x01\x0e"),
        _text_ref("GetMask", ns="UnityEngine", type_name="LayerMask",
                  signature=b"\x00\x01\x01\x1d"),
    ]


_PRINT = 0x0A000002
_ISNAME = 0x0A000003
_GETMASK = 0x0A000004


class TestDebugPrintSink:
    """MonoBehaviour.print = 开发控制台日志（Debug.Log 别名）。"""

    def test_print_consumed_stays_skipped(self, tmp_path, monkeypatch):
        # void Update() { print("Scene Loaded"); }
        heap, tokens = _heap_of(["Scene Loaded"])
        code = (_ldstr(tokens[0]) + _call(_PRINT) + b"\x2a")
        bodies = {0x2000: bytes(((len(code) << 2) | 2,)) + code}
        by_original = _extract(heap, bodies, _members(), monkeypatch, tmp_path)
        e = by_original["Scene Loaded"]
        assert e.status == "skipped"
        assert e.meta["reason"] == "mono_diagnostic"

    def test_print_sentence_not_released_by_f33(self, tmp_path, monkeypatch):
        # 即使形态像真实句子（'Video has ended.'），print 消费 = 开发日志
        heap, tokens = _heap_of(["Video has ended."])
        code = (_ldstr(tokens[0]) + _call(_PRINT) + b"\x2a")
        bodies = {0x2000: bytes(((len(code) << 2) | 2,)) + code}
        by_original = _extract(heap, bodies, _members(), monkeypatch, tmp_path)
        assert by_original["Video has ended."].status == "skipped"
        assert by_original["Video has ended."].meta["reason"] == \
            "mono_diagnostic"


class TestAnimStateNameSink:
    """AnimatorStateInfo.IsName(string) = 运行时动画状态名查找。"""

    def test_isname_consumed_proven_structural(self, tmp_path, monkeypatch):
        # if (state.IsName("Run Away")) { ... }
        heap, tokens = _heap_of(["Run Away"])
        # callvirt 接收者（state struct）在其下方，字符串恒为栈顶
        code = (_ldstr(tokens[0]) + _callvirt(_ISNAME) + b"\x2a")
        bodies = {0x2000: bytes(((len(code) << 2) | 2,)) + code}
        by_original = _extract(heap, bodies, _members(), monkeypatch, tmp_path)
        e = by_original["Run Away"]
        assert e.status == "skipped"
        assert e.meta["reason"] == "mono_structural_sink"


class TestLayerMaskParamsSink:
    """LayerMask.GetMask(params string[]) 数组元素证明链。

    C# 编译器产物：newarr → dup → ldc.i4 → ldstr → stelem.ref（逐元素）。
    """

    def _getmask_code(self, elem_tokens):
        code = bytearray()
        code += b"\x16"                       # ldc.i4.0 = 数组长度
        code += b"\x8d" + struct.pack("<I", 0x1D000000)  # newarr string[]
        for i, t in enumerate(elem_tokens):
            code += b"\x25"                   # dup 数组引用
            code += bytes((0x16 + i,)) if i < 4 else b"\x1d"  # ldc.i4 index
            code += _ldstr(t)
            code += b"\xa2"                   # stelem.ref
        code += _call(_GETMASK)
        code += b"\x2a"
        return bytes(code)

    def test_params_array_elements_proven_structural(self, tmp_path,
                                                     monkeypatch):
        heap, tokens = _heap_of(["Ignore Raycast", "Block", "Dialogue"])
        code = self._getmask_code(tokens)
        bodies = {0x2000: bytes(((len(code) << 2) | 2,)) + code}
        by_original = _extract(heap, bodies, _members(), monkeypatch, tmp_path)
        for name in ("Ignore Raycast", "Block", "Dialogue"):
            e = by_original[name]
            assert e.status == "skipped", name
            assert e.meta["reason"] == "mono_structural_sink", name

    def test_params_array_ui_evidence_does_not_override(self, tmp_path,
                                                        monkeypatch):
        # 'Ignore Raycast' 同时流入 set_text 与 GetMask 数组——结构证明
        # 优先（宁漏勿坏，对象名=显示文本实证形态）
        heap, tokens = _heap_of(["Ignore Raycast"])
        code = (_ldstr(tokens[0]) + _callvirt(_SETTER)
                + b"\x16" + b"\x8d" + struct.pack("<I", 0x1D000000)
                + b"\x25" + b"\x16" + _ldstr(tokens[0]) + b"\xa2"
                + _call(_GETMASK) + b"\x2a")
        bodies = {0x2000: bytes(((len(code) << 2) | 2,)) + code}
        by_original = _extract(heap, bodies, _members(), monkeypatch, tmp_path)
        assert by_original["Ignore Raycast"].status == "skipped"
        assert by_original["Ignore Raycast"].meta["reason"] == \
            "mono_structural_sink"


class TestInputKeyNameLabel:
    """输入绑定按键名：虽被证明为 UI 文本，翻译后绑定失效——硬跳过。"""

    def _ui_path_code(self, token):
        # set_text("JS_Button0") —— 直通 UI setter（会验证为 mono_ui_setter）
        return _ldstr(token) + _callvirt(_SETTER) + b"\x2a"

    def test_input_key_label_skipped_despite_ui_proof(self, tmp_path,
                                                      monkeypatch):
        heap, tokens = _heap_of(["JS_Button0", "Left Shift", "Arrow Up"])
        # 单方法体内三条独立 set_text 链——一次提取三个按键名全验证
        code = bytearray()
        for t in tokens:
            code += self._ui_path_code(t)
        bodies = {0x2000: bytes(((len(code) << 2) | 2,)) + code}
        by_original = _extract_all(heap, bodies, _members(),
                                   monkeypatch, tmp_path)
        for name in ("JS_Button0", "Left Shift", "Arrow Up"):
            e = by_original[name]
            assert e.status == "skipped", name
            assert e.key_path.startswith("skip/"), name
            assert e.meta["reason"] == "input_key_label", name

    def test_real_button_text_untouched(self, tmp_path, monkeypatch):
        # 'Start Investigating Door' 是真实 UI 文本（对象名=按钮文本形态），
        # 非按键名——按键名守卫不得误伤
        heap, tokens = _heap_of(["Start Investigating Door"])
        code = self._ui_path_code(tokens[0])
        bodies = {0x2000: bytes(((len(code) << 2) | 2,)) + code}
        by_original = _extract(heap, bodies, _members(), monkeypatch, tmp_path)
        assert by_original["Start Investigating Door"].status == "pending"
        assert by_original["Start Investigating Door"].meta["reason"] == \
            "mono_ui_setter"
