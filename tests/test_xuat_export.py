# -*- coding: utf-8 -*-
"""XUAT 词典导出回归（0.50.0 A14）。

encode_xuat_line_value 是 XUnity.AutoTranslator 5.6.1
Utilities/TextHelper.cs EscapeNewlines 的 Python 等价实现；
xuat_decode 是 ReadTranslationLineAndDecode 的移植——用权威解码
语义验证导出文件逐行可被 XUAT 正确读回（round-trip 锁定）。
"""
from pathlib import Path

from hanhua.core.xuat_export import (
    XUAT_SKIPPED_PREFIXES, encode_xuat_entry, encode_xuat_line_value,
    export_xuat_translations, xuat_decode)


# ── XUAT 解码侧权威移植行为锁定（ReadTranslationLineAndDecode）──

def test_decode_reference_semantics():
    # 基本分隔
    assert xuat_decode("a=b") == ("a", "b")
    # 两个未转义 = → 整行无效
    assert xuat_decode("a=b=c") is None
    # 转义 = 不计入分隔符
    assert xuat_decode("a\\=b=c") == ("a=b", "c")
    # \n \r \\ 还原
    assert xuat_decode("a\\nb=c\\rd") == ("a\nb", "c\rd")
    # \uXXXX 还原
    assert xuat_decode("\\u4f60=你") == ("你", "你")
    # 未知转义 \/ 不还原（有损根源——XUAT 源码 default 分支保留 \x）
    assert xuat_decode("a\\/b=c") == ("a\\/b", "c")
    # %3D 旧式转义 → =
    assert xuat_decode("a%3Db=c") == ("a=b", "c")
    # // 注释：键段 → None；值段 → 截断
    assert xuat_decode("// c=v") is None
    assert xuat_decode("k=va// lue") == ("k", "va")
    # 空行 / 无分隔符 / 键段截断
    assert xuat_decode("") is None
    assert xuat_decode("novalsep") is None


# ── Encode 转义规则（对照 EscapeNewlines 逐字符）──

def test_encode_escapes_all_special_chars():
    assert encode_xuat_line_value("a=b") == "a\\=b"
    assert encode_xuat_line_value("a\\b") == "a\\\\b"
    assert encode_xuat_line_value("a\nb") == "a\\nb"
    assert encode_xuat_line_value("a\rb") == "a\\rb"
    # // 两个字符都转义（\/\/）——防行内注释
    assert encode_xuat_line_value("a//b") == "a\\/\\/b"
    # 单根 / 不转义（解码侧只认 //）
    assert encode_xuat_line_value("a/b") == "a/b"
    # 混合形态
    assert (encode_xuat_line_value("x=y//z\\w\n")
            == "x\\=y\\/\\/z\\\\w\\n")


def test_encode_empty_and_plain_forms():
    assert encode_xuat_line_value("") == ""
    assert encode_xuat_line_value("plain") == "plain"
    # CJK / 全角字符原样通过
    assert encode_xuat_line_value("你好=世界") == "你好\\=世界"


def test_encode_entry_single_separator():
    line = encode_xuat_entry("key=1", "val=2")
    assert line.count("=") - line.count("\\=") == 1
    assert xuat_decode(line) == ("key=1", "val=2")


# ── Round-trip：无恙文本经 Encode 后 XUAT 解码逐字符还原 ──

def test_roundtrip_against_xuat_decode_semantics():
    samples = [
        "Hello, world!",
        "He said \"hi\" = true",
        "line1\nline2",
        "path\\to\\file = C:\\Games",
        "http:/single-slash.com",
        "你好，世界＝全角等号",   # 全角＝不是分隔符，必须原样
        "100% sure %20 percent",
        "trailing spaces   ",
        "   leading spaces",
        "tab\there",
        "mixed\n=\\ http:/a",
    ]
    for text in samples:
        line = encode_xuat_entry(text, "译=" + text)
        decoded = xuat_decode(line)
        assert decoded is not None, f"round-trip 失效（整行无效）：{text!r}"
        assert decoded[0] == text, f"键还原失败：{text!r} → {decoded[0]!r}"
        assert decoded[1] == "译=" + text


def test_roundtrip_lossy_forms_are_known():
    r"""有损形态实证（XUAT 格式限制，非实现缺陷）：
    // 转义后解码不还原（\/ 保留）；%3D 被解码改写成 =。"""
    line = encode_xuat_entry("http://a//b", "值")
    assert xuat_decode(line) == ("http:\\/\\/a\\/\\/b", "值")
    line = encode_xuat_entry("a%3Db", "值")
    assert xuat_decode(line) == ("a=b", "值")


# ── 导出：文件布局 / 内容 / 过滤 ──

class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def get_entries(self, status=None):
        if status == "translated":
            return [r for r in self._rows if r["status"] == "translated"]
        return list(self._rows)


class _FakeProfile:
    game_name = "测试游戏: Demo"


class _FakeProject:
    def __init__(self, rows):
        self.store = _FakeStore(rows)
        self.profile = _FakeProfile()


def _entry(key, value, status="translated"):
    return {"original": key, "translation": value, "status": status,
            "file_id": "f", "key_path": "k"}


def _dict_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").split("\n")
            if ln and not ln.startswith("//")]


def test_export_layout_and_content(tmp_path: Path):
    rows = [
        _entry("Start Game", "开始游戏"),
        _entry("Options", "选项"),
        _entry("a=b\\c", "甲=乙"),
    ]
    path = export_xuat_translations(_FakeProject(rows), tmp_path, lang="zh")
    assert path is not None
    assert path == (tmp_path / "Translation" / "zh" / "Text"
                    / "00_测试游戏_ Demo.txt")
    text = path.read_text(encoding="utf-8")
    # UTF-8 无 BOM（XUAT StreamReader UTF8 会把 BOM 吞进首行键）
    assert not text.startswith("﻿")
    lines = text.split("\n")
    # 头部注释块 + 词典行
    assert all(ln.startswith("//") for ln in lines[:5])
    assert "Start Game=开始游戏" in lines
    assert "a\\=b\\\\c=甲\\=乙" in lines
    # 每个词典行 round-trip 可解码
    for ln in _dict_lines(path):
        key, value = xuat_decode(ln)
        assert key and value


def test_export_lossy_gate_skips_and_reports(tmp_path: Path):
    """round-trip 闸门：// 与 %3D 文本跳过导出 + 文件头计数透明化。"""
    rows = [
        _entry("OK", "好"),
        _entry("http://a//b", "网址"),       # // 有损 → 跳过
        _entry("a%3Db", "百分号"),           # %3D 有损 → 跳过
        _entry("译值//含注释", "值"),        # 译文侧 // 有损 → 跳过
    ]
    path = export_xuat_translations(_FakeProject(rows), tmp_path)
    assert path is not None
    assert _dict_lines(path) == ["OK=好"]
    head = path.read_text(encoding="utf-8")
    assert "3 条" in head and "无法无损表示" in head


def test_export_filters_and_skips(tmp_path: Path):
    rows = [
        _entry("OK", "好"),                       # 正常
        _entry("NoTrans", ""),                    # 空译文 → 跳过
        _entry("Pending", "待", status="pending"),  # 非 translated → 跳过
        _entry("Failed", "败", status="failed"),    # failed → 跳过
        _entry("r:regex.*key", "正则键"),          # r: 前缀 → 跳过
        _entry("sr:split.*key", "拆分键"),         # sr: 前缀 → 跳过
        _entry("  ", "空键"),                      # 空白键 → 跳过
    ]
    path = export_xuat_translations(_FakeProject(rows), tmp_path)
    assert path is not None
    assert _dict_lines(path) == ["OK=好"]
    assert set(XUAT_SKIPPED_PREFIXES) == {"r:", "sr:"}


def test_export_dedupes_same_original(tmp_path: Path):
    """同原文多条（多场景同文本）：首见优先，一行一个键。"""
    rows = [_entry("Same", "首见"), _entry("Same", "后见")]
    path = export_xuat_translations(_FakeProject(rows), tmp_path)
    assert _dict_lines(path) == ["Same=首见"]


def test_export_no_translated_entries_returns_none(tmp_path: Path):
    rows = [_entry("x", "y", status="skipped")]
    assert export_xuat_translations(_FakeProject(rows), tmp_path) is None
    # store 缺失（未初始化 Project）→ None 不抛
    assert export_xuat_translations(object(), tmp_path) is None
    # 全部有损跳过 → 无可导出条目 → None
    rows = [_entry("a//b", "c")]
    assert export_xuat_translations(_FakeProject(rows), tmp_path) is None


def test_export_filename_override_and_prefix_priority(tmp_path: Path):
    rows = [_entry("A", "甲")]
    # 00_ 前缀语义：XUAT 按 FullName 降序加载、后加载覆盖 → 字典序
    # 最小文件优先级最高（压过 _AutoGeneratedTranslations.txt）
    path = export_xuat_translations(_FakeProject(rows), tmp_path,
                                    filename="00_manual.txt")
    assert path.name == "00_manual.txt"
    # 前缀可自定义但默认 00_
    path2 = export_xuat_translations(_FakeProject(rows), tmp_path / "b",
                                     file_prefix="01_")
    assert path2.name.startswith("01_")


def test_export_multiline_text_roundtrip(tmp_path: Path):
    """多行原文（\n 分隔对话）经转义单行存储，解码还原多行。"""
    rows = [_entry("line one\nline two", "第一行\n第二行")]
    path = export_xuat_translations(_FakeProject(rows), tmp_path)
    lines = _dict_lines(path)
    assert len(lines) == 1
    key, value = xuat_decode(lines[0])
    assert key == "line one\nline two"
    assert value == "第一行\n第二行"
