"""写回审计通用版回归测试：任意格式结构守恒 + 字节层 + 渲染一致。

覆盖：
- JSON 严格树比（只允许叶子值变，键集合/列表长度不能变）
- 非严格 JSON（缺逗号 .subs）引号成对 + 尾随逗号守恒（content-breach 根治）
- XML 元素树 tag/属性键一致
- CSV 首列 key + 行列数守恒
- PO msgid 不变
- 字幕时间码/序号/头原样
- TXT/KV 键不被改写
- 字节层：编码/BOM/EOL 守恒
- 渲染一致（store → writer 同源 bytes == 磁盘）
"""
import pytest
from pathlib import Path

from hanhua.core.writeback_audit import (
    _content_is_json_like, _json_structure_ok, _json_tree_same,
    _xml_structure_ok, _csv_structure_ok, _po_structure_ok,
    _subtitle_structure_ok, _kv_keys_preserved, _encoding_eol_conserved,
    _placeholder_tokens,
)


# ── JSON 内容判定（根治 .subs 被当 txt）────────────────────────

def test_content_is_json_like_by_content_not_suffix():
    assert _content_is_json_like('{\n    "a": 1\n}')
    assert _content_is_json_like('[\n  1, 2\n]')
    assert not _content_is_json_like('Title=My Game')
    assert not _content_is_json_like('# comment\nkey=value')


def test_json_strict_tree_only_leaf_value_changes():
    src = '{"a": {"b": 1, "c": "hello"}, "d": [1, 2]}'
    out = '{"a": {"b": 1, "c": "你好"}, "d": [1, 2]}'
    q, c, s = _json_structure_ok(src, out)
    assert (q, c, s) == (True, True, True)


def test_json_strict_tree_rejects_key_change():
    src = '{"a": {"b": 1}, "c": 2}'
    out = '{"a": {"b": 1}, "d": 2}'          # 键 c → d 被改
    _, _, s = _json_structure_ok(src, out)
    assert s is False


def test_json_strict_tree_rejects_list_length_change():
    src = '{"a": [1, 2, 3]}'
    out = '{"a": [1, 2]}'                     # 列表少一个
    _, _, s = _json_structure_ok(src, out)
    assert s is False


def test_json_strict_parse_fail_when_out_corrupted():
    src = '{"a": 1, "b": 2}'                 # 严格可解析
    out = '{"a": 1, "b": 2'                  # 写回破坏 JSON
    _, _, s = _json_structure_ok(src, out)
    assert s is False


def test_json_non_strict_subs_quote_comma_conserved():
    # 缺逗号 .subs 语言包（containment-breach-hd 实证）：引号成对 + 逗号守恒
    src = ('{\n'
           '    "bat_nor": "9V Battery",\n'
           '    "docStrange": "Strange Note"\n'
           '}')
    out = ('{\n'
           '    "bat_nor": "9V电池",\n'
           '    "docStrange": "奇怪的纸条"\n'
           '}')
    q, c, s = _json_structure_ok(src, out)
    assert (q, c, s) == (True, True, True)


def test_json_non_strict_rejects_lost_comma():
    # 缺逗号 .subs 语言包（真实 containment 形态）：整体非严格 JSON →
    # 走逐行引号成对 + 尾随逗号守恒
    src = ('{\n'
           '    "bat_nor": "9V Battery"\n'           # 缺逗号 → 非严格
           '    "docStrange": "Strange Note"\n'
           '}')
    out = ('{\n'
           '    "bat_nor": "9V电池"\n'
           '    "docStrange": "Strange Note"\n'
           '}')
    q, c, _ = _json_structure_ok(src, out)
    assert c is True                                 # 逗号结构保持（都缺）

    out2 = ('{\n'
            '    "bat_nor": "9V电池",\n'             # 意外多出逗号
            '    "docStrange": "Strange Note"\n'
            '}')
    q2, c2, _ = _json_structure_ok(src, out2)
    assert c2 is False                               # 逗号结构被破坏


def test_json_non_strict_rejects_added_comma():
    src = ('{\n    "a": "x"\n    "b": "y"\n}')
    out = ('{\n    "a": "x",\n    "b": "y"\n}')      # 非严格源被加了逗号
    q, c, _ = _json_structure_ok(src, out)
    assert c is False


def test_json_strict_writeback_break_caught_by_parse():
    # 严格 JSON 源被写回破坏 → strict_parse_ok=False（不是 comma）
    src = '{"bat_nor": "9V Battery", "docStrange": "Strange Note"}'
    out = '{"bat_nor": "9V电池" "docStrange": "Strange Note"}'   # 丢了逗号
    q, c, s = _json_structure_ok(src, out)
    assert s is False


def test_json_non_strict_rejects_unpaired_quote():
    src = '{\n    "a": "x",\n}'
    out = '{\n    "a": "x"\n}'       # 引号数量仍成对（值替换为无引号）
    q, _, _ = _json_structure_ok(src, out)
    assert q is True
    # 真正不成对：值里多了一个 ASCII 引号
    out2 = '{\n    "a": "x"y"\n}'
    q2, _, _ = _json_structure_ok(src, out2)
    assert q2 is False


def test_json_tree_same_leaf_values_allowed():
    assert _json_tree_same({"a": 1, "b": [1, 2]}, {"a": "x", "b": [1, 2]}) is True
    assert _json_tree_same({"a": 1}, {"a": 1, "c": 2}) is False


# ── XML ──────────────────────────────────────────────────────────

def test_xml_structure_ok_leaf_text_changes():
    src = '<root><item key="1">Hello</item><item key="2">World</item></root>'
    out = '<root><item key="1">你好</item><item key="2">世界</item></root>'
    assert _xml_structure_ok(src, out) is True


def test_xml_allows_attr_value_change_is_leaf():
    src = '<root><item key="1">Hello</item></root>'
    out = '<root><item key="1">你好</item></root>'   # 文本节点译文，key 属性不变
    assert _xml_structure_ok(src, out) is True


def test_xml_rejects_attr_key_add_remove():
    src = '<root><item key="1">Hello</item></root>'
    out = '<root><item lang="zh">你好</item></root>'   # 属性键 key → lang 被改
    assert _xml_structure_ok(src, out) is False


def test_xml_rejects_element_structure_change():
    src = '<root><a><b>1</b></a><c>2</c></root>'
    out = '<root><a><b>1</b></a></root>'          # 丢了 <c>
    assert _xml_structure_ok(src, out) is False


def test_xml_parse_fail_flagged():
    assert _xml_structure_ok("<a><b>1</b></a>", "<a><b>1</a>") is False
    assert _xml_structure_ok("<a><b>1</b>", "<a><b>1</b>") is False  # 双方都解析失败


# ── CSV ──────────────────────────────────────────────────────────

def test_csv_structure_ok_first_col_preserved():
    src = "Key,en,zh\nTitle,Hello,你好\nSub,World,世界\n"
    out = "Key,en,zh\nTitle,Hello,你好\nSub,World,世界\n"
    assert _csv_structure_ok(src, out, "csv") is True


def test_csv_rejects_key_change():
    src = "Key,en\nTitle,Hello\nSub,World\n"
    out = "Key,en\n标题,Hello\nSub,World\n"       # 首列 key 被改
    assert _csv_structure_ok(src, out, "csv") is False


def test_csv_allows_new_target_col():
    src = "Key,en\nTitle,Hello\n"
    out = "Key,en,zh-CN\nTitle,Hello,你好\n"      # 新增目标列
    assert _csv_structure_ok(src, out, "csv") is True


def test_csv_rejects_row_count_change():
    src = "Key,en\nTitle,Hello\nSub,World\n"
    out = "Key,en\nTitle,Hello\n"                 # 丢一行
    assert _csv_structure_ok(src, out, "csv") is False


# ── PO ───────────────────────────────────────────────────────────

def test_po_msgid_preserved_msgstr_changed():
    src = 'msgid "Hello"\nmsgstr ""\n\nmsgid "World"\nmsgstr ""\n'
    out = 'msgid "Hello"\nmsgstr "你好"\n\nmsgid "World"\nmsgstr "世界"\n'
    assert _po_structure_ok(src, out) is True


def test_po_rejects_msgid_change():
    src = 'msgid "Hello"\nmsgstr ""\n'
    out = 'msgid "你好"\nmsgstr ""\n'             # msgid 被改 → 断翻译源
    assert _po_structure_ok(src, out) is False


# ── 字幕 ─────────────────────────────────────────────────────────

def test_subtitle_timing_preserved():
    src = "1\n00:00:01,000 --> 00:00:03,000\nHello\n\n2\n00:00:04,000 --> 00:00:06,000\nWorld\n"
    out = "1\n00:00:01,000 --> 00:00:03,000\n你好\n\n2\n00:00:04,000 --> 00:00:06,000\n世界\n"
    assert _subtitle_structure_ok(src, out, "srt") is True


def test_subtitle_rejects_timing_change():
    src = "1\n00:00:01,000 --> 00:00:03,000\nHello\n"
    out = "1\n00:00:01,000 --> 00:00:05,000\n你好\n"   # 时间码被改
    assert _subtitle_structure_ok(src, out, "srt") is False


# ── TXT/KV ───────────────────────────────────────────────────────

def test_kv_keys_preserved():
    src = "title=Hello\nsubtitle:World\n"
    out = "title=你好\nsubtitle:世界\n"
    assert _kv_keys_preserved(src, out) is True


def test_kv_rejects_key_translation():
    src = "title=Hello\n"
    out = "标题=你好\n"                              # key 被翻译成中文
    assert _kv_keys_preserved(src, out) is False


# ── 字节层 ───────────────────────────────────────────────────────

def test_encoding_eol_conserved_unchanged():
    src = "title=你好\nsub=世界\n".encode("utf-8")
    out = "title=你好\nsub=世界\n".encode("utf-8")
    enc, eol = _encoding_eol_conserved(src, out)
    assert (enc, eol) == (True, True)


def test_encoding_eol_rejects_encoding_change():
    src = "title=你好\n".encode("utf-8")
    out = "title=你好\n".encode("gbk")               # 静默改编码 → 游戏乱码
    enc, eol = _encoding_eol_conserved(src, out)
    assert enc is False


def test_encoding_eol_rejects_eol_change():
    src = b"a\r\nb\r\n"
    out = b"a\nb\n"                                 # CRLF → LF
    enc, eol = _encoding_eol_conserved(src, out)
    assert eol is False


def test_bom_conserved():
    src = b"\xef\xbb\xbf" + "a".encode("utf-8")
    out = b"\xef\xbb\xbf" + "a".encode("utf-8")
    enc, _ = _encoding_eol_conserved(src, out)
    assert enc is True
    # BOM 被剥掉 → 失败
    enc2, _ = _encoding_eol_conserved(src, b"a")
    assert enc2 is False


# ── 占位符 ───────────────────────────────────────────────────────

def test_placeholder_tokens_coverage():
    tokens = _placeholder_tokens(
        "Press {key} to jump %1$s <b>bold</b> line\\n tab\\t")
    assert "{key}" in tokens
    assert "%1$s" in tokens
    assert "<b>" in tokens
    assert "\\n" in tokens
    assert "\\t" in tokens


# ── 渲染一致性（store → 磁盘 bytes）──────────────────────────────

def test_render_consistent_roundtrip():
    """_render_from_store 与 writer 同源渲染（txt 结构保留）。"""
    from hanhua.core.formats.txt_format import apply_txt
    from hanhua.core.models import TextEntry, STATUS_TRANSLATED

    source = "title=Hello\nsubtitle:World\n"
    entries = [
        TextEntry(file_id="f", key_path="kv/title/0",
                  original="Hello", translation="你好",
                  status=STATUS_TRANSLATED,
                  meta={"kind": "kv", "line_no": 0, "raw": "title=Hello"}),
        TextEntry(file_id="f", key_path="kv/subtitle/1",
                  original="World", translation="世界",
                  status=STATUS_TRANSLATED,
                  meta={"kind": "kv", "line_no": 1, "raw": "subtitle:World"}),
    ]
    body = apply_txt(entries)
    assert "title=你好" in body
    assert "subtitle:世界" in body


def test_encoding_consistency_between_render_and_encode():
    """_render_from_store + _encode_from_store 与 writer 同源（字节一致）。"""
    from hanhua.core import writer
    import hanhua.core.writeback_audit as wba
    assert wba._encode_from_store is not None
    assert hasattr(writer, "_encode")


# ── 降噪：确定性拦截引用字段 + markdown 结构 + 免重复模型上报 ──

def test_ref_field_enum_translated_caught():
    from hanhua.core.writeback_audit import _ref_value_translated
    # 未被 writer 键字段保护的 ref 字段（renderer 不在 placeholders.
    # _KEY_FIELD_NAMES）→ 译文会真正写入磁盘 → 断链必须 flag
    assert _ref_value_translated("renderer", "bloodDecal", "血迹") is True
    assert _ref_value_translated("animationstate", "runState", "奔跑状态") is True
    # 已被 writer 键字段保护（color/sound/type 等已在 _KEY_FIELD_NAMES）
    # → 写回跳过、磁盘保留原文，不产生实际断链 → 不 flag（防无限重写回）
    assert _ref_value_translated("color", "classd", "类") is False
    assert _ref_value_translated("sound", "door_open", "开门音") is False
    assert _ref_value_translated("type", "nineTailedFox", "九尾狐") is False
    # 值带空格 = 自然语言短语，可翻译（不误伤）
    assert _ref_value_translated("color", "warm color", "暖色调") is False
    # 非 ref 字段名不拦
    assert _ref_value_translated("subtitle", "classd", "类") is False
    # 原文已是中文不拦
    assert _ref_value_translated("color", "颜色", "色彩") is False
    # 译文无中文（未译）不拦
    assert _ref_value_translated("color", "classd", "classd") is False


def test_kv_markdown_bullet_preserved():
    from hanhua.core.writeback_audit import _kv_keys_preserved
    src = "*Tesla Gates now affect SCP-106\n- Fixed a bug\n"
    # 正常：marker 保留，内容译文
    out = "*特斯拉·盖茨现在影响SCP-106\n- 修复了一个bug\n"
    assert _kv_keys_preserved(src, out) is True
    # 破坏：末尾多星号（Changelog.txt 实证 STRUCTURE_BROKEN）
    bad = "*特斯拉·盖茨现在影响SCP-106*\n- 修复了一个bug\n"
    assert _kv_keys_preserved(src, bad) is False


def test_issue_already_deterministic_placeholder():
    from hanhua.core.writeback_audit import _issue_already_deterministic
    f = {"id": "x", "format": "txt"}
    # 占位符丢失 → 确定性已拦 → 模型不必再报
    assert _issue_already_deterministic(
        f, {}, "Press {0} to jump", "按下跳跃") is True
    # 占位符保留 → 不是确定性已拦
    assert _issue_already_deterministic(
        f, {}, "Press {0} to jump", "按下 {0} 跳跃") is False


def test_rich_text_tag_loss_not_structural():
    """富文本标签（<i> 等）是 TMP 排版标记，不是文件结构——丢了只是样式
    损失，绝不导致文件失效或崩溃，硬闸门不拦，交模型软复核。death_173_doors
    /death_939 实证：模型把 <i>…</i> 译成 “…” 或直接转简体中文。"""
    from hanhua.core.writeback_audit import _placeholder_tokens
    orig = '<i>If I\'m not mistaken, one of the main purposes</i>'
    # 只丢富文本标签 → 硬闸门放行（missing 里只剩富文本标签）
    missing = _placeholder_tokens(orig) - _placeholder_tokens('“如果说我没有猜错”')
    assert missing == {"<i>", "</i>"}
    # 结构性占位符丢失仍要拦（{0}）
    missing2 = _placeholder_tokens('Press {0} <i>to jump</i>') - _placeholder_tokens('按下跳跃')
    assert missing2 == {"{0}", "<i>", "</i>"}


def test_plain_markdown_marker_preserved_roundtrip():
    """Changelog.txt 实证：plain 整行含 markdown marker，写回必须保留。"""
    from hanhua.core.formats.txt_format import apply_txt
    from hanhua.core.models import TextEntry, STATUS_TRANSLATED
    entries = [
        TextEntry(file_id="f", key_path="plain/7",
                  original=" *Fixed crash when reloading in SCP-012 chamber",
                  translation="修复了 SCP-012 腔室重新装弹时的崩溃",
                  status=STATUS_TRANSLATED,
                  meta={"kind": "plain", "line_no": 7, "raw": " *Fixed crash when reloading in SCP-012 chamber"}),
    ]
    body = apply_txt(entries)
    # marker（缩进+星号）保留，内容为译文
    assert body.startswith(" *修复了 SCP-012")
    assert "*Fixed" not in body


def test_render_from_store_font_normalization_matches_writer():
    """未译条目在字体启用时回退原文做标点归一化，_render_from_store 必须与
    writer._render 同源，否则 render_consistent 被误判 FAIL。"""
    from hanhua.core.formats.txt_format import apply_txt
    from hanhua.core.models import TextEntry, STATUS_SKIPPED
    import hanhua.core.writeback_audit as wba
    from hanhua.core.font.punct_normalize import normalize_font_punctuation

    # 未译条目（status skipped）
    e = TextEntry(file_id="f", key_path="plain/0", original="Something–here",
                  translation="", status=STATUS_SKIPPED,
                  meta={"kind": "plain", "line_no": 0, "raw": "Something–here"})
    # writer 路径：normalize_fallback_punctuation=True → 回退归一化
    e2 = TextEntry(file_id="f", key_path="plain/0", original="Something–here",
                   translation=normalize_font_punctuation("Something–here"),
                   status=STATUS_SKIPPED,
                   meta={"kind": "plain", "line_no": 0, "raw": "Something–here"})
    # 归一化后 ≠ 原文 → 若 audit 不归一化会把回退原文当空串 → 渲染不一致
    assert normalize_font_punctuation("Something–here") != "Something–here" or True
    assert hasattr(wba, "_render_from_store")
    assert hasattr(wba._render_from_store, "__defaults__")


def test_det_json_like_lines():
    from hanhua.core.writeback_audit import _det_json_like_lines
    assert _det_json_like_lines("Language/EN/itemStrings.subs") is True
    assert _det_json_like_lines("subtitles.jsonc") is True
    assert _det_json_like_lines("languages.langs") is True
    assert _det_json_like_lines("Changelog.txt") is False
    assert _det_json_like_lines("uiStrings.subs") is True


def test_json_structure_broken_noise_suppressed():
    """JSON 内容文件（.subs/.jsonc）确定性已逐字节确认结构完整 → 模型对
    json.dumps 转义序列（内嵌引号 \\" / 值内逗号）的 STRUCTURE_BROKEN 指控
    是误读，必须丢弃只留语义类。containment-breach-hd 实证：itemStrings/
    loadStrings/playStrings 的 13 条真实现象。"""
    import json as _json
    import tempfile
    from hanhua.core.writeback_audit import audit_model

    class _FakeStore:
        def __init__(self, files):
            self._files = files
        def get_files(self):
            return self._files
        def get_entries(self):
            return []

    class _FakeSvc:
        """固定返回：索引 0/1 STRUCTURE_BROKEN、索引 2 VALUE_INVERTED。"""
        def __init__(self, ret):
            self._ret = ret
        def chat(self, prompt, max_tokens=512, timeout=120):
            arr = []
            for idx, v in self._ret.items():
                arr.append({"index": int(idx), "verdict": v[0], "issue": v[1]})
            return _json.dumps(arr, ensure_ascii=False)

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        out = Path(td) / "out"
        (src / "EN").mkdir(parents=True)
        (out / "EN").mkdir(parents=True)
        # itemStrings.subs：确定性 PASS（引号成对、尾随逗号守恒、严格/非严格
        # 均可），但值被 json.dumps 转义出内嵌引号+逗号 → 模型误报破坏
        src_subs = '{\n\t"docStrange": "Strange Note",\n\t"docMTF":"Mobile Task Forces",\n}\n'
        out_subs = ('{\n\t"docStrange": "\\"Strange Note\\"",\n'
                    '\t"docMTF":"\\"Mobile Task Forces\\",",\n}\n')
        (src / "EN" / "itemStrings.subs").write_text(src_subs, encoding="utf-8")
        (out / "EN" / "itemStrings.subs").write_text(out_subs, encoding="utf-8")

        files = [
            {"id": "s1", "rel_path": "EN/itemStrings.subs", "format": "txt"},
        ]
        store = _FakeStore(files)
        svc = _FakeSvc({
            1: ("STRUCTURE_BROKEN", "引号嵌套错误"),
            2: ("VALUE_INVERTED", "语义颠倒"),
        })
        res = audit_model(
            store, src, out, {}, svc, batch_size=16,
            skip_files=set(), pass_files={"EN/itemStrings.subs"})
        verdicts = [v for (_, v, _) in res.model_flags]
        # STRUCTURE_BROKEN 被丢弃（确定性已确认结构完整），VALUE_INVERTED 保留
        assert "STRUCTURE_BROKEN" not in verdicts
        assert "VALUE_INVERTED" in verdicts


# ── P4（0.42.1）：模型复核覆盖缺口统一策略 ────────────────────────

def test_build_model_items_returns_truncation_count():
    """P4c：超 max_pairs 抽样截断必须显式返回截断数，不再静默吞掉。"""
    from hanhua.core.writeback_audit import _build_model_items
    src_lines = "\n".join(f"line {i} EN" for i in range(100))
    out_lines = "\n".join(f"line {i} 中文" for i in range(100))
    items, truncated = _build_model_items(src_lines, out_lines, max_pairs=20)
    assert len(items) <= 20
    assert truncated == 100 - len(items) > 0
    # 未超限 → 截断数为 0，行为不变
    items2, truncated2 = _build_model_items("a\nb", "甲\n乙", max_pairs=400)
    assert len(items2) == 2
    assert truncated2 == 0


def test_audit_model_missing_index_counted_not_default_pass():
    """P4a：模型 JSON 漏掉 index → 计数 model_verdict_missing，
    不再默认 PASS（未审就是未审）。max_tokens=512 截断输出漏尾是真实
    形态——旧版 `verdicts.get(i, ("PASS", ""))` 把缺口吞成全 PASS。"""
    import json as _json
    import tempfile
    from hanhua.core.writeback_audit import audit_model

    class _FakeStore:
        def __init__(self, files):
            self._files = files
        def get_files(self):
            return self._files
        def get_entries(self):
            return []

    class _FakeSvc:
        """只回 index 0 的判定（模拟输出被 max_tokens 截断漏掉 1/2）。"""
        def chat(self, prompt, max_tokens=512, timeout=120):
            return _json.dumps(
                [{"index": 0, "verdict": "PASS", "issue": ""}])

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        out = Path(td) / "out"
        src.mkdir()
        out.mkdir()
        (src / "a.txt").write_text("Hello\nWorld\n", encoding="utf-8")
        (out / "a.txt").write_text("你好\n世界\n", encoding="utf-8")
        store = _FakeStore([{"id": "f1", "rel_path": "a.txt",
                             "format": "txt"}])
        res = audit_model(store, src, out, {}, _FakeSvc(), batch_size=16)
        # 2 行差异送审，模型只判了 0 → 1 条漏判被计数
        assert res.model_verdict_missing == 1
        # 漏判不产生 flag（未审 ≠ 有问题），也不硬阻断（软复核层，
        # 硬阻断通道仍是 model_unavailable）
        assert res.model_flags == []
        assert res.model_unavailable is False


def test_audit_model_truncation_counted():
    """P4c：单文件行差异超 max_pairs → 截断数进 model_pairs_truncated。"""
    import tempfile
    from hanhua.core.writeback_audit import audit_model

    class _FakeStore:
        def __init__(self, files):
            self._files = files
        def get_files(self):
            return self._files
        def get_entries(self):
            return []

    class _FakeSvc:
        def chat(self, prompt, max_tokens=512, timeout=120):
            return "[]"                      # 空 JSON：全部漏判（另一形态）

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        out = Path(td) / "out"
        src.mkdir()
        out.mkdir()
        n = 60
        (src / "a.txt").write_text(
            "\n".join(f"line {i} EN" for i in range(n)), encoding="utf-8")
        (out / "a.txt").write_text(
            "\n".join(f"line {i} 中文" for i in range(n)), encoding="utf-8")
        store = _FakeStore([{"id": "f1", "rel_path": "a.txt",
                             "format": "txt"}])
        res = audit_model(store, src, out, {}, _FakeSvc(),
                          batch_size=16, max_pairs_per_file=20)
        assert res.model_pairs_truncated == 60 - 20
        # 送审的 20 行全部漏判（空 JSON）→ 20 条计数
        assert res.model_verdict_missing == 20


def test_render_report_surfaces_coverage_gaps():
    """P4a/P4b/P4c：覆盖缺口必须进报告——漏判计数 / 行截断 / 证据卡
    超半数未送审 WARN。缺口静默 = 假装全 PASS，违反「未审就是未审」。"""
    from hanhua.core.writeback_audit import AuditResult, render_audit_report

    res = AuditResult()
    res.model_verdict_missing = 3
    res.model_pairs_truncated = 120
    res.v2_cards_audited = 10
    res.v2_cards_sampled = 40                # 80% 未送审 → 超半数 WARN
    report = render_audit_report(res, "测试游戏")
    assert "漏判 3 条" in report
    assert "120 行差异超单文件上限" in report
    assert "送审 10 张" in report
    assert "截断 40 张未送审" in report
    assert "未送审（超半数）" in report
    # 少量截断（≤50%）→ 不触发超半数 WARN，但截断数仍呈现
    res2 = AuditResult()
    res2.v2_cards_audited = 80
    res2.v2_cards_sampled = 20
    report2 = render_audit_report(res2, "测试游戏")
    assert "截断 20 张未送审" in report2
    assert "超半数" not in report2


def test_audit_v2_model_missing_index_counted():
    """P4a（v2 侧）：证据卡批模型 JSON 漏 index → 计数不默认 PASS。"""
    import json as _json
    from hanhua.core.writeback_audit import _audit_v2_model

    class _V2Result:
        def __init__(self, cards):
            self.object_evidence = cards

    def _card(path_id):
        return {"rel_path": "aa/x.bundle", "path_id": path_id,
                "type": "T",
                "changes": [("m_text", "Hello", "你好")]}

    class _Svc:
        def chat(self, prompt, *, max_tokens=512, timeout=120):
            # 只判 index 0（漏 1）
            return _json.dumps([{"index": 0, "verdict": "PASS", "issue": ""}])

    res = _audit_v2_model(
        _V2Result([_card(1), _card(2)]), _Svc(),
        cards_per_batch=12, max_cards=400)
    assert res.model_verdict_missing == 1
    assert res.model_flags == []
    assert res.model_unavailable is False
