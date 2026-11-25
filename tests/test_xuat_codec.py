"""Tests for XUAT dictionary codec and round-trip gate."""
from pathlib import Path

from ul10n.core.models import Entry, STATUS_EDITED, STATUS_PENDING, STATUS_TRANSLATED
from ul10n.core.xuat_codec import (
    decode_xuat_line,
    encode_xuat_entry,
    export_xuat_file,
    parse_xuat_file,
)


def test_encode_decode_roundtrip_special_chars():
    cases = [
        ("New Game", "新游戏"),
        ("HP = 100\nMP = 50", "生命 = 100\n法力 = 50"),
        ("Path\\to\\file", "路径\\到\\文件"),
        ("<color=#ff0>Warning!</color>", "<color=#ff0>警告！</color>"),
    ]
    for orig, tr in cases:
        line = encode_xuat_entry(orig, tr)
        assert line is not None
        assert decode_xuat_line(line) == (orig, tr)


def test_lossy_patterns_blocked_by_roundtrip_gate():
    # '//' and '%3D' cannot round-trip cleanly in XUAT's TextHelper
    assert encode_xuat_entry("Visit https://example.com", "访问 https://example.com") is None
    assert encode_xuat_entry("a%3Db", "a%3Db") is None
    assert encode_xuat_entry("r:^Score: (.*)$", "分数：$1") is None


def test_export_and_parse_utf8_no_bom(tmp_path: Path):
    entries = [
        Entry(1, "Start", "开始", status=STATUS_TRANSLATED),
        Entry(2, "Save\nGame", "保存\n游戏", status=STATUS_EDITED),
        Entry(3, "Pending text", "", status=STATUS_PENDING),
        Entry(4, "http://lossy", "http://lossy", status=STATUS_TRANSLATED),
    ]
    out = tmp_path / "00_UL10nForge.txt"
    stats = export_xuat_file(entries, out)
    assert stats.exported == 2
    assert stats.skipped_lossy == 1

    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # Must be UTF-8 without BOM

    parsed = parse_xuat_file(out)
    assert parsed == [("Start", "开始"), ("Save\nGame", "保存\n游戏")]
