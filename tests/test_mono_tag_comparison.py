"""A11（midnight-maid-night 黑屏根因）：tag/name 比较 IL 模式回归测试。

C# `gameObject.tag == "Ruth"` 编译为 get_tag → ldstr → op_Equality →
brfalse.s——属性 getter 无字符串参数、操作符无 sink 身份，证明链此前
两处都看不见（多词标签/对象名再叠加句子形态放行）→ 逻辑键进池翻译 →
写回后 tag 比较恒 false → 控制器空引用级联 → 开场动画一过即黑屏。

本组用合成 IL + 假 PE 验证：
1. get_tag/get_name 与 ldstr 的 op_Equality/op_Inequality 比较 → 字面量
   确定性结构跳过（mono_structural_sink）；
2. 双角色串（同一字面量另有 set_text 用途）→ 结构证明优先（宁漏勿坏）；
3. 纯 UI 用途（get_tag 不在场）不受影响——精确拦截比较点，不误伤显示
   文本。
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


def _ldstr(token):
    return b"\x72" + struct.pack("<I", 0x70000000 | token)


def _call(token):
    return b"\x28" + struct.pack("<I", token)


def _callvirt(token):
    return b"\x6f" + struct.pack("<I", token)


# MemberRef token 槽位（按 _members() 顺序）
_SETTER = 0x0A000001
_GET_TAG = 0x0A000002
_GET_NAME = 0x0A000003
_OP_EQ = 0x0A000004
_OP_NEQ = 0x0A000005


def _members():
    return [
        _setter_ref(),
        _text_ref("get_tag", ns="UnityEngine", type_name="GameObject",
                  signature=b"\x00\x00\x0e"),
        _text_ref("get_name", ns="UnityEngine", type_name="Object",
                  signature=b"\x00\x00\x0e"),
        _text_ref("op_Equality", signature=b"\x00\x02\x02\x0e\x0e"),
        _text_ref("op_Inequality", signature=b"\x00\x02\x02\x0e\x0e"),
    ]


def _body(code):
    return bytes(((len(code) << 2) | 2,)) + code


# ldarg.0（this）——get_tag/get_name 是实例属性，接收者先入栈
_LDARG0 = b"\x02"


class TestTagNameComparison:
    def test_tag_equality_proven_structural(self, tmp_path, monkeypatch):
        # gameObject.tag == "Ruth"（midnight-maid-night 实证形态）
        heap, tokens = _heap_of(["Ruth"])
        code = (_LDARG0 + _callvirt(_GET_TAG)
                + _ldstr(tokens[0]) + _call(_OP_EQ) + b"\x2a")
        bodies = {0x2000: _body(code)}
        by_original = _extract(heap, bodies, _members(), monkeypatch, tmp_path)
        e = by_original["Ruth"]
        assert e.status == "skipped"
        assert e.meta["reason"] == "mono_structural_sink"

    def test_name_inequality_proven_structural(self, tmp_path, monkeypatch):
        # go.name != "Living Room Door"（协程 MoveNext 实证形态）
        heap, tokens = _heap_of(["Living Room Door"])
        code = (_LDARG0 + _callvirt(_GET_NAME)
                + _ldstr(tokens[0]) + _call(_OP_NEQ) + b"\x2a")
        bodies = {0x2000: _body(code)}
        by_original = _extract(heap, bodies, _members(), monkeypatch, tmp_path)
        assert by_original["Living Room Door"].status == "skipped"
        assert by_original["Living Room Door"].meta["reason"] == \
            "mono_structural_sink"

    def test_operand_order_both_sides(self, tmp_path, monkeypatch):
        # "Ruth" == go.tag（字面量在左侧）——比较无交换律假设
        heap, tokens = _heap_of(["Ruth"])
        code = (_ldstr(tokens[0])
                + _LDARG0 + _callvirt(_GET_TAG)
                + _call(_OP_EQ) + b"\x2a")
        bodies = {0x2000: _body(code)}
        by_original = _extract(heap, bodies, _members(), monkeypatch, tmp_path)
        assert by_original["Ruth"].status == "skipped"

    def test_dual_role_structural_beats_ui(self, tmp_path, monkeypatch):
        # 'Ruth' 同时流入 set_text（协程显示）与 tag 比较（Start 分发）——
        # 结构证明优先（宁漏勿坏，midnight-maid-night 黑屏实证形态）
        heap, tokens = _heap_of(["Ruth"])
        code = (_ldstr(tokens[0]) + _callvirt(_SETTER)   # UI 路径
                + _LDARG0 + _callvirt(_GET_TAG)
                + _ldstr(tokens[0]) + _call(_OP_EQ)      # 比较路径
                + b"\x2a")
        bodies = {0x2000: _body(code)}
        by_original = _extract(heap, bodies, _members(), monkeypatch, tmp_path)
        assert by_original["Ruth"].status == "skipped"
        assert by_original["Ruth"].meta["reason"] == "mono_structural_sink"

    def test_plain_ui_text_not_affected(self, tmp_path, monkeypatch):
        # 无 tag/name getter 在场：同一字面量纯 set_text 用途——
        # 显示证明不受 A11 拦截影响（不误伤）
        heap, tokens = _heap_of(["Naomi arrives"])
        code = (_ldstr(tokens[0]) + _callvirt(_SETTER) + b"\x2a")
        bodies = {0x2000: _body(code)}
        by_original = _extract(heap, bodies, _members(), monkeypatch, tmp_path)
        assert by_original["Naomi arrives"].status == "pending"
        assert by_original["Naomi arrives"].meta["reason"] == "mono_ui_setter"

    def test_comparison_with_non_name_operand_free(self, tmp_path, monkeypatch):
        # ldstr == ldstr（两个字面量互相比较，无 tag/name 值）——
        # 不是逻辑键形态，不触发结构跳过
        heap, tokens = _heap_of(["Alpha phrase", "Beta phrase"])
        code = (_ldstr(tokens[0]) + _ldstr(tokens[1])
                + _call(_OP_EQ) + b"\x2a")
        bodies = {0x2000: _body(code)}
        by_original = _extract(heap, bodies, _members(), monkeypatch, tmp_path)
        # 无 setter 消费 → 未证明路径（句子形态放行），绝不是 mono_structural_sink
        for text in ("Alpha phrase", "Beta phrase"):
            assert by_original[text].meta["reason"] != "mono_structural_sink"
