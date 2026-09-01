"""extractor 路径与 txt_format 对齐测试。

backrooms 实证：boot.config 的 'nolog=' 曾走 extractor 内联版（缺
kv_empty 分支）→ 落 plain 被模型回显 → untranslated_text 恒败。
修复后 extractor 与 txt_format.extract_txt 三层 kv 分类一致。
"""
import tempfile
from pathlib import Path

from hanhua.core.extractor import parse_file
from hanhua.core.models import STATUS_SKIPPED


def test_kv_empty_lines_skipped_in_content_routed_txt():
    """空值 kv 行（nolog=）在 extractor 内容路由路径也跳过（kv_empty），
    不落入 plain 参与翻译。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "boot.config"
        p.write_text(
            "wait-for-native-debugger=0\n"
            "hdr-display-enabled=0\n"
            "single-instance=\n"
            "nolog=\n"
            "build-guid=4195ad97e02d4ad8ac75aa14581a8be3\n",
            encoding="utf-8")
        parsed = parse_file(p)

    assert parsed.format == "txt"
    nolog = [e for e in parsed.entries
             if e.meta.get("kind") == "kv_empty"
             and e.meta.get("key") == "nolog"]
    assert nolog and nolog[0].status == STATUS_SKIPPED
    # 所有行均被结构/空值跳过，无一条落入待翻译
    pending = [e for e in parsed.entries if e.status == "pending"]
    assert not pending


def test_jsonc_suffix_parsed_as_json_with_comments():
    """.jsonc（JSON with Comments）后缀走 JSON 解析路径：注释剥离 +
    键值提取（containment 实证：Language/EN/subtitles.jsonc 曾因后缀
    不在列表被逐行提取，639 条 JSON 行文本落 plain 全失败）。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "subtitles.jsonc"
        p.write_text(
            "{\n"
            "    // 字幕文件（Unity 本地化 jsonc 格式）\n"
            "    \"subs\": {\n"
            "        \"intro_line\": \"Welcome to the facility\",\n"
            "        \"door_hint\": \"The door is locked\",\n"
            "    },\n"
            "}\n",
            encoding="utf-8")
        parsed = parse_file(p)

    assert parsed.format == "json"
    texts = {e.original for e in parsed.entries}
    assert "Welcome to the facility" in texts
    assert "The door is locked" in texts
    assert all(e.status != STATUS_SKIPPED for e in parsed.entries)


def test_list_marker_skipped_f37():
    """F37（78-hour-rain 实证）：'- a' 列表项标记跳过（模型回显恒败
    target_script_mismatch，无可译语义内容）。"""
    from hanhua.core.unity.extractor import _LIST_MARKER
    assert _LIST_MARKER.match("- a")
    assert _LIST_MARKER.match("• x")
    assert _LIST_MARKER.match("– ok")
    assert not _LIST_MARKER.match("- Take the axe")   # 真实列表项文本
    assert not _LIST_MARKER.match("- a longer phrase")
    assert not _LIST_MARKER.match("a")
    assert not _LIST_MARKER.match("---")


def test_corescript_dialogue_extracted_f51():
    """F51（shellcore 实证 900+ 条对话真盲区）：.corescript 对话脚本
    Text("key", "对话") 行——引号内 value 进池，key 保留，注释行跳过。"""
    import tempfile
    from pathlib import Path
    from hanhua.core.extractor import parse_file
    from hanhua.core.formats.txt_format import apply_txt
    from hanhua.core.models import STATUS_TRANSLATED
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "dialogue.corescript"
        p.write_text(
            'Text("Legend_d1", "Time to carve through.")\n'
            '//Text("Legend_old", "commented out dialogue")\n'
            'Text("Sector_status", "PRESS \'E\' TO OPEN")\n',
            encoding="utf-8")
        pf = parse_file(p)
    pend = [e for e in pf.entries if e.status == "pending"]
    assert len(pend) == 2, [e.original for e in pf.entries]
    assert pend[0].original == "Time to carve through."
    assert pend[0].meta["cs_key"] == "Legend_d1"
    assert pend[0].meta["kind"] == "corescript"
    # 注释行跳过
    assert all(e.status != "pending"
               or "commented out" not in e.original for e in pf.entries)
    # 写回：key 保留、value 替换（apply_txt 需全部条目重建行序）
    e = pend[0]
    e.status = STATUS_TRANSLATED
    e.translation = "该杀出一条路了。"
    out = apply_txt(pf.entries)
    assert 'Text("Legend_d1", "该杀出一条路了。")' in out
    assert '//Text("Legend_old"' in out
    assert "PRESS 'E' TO OPEN" in out


def test_nodecanvas_graph_data_excluded_f51c():
    """F51c：NodeCanvas 图数据（.sectordata 等 XML 节点图）不进文本
    扫描（节点名/变量名=代码键）；.corescript 对话脚本保留。"""
    import tempfile
    from pathlib import Path
    from hanhua.core.scanner import discover
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "map.sectordata").write_text(
            '<?xml version="1.0"?><NodeCanvas><Node name="Enter Sector"/></NodeCanvas>',
            encoding="utf-8")
        (root / "dialogue.corescript").write_text(
            'Text("k1", "Hello world.")\n', encoding="utf-8")
        found = [p.name for p in discover(root)]
    assert "dialogue.corescript" in found
    assert "map.sectordata" not in found


def test_readme_credit_file_skipped_f54():
    """F54（bad-faith 实证）：README_Credits_MoreInfo.txt（windows-1252
    致谢文件）跳过——credit/README 组合文件名段匹配。"""
    import tempfile
    from pathlib import Path
    from hanhua.core.scanner import discover
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "README_Credits_MoreInfo.txt").write_text(
            "This piece wouldn't be possible without...", encoding="utf-8")
        (root / "dialogue.txt").write_text(
            "Hello world", encoding="utf-8")
        found = [p.name for p in discover(root)]
    assert "README_Credits_MoreInfo.txt" not in found
    assert "dialogue.txt" in found


def test_launcher_bat_cmd_skipped():
    """Windows 启动脚本（.bat/.cmd）整文件跳过——引用 exe 路径/窗口
    参数，翻译破坏启动（electric-trains et.bat 实证：'-screen-fullscreen'
    被误译成中文破折号且引号变弯引号，破坏启动参数）。"""
    import tempfile
    from pathlib import Path
    from hanhua.core.extractor import parse_file
    from hanhua.core.models import STATUS_SKIPPED
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "et.bat"
        p.write_text(
            '"Electric Trains.exe" -screen-fullscreen 0 -screen-width 1920 '
            '-screen-height 1080\n',
            encoding="utf-8")
        parsed = parse_file(p)
    assert parsed.noise is True
    assert parsed.format == "txt"
    assert all(e.status == STATUS_SKIPPED for e in parsed.entries)
    assert parsed.entries[0].meta.get("kind") == "launcher_script"
    assert not [e for e in parsed.entries if e.status == "pending"]
    # 占位条目 line_no/raw 保真——写回走 apply_txt 的 skipped 分支
    # 逐行原样重建，不产生空文件也不破坏启动参数（KeyError: 'line_no'
    # 实证：占位缺字段会崩写回管线）。
    assert parsed.entries[0].meta["line_no"] == 0
    assert parsed.entries[0].meta["raw"] == (
        '"Electric Trains.exe" -screen-fullscreen 0 -screen-width 1920 '
        '-screen-height 1080')


def test_launcher_bat_cmd_roundtrip_byte_exact():
    """写回保真：.bat 的 skipped 占位经 writer._render + _encode 逐字节
    还原（含 CRLF 与行尾换行）——写回后启动脚本不被清空/改写（B7）。"""
    import tempfile
    from pathlib import Path
    import json
    from hanhua.core.extractor import parse_file
    from hanhua.core.writer import _render, _encode
    payload = (
        '@echo off\r\n'
        '"Electric Trains.exe" -screen-fullscreen 0 -screen-width 1920 '
        '-screen-height 1080\r\n'
        'pause\r\n')
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "et.bat"
        p.write_bytes(payload.encode("utf-8"))
        pf = parse_file(p)
        f = {"id": "et.bat", "rel_path": "et.bat", "format": pf.format,
             "encoding": pf.encoding, "eol": pf.eol, "meta": pf.meta}
        entries = [{"file_id": "et.bat", "key_path": e.key_path,
                    "original": e.original, "translation": "",
                    "status": e.status,
                    "meta": json.dumps(e.meta, ensure_ascii=False)}
                   for e in pf.entries]
        body = _render(p, f, entries, "zh-CN")
        out = _encode(body, p, f)
    assert out == payload.encode("utf-8"), (
        f".bat 写回不逐字节还原：\n{out!r}\nvs\n{payload.encode('utf-8')!r}")
