# -*- coding: utf-8 -*-
"""消息脚本 TextAsset 提取/写回测试（fromivan 实证 2026-09-01）。

背景：'RECEIVED_MSG|Hey, kiddo!' 逐行对话脚本是「每行 2 列」的一致宽度
竖线分隔表，会被 looks_like_csv_text 误判 CSV——CSV 分支把首行当表头、
命令 token 当源列，只提取 DELAY/TYPING 等命令词进池。且 '|' 左列是引擎
指令（DELAY/TYPING/RECEIVED_MSG/…），翻译写坏对话时序。

修复：_is_msg_script 整文件判定（命令列全大写占比 ≥80%）→ 消息脚本走
line 拆分；行级只进「命令|对话内容」真对话行；写回侧剥掉命令前缀只替换
'|' 后内容。本测试锁定判定与提取/写回三条链。
"""
import sys

sys.path.insert(0, "")

from hanhua.core.formats.txt_format import apply_txt  # noqa: E402
from hanhua.core.unity.extractor import (  # noqa: E402
    _is_msg_script,
    _is_msg_script_line,
    _msg_dialogue_content,
    _textasset_entries,
)

_MSG = """RECEIVED_MSG|Hey, kiddo!
DELAY|1

TYPING|
DELAY|1
RECEIVED_MSG|I'm so sorry.
SENT_MSG|Hey, Dad!
DELAY|2
RECEIVED_MSG|Good morning, comrade."""


def test_is_msg_script_true():
    assert _is_msg_script(_MSG) is True


def test_is_msg_script_false_csv():
    # 真 CSV 词典表（I2 风格，含逗号与英文值）——不判消息脚本
    csv = "ID,IND,ENG,CHN\n1,key1,Hello,你好\n2,key2,World,世界"
    assert _is_msg_script(csv) is False


def test_is_msg_script_false_pipe_csv():
    # 真竖线分隔表（列名非全大写）——不判消息脚本
    csv = "Name|Age\nAlice|30\nBob|25"
    assert _is_msg_script(csv) is False


def test_is_msg_script_false_dialogue_table():
    # 对话表（Speaker|Dialogue 列名 TitleCase，非全大写命令）——不判
    csv = "Speaker|Dialogue\nSeaWall_D1|Arum: Apa kau sedang lakukan?\nGuard|Serah diri!"
    assert _is_msg_script(csv) is False


def test_msg_dialogue_content():
    assert _msg_dialogue_content("RECEIVED_MSG|Hey, kiddo!") == "Hey, kiddo!"
    assert _msg_dialogue_content("DELAY|1") == ""          # 右列纯数字
    assert _msg_dialogue_content("TYPING|") == ""          # 右列空
    assert _msg_dialogue_content("WAIT_FOR_PRESS|") == ""
    # OPEN_IF 右列全命令/标识符（分支参数）→ 跳过
    assert _msg_dialogue_content(
        "OPEN_IF|INDEPENDENT|FRIEND|Set4-Friend-N") == ""
    # 句末标点真句子保留
    assert _msg_dialogue_content(
        "RECEIVED_MSG|Please tell me there was some improvement.") \
        == "Please tell me there was some improvement."


def test_extract_msg_script_only_dialogue():
    entries = _textasset_entries("f", 1, _MSG.encode())
    pending = [e for e in entries if e.status != "skipped"]
    # 只保留「命令|对话内容」真对话行；DELAY/TYPING 命令行跳过
    originals = {e.original for e in pending}
    assert "RECEIVED_MSG|Hey, kiddo!" in originals
    assert "RECEIVED_MSG|I'm so sorry." in originals
    assert "SENT_MSG|Hey, Dad!" in originals
    assert "RECEIVED_MSG|Good morning, comrade." in originals
    assert not any(e.original.startswith(("DELAY|", "TYPING|"))
                   for e in pending)
    # 行级条目带 msg_script 标记（写回侧剥命令前缀用）
    assert all(e.meta.get("msg_script") for e in pending)


def test_extract_not_csv_branch():
    """消息脚本不落 CSV 分支（无 csv 形态 key_path 条目）。"""
    entries = _textasset_entries("f", 1, _MSG.encode())
    assert not any(e.key_path.endswith("/csv/row/0")
                   for e in entries)


def test_writeback_preserves_command_prefix():
    """写回侧剥掉命令前缀，只替换 '|' 后对话内容（命令原样保留）。

    注意：apply_txt 不解析 msg_script——命令前缀保护在 writer 侧
    _patch_textasset。此测试验证提取侧的 original 完整性（命令+内容
    同源），写回保护由 _patch_textasset 测试覆盖。
    """
    entries = _textasset_entries("f", 1, _MSG.encode())
    pending = [e for e in entries if e.status != "skipped"]
    # 行级条目 original = 完整命令行（命令|内容），不是剥离后的内容
    assert all("|" in e.original for e in pending)
    assert all(e.meta.get("msg_script") for e in pending)


def test_writeback_meta_msg_script_flag():
    entries = _textasset_entries("f", 1, _MSG.encode())
    pending = [e for e in entries if e.status != "skipped"]
    assert pending and all(e.meta.get("msg_script") for e in pending)


def _patch_roundtrip(script: bytes, entries) -> bytes:
    """经 _patch_textasset 写回（msg_script 命令前缀保护）。

    写回侧 items 的 meta 是 JSON 字符串（与 store 持久化一致），
    e.meta 若已是 dict 需 json.dumps 序列化。
    """
    import json as _json

    from hanhua.core.unity.writer import WriteResult, _patch_textasset
    items = []
    for e in entries:
        if e.status == "skipped":
            continue
        meta_str = (e.meta if isinstance(e.meta, str)
                    else _json.dumps(e.meta, ensure_ascii=False))
        items.append(({
            "file_id": e.file_id,
            "key_path": e.key_path,
            "original": e.original,
            "translation": e.translation,
            "status": e.status,
            "meta": meta_str,
        }, e.meta))
    res = WriteResult()
    return _patch_textasset(script, items, [], res)


def test_patch_textasset_msg_script():
    script = _MSG.encode("utf-8")
    entries = _textasset_entries("f", 1, script)
    for e in entries:
        if e.status == "skipped":
            continue
        e.translation = "译文"          # 无 '|' 的干净译文
    out = _patch_roundtrip(script, entries)
    text = out.decode("utf-8")
    lines = text.splitlines()
    assert "RECEIVED_MSG|译文" in lines, text
    assert "SENT_MSG|译文" in lines, text
    assert "DELAY|1" in lines          # 命令行原样
    assert "TYPING|" in lines


def test_patch_textasset_msg_script_pipe_translation():
    """模型把命令+内容一起回显（'RECEIVED_MSG|你好'）→ 命令剥掉。"""
    script = _MSG.encode("utf-8")
    entries = _textasset_entries("f", 1, script)
    for e in entries:
        if e.status == "skipped":
            continue
        e.translation = "RECEIVED_MSG|你好"
    out = _patch_roundtrip(script, entries)
    text = out.decode("utf-8")
    lines = text.splitlines()
    # 命令前缀原样保留 + 译文只取 '|' 前部分（RECEIVED_MSG 剥掉）
    assert "RECEIVED_MSG|你好" in lines, text
    # 不得出现双命令（RECEIVED_MSG|RECEIVED_MSG|…）
    assert "RECEIVED_MSG|RECEIVED_MSG" not in text
