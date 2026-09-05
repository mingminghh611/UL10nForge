# -*- coding: utf-8 -*-
"""B16b/B16c 回归测试（snowday_汉化 实证 2026-09-05）。

B16b（漏提根因之二——写回白写）：表格无中文目标列（target_col=None）
且游戏运行时只读唯一语言源列（snowday：Spanish 列内容是英语）——
默认写回走「追加 ChineseSimplified 新列」游戏不读 → 汉化白写。
修复：自动覆盖源列判据——数据行 ≥8 + 唯一高填充句子态列 ==
source_col → overwrite_source=True，写回列=源列。

B16c（写回破坏行结构）：TextAsset 内嵌 '|' 表 writer 只传
{"kind":"textasset"}，apply_format_text 缺省按 ',' 重建 → 译文追加成
',,,,' 尾巴落进最后一个管道字段，行结构破坏。修复：提取端 delimiter
留档条目 meta，写回端三级取值（file meta → 条目 meta → 后缀缺省）。
"""
from __future__ import annotations

from hanhua.core.formats import apply_format_text
from hanhua.core.formats.csv_format import extract_csv_text
from hanhua.core.models import STATUS_TRANSLATED, TextEntry


# snowday DialogueStructure 形态（简化）：'|' 分隔，唯一语言列 Spanish
# 内容是英语；Notes/ChoicesLink 是低填充注释列；Name 列人名并列为真实
# snowday 形态（ID 列在 NON_LANG_HEADERS 被排除，Name 列非空最多但非
# 句子态 → source_col 判定依赖「唯一句子态列 == source_col」并修正
# source_col 本身——见 test_b16b_name_column_not_source）
SNOWDAY_TABLE = "\n".join([
    "ID|Notes|ActionEvents|Name|ChoicesLink|Spanish",
    "pid1|||Mika||How was your day at school?",
    "pid2|greeting||Tomás||It was fine, I guess.",
    "pid3|||Mika||Did you play in the snow again?",
    "pid4|reply||Tomás||Sure did, best day ever.",
    "pid5|||Mika||Do you want to build a snowman?",
    "pid6|offer||Tomás||Let us go outside right now.",
    "pid7|||Mika||Be careful with the ice, ok?",
    "pid8|warning||Tomás||I will be careful, promise.",
    "pid9|||Mika||Come inside when you are cold.",
    "pid10|end||Tomás||See you tomorrow at school.",
])


def test_b16b_name_column_not_source() -> None:
    """Name 列（人名，全标识符态）非空数与 Spanish 并列 → source_col
    必须落句子态列（col5），不得落在人名列（提取端源列选择判据）。"""
    entries, write_col = extract_csv_text(SNOWDAY_TABLE, "snowday", "")
    assert write_col == 5
    assert entries[0].meta.get("source_col") == 5
    originals = [e.original for e in entries]
    assert "How was your day at school?" in originals
    assert "Mika" not in originals, "人名列不得当选源列"


def test_b16b_snowday_triggers_auto_overwrite() -> None:
    """snowday 形态：无中文目标列 + 唯一句子态源列 → 自动覆盖源列。"""
    entries, write_col = extract_csv_text(SNOWDAY_TABLE, "snowday", "")
    assert write_col == 5, "写回列必须是 Spanish 源列（追加列游戏不读）"
    for e in entries:
        assert e.meta.get("overwrite_source") is True


def test_b16b_rendezvous_chn_table_no_auto_overwrite() -> None:
    """对照组：命中中文目标列（CHN）→ 不触发自动覆盖（写中文列更安全）。"""
    rendezvous = (
        " ,IND,ENG,SPA,CHN\n"
        "SeaWall_D1,Arum: Apa kau,Do you remember?,¿Recuerdas?,你还记得吗\n"
        "SeaWall_D2,Mereka pulang,After our parents,Que se van,父母去世后\n"
        "SeaWall_D3,Mereka bertemu,Is this really how,Se reúnen,真的吗\n"
        "SeaWall_D4,Kau ingat apa,what it was called?,Te suena?,它叫什么\n"
        "SeaWall_D5,Aku tak ingat,I can't remember,No lo sé,我不记得了\n"
        "SeaWall_D6,Setelah orang,Tu te souviens?,Recuerdas?,你还记得吗\n"
        "SeaWall_D7,Mereka kembali,Ils rentrent,Se van,他们回来了\n"
        "SeaWall_D8,Benarkah ini,Is this really,De verdad?,真的是这样吗\n"
    )
    entries, write_col = extract_csv_text(rendezvous, "rdvs", "")
    assert write_col == 4, "中文目标列在场 → 写 CHN 列"
    for e in entries:
        assert e.meta.get("overwrite_source") is None


def test_b16b_small_table_no_auto_overwrite() -> None:
    """数据行 < 8：样本不足防误判，保持追加列模式（宁漏勿坏）。"""
    small = "\n".join([
        "ID|Notes|Spanish",
        "pid1||How was your day?",
        "pid2||It was fine.",
        "pid3||Did you play?",
        "pid4||Sure did.",
    ])
    entries, write_col = extract_csv_text(small, "small", "")
    assert write_col is None, "无中文列且不触发覆盖 → 追加列模式"
    for e in entries:
        assert e.meta.get("overwrite_source") is None


def test_b16c_delimiter_stamped_in_entry_meta() -> None:
    """提取端 delimiter 留档：写回端三级取值的依据。"""
    entries, _ = extract_csv_text(SNOWDAY_TABLE, "snowday", "")
    assert entries, "对话行必须产出条目"
    assert entries[0].meta.get("delimiter") == "|"


def test_b16c_writeback_lands_in_pipe_column5_row_count_conserved() -> None:
    """端到端：'|' 表结构化写回 → 译文落 col5、行列守恒、无 ',,,,' 尾巴。

    B16c 实证 bug：writer 只传 {"kind":"textasset"}（无 delimiter），
    旧代码按 ',' 重建 → 'pid1|||Mika||How was...,,,,测试' 毁表。"""
    from tests.test_b15_b16_snowday import _textasset_entries  # noqa: F401
    from hanhua.core.unity.extractor import _textasset_entries as _tae

    raw = SNOWDAY_TABLE.encode("utf-8")
    display = _tae("f", 46, raw, "table.assets", None)
    assert display, "管道表必须被 csv 分支提取"
    items = []
    for e in display:
        items.append(TextEntry(
            file_id="f", key_path=e.key_path, original=e.original,
            translation=f"【{e.original}】", status=STATUS_TRANSLATED,
            meta=e.meta))
    # writer 同构调用：meta 只有 {"kind":"textasset"}，delimiter 须从
    # 条目 meta 兜底（B16c 核心）
    out = apply_format_text(
        "csv", items, SNOWDAY_TABLE, {"kind": "textasset"})
    out_rows = [r for r in out.splitlines() if r]
    src_rows = [r for r in SNOWDAY_TABLE.splitlines() if r]
    assert len(out_rows) == len(src_rows), "行数守恒"
    assert all(r.count("|") == 5 for r in out_rows), "列数守恒"
    assert ",,," not in out, "不得出现逗号尾巴（B16c 毁表形态）"
    header = out_rows[0].split("|")
    assert "ChineseSimplified" not in header, \
        "B16b 覆盖源列模式不追加中文列（游戏不读）"
    data0 = out_rows[1].split("|")
    assert data0[5] == "【How was your day at school?】", \
        "译文必须写进 Spanish 源列（col5）"


def test_b16b_file_meta_delimiter_takes_precedence() -> None:
    """三级取值：file 级 meta.delimiter 优先于条目级。"""
    entries, _ = extract_csv_text(SNOWDAY_TABLE, "snowday", "")
    from hanhua.core.formats.csv_format import apply_csv
    out = apply_csv(entries, SNOWDAY_TABLE, delimiter="|", target_col=5)
    assert out.splitlines()[0] == SNOWDAY_TABLE.splitlines()[0], \
        "表头不变（写源列不追加）"
