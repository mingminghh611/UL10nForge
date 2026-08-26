"""rawstr 提取器排除 m_Name（对象标识名）回归测试。

Rendezvous 2026-08-18 实证：rawstr 二进制扫描把 MonoBehaviour 的
m_Name（对象标识名，Inspector 标签/Find 查找键）当文本提取翻译，
游戏代码按原名查找断链 → 过场流程空指针崩溃。修复：_raw_string_entries
按 MonoBehaviour 固定布局（m_Name 长度头 @28）定位并排除 m_Name 跨度。
"""
import struct

from hanhua.core.unity.extractor import (
    _mono_object_name_span,
    _raw_string_entries,
)


def _fake_mono(name: str, payload: bytes = b"\x00" * 16) -> bytes:
    """构造最小 MonoBehaviour 序列化：头部 + m_Name + 后续字段。"""
    head = struct.pack("<i", 0) + struct.pack("<q", 0)   # m_GameObject PPtr(12)
    head += struct.pack("<i", 1)         # m_Enabled
    head += struct.pack("<i", 0)         # m_Script fileID
    head += struct.pack("<q", 2000)      # m_Script pathID (TMP)
    name_b = name.encode("utf-8")
    head += struct.pack("<i", len(name_b)) + name_b
    head += b"\x00" * 4                  # 对齐
    return head + payload


def test_mono_object_name_span_detects_name():
    raw = _fake_mono("SceneLabel_Start")
    span = _mono_object_name_span(raw)
    assert span is not None
    start, end = span
    # 长度头在 28，内容在 32..32+len
    assert start == 28
    assert end == 32 + len("SceneLabel_Start")


def test_mono_object_name_span_rejects_binary():
    raw = b"\x00" * 40  # 没有合法名字
    assert _mono_object_name_span(raw) is None


def test_mono_object_name_span_rejects_nonprintable():
    raw = bytearray(_fake_mono("ok"))
    raw[32:36] = b"\xff\xfe\x00\x01"  # 内容非 UTF-8 可打印
    assert _mono_object_name_span(bytes(raw)) is None


def test_raw_string_entries_excludes_mname():
    """m_Name 字符串不得进入翻译池（Rendezvous 崩溃根因回归）。"""
    raw = _fake_mono(
        "Northern Sea",
        # payload 里放一个真正的显示文本（m_Text 区域）
        struct.pack("<i", 12) + b"Hello world!".ljust(12, b"\x00"),
    )
    entries = _raw_string_entries("test#1", 5, raw, {})
    texts = [e.original for e in entries]
    assert "Northern Sea" not in texts
    assert any("Hello world!" in t for t in texts)


def test_raw_string_entries_still_extracts_display_text():
    """非 m_Name 的显示文本仍正常提取（排除不越界）。

    注意：孤立单字符串对象会被对象级过滤跳过（工具既有设计——
    无值特征/键证据时不提取，防误译配置类对象）。本测试用带
    显示证据的多字符串对象验证 m_Name 排除不影响其他字符串。
    """
    texts = ["Press E to open the door.", "Talk to Setyo", "Inspect"]
    payload = b""
    for t in texts:
        tb = t.encode("utf-8")
        payload += struct.pack("<i", len(tb)) + tb + b"\x00" * 4
    raw = _fake_mono("SomeObject", payload)
    entries = _raw_string_entries("test#1", 5, raw, {})
    texts_found = [e.original for e in entries if e.status != "skipped"]
    # 至少一个显示文本进入待翻译池，且 m_Name 绝不在内
    assert texts_found, "display texts should be extracted"
    assert "SomeObject" not in [e.original for e in entries]


# ── 写回侧 m_Name 兜底（2026-08-26 任务三补漏） ─────────────────────
# 提取器已排除 m_Name；写回侧 _patch_asset 再按同一布局兜底，防提取器
# 漏判/旧库残留的定位器仍指向 m_Name 跨度。MonoBehaviour 固定布局：
# m_GameObject PPtr(12) + m_Enabled(4) + m_Script PPtr(8) + 长度头(4 @28)
# + 内容(内容 @32) + 对齐零 + 后续字段（含显示文本）。
import json
import tempfile
from pathlib import Path

import UnityPy

from hanhua.core.unity.extractor import _mono_object_name_span
from hanhua.core.unity.writer import WriteResult, _patch_asset


def _fake_mono_with_display(mname: str, display: str) -> tuple[bytes, int, int]:
    """构造 MonoBehaviour：m_Name 内容 @32，紧随一个显示文本（内容偏移返回）。

    返回 (raw, mname_content_offset, display_content_offset)。"""
    head = struct.pack("<i", 0) + struct.pack("<q", 0)      # m_GameObject @0..11
    head += struct.pack("<i", 1)                             # m_Enabled @12
    head += struct.pack("<i", 0) + struct.pack("<q", 2000)   # m_Script @16..27
    nb = mname.encode()
    head += struct.pack("<i", len(nb)) + nb                  # 长度头@28, 内容@32
    head += b"\x00" * ((4 - (len(head) % 4)) % 4)
    mname_off = 32
    db = display.encode()
    disp_off = len(head) + 4
    head += struct.pack("<i", len(db)) + db
    head += b"\x00" * ((4 - (len(head) % 4)) % 4)
    return head, mname_off, disp_off


class _FakeMonoObject:
    def __init__(self, raw, assets_name):
        self.path_id = 7
        self.assets_file = type("AF", (), {"name": assets_name})()
        self.type = type("OT", (), {"name": "MonoBehaviour"})()
        self.raw = raw

    def get_raw_data(self):
        return self.raw

    def set_raw_data(self, r):
        self.raw = bytes(r)


class SerializedFile:
    reader = None

    def __init__(self, environment):
        self.environment = environment

    def save(self):
        return self.environment.objects[0].get_raw_data()


class _FakeEnv:
    def __init__(self):
        self.objects, self.files = [], {}

    def load(self, paths):
        self.objects = [_FakeMonoObject(Path(paths[0]).read_bytes(), "g.assets")]
        self.files = {"main": SerializedFile(self)}


def _write_patch(tmp_path, raw, entry, assets_name="g.assets"):
    import UnityPy
    UnityPy.Environment = _FakeEnv
    path = tmp_path / assets_name
    path.write_bytes(raw)
    result = WriteResult()
    _patch_asset(path, [entry], result)
    return path, result


def _rawstr_entry(key_path, original, translation, offset, assets_name="g.assets"):
    return {
        "file_id": "fixture",
        "key_path": key_path,
        "original": original,
        "translation": translation,
        "meta": json.dumps({
            "kind": "rawstr", "asset_file": assets_name, "obj": 7,
            "offset": offset, "obj_has_values": True,
            "role": "display", "disposition": "translate",
        }),
    }


def test_writer_side_reverts_rawstr_pointing_at_mname(tmp_path):
    """写回侧兜底：定位器指向 m_Name 跨度 → 回退保留原文（对象名不译）。"""
    raw, mname_off, disp_off = _fake_mono_with_display("Start", "Start")
    assert _mono_object_name_span(raw) == (28, 32 + len("Start"))
    path, result = _write_patch(
        tmp_path, raw,
        _rawstr_entry("asset#g.assets#7/str/0", "Start", "开始", mname_off))
    assert result.entries == 0
    assert result.logic_reverted == 1
    assert "rawstr_object_mname" in result.logic_reverted_items[0]
    assert path.read_bytes()[mname_off:mname_off + 5] == b"Start"  # 原文保留


def test_writer_side_display_text_outside_mname_still_writes(tmp_path):
    """兜底不越界：m_Name 之后的真实显示文本仍正常写回。"""
    raw, mname_off, disp_off = _fake_mono_with_display("Start", "Start")
    path, result = _write_patch(
        tmp_path, raw,
        _rawstr_entry("asset#g.assets#7/str/1", "Start", "开始", disp_off))
    assert result.entries == 1
    assert path.read_bytes()[disp_off:disp_off + len("开始".encode())] == "开始".encode()
