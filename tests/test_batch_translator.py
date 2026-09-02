import json
import threading
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hanhua.core.batch_translator import BatchTranslator
from hanhua.core.memory import ProjectStore, settle_translation_memory
from hanhua.core.models import TextEntry, is_actionable_translation
from hanhua.core.translator import BaseClient, Usage


class FakeClient(BaseClient):
    """按原文映射翻译的假客户端。"""

    def __init__(self, mapping=None):
        self.mapping = mapping or {}
        self.calls = 0

    def chat(self, system, messages):
        self.calls += 1
        out = []
        for m in messages:
            for line in m["content"].splitlines():
                if '": ' in line:
                    kid, text = line.split('": ', 1)
                    kid = kid.strip().strip('"')
                    out.append({"id": kid, "translation": self.mapping.get(text, "译文")})
        return json.dumps(out, ensure_ascii=False), Usage(10, 5)


def _entries(n=60):
    return [{"file_id": "f", "key_path": f"k{i}", "original": f"text{i}"} for i in range(n)]


# ── #27：本地逐条模式进度口径 ─────────────────────────────────
# 用户实证：并发 1 槽没问题，但「每批 16 条设置，实际每批 2 条每批 2 条
# 显示，很奇怪」。根因：本地模型走 native 逐条路径（每条一次
# translate_text），「每批 N 条」只控制进度刷新粒度——活动流 delta 是
# 真实完成条数。设置 16 而每批显示 2 = 真实唯一文本就 2 条（去重扇出后
# 短批次）。
#
# 旧节流两个缺陷（本测试覆盖）：
# ① last_emit_ts 初始 0.0 → 首条完成时 now-0.0 恒 >1.5s（绝对时间）→
#    首条假 emit（delta=1），随后批内逐条（间隔 <1.5s）不触发，末条才
#    再 emit → 活动流只见「首条 + 全批」两条虚批。
# ② 按批次号（completed_representatives % batch_size==0）在同文分组/
#    短批下不触发 → 只靠 1.5s 时间兜底。
# 修后：last_emit_ts 初始化为当前时间（消假 emit）+ 按真实完成条数
# 累计 emit（done_since_emit >= batch_size 或 1.5s 或 末条）。

def test_native_progress_accumulates_real_items_not_batch_marks():
    _WORDS = ["Resume", "Settings", "Options", "Graphics", "Volume", "Credits"]

    class NativeClient:
        config = SimpleNamespace(timeout=120.0)

        def translate_text(self, source, _target_lang, _glossary):
            i = _WORDS.index(source)
            return f"译文{i}", Usage(1, 1)

    entries = _to_model([
        {"file_id": "f", "key_path": f"k{i}", "original": w,
         "meta": {"role": "display"}}
        for i, w in enumerate(_WORDS)
    ])
    progress = []

    # batch_size=16 > 唯一文本数 6：全部在 <1.5s 内完成 → 首条 emit 即
    # done=6（done_since_emit 到 16 才触发，6 条 < 16 由末条
    # completed==len 兜底）。重点：首条不再是假 emit done=1（旧
    # last_emit_ts=0.0 的绝对时间假阳性），而是完整真实进度。
    stats = BatchTranslator(
        NativeClient(), batch_size=16, concurrency=1,
        lang="en→zh-CN",
    ).run(entries, progress_cb=progress.append)

    assert stats.done == 6 and stats.failed == 0
    assert progress
    # 进度单调递增（finally 允许与末条同值，不得倒退）
    assert all(progress[i].done <= progress[i + 1].done
               for i in range(len(progress) - 1))
    # 首个 emit 反映真实完成数（6 条全在 1.5s 内 → 首条即 6；不得是
    # 旧逻辑的假 done=1）
    assert progress[0].done == 6
    assert progress[-1].done == 6


def test_native_progress_batch_size_1_emits_per_item():
    _WORDS = ["Resume", "Settings", "Options", "Graphics"]

    class NativeClient:
        config = SimpleNamespace(timeout=120.0)

        def translate_text(self, source, _target_lang, _glossary):
            return f"译文{_WORDS.index(source)}", Usage(1, 1)

    entries = _to_model([
        {"file_id": "f", "key_path": f"k{i}", "original": w,
         "meta": {"role": "display"}}
        for i, w in enumerate(_WORDS)
    ])
    progress = []

    stats = BatchTranslator(
        NativeClient(), batch_size=1, concurrency=1, lang="en→zh-CN",
    ).run(entries, progress_cb=progress.append)

    assert stats.done == 4
    # batch_size=1 → done_since_emit 每条 +1>=1 立即 emit（+finally 末条）
    assert len(progress) == 5
    # delta 序列逐条 +1（finally 末条 delta=0 不算新批）
    deltas = [p.done - (progress[i - 1].done if i else 0)
              for i, p in enumerate(progress)]
    assert deltas[:4] == [1, 1, 1, 1]
    assert deltas[4] == 0



@pytest.mark.parametrize(
    ("role", "disposition", "expected"),
    (("display", "structural", False),
     ("structural", "translate", True),
     ("display", "preserve", False)),
)
def test_actionability_uses_disposition_as_authoritative_scope(
        role, disposition, expected):
    entry = TextEntry(
        "f", "k", "Press E", meta={
            "role": role, "disposition": disposition, "confidence": "high",
        })
    assert is_actionable_translation(entry) is expected


def test_failed_entries_are_retried_on_next_run():
    # 质量门失败的条目不永久卡死：下次翻译会重试
    # （否则「质量门失败原因：untranslated_text N」统计残留，用户看到
    #  的就是翻译失败卡死的旧条目）
    entry = TextEntry(
        "f", "k", "Hello, my name is Mitch.", translation="Hello, my name is Mitch.",
        status="failed", meta={
            "role": "display", "disposition": "translate", "confidence": "high",
            "quality_passed": False, "quality_reasons": ["untranslated_text"],
        })
    assert is_actionable_translation(entry) is True

    client = FakeClient(mapping={"Hello, my name is Mitch.": "你好，我叫米奇。"})
    stats = BatchTranslator(
        client, batch_size=1, concurrency=1, lang="en→zh-CN",
    ).run([entry])
    assert client.calls == 1
    assert stats.done == 1 and stats.failed == 0
    assert entry.status == "translated"


def test_skipped_entries_never_reenter_run_scope():
    entry = TextEntry(
        "f", "k", "DOShakePosition: duration can't be 0", status="skipped",
        meta={"role": "display", "disposition": "structural",
              "confidence": "high"})
    assert is_actionable_translation(entry) is False


def _item_count(content: str) -> int:
    """条目数 = 以引号开头的行数（指令/标注行均不以引号开头）。"""
    return sum(1 for line in content.splitlines() if line.startswith('"'))


def _to_model(rows):
    from hanhua.core.models import TextEntry
    return [TextEntry(**r) for r in rows]


def test_batch_translator_all():
    bt = BatchTranslator(FakeClient(mapping={"text1": "文本一"}), batch_size=25, concurrency=2)
    entries = _to_model(_entries())
    stats = bt.run(entries)
    assert stats.total == 60 and stats.done == 60
    assert entries[1].translation == "文本一"
    assert all(e.status == "translated" for e in entries)
    assert stats.requests == 3
    assert stats.input_tokens == 30 and stats.output_tokens == 15


def test_progress_scope_excludes_structural_and_historical_entries():
    rows = [
        {
            "file_id": "code", "key_path": f"skip/{index}",
            "original": f"Method{index}", "status": "skipped",
            "meta": {"role": "structural", "confidence": "low"},
        }
        for index in range(1700)
    ]
    rows.extend(_entries(300))
    rows.append({
        "file_id": "ui", "key_path": "history/settings",
        "original": "Settings", "translation": "设置",
        "status": "translated", "meta": {"role": "display"},
    })
    progress = []

    stats = BatchTranslator(
        FakeClient(), batch_size=300, concurrency=1,
    ).run(_to_model(rows), progress_cb=progress.append)

    assert stats.total == 300
    assert stats.done == 300
    assert stats.failed == 0
    assert progress
    assert all(item.total == 300 for item in progress)
    assert progress[-1].done == 300


@pytest.mark.parametrize("batch_size", [1, 4])
def test_native_scheduler_never_multiplies_outer_and_inner_concurrency(
        batch_size):
    lock = threading.Lock()
    active = 0
    peak = 0

    class NativeClient:
        config = SimpleNamespace(timeout=120.0)

        def translate_text(self, source, _target_lang, _glossary):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return "译文", Usage(1, 1)

    stats = BatchTranslator(
        NativeClient(), batch_size=batch_size, concurrency=3,
    ).run(_to_model(_entries(12)))

    assert stats.done == 12
    assert peak == 3


@pytest.mark.parametrize(("source", "wrong"), [
    # 2026-08-14：内置 UI 词（Settings/Quit/…）已被确定性直填短路，
    # wrong-script 重试链改用非内置词验证
    ("Mission Briefing", "설정"),
    ("ゲーム設定", "ゲーム設定"),
    ("게임 설정", "設定です"),
    ("Mission Briefing", "设置 Настройки"),
    ("Mission Briefing", "设置 الإعدادات"),
    ("Mission Briefing", "设置 설정"),
    ("Mission Briefing", "设置 Menu"),
])
def test_chinese_target_retries_wrong_script_output(source, wrong):
    class WrongThenChinese:
        config = SimpleNamespace(timeout=120.0)
        calls = 0

        def translate_text(self, *_args):
            self.calls += 1
            return (wrong if self.calls == 1 else "游戏设置"), Usage(1, 1)

    client = WrongThenChinese()
    entry = _to_model([{
        "file_id": "ui", "key_path": "settings", "original": source,
        "meta": {"role": "ui", "disposition": "translate"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])

    assert client.calls == 2
    assert stats.done == 1 and entry.translation == "游戏设置"


@pytest.mark.parametrize(("source", "translation"), [
    (
        "A <#0080ff>simple</color> line of text.",
        "一条<#0080ff>简单的</color>文本行。",
    ),
    (
        "Please set the <b>API Key</b> in the <b>Gemini Dialogue</b> Game Object.",
        "请在<b>Gemini Dialogue</b>游戏对象中设置<b>API Key</b>。",
    ),
    (
        "You have selected link <#ffff00> ID 01",
        "您选择了链接 <#ffff00> ID 01",
    ),
    ("'X' to close", "按 X 键关闭"),
    ("Welcome {playerName}", "欢迎 {playerName}"),
    (
        "<b>WASD</b> - Movement\n<b>LMB</b> - Interact\n<b>RMB</b> - Focus/Zoom",
        "<b>WASD</b> - 移动\n<b>LMB</b> - 交互\n<b>RMB</b> - 聚焦/缩放",
    ),
    (
        "The Thirteenth Floor by Mike Lythgoe",
        "《第十三层》作者：Mike Lythgoe",
    ),
    ("A game by Comp-3 Interactive", "由 Comp-3 Interactive 制作的游戏"),
    ("Thanks to MC Mazzocchi for playtesting one of the first versions.",
     "感谢 MC Mazzocchi 对早期版本进行了测试。"),
    ("Thanks to MrPodunkian and Zizi for peering into the reasons my windows build wasn't working.",
     "感谢 MrPodunkian 和 Zizi，他们帮我弄清了为什么我的窗口构建无法正常运行的原因。"),
    ("<b>NVIDIA</b> graphics", "<b>NVIDIA</b> 显卡"),
    ("<b>STEAM</b> account", "绑定 <b>STEAM</b> 账户"),
    ("NVIDIA graphics", "NVIDIA 显卡"),
    ("STEAM account", "绑定 STEAM 账户"),
    ("SFX volume", "SFX 音量"),
    ("VFX quality", "VFX 质量"),
    # 真实语料：完美翻译保留专名/按键名/品牌，曾被误判 target_script_mismatch
    ("Escape exits the game. P will skip a scene instantly.",
     "Escape会退出游戏。P则会立即跳过当前场景。"),
    ("Thanks to MrPodunkian and Zizi for peering into the reasons "
     "my windows build was not working.",
     "感谢 MrPodunkian 和 Zizi，他们帮我明白了为什么我的 Windows "
     "构建过程无法正常运行的原因。"),
    ("Clips from youtube movies used in creating this game :",
     "用于制作此游戏的 YouTube 视频片段："),
    ("cbs intro", "CBS开场镜头"),
    ("Look Orbit X", "看看 Orbit X"),
    ("3D models used or modified for this game",
     "用于或修改用于此游戏的 3D 模型"),
    ("Sprite资源", "Sprite 资源"),
    ("UI_Title Screen", "UI_Title 屏幕"),
    ("Press Escape to open the menu", "按 Escape 打开菜单"),
])
def test_protected_target_script_spans(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True
    assert entry.meta["quality_passed"] is True


@pytest.mark.parametrize(("source", "translation"), [
    # 专名/标签回显（字母序列相同 + 无小写/词典词）→ target_script_mismatch 豁免
    ("[ S K I P ]", "[S K I P]"),
    ("AI", "AI"),
    ("AR", "AR"),
    ("3DI70R 2024", "3DI70R 2024"),
])
def test_proper_name_echo_not_target_script_mismatch(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True
    assert entry.meta["quality_passed"] is True


@pytest.mark.parametrize("fmt", [
    "yyyy-MM-dd HH:mm:ss.FFFFFF",
    "yyyy-MM-dd HH:mm:ss.FFFF",
    "HH:mm:ss",
])
def test_format_template_echo_not_target_script_mismatch(fmt):
    """fix-26 格式模板回显豁免 target_script_mismatch：日期/数字格式串
    是不可译文本，回显是正确行为（force-reboot 第三轮 3 条恒败实证；
    quality 侧 untranslated_text 已豁免，此处补目标脚本判定缺口）。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", fmt,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, fmt) is True
    assert entry.meta["quality_passed"] is True
    assert entry.meta["echo_exempt"] == "format_template"


def test_format_template_altered_by_model_is_restored():
    """fix-26 格式模板自愈：模型「修正」格式串（.ss→:ss）是格式破坏
    → 恢复原文（不可译文本，任何改动都破坏游戏时间格式），不得写回
    破坏版（force-reboot 实证 yyyy-MM-dd HH:mm.ss.FFFF）。"""
    source = "yyyy-MM-dd HH:mm.ss.FFFF"
    altered = "yyyy-MM-dd HH:mm:ss.FFFF"
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, altered) is True
    assert entry.translation == source
    assert entry.meta["quality_passed"] is True
    assert entry.meta["echo_exempt"] == "format_template"


def test_proper_name_echo_marked_exempt():
    """Q4 回归：回显豁免通过的条目必须打 echo_exempt 标（写回/统计可见
    它是「模型未翻译」而非真译文），且不得作为记忆源。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", "[ S K I P ]",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, "[S K I P]") is True
    assert entry.meta.get("echo_exempt") == "proper_name"


def test_echo_exempt_translation_not_added_to_memory():
    """Q4 回归：回显保留条目（译文=原文）不进记忆——防「原文→原文」
    无效记忆污染跨游戏一致性锚定。正常译文照常进记忆。"""
    import tempfile
    store = ProjectStore(Path(tempfile.mkdtemp()) / "echo.db")
    store.init_schema()
    echo_entry = _to_model([{
        "file_id": "ui", "key_path": "title/1",
        "original": "Crash Bandicoot",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]
    normal_entry = _to_model([{
        "file_id": "ui", "key_path": "menu/2",
        "original": "Open Door",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]
    client = FakeClient({
        "Crash Bandicoot": "Crash Bandicoot",   # 回显（专名保留合理）
        "Open Door": "打开门",
    })
    BatchTranslator(
        client, memory=store, model="m", lang="en→zh-CN",
        batch_size=1, concurrency=1,
    ).run([echo_entry, normal_entry])

    # 回显条目打标（写回/统计可见「模型未翻译」）
    assert echo_entry.meta.get("echo_exempt") == "proper_name"
    # 回显不产生记忆；正常译文进记忆（Phase B：翻译批入 pending 桶，
    # 审后结算（无审核终态 → 机械门即最后裁决）才提交可见）
    assert "Crash Bandicoot" not in store.get_memory_hits(
        ["Crash Bandicoot"], "m", "en→zh-CN")
    settle_translation_memory(store, [echo_entry, normal_entry],
                              "m", "en→zh-CN")
    assert "Crash Bandicoot" not in store.get_memory_hits(
        ["Crash Bandicoot"], "m", "en→zh-CN")
    assert store.get_memory_hits(["Open Door"], "m", "en→zh-CN") == {
        "Open Door": "打开门",
    }


def test_uppercase_action_echo_fails_by_knowledge_rule():
    """知识库规则：全大写动作指令（TOSS TRASH）回显一律判失败。

    taxes 实证：TOSS TRASH 曾因 proper_name_echo 豁免而漏翻回显。
    大写动作指令是可翻译语义文本，回显 = untranslated_text，不得豁免。
    """
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", "TOSS TRASH",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, "TOSS TRASH") is False
    assert "untranslated_text" in entry.meta["quality_reasons"]


@pytest.mark.parametrize(("source", "translation"), [
    # 模型正确保留的专名载体不算英文残留（v3 失败样本误伤修复）：
    # 3+ 段路径、域名、@用户名、版本号、@ 显示名
    ("Screenshot saved to User/Blah/Hey/HotelParadiseScreenshot 90909090",
     "截图保存在 User/Blah/Hey/HotelParadiseScreenshot 90909090 目录下。"),
    ("(Only savefiles from 0.4.0beta are compatible)",
     "仅 0.4.0beta 版本的存档文件才兼容。"),
    ("game by fie (@zkfie)", "游戏由 fie (@zkfie) 制作"),
    ("Let us know in the comments on itch.io what you'd like to see",
     "请在 itch.io 的评论区告诉我们，您希望在完整游戏中看到什么内容！"),
    ("3D Models & additional assets from\nUnity Asset Store & OpenGameArt.com",
     "3D模型及来自……的其他资源\nUnity Asset Store & OpenGameArt.com"),
    ("@SoftdevWu", "@SoftdevWu"),
])
def test_safe_keeper_spans_are_not_target_script_mismatch(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True
    assert entry.meta["quality_passed"] is True


@pytest.mark.parametrize(("source", "translation"), [
    # 带重音拉丁字母的专名（alisa-demo [6] 法语设备名 Pulsomètre）：
    # _ENGLISH_WORD 纯 ASCII 会把 "Pulsomètre" 拆成 "Pulsom"+"tre" 碎片，
    # "tre" 是小写普通词 → 归一化后成完整词走 TitleCase 专名豁免
    # （译文已含中文翻译）
    ("J'ai emprunté le Pulsomètre pour un moment.",
     "我暂时借用了Pulsomètre这个设备使用。"),
    ("Dr. Edminston", "埃德明斯顿博士。"),
    # 原文重音词完整进 source_terms（防碎片漏检）
    ("Chiave di Ferro", "铁钥匙"),
])
def test_accented_proper_name_not_target_script_mismatch(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True
    assert entry.meta["quality_passed"] is True


def test_accented_word_fragments_still_target_script_mismatch():
    """重音碎片修复不放松真残留：独立小写词残留仍判失败。

    归一化只把「重音词」合成完整词；真正的英文残留（小写普通词）
    不受影响（alisa-demo 意语段 'Ve ne preghiamo' 的英语回显仍拦截）。
    """
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", "Ve ne preghiamo, qualcuno, faccia qualcosa!",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(
        entry, "我们恳求某人做点什么！ please 帮忙") is False


@pytest.mark.parametrize(("source", "translation"), [
    # 日文专名回显（VTuber 频道名，proper_name_echo 也豁免日文脚本）：
    # 字母序列相同 + 无小写词 → 保留原文合理
    ("Korone Ch. 戌神ころね", "Korone Ch. 戌神ころね"),
])
def test_japanese_proper_name_echo_is_allowed(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True


@pytest.mark.parametrize(("source", "translation"), [
    # 真半翻仍失败：普通小写词保留（非 @/域名/路径/版本号）
    ("Adjust ram pressure", "调整 ram 压力"),
    ("Open the steam valve", "打开 steam 阀门"),
    ("ragdoll count", "ragdoll 计数"),
    # F16：3+ 连续辅音含 j/q/z/k 的乱串/真词边界——length 的 ngth、
    # spring 的 spr 是真实词组合（含 s 开头合法连缀），不得豁免
    ("Adjust spring pressure", "调整 spring 压力"),
    ("Change the length", "改变 length"),
])
def test_common_word_leftovers_still_target_script_mismatch(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is False


@pytest.mark.parametrize(("source", "translation"), [
    # F16-A 连字符专名段：Loam-arino 的 arino 是专名的一部分（doubleshake
    # 实证 'Hi, Loam-arino!' 译文保留 arino 被判 target_script_mismatch）
    ("Howdy, Loam-arino! Is there anything I can help you with?",
     "嗨，Loam-arino！有什么我可以帮助你的吗？"),
    # F16-B 测试噪音块子串：asd ⊂ asdasdasdasd（重复 3-gram 乱串块），
    # 模型保留噪音段是正确行为（doubleshake 测试文本实证）
    ("asd\nasdasdasdasd\nfiller text", "asd\nasdasdasdasd\n填充文本"),
])
def test_noise_and_hyphen_proper_names_allowed(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True


@pytest.mark.parametrize(("source", "translation"), [
    # F16-B 罕见辅音连缀乱串：ksjdh（含 j/k）是英语没有的辅音组合
    # （doubleshake aksjdhashd 实证）→ 保留乱串豁免；真词组合
    # （spring 的 spr、length 的 ngth）不含 j/q/z/k 照常判失败
    ("Come to Caliko Coast!!!!\naksjdhashd\nasdlajsdhasjkdh",
     "快来卡利科海岸吧！！！\naksjdhashd\nasdlajsdhasjkdh"),
])
def test_rare_consonant_run_noise_allowed(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True


@pytest.mark.parametrize(("source", "translation"), [
    # 破折号后署名位小写名（resonance-of-the-ocean 译者署名 yamur）→ 允许
    ("Turkish Localization - yamur <3", "土耳其语本地化 – yamur <3"),
    # 文件扩展名（spolous 真实样本：SPOLOUS.exe 保留）
    ("「SPOLOUS.exe」をダブルクリックすれば、ゲームがスタートします。",
     "双击“SPOLOUS.exe”即可启动游戏。"),
    # 代码标记（the-supper 真实样本：变量管理器语法 [var:ID]）
    ("Sets the value of both Global and Local Variables, as declared in the "
     "Variables Manager. Integers can be set to absolute, incremented or "
     "assigned a random value. Strings can also be set to the value of a "
     "MenuInput element; Integers, booleans and floats can be set to Mecanim "
     "parameter values. When setting integers and floats, a formula can be "
     "entered, e.g. 2 + 3 * 4. Formulas can contain [var:ID] tags that "
     "represent the value of the variable, where ID is the unique number "
     "assigned to the variable in the Variables Manager.",
     "它用于设置全局变量和局部变量的值，这些变量是在变量管理器中声明的。"
     "整数可以被设置为绝对值、递增值或随机值。字符串也可以被设置为 "
     "MenuInput 元素的值；而整数、布尔值和浮点数则可以被设置为 Mecanim "
     "参数的值。在设置整数和浮点数时，还可以输入公式（例如 2 + 3 * 4），"
     "公式中可以包含 [var:ID] 这样的标记，用来表示变量的值，其中 ID "
     "是变量管理器中为该变量分配的唯一编号"),
])
def test_proper_name_carriers_and_signatures_are_allowed(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True


@pytest.mark.parametrize(("source", "translation"), [
    # 无破折号的普通小写残留仍失败（签名豁免不适用）
    ("Turkish Localization - yamur <3", "Turkish Localization - yamur <3"),
    ("press any key", "Press any key"),
    # 首行英文词超过 2 个（问候 + 其他）→ 问候豁免不适用，仍失败
    ("press any key", "Hello, press any key 世界"),
])
def test_signature_echo_without_chinese_still_fails(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is False


@pytest.mark.parametrize(("source", "translation"), [
    # 问候行豁免（mimic-search 真实样本）：译文首行保留英文问候，
    # 其余已译为中文 → 本地化惯例，允许
    ("Hello,\n\n\nA few hours ago we received an anonymous phone call "
     "about a missing person.",
     "Hello,\n\n\n几小时前，我们接到了一个关于失踪人员的匿名电话。"),
    # 问候行豁免（soul-delivery 真实样本）：Hello, there. 双词问候
    ("Hello, there\n\nI've been working really hard on this game "
     "for the past 6 months",
     "Hello, there.\n\n在过去的6个月里，我一直在努力完善这款游戏"),
])
def test_greeting_first_line_allowed_when_rest_is_chinese(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True


@pytest.mark.parametrize(("source", "translation"), [
    # rich-text 包裹的作者名（slendergus 真实样本：<b>lucd</b> 高亮作者名 +
    # lucd#9569 Discord id，credit 行其余已译中文）→ 允许
    ("\n<color=#FFD700><b>lucd</b></color> - Creator \n(lucd#9569)\n\n"
     "<color=#00FFFF>Gardok</color> -\n pages texture and logo\n\n"
     '<color=#FFA500>RudyRudys</color> - \ndoor model\n\n'
     '<color="red">MRBYE</color> - \ngame ',
     "\n<color=#FFD700><b>lucd</b></color> – 创作者\n(lucd#9569)\n\n"
     "<color=#00FFFF>加多克</color> -\n页面纹理和徽标\n\n"
     "<color=#FFA500>RudyRudys</color> -\n门型号\n\n"
     '<color="red">MRBYE</color> -\n游戏'),
])
def test_rich_text_wrapped_proper_name_allowed_when_rest_is_chinese(
        source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True


@pytest.mark.parametrize(("source", "translation"), [
    # 回显仍判失败：UI 词典多字母词（QUIT）或小写词（Hello world）；
    # 全大写 ≤3 缩写（SFX）走缩写豁免（见 test_vsync_echo_passes_
    # proper_name_echo）
    ("QUIT", "QUIT"),
    ("Hello world", "Hello world"),
])
def test_real_echo_still_target_script_mismatch(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is False


@pytest.mark.parametrize(("source", "translation"), [
    ("Ahoy matey", "Hey there!"),
    ("Settings", "设置 Menu"),
    ("Press E to Open", "按 E 键 Open"),
    ("SETTINGS", "设置 SETTINGS"),
    ("WELCOME HOME", "欢迎 WELCOME HOME"),
    ("<b>SETTINGS</b>", "<b>设置 SETTINGS</b>"),
    ("Open the steam valve", "打开 steam 阀门"),
    ("An epic battle", "一场 epic 战斗"),
    ("Adjust ram pressure", "调整 ram 压力"),
    # 按键名豁免不适用：除 Escape 外还有残留词 → 仍判失败
    ("Press Escape to open", "按 Escape 键打开 Open"),
])
def test_protected_target_script_does_not_allow_semantic_english(
        source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported-invalid", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is False
    assert entry.meta["quality_passed"] is False


@pytest.mark.parametrize(("source", "translation"), [
    # TitleCase 专名短语中的 UI 词典词（baldis 真实样本：游戏名
    # 《Baldi's Fun New School Remastered》 的 New 命中 UI 词典）→ 允许
    ("<size=50><color=green><u>WELCOME TO THE GAME CONTROLLER SETUP!"
     "</u></color></size>\n\nThis Setup Will Help You With Getting A Game "
     "Controller Connected Via <color=blue>Bluetooth</color> To Play "
     "Baldi's Fun New School Remastered.",
     "<size=50><color=green><u>欢迎使用游戏控制器设置程序！</u></color></size>"
     "\n\n此设置将帮助您通过 <color=blue>蓝牙</color> 连接游戏控制器，"
     "以便能够玩《Baldi's Fun New School Remastered》这款游戏。"),
])
def test_title_case_ui_word_inside_proper_name_phrase_allowed(
        source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True


@pytest.mark.parametrize(("source", "translation"), [
    # driftapocalypse 真实样本：日志串中 Play Games Plugin 插件专名的
    # Play 是品牌词（Google Play Games），模型保留正确；DateTime.Now 是
    # .NET API 名（驼峰豁免）。F18 修复前 Play 是 UI 词典词且在短语
    # 开头（left_title=False）→ 误判 target_script_mismatch
    ("*** [Play Games Plugin 0.10.12] ERROR: Failed to format DateTime.Now",
     "[Play Games Plugin 0.10.12] 错误：无法格式化 DateTime.Now"),
    # 服务短语（Play Store）语义层已剥除 → 天然豁免
    ("Play Store", "请查看 Play Store 评分"),
])
def test_brand_ui_word_in_proper_phrase_allowed(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True


@pytest.mark.parametrize(("source", "translation"), [
    # eyeless-jack 真实样本：Pixabay 音乐作者用户名列表（下划线连接
    # 标识符形态），模型翻译主体+保留用户名正确。F20 修复前 Music
    # （UI 词典词）在词序末尾被判英文残留 → 误杀 target_script_mismatch
    ("MUSIC<br>All music is from the following Pixabay Users:<br>"
     "UNIVERSFIELD<br>Tim_Kulig_Free_Music<br>Eremit_der_Schatten<br>"
     "Brotheration_Records",
     "音乐<br>所有音乐均来自以下 Pixabay 用户：<br>UNIVERSFIELD<br>"
     "Tim_Kulig_Free_Music<br>Eremit_der_Schatten<br>"
     "Brotheration_Records"),
])
def test_underscore_identifier_words_allowed(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True


def test_underscore_identifier_does_not_mask_real_half_translation():
    # 对照：真半翻（中文+英文句子残留）不受下划线豁免影响
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", "Open the file",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, "打开 Open the file") is False


@pytest.mark.parametrize(("source", "translation"), [
    # 短组合漏翻回显：Play Button 是「播放按钮」不是专名 → 仍判失败
    ("Play Button", "播放 按钮 Play Button"),
    ("Play Button", "Play Button"),
    # 全词典词 TitleCase 序列漏翻回显 → 仍判失败（无非词典专名词）
    ("Play Settings Resume", "Play Settings Resume"),
])
def test_ui_word_short_proper_combo_still_fails(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is False


def test_lowercase_article_inside_proper_name_phrase_allowed():
    # eggs-for-bart 真实样本 credit 页（完整 2294 字符原文 + 1558 字符译文）：
    # 'Darth-artisan on the\nUnity Asset Store' 的 the 在语义剥离后的专名
    # 序列中 → 允许。构造最小用例会被 credit 术语剥离干扰，故用完整样本
    sample = json.loads(
        (Path(__file__).parent / "fixtures" / "eggs-for-bart-credit-page.json")
        .read_text(encoding="utf-8"))
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", sample["original"],
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, sample["translation"]) is True


@pytest.mark.parametrize(("source", "translation"), [
    # 小写冠词不夹在 TitleCase 词之间（真实半翻）→ 仍判失败
    ("Press the button to continue", "按下 the button 继续"),
    ("The End is near", "这是 the End 的开始"),
    # 孤立 UI 词典词半翻 → 仍判失败
    ("Save game", "保存 Save 游戏"),
])
def test_isolated_lowercase_word_inside_chinese_still_fails(
        source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is False


@pytest.mark.parametrize(("source", "translation"), [
    # 驼峰技术缩写（VSync）→ 界面标准术语，保留原文合理（vincent 真实样本）
    ("VSync: OFF", "VSync：关闭"),
])
def test_technical_camel_case_term_preserved_allowed(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True


@pytest.mark.parametrize("source", [
    # lorem ipsum 占位文本（开发者填充的假拉丁文本）→ 回显是合理行为
    # （zero-deaths 真实样本，'Loem iipsum solar' 是错拼变体）
    "Loem iipsum solar",
    "Loem iipsum solar em demit solo demmy sorenson.",
    "Lorem ipsum dolor sit amet",
])
def test_lorem_ipsum_placeholder_echo_allowed(source):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, source) is True


def test_non_ascii_proper_name_echo_allowed():
    # zero-deaths 真实样本：'Stefánsson' 的 á 会让 _ENGLISH_WORD（ASCII）
    # 拆出 'nsson' 小写碎片 → 旧判定误拦；独立小写词检查应豁免专名回显
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", "Sir Stefán Karl Stefánsson",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, "Sir Stefán Karl Stefánsson") is True


@pytest.mark.parametrize("source", [
    # 品牌纯串：模型保留原文合理，不应判 target_script_mismatch
    "Playstation",
    "Xbox",
    "NVIDIA",
])
def test_quality_allows_brand_only_source_kept_as_is(source):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, source) is True


def test_chinese_target_does_not_exempt_wrong_script_after_proper_name():
    class MixedProperNameClient:
        config = SimpleNamespace(timeout=120.0)
        calls = 0

        def translate_text(self, *_args):
            self.calls += 1
            return "爱丽丝 설정", Usage(1, 1)

    client = MixedProperNameClient()
    entry = _to_model([{
        "file_id": "dialogue", "key_path": "speaker/name",
        "original": "Alice",
        "meta": {"role": "proper_name", "disposition": "translate"},
    }])[0]

    stats = BatchTranslator(
        client, batch_size=1, concurrency=1, lang="en→zh-CN",
    ).run([entry])

    assert client.calls == 1
    assert stats.failed == 1
    assert entry.quality_reasons == ("target_script_mismatch",)


def test_chinese_target_glossary_allowance_uses_source_token_boundaries():
    class WrongThenChinese:
        config = SimpleNamespace(timeout=120.0)
        calls = 0

        def translate_text(self, *_args):
            self.calls += 1
            return ("开始 Menu" if self.calls == 1 else "开始"), Usage(1, 1)

    client = WrongThenChinese()
    entry = _to_model([{
        "file_id": "ui", "key_path": "start", "original": "Start",
        "meta": {"role": "ui", "disposition": "translate"},
    }])[0]

    stats = BatchTranslator(
        client, batch_size=1, concurrency=1, lang="en→zh-CN",
        glossary=[("art", "Menu")],
    ).run([entry])

    assert client.calls == 2
    assert stats.done == 1 and entry.translation == "开始"


def test_chinese_target_allows_applied_latin_glossary_target():
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN",
        glossary=[("Moon Key", "MoonKey")],
    )
    entry = TextEntry(
        "ui", "item", "Use the Moon Key",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, "使用 MoonKey") is True


def test_chinese_target_accepts_cjk_extension_ideograph():
    class ExtensionIdeographClient:
        config = SimpleNamespace(timeout=120.0)
        calls = 0

        def translate_text(self, *_args):
            self.calls += 1
            return "𠀀", Usage(1, 1)

    client = ExtensionIdeographClient()
    entry = _to_model([{
        "file_id": "ui", "key_path": "rare", "original": "Rare",
        "meta": {"role": "ui", "disposition": "translate"},
    }])[0]

    stats = BatchTranslator(
        client, batch_size=1, concurrency=1, lang="en→zh-CN",
    ).run([entry])

    assert client.calls == 1
    assert stats.done == 1 and entry.translation == "𠀀"


def test_local_single_item_fallback_accepts_plain_translation():
    class PlainLocalClient:
        accepts_plain_single = True
        config = SimpleNamespace(timeout=120.0)

        def chat(self, _system, _messages):
            return "按 E 键打开", Usage(8, 4)

    entry = _to_model([{
        "file_id": "ui", "key_path": "prompt/open",
        "original": "Press E to open", "meta": {"role": "display"},
    }])[0]

    stats = BatchTranslator(
        PlainLocalClient(), batch_size=1, concurrency=1,
    ).run([entry])

    assert stats.done == 1 and stats.failed == 0
    assert entry.translation == "按 E 键打开"


def test_request_error_keeps_detail_for_diagnosis():
    class RaisingClient:
        def chat(self, _system, _messages):
            raise RuntimeError("HTTP 404: endpoint mismatch")

    entry = _to_model([{
        "file_id": "ui", "key_path": "prompt/open",
        "original": "Press E to open", "meta": {"role": "display"},
    }])[0]

    stats = BatchTranslator(
        RaisingClient(), batch_size=1, concurrency=1,
    ).run([entry])

    assert stats.failed == 1
    assert entry.quality_reasons == ("request_error",)
    assert json.loads(entry.meta["request_error_detail"]) == {
        "type": "RuntimeError", "status": None,
        "message": "HTTP 404: endpoint mismatch",
    }


def test_cancellation_stops_scheduling_pending_batches():
    cancelled = threading.Event()

    class CancellingClient(FakeClient):
        def chat(self, system, messages):
            result = super().chat(system, messages)
            cancelled.set()
            return result

    client = CancellingClient()
    entries = _to_model(_entries(12))
    stats = BatchTranslator(
        client, batch_size=1, concurrency=1,
        cancellation_event=cancelled,
    ).run(entries)

    assert client.calls == 1
    assert stats.done == 0 and stats.failed == 0
    assert all(entry.status == "pending" for entry in entries)


def test_request_error_detail_redacts_credentials_and_bodies():
    secret = "sk-raw-secret"

    class ConfiguredClient(FakeClient):
        config = type("Config", (), {"api_key": secret})()

        def chat(self, _system, _messages):
            raise RuntimeError(
                f"Authorization: Bearer {secret} "
                f"https://user:{secret}@host/path?api_key={secret} "
                f'body={{"token":"{secret}"}}')

    entry = _to_model(_entries(1))[0]
    BatchTranslator(ConfiguredClient(), batch_size=1).run([entry])
    detail = entry.meta["request_error_detail"]
    diagnostic = json.loads(detail)

    assert set(diagnostic) == {"type", "status", "message"}
    assert secret not in detail
    assert "Authorization" not in diagnostic["message"]
    assert "api_key=" not in diagnostic["message"]
    assert "token" not in diagnostic["message"]


def test_native_local_translation_bypasses_json_batch_prompt_for_multiline_text():
    class NativeLocalClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, source, target_lang, glossary):
            self.calls.append((source, target_lang, tuple(glossary)))
            translations = {
                "Key30\nG - to throw\n": "Key30\nG – 投掷",
                "Operator: Flabby Pizza. Human Resources.\n":
                    "操作员：Flabby Pizza。人力资源部。",
            }
            return translations[source], Usage(20, 8)

        def chat(self, _system, _messages):
            raise AssertionError("native local translation must not use JSON chat")

    client = NativeLocalClient()
    entries = _to_model([
        {"file_id": "level3", "key_path": "prompt/throw",
         "original": "Key30\nG - to throw\n", "meta": {"role": "display"}},
        {"file_id": "level3", "key_path": "dialogue/hr",
         "original": "Operator: Flabby Pizza. Human Resources.\n",
         "meta": {"role": "display"}},
    ])

    stats = BatchTranslator(
        client, batch_size=8, concurrency=1,
        lang="auto→zh-CN", glossary=[
            ("throw", "投掷"), ("Key30", "Key30"),
            ("Flabby Pizza", "Flabby Pizza"),
        ],
    ).run(entries)

    assert stats.done == 2 and stats.failed == 0 and stats.requests == 2
    assert entries[0].translation == "Key30\nG – 投掷\n"
    assert len(client.calls) == 2
    assert all(call[1] == "zh-CN" for call in client.calls)


def test_native_multiline_mismatch_repairs_segments_and_exact_delimiters_once():
    class CollapsingClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, source, _target_lang, _glossary):
            self.calls.append(source)
            translations = {
                "\r\nSettings\r\n\r\n{0}kg\\n£{1:0.00}\r\n":
                    "设置\r\n{0}千克£{1:0.00}",
                "Settings": "设置",
                "{0}kg": "{0}千克",
                "£{1:0.00}": "£{1:0.00}",
            }
            return translations[source], Usage(5, 2)

    source = "\r\nSettings\r\n\r\n{0}kg\\n£{1:0.00}\r\n"
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/settings",
        "original": source, "meta": {"role": "ui"},
    }])[0]
    client = CollapsingClient()

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 1 and stats.failed == 0
    assert entry.translation == "\r\n设置\r\n\r\n{0}千克\\n£{1:0.00}\r\n"
    assert client.calls == [source, "Settings", "{0}kg", "£{1:0.00}"]
    assert stats.requests == 4


def test_native_echo_repair_splits_long_paragraph_into_sentences():
    """长单段文本超出 ctx 时模型回显原文（untranslated_text）→ 拆句翻译拼接。

    这是后半段失败的稳定形态：长 prompt + 大输出被 clamp/截断后模型直接
    回显原文。短句回显概率极低，拆句逐段翻译后拼接再校验。
    """
    original = ("A long paragraph about the world that the model will echo "
                "back verbatim. It has many sentences inside it. And the "
                "last sentence ends it here.")
    sentences = {
        "A long paragraph about the world that the model will echo back "
        "verbatim.": "关于世界的长段落。",
        "It has many sentences inside it.": "里面有很多句子。",
        "And the last sentence ends it here.": "最后一句话到此结束。",
    }

    class EchoingParagraphClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, source, _target_lang, _glossary):
            self.calls.append(source)
            if source == original:
                return original, Usage(10, 2)   # 回显原文 → untranslated_text
            return sentences[source], Usage(3, 2)

    client = EchoingParagraphClient()
    entry = _to_model([{
        "file_id": "text", "key_path": "story/paragraph",
        "original": original, "meta": {"role": "display"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 1 and stats.failed == 0
    # 句间空白（原文标点后空格）忠实保留
    assert entry.translation == ("关于世界的长段落。 里面有很多句子。 "
                                 "最后一句话到此结束。")
    assert client.calls == [
        original, "A long paragraph about the world that the model will "
                  "echo back verbatim.",
        "It has many sentences inside it.",
        "And the last sentence ends it here.",
    ]


def test_native_multiline_repair_precedes_actionable_ui_retry():
    class CollapsingUiClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, source, _target_lang, _glossary):
            self.calls.append(source)
            translations = {
                "Settings\nApply": "Settings Apply",
                "Settings": "设置",
                "Apply": "应用",
            }
            return translations[source], Usage(5, 2)

    client = CollapsingUiClient()
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/settings-apply",
        "original": "Settings\nApply",
        "meta": {"role": "ui", "disposition": "translate"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 1 and stats.failed == 0 and stats.requests == 3
    assert entry.translation == "设置\n应用"
    assert client.calls == ["Settings\nApply", "Settings", "Apply"]


def test_slot_repair_preserves_rich_text_newlines_and_inputs():
    source = "<b>Settings</b>\nPress E to Open {0}"

    class BreakingThenSegmentClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, text, _target_lang, _glossary):
            self.calls.append(text)
            translations = {
                source: "Settings 按 E 打开",
                "Settings": "设置",
                "Press": "按",
                "to Open": "以打开",
            }
            return translations[text], Usage(5, 2)

    client = BreakingThenSegmentClient()
    entry = TextEntry(
        "ui", "menu/slot-repair", source,
        meta={"role": "ui", "disposition": "translate"},
    )

    stats = BatchTranslator(
        client, batch_size=1, concurrency=1, lang="en→zh-CN").run([entry])

    assert stats.done == 1 and stats.failed == 0
    assert entry.translation == "<b>设置</b>\n按 E 以打开 {0}"
    assert entry.meta["quality_passed"] is True
    assert client.calls == [source, "Settings", "Press", "to Open"]


def test_slot_repair_handles_untranslated_semantics_around_input_token():
    source = "Press RMB to attack"

    class PartialThenSegmentClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, text, _target_lang, _glossary):
            self.calls.append(text)
            return {
                source: "按 RMB to attack",
                "Press": "按",
                "to attack": "以攻击",
            }[text], Usage(5, 2)

    client = PartialThenSegmentClient()
    entry = TextEntry(
        "ui", "prompt/attack", source,
        meta={"role": "display", "disposition": "translate"},
    )

    stats = BatchTranslator(
        client, batch_size=1, concurrency=1, lang="en→zh-CN").run([entry])

    assert stats.done == 1 and stats.failed == 0
    assert entry.translation == "按 RMB 以攻击"
    assert client.calls == [source, "Press", "to attack"]


def test_chat_slot_repair_preserves_rich_text_newlines_and_inputs():
    source = "<b>Settings</b>\nPress E to Open {0}"

    class BreakingThenSegmentChatClient:
        def __init__(self):
            self.calls = 0

        def chat(self, _system, _messages):
            translations = ["Settings 按 E 打开", "设置", "按", "以打开"]
            translation = translations[self.calls]
            self.calls += 1
            return json.dumps([{
                "id": "menu/chat-slot-repair@ui",
                "translation": translation,
            }], ensure_ascii=False), Usage(5, 2)

    client = BreakingThenSegmentChatClient()
    entry = TextEntry(
        "ui", "menu/chat-slot-repair", source,
        meta={"role": "ui", "disposition": "translate"},
    )

    stats = BatchTranslator(
        client, batch_size=1, concurrency=1, lang="en→zh-CN").run([entry])

    assert stats.done == 1 and stats.failed == 0 and stats.requests == 4
    assert entry.translation == "<b>设置</b>\n按 E 以打开 {0}"
    assert entry.meta["quality_passed"] is True
    assert client.calls == 4


@pytest.mark.parametrize("delimiter", ["\n", "\r\n", r"\n"])
def test_native_multiline_repair_restores_empty_segment_topology(delimiter):
    # 用真显示词（空行拓扑的空行由调度/重建逻辑处理，与语义无关）
    source = delimiter.join(("Alpha", "", "Bravo", "Charlie"))
    segments = {"Alpha": "甲", "Bravo": "乙", "Charlie": "丙"}

    class MovingBlankLineClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self, source):
            self.source = source
            self.calls = []

        def translate_text(self, source, _target_lang, _glossary):
            self.calls.append(source)
            if source == self.source:
                return delimiter.join(("甲", "乙", "", "丙")), Usage(5, 2)
            return segments[source], Usage(5, 2)

    client = MovingBlankLineClient(source)
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/topology",
        "original": source, "meta": {"role": "ui"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 1 and stats.failed == 0 and stats.requests == 4
    assert entry.translation == delimiter.join(("甲", "", "乙", "丙"))
    assert client.calls == [source, "Alpha", "Bravo", "Charlie"]


def test_native_multiline_repair_fails_when_a_meaningful_segment_stays_empty():
    class DroppingClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, source, _target_lang, _glossary):
            self.calls.append(source)
            translations = {
                "First\nSecond\nThird": "第一\n\n第三",
                "First": "第一",
                "Second": "",
            }
            return translations[source], Usage(5, 2)

    entry = _to_model([{
        "file_id": "dialogue", "key_path": "line/three",
        "original": "First\nSecond\nThird", "meta": {"role": "display"},
    }])[0]
    client = DroppingClient()

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 0 and stats.failed == 1
    assert entry.quality_reasons == ("line_content_mismatch",)
    assert client.calls == ["First\nSecond\nThird", "First", "Second"]
    assert stats.requests == 3


def test_native_line_merge_fallback_releases_after_repair_echoes_first_line():
    """baldis [6] 实证：'Error please contact game owner\\nand check log.'
    首译合并为一行中文（newline_mismatch + line_content_mismatch）→
    multiline repair 逐行重译时首行被模型回显英文（target_script_mismatch）
    → 修复失败后必须恢复首译失败状态，换行合并兜底才能基于首译判定
    放行（此前 repair 的复查覆盖 quality_reasons，兜底 ≤ 集合条件失准
    → 语义完整首译被卡死恒败）。"""
    class EchoFirstLineClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, source, _target_lang, _glossary):
            self.calls.append(source)
            translations = {
                "Error please contact game owner\nand check log.":
                    "出现错误，请联系游戏所有者，并查看日志信息。",
                "Error please contact game owner":
                    "Error, please contact the game owner.",
                "and check log.": "并查看日志信息。",
            }
            return translations[source], Usage(5, 2)

    entry = _to_model([{
        "file_id": "dialogue", "key_path": "line/error",
        "original": "Error please contact game owner\nand check log.",
        "meta": {"role": "display"},
    }])[0]
    client = EchoFirstLineClient()

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 1 and stats.failed == 0
    assert entry.translation == "出现错误，请联系游戏所有者，并查看日志信息。"
    assert entry.meta["line_merged"] is True
    assert client.calls == [
        "Error please contact game owner\nand check log.",
        "Error please contact game owner", "and check log.",
    ]


def test_word_residue_reference_retry_translates_echoed_lowercase_phrase():
    """baldis 'outstanding citizen' 实证：模型对纯小写普通词整句回显
    （untranslated_text）→ 词级补译裸翻译仍回显 → 逐词引用两跳
    （outstanding→outstanding, citizen→citizen）→ 模型引用后直译
    '杰出公民'（实测：裸→回显 / 引用→杰出公民）。"""
    class EchoingClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, text, _target_lang, glossary):
            self.calls.append((text, tuple(glossary)))
            if text == "outstanding citizen":
                if any(pair == ("outstanding", "outstanding")
                       for pair in glossary):
                    return "杰出公民", Usage(5, 2)
                return "outstanding citizen", Usage(5, 2)
            return "译文", Usage(5, 2)

    entry = _to_model([{
        "file_id": "dialogue", "key_path": "line/outstanding",
        "original": "outstanding citizen",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]
    client = EchoingClient()

    stats = BatchTranslator(
        client, batch_size=1, concurrency=1, lang="en→zh-CN").run([entry])

    assert stats.done == 1 and stats.failed == 0
    assert entry.translation == "杰出公民"
    assert len(client.calls) == 3
    # 第三次调用注入 (outstanding, outstanding) 词对引用
    assert any(pair == ("outstanding", "outstanding")
               for pair in client.calls[-1][1])


def test_native_actionable_ui_uses_builtin_references_and_retries_only_once():
    class EchoThenTranslateClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, source, target_lang, glossary):
            self.calls.append((source, target_lang, tuple(glossary)))
            result = "PAUSE MENU" if len(self.calls) == 1 else "暂停菜单"
            return result, Usage(5, 2)

    client = EchoThenTranslateClient()
    # 2026-08-14：内置 UI 词（QUIT）已被确定性直填短路，回显重试链
    # 改用非内置词验证
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/pause", "original": "PAUSE MENU",
        "meta": {"role": "ui", "disposition": "translate"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 1 and stats.failed == 0 and stats.requests == 2
    assert entry.translation == "暂停菜单"
    assert len(client.calls) == 2
    expected = {
        ("Settings", "设置"), ("Quit", "退出"),
        ("Resolution", "分辨率"), ("SFX", "音效"),
        ("Volume", "音量"), ("Resume", "继续"),
    }
    assert expected <= set(client.calls[0][2])
    assert client.calls[0][2] == client.calls[1][2]


def test_native_actionable_retry_on_input_token_loss():
    # deadbeat 真实样本：'tab : config' 按键被模型翻译成 '标签：配置'（丢按键）
    # → input_token_mismatch 触发 protected repair（剥离按键段 → 单独翻译
    #   ': config' → 回填按键前缀）；第二次保留按键 → 成功
    class TabThenFixClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, source, target_lang, glossary):
            self.calls.append((source, target_lang, tuple(glossary)))
            return ("标签：配置" if len(self.calls) == 1 else "Tab 键：配置",
                    Usage(5, 2))

    client = TabThenFixClient()
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/config", "original": "tab : config",
        "meta": {"role": "ui", "disposition": "translate",
                 "reason": "interaction_prompt"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 1 and stats.failed == 0 and stats.requests == 2
    # 按键 tab 从原文剥离后回填（原文按键前缀 + 译文）；模型第二次补全按键
    assert entry.translation == "tab Tab 键：配置"
    assert [call[0] for call in client.calls] == ["tab : config", ": config"]


def test_protected_repair_backfills_key_when_stripped_segment_echoes():
    # deadbeat 真实失败：剥离段 ': config' 模型回显 'config'（无中文）→
    # protected repair 的剥离段翻译失败 → 降级：整段译文 '标签：配置' 语义
    # 已正确，只缺按键段 → 回填缺失的 protected 段 'tab' → 'tab 标签：配置'
    class EchoStrippedClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, source, target_lang, glossary):
            self.calls.append(source)
            if source == "tab : config":
                return "标签：配置", Usage(5, 2)
            return "config", Usage(2, 1)  # 剥离段回显，无中文

    client = EchoStrippedClient()
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/config", "original": "tab : config",
        "meta": {"role": "ui", "disposition": "translate",
                 "reason": "interaction_prompt"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 1 and stats.failed == 0 and stats.requests == 2
    # 剥离段没翻出来 → 按键回填到整段译文前（不回填已含按键的译文）
    assert entry.translation == "tab 标签：配置"
    assert client.calls == ["tab : config", ": config"]


def test_protected_repair_backfill_skips_when_key_already_in_whole():
    # fix-30（headache 实证）：回车 是 ENTER 的中文通称 → 整段译文
    # 「回车：配置」直接通过质量门（键名保留语义），不再触发 protected
    # repair 回填（旧行为：无 ENTER 字面量 → input_token_mismatch 误杀
    # → 剥离按键段重建 'enter 配置'——译文反而生硬）
    class StripOkClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, source, target_lang, glossary):
            self.calls.append(source)
            if source == "enter : config":
                return "回车：配置", Usage(5, 2)
            return "配置", Usage(2, 1)

    client = StripOkClient()
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/config", "original": "enter : config",
        "meta": {"role": "ui", "disposition": "translate",
                 "reason": "interaction_prompt"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 1 and stats.failed == 0 and stats.requests == 1
    assert entry.translation == "回车：配置"
    assert client.calls == ["enter : config"]


def test_input_token_mismatch_is_actionable_ui_retry():
    # input_token_mismatch（按键丢失）加入可重试：protected repair 失败后
    # 仍有第二次完整原文重试机会
    entry = TextEntry(
        "ui", "reported", "tab : config",
        meta={"role": "ui", "disposition": "translate"},
    )
    entry.quality_reasons = ("input_token_mismatch",)

    assert BatchTranslator._is_actionable_ui_retry(entry) is True


def test_native_actionable_retry_stops_after_first_response_cancels():
    cancelled = threading.Event()

    class CancellingEchoClient:
        config = SimpleNamespace(timeout=120.0)
        calls = 0

        def translate_text(self, source, _target_lang, _glossary):
            self.calls += 1
            cancelled.set()
            return source, Usage(1, 1)

    client = CancellingEchoClient()
    # 2026-08-14：内置 UI 词（SFX）已被确定性直填短路，取消链改用
    # 非内置词验证
    entry = _to_model([{"file_id": "ui", "key_path": "menu/sfx",
                        "original": "TAP TO CONTINUE",
                        "meta": {"role": "ui"}}])[0]
    stats = BatchTranslator(
        client, batch_size=1, concurrency=1,
        cancellation_event=cancelled,
    ).run([entry])

    assert client.calls == 1
    assert stats.done == stats.failed == 0
    assert entry.status == "pending" and entry.translation == ""


def test_native_actionable_retry_response_cancel_does_not_mutate_entry():
    cancelled = threading.Event()

    class RetryCancellingClient:
        config = SimpleNamespace(timeout=120.0)
        calls = 0

        def translate_text(self, source, _target_lang, _glossary):
            self.calls += 1
            if self.calls == 2:
                cancelled.set()
                return "设置", Usage(1, 1)
            return source, Usage(1, 1)

    client = RetryCancellingClient()
    # 2026-08-14：内置 UI 词（Settings）已被确定性直填短路，取消链
    # 改用非内置词验证
    entry = _to_model([{"file_id": "ui", "key_path": "menu/settings",
                        "original": "TAP TO CONTINUE",
                        "meta": {"role": "ui"}}])[0]
    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            cancellation_event=cancelled).run([entry])

    assert client.calls == 2
    assert stats.done == stats.failed == 0
    assert entry.status == "pending" and entry.translation == ""
    assert "quality_passed" not in entry.meta
    assert "quality_reasons" not in entry.meta


def test_chat_batch_and_single_fallback_include_builtin_ui_references():
    class EchoThenTranslateChatClient:
        def __init__(self):
            self.prompts = []

        def chat(self, _system, messages):
            self.prompts.append(messages[0]["content"])
            translation = ("EXIT TO MENU" if len(self.prompts) == 1
                           else "返回主菜单")
            return json.dumps([{
                "id": "menu/quit@ui", "translation": translation,
            }], ensure_ascii=False), Usage(5, 2)

    client = EchoThenTranslateChatClient()
    # 2026-08-14：内置 UI 词（QUIT）已被确定性直填短路，回显重试链
    # 改用非内置词验证
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/quit", "original": "EXIT TO MENU",
        "meta": {"role": "ui", "disposition": "translate"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 1 and stats.failed == 0 and stats.requests == 2
    assert entry.translation == "返回主菜单" and len(client.prompts) == 2
    for prompt in client.prompts:
        assert "Reference the following translations:" in prompt
        for source, target in (
                ("Settings", "设置"), ("Quit", "退出"),
                ("Resolution", "分辨率"), ("SFX", "音效"),
                ("Volume", "音量"), ("Resume", "继续")):
            assert f"{source} translates to {target}" in prompt


def test_chat_multiline_mismatch_repairs_segments_without_whole_retry():
    class CollapsingChatClient:
        def __init__(self):
            self.prompts = []

        def chat(self, _system, messages):
            prompt = messages[0]["content"]
            self.prompts.append(prompt)
            translations = ["Settings Apply", "设置", "应用"]
            return json.dumps([{
                "id": "menu/settings-apply@ui",
                "translation": translations[len(self.prompts) - 1],
            }], ensure_ascii=False), Usage(5, 2)

    client = CollapsingChatClient()
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/settings-apply",
        "original": "Settings\nApply",
        "meta": {"role": "ui", "disposition": "translate"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 1 and stats.failed == 0 and stats.requests == 3
    assert entry.translation == "设置\n应用"
    assert len(client.prompts) == 3
    assert client.prompts[0].endswith(
        '"menu/settings-apply@ui": Settings\nApply')
    assert client.prompts[1].endswith(
        '"menu/settings-apply@ui": Settings')
    assert client.prompts[2].endswith(
        '"menu/settings-apply@ui": Apply')
    assert all("Reference the following translations:" in prompt
               for prompt in client.prompts)


def test_native_multiline_cancel_between_segments_stops_provider_calls():
    cancelled = threading.Event()

    class CancellingSegmentClient:
        config = SimpleNamespace(timeout=120.0)
        calls = []

        def translate_text(self, source, _target_lang, _glossary):
            self.calls.append(source)
            if len(self.calls) == 2:
                cancelled.set()
            return ({"One\nTwo": "One Two", "One": "一"}.get(source, "二"),
                    Usage(1, 1))

    client = CancellingSegmentClient()
    entry = _to_model([{"file_id": "ui", "key_path": "menu/two",
                        "original": "One\nTwo", "meta": {"role": "ui"}}])[0]
    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            cancellation_event=cancelled).run([entry])

    assert client.calls == ["One\nTwo", "One"]
    assert stats.done == stats.failed == 0 and entry.status == "pending"


def test_chat_multiline_cancel_between_segments_stops_provider_calls():
    cancelled = threading.Event()

    class CancellingSegmentChatClient:
        calls = 0

        def chat(self, _system, _messages):
            self.calls += 1
            if self.calls == 2:
                cancelled.set()
            translation = ["One Two", "一"][self.calls - 1]
            return json.dumps([{"id": "menu/two@ui",
                                "translation": translation}]), Usage(1, 1)

    client = CancellingSegmentChatClient()
    entry = _to_model([{"file_id": "ui", "key_path": "menu/two",
                        "original": "One\nTwo", "meta": {"role": "ui"}}])[0]
    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            cancellation_event=cancelled).run([entry])

    assert client.calls == 2
    assert stats.done == stats.failed == 0 and entry.status == "pending"


def test_chat_multiline_repair_fails_when_a_segment_stays_empty():
    class DroppingChatClient:
        def __init__(self):
            self.calls = 0

        def chat(self, _system, _messages):
            translations = ["设置应用", "设置", ""]
            translation = translations[self.calls]
            self.calls += 1
            return json.dumps([{
                "id": "menu/settings-apply@ui",
                "translation": translation,
            }], ensure_ascii=False), Usage(5, 2)

    client = DroppingChatClient()
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/settings-apply",
        "original": "Settings\nApply",
        "meta": {"role": "ui", "disposition": "translate"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 0 and stats.failed == 1 and stats.requests == 3
    assert entry.quality_reasons == ("line_content_mismatch",)
    assert client.calls == 3


def test_chat_multiline_proper_name_preserve_is_not_requested():
    class ProperNameChatClient:
        def __init__(self):
            self.calls = 0

        def chat(self, _system, _messages):
            self.calls += 1
            return json.dumps([{
                "id": "speaker/name@dialogue",
                "translation": "Flabby Pizza",
            }]), Usage(5, 2)

    client = ProperNameChatClient()
    entry = _to_model([{
        "file_id": "dialogue", "key_path": "speaker/name",
        "original": "Flabby\nPizza",
        "meta": {"role": "proper_name", "disposition": "preserve"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.total == 0 and stats.done == 0 and stats.failed == 0
    assert stats.requests == 0 and client.calls == 0
    assert entry.status == "pending" and entry.quality_reasons == ()


def test_native_actionable_ui_retry_is_not_limited_to_builtin_terms():
    class EchoThenTranslateClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = 0

        def translate_text(self, _source, _target_lang, _glossary):
            self.calls += 1
            result = "Apply Changes" if self.calls == 1 else "应用更改"
            return result, Usage(5, 2)

    client = EchoThenTranslateClient()
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/apply",
        "original": "Apply Changes",
        "meta": {"role": "ui", "disposition": "translate"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.done == 1 and stats.failed == 0 and stats.requests == 2
    assert entry.translation == "应用更改" and client.calls == 2


def test_native_proper_name_preserve_is_not_requested():
    class ProperNameClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = 0

        def translate_text(self, _source, _target_lang, _glossary):
            self.calls += 1
            return "Flabby Pizza", Usage(5, 2)

    client = ProperNameClient()
    entry = _to_model([{
        "file_id": "dialogue", "key_path": "speaker/name",
        "original": "Flabby Pizza",
        "meta": {"role": "proper_name", "disposition": "preserve"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.total == 0 and stats.done == 0 and stats.failed == 0
    assert entry.status == "pending" and entry.quality_reasons == ()
    assert client.calls == 0 and stats.requests == 0


@pytest.mark.parametrize(("role", "disposition"), [
    ("proper_name", ""),
    ("ui", "preserve"),
    ("ui", "structural"),
    ("ui", "code"),
    ("ui", "key"),
])
def test_native_multiline_nontranslate_disposition_is_not_requested(
        role, disposition):
    class PreserveNativeClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = []

        def translate_text(self, source, _target_lang, _glossary):
            self.calls.append(source)
            return "Flabby Pizza", Usage(5, 2)

    client = PreserveNativeClient()
    entry = _to_model([{
        "file_id": "dialogue", "key_path": "speaker/name",
        "original": "Flabby\nPizza",
        "meta": {"role": role, "disposition": disposition},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.total == 0 and stats.done == 0 and stats.failed == 0
    assert stats.requests == 0 and client.calls == []
    assert entry.status == "pending" and entry.quality_reasons == ()


def test_chat_proper_name_preserve_is_not_requested():
    class ProperNameChatClient:
        def __init__(self):
            self.calls = 0

        def chat(self, _system, _messages):
            self.calls += 1
            return json.dumps([{
                "id": "speaker/name@dialogue",
                "translation": "Flabby Pizza",
            }]), Usage(5, 2)

    client = ProperNameChatClient()
    entry = _to_model([{
        "file_id": "dialogue", "key_path": "speaker/name",
        "original": "Flabby Pizza",
        "meta": {"role": "proper_name", "disposition": "preserve"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1).run([entry])

    assert stats.total == 0 and stats.done == 0 and stats.failed == 0
    assert entry.status == "pending" and entry.quality_reasons == ()
    assert client.calls == 0 and stats.requests == 0


def test_duplicate_source_and_role_share_one_native_translation_request():
    class VaryingNativeClient:
        config = SimpleNamespace(timeout=120.0)

        def __init__(self):
            self.calls = 0

        def translate_text(self, _source, _target_lang, _glossary):
            self.calls += 1
            translation = "打开门" if self.calls == 1 else "开启门"
            return translation, Usage(10, 4)

    client = VaryingNativeClient()
    entries = _to_model([
        {"file_id": "level1", "key_path": "door/1",
         "original": "Open Door", "meta": {"role": "display"}},
        {"file_id": "level2", "key_path": "door/2",
         "original": "Open Door", "meta": {"role": "display"}},
    ])

    stats = BatchTranslator(
        client, batch_size=8, concurrency=1, lang="auto→zh-CN",
    ).run(entries)

    assert client.calls == 1
    assert stats.requests == 1
    assert stats.done == 2 and stats.failed == 0
    assert [entry.translation for entry in entries] == ["打开门", "打开门"]


def test_local_single_item_fallback_extracts_translation_from_echoed_prompt():
    class EchoingLocalClient:
        accepts_plain_single = True
        config = SimpleNamespace(timeout=120.0)

        def chat(self, _system, _messages):
            return (
                "[来源文件] ui\n"
                "[定位键] prompt/open\n"
                "[文本角色] display\n"
                "[输入按键] 译文必须原样保留：E\n"
                '"prompt/open@ui": 按 E 键打开',
                Usage(8, 4),
            )

    entry = _to_model([{
        "file_id": "ui", "key_path": "prompt/open",
        "original": "Press E to open", "meta": {"role": "display"},
    }])[0]

    stats = BatchTranslator(
        EchoingLocalClient(), batch_size=1, concurrency=1,
    ).run([entry])

    assert stats.done == 1 and stats.failed == 0
    assert entry.translation == "按 E 键打开"


def test_local_single_item_ignores_example_json_in_full_prompt_echo():
    class FullPromptEchoClient:
        accepts_plain_single = True
        config = SimpleNamespace(timeout=120.0)

        def chat(self, _system, messages):
            content = messages[0]["content"].replace(
                '"prompt/open@ui": Press E to open',
                '"prompt/open@ui": 按 E 键打开',
            )
            return content, Usage(8, 4)

    entry = _to_model([{
        "file_id": "ui", "key_path": "prompt/open",
        "original": "Press E to open", "meta": {"role": "display"},
    }])[0]

    stats = BatchTranslator(
        FullPromptEchoClient(), batch_size=1, concurrency=1,
    ).run([entry])

    assert stats.done == 1 and stats.failed == 0
    assert entry.translation == "按 E 键打开"


def test_placeholder_mismatch_marks_failed_when_slot_repair_is_invalid():
    class BadClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def chat(self, system, messages):
            translation = "没有占位符" if self.calls == 0 else ""
            self.calls += 1
            return json.dumps([{
                "id": "k0@f", "translation": translation,
            }], ensure_ascii=False), Usage(1, 1)

    bt = BatchTranslator(BadClient(), batch_size=25, concurrency=1)
    entries = _to_model([{"file_id": "f", "key_path": "k0", "original": "Take {item} now"}])
    stats = bt.run(entries)
    assert entries[0].status == "failed"
    assert stats.failed == 1


@pytest.mark.parametrize("invalid_translation", [None, 123, {"text": "打开"}])
def test_model_response_rejects_non_string_translation(invalid_translation):
    class InvalidSchemaClient(FakeClient):
        def chat(self, system, messages):
            return json.dumps([{
                "id": "k0@f", "translation": invalid_translation,
            }], ensure_ascii=False), Usage(1, 1)

    entry = _to_model([{
        "file_id": "f", "key_path": "k0", "original": "Open Door",
    }])[0]

    stats = BatchTranslator(InvalidSchemaClient(), batch_size=1).run([entry])

    assert stats.failed == 1 and entry.status == "failed"
    assert entry.quality_reasons == ("invalid_response",)


def test_model_translation_must_follow_glossary_and_persist_quality_metadata():
    store = ProjectStore(Path(tempfile.mkdtemp()) / "glossary-quality.db")
    store.init_schema()
    store.upsert_entries([{
        "file_id": "dialogue", "key_path": "line/1",
        "original": "Use the Moon Key", "status": "pending",
        "meta": {"role": "dialogue"},
    }])
    entry = _to_model([{
        "file_id": "dialogue", "key_path": "line/1",
        "original": "Use the Moon Key", "meta": {"role": "dialogue"},
    }])[0]
    client = FakeClient({"Use the Moon Key": "使用月之钥匙"})

    stats = BatchTranslator(
        client, memory=store, glossary=[("Moon Key", "月光钥匙")],
        batch_size=1, concurrency=1,
    ).run([entry])

    assert stats.failed == 1 and entry.status == "failed"
    assert entry.quality_reasons == ("glossary_mismatch",)
    row_meta = json.loads(store.get_entries()[0]["meta"])
    assert row_meta["quality_passed"] is False
    assert row_meta["quality_reasons"] == ["glossary_mismatch"]


def test_quality_normalized_translation_is_used_for_database_and_memory():
    store = ProjectStore(Path(tempfile.mkdtemp()) / "normalized.db")
    store.init_schema()
    row = {"file_id": "ui", "key_path": "menu/open", "original": "Open Door"}
    store.upsert_entries([row])
    entry = _to_model([row])[0]

    BatchTranslator(
        FakeClient({"Open Door": "  打开门  "}), memory=store,
        model="m", lang="en→zh-CN", batch_size=1, concurrency=1,
    ).run([entry])

    assert entry.translation == "打开门"
    assert store.get_entries()[0]["translation"] == "打开门"
    # Phase B：批记忆先入 pending 桶（不可见），审后结算提交才可命中
    assert store.get_memory_hits(["Open Door"], "m", "en→zh-CN") == {}
    settle_translation_memory(store, [entry], "m", "en→zh-CN")
    assert store.get_memory_hits(["Open Door"], "m", "en→zh-CN") == {
        "Open Door": "打开门",
    }


def test_memory_hit_skips_api():
    store = ProjectStore(Path(tempfile.mkdtemp()) / "m.db")
    store.init_schema()
    store.add_memory("text5", "已有缓存", "m", "en→zh-CN")
    fc = FakeClient()
    bt = BatchTranslator(fc, batch_size=25, concurrency=1, memory=store,
                         model="m", lang="en→zh-CN")
    entries = _to_model(_entries())
    stats = bt.run(entries)
    assert stats.from_memory == 1
    assert entries[5].translation == "已有缓存"


def test_bad_memory_is_evicted_and_falls_back_to_model_in_same_run():
    store = ProjectStore(Path(tempfile.mkdtemp()) / "quality-memory.db")
    store.init_schema()
    store.upsert_entries([{
        "file_id": "ui", "key_path": "menu/open", "original": "Open Door",
        "status": "pending", "meta": {"role": "ui", "max_chars": 12},
    }])
    store.add_memory("Open Door", "Open Door", "m", "en→zh-CN")
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/open", "original": "Open Door",
        "meta": {"role": "ui", "max_chars": 12},
    }])[0]

    client = FakeClient({"Open Door": "打开门"})
    stats = BatchTranslator(
        client, memory=store, model="m", lang="en→zh-CN",
    ).run([entry])

    assert stats.done == 1 and stats.failed == 0 and stats.from_memory == 0
    assert client.calls == 1
    assert entry.status == "translated" and entry.translation == "打开门"
    assert entry.quality_reasons == ()
    row = store.get_entries()[0]
    persisted_meta = json.loads(row["meta"])
    assert persisted_meta["quality_passed"] is True
    # Phase B：坏记忆已驱逐；好译文 pending 桶经审后结算提交后可见
    assert store.get_memory_hits(["Open Door"], "m", "en→zh-CN") == {}
    settle_translation_memory(store, [entry], "m", "en→zh-CN")
    assert store.get_memory_hits(["Open Door"], "m", "en→zh-CN") == {
        "Open Door": "打开门",
    }


def test_memory_rejected_with_exhausted_budget_marks_failed():
    """Q3 C4：记忆命中拒绝且 attempt 预算已耗尽 → 直接置 failed（带
    memory_rejected_reasons），不落 pending 黑洞——预算耗尽条目已不在
    run_scope，若置 pending 将永远 pending 不 fail 不 translated，
    统计不可见。"""
    from hanhua.core.batch_translator import _record_failure_attempt
    store = ProjectStore(Path(tempfile.mkdtemp()) / "c4.db")
    store.init_schema()
    store.upsert_entries([{
        "file_id": "ui", "key_path": "menu/open", "original": "Open Door",
        "status": "failed", "meta": {"role": "ui", "max_chars": 12},
    }])
    store.add_memory("Open Door", "Open Door", "m", "en→zh-CN")  # 回显=坏记忆
    entry = _to_model([{
        "file_id": "ui", "key_path": "menu/open", "original": "Open Door",
        "meta": {"role": "ui", "max_chars": 12},
    }])[0]
    # 跨轮累计失败至 model_behavior 预算耗尽（2 次）
    for _ in range(2):
        entry = _to_model([{
            "file_id": "ui", "key_path": "menu/open", "original": "Open Door",
            "meta": dict(entry.meta),
        }])[0]
        _record_failure_attempt(entry, "untranslated_text")

    client = FakeClient({"Open Door": "打开门"})
    stats = BatchTranslator(
        client, memory=store, model="m", lang="en→zh-CN",
    ).run([entry])

    assert client.calls == 0                      # 预算耗尽不再请求模型
    assert entry.status == "failed"               # 不落 pending 黑洞
    assert "memory_rejected_reasons" in entry.meta
    assert stats.failed == 1                      # 统计可见


def test_locked_entries_skipped():
    bt = BatchTranslator(FakeClient(), batch_size=25, concurrency=1)
    rows = _entries(3)
    rows[1]["locked"] = True
    entries = _to_model(rows)
    stats = bt.run(entries)
    assert entries[1].status == "pending" and entries[1].translation == ""
    assert stats.done == 2


def test_automatic_translation_only_requests_display_text_with_sufficient_confidence():
    client = FakeClient()
    entries = _to_model([
        {"file_id": "f", "key_path": "code", "original": "PlayerController",
         "meta": {"role": "structural", "confidence": "high"},
         "confidence": "high"},
        {"file_id": "f", "key_path": "raw", "original": "Maybe visible",
         "meta": {"role": "display", "confidence": "low"},
         "confidence": "low"},
        {"file_id": "f", "key_path": "ui", "original": "Open Door",
         "meta": {"role": "display", "confidence": "high"},
         "confidence": "high"},
    ])

    stats = BatchTranslator(client, batch_size=10).run(entries)

    assert stats.done == 1 and client.calls == 1
    assert entries[0].status == "pending" and entries[0].translation == ""
    assert entries[1].status == "pending" and entries[1].translation == ""
    assert entries[2].status == "translated"


def test_request_exception_persists_stable_failure_reason():
    class ExplodingClient(FakeClient):
        def chat(self, system, messages):
            raise RuntimeError("provider unavailable")

    store = ProjectStore(Path(tempfile.mkdtemp()) / "request-error.db")
    store.init_schema()
    row = {"file_id": "f", "key_path": "k0", "original": "Open Door"}
    store.upsert_entries([row])
    entry = _to_model([row])[0]

    stats = BatchTranslator(
        ExplodingClient(), memory=store, batch_size=1, concurrency=1,
    ).run([entry])

    assert stats.failed == 1 and entry.status == "failed"
    assert entry.quality_reasons == ("request_error",)
    persisted = json.loads(store.get_entries()[0]["meta"])
    assert persisted["quality_passed"] is False
    assert persisted["quality_reasons"] == ["request_error"]


class BrokenJsonClient(FakeClient):
    """批量请求返回非法 JSON（模拟译文含未转义引号），单条请求返回合法 JSON。"""

    def chat(self, system, messages):
        self.calls += 1
        content = messages[0]["content"]
        if _item_count(content) > 1:
            return '不是JSON [{"id": "x', Usage(10, 5)
        return '[{"id": "k0@f", "translation": "单条翻译成功"}]', Usage(10, 5)


def test_batch_json_failure_falls_back_to_single():
    """整批 JSON 解析失败 → 逐条降级重试必须成功。"""
    bt = BatchTranslator(BrokenJsonClient(), batch_size=25, concurrency=1)
    entries = _to_model([{"file_id": "f", "key_path": "k0", "original": "text0"}])
    stats = bt.run(entries)
    assert stats.done == 1 and stats.failed == 0
    assert entries[0].translation == "单条翻译成功"


class HalfBadClient(FakeClient):
    """批内一条翻译为空（模拟缺条），单条请求成功。"""

    def chat(self, system, messages):
        self.calls += 1
        content = messages[0]["content"]
        if _item_count(content) > 1:
            return '[{"id": "k0@f", "translation": "第一条"}]', Usage(10, 5)
        if "k0@f" in content:
            return '[{"id": "k0@f", "translation": "补译第一条"}]', Usage(10, 5)
        return '[{"id": "k1@f", "translation": "补译第二条"}]', Usage(10, 5)


def test_batch_partial_failure_retries_failed_only():
    bt = BatchTranslator(HalfBadClient(), batch_size=25, concurrency=1)
    entries = _to_model(_entries(2))
    stats = bt.run(entries)
    assert stats.done == 2 and stats.failed == 0
    assert entries[0].translation == "第一条" or entries[0].translation == "补译第一条"


def test_same_source_and_role_share_one_consistent_translation():
    class DriftClient(FakeClient):
        def chat(self, system, messages):
            content = messages[0]["content"]
            out = []
            if "a@f" in content:
                out.append({"id": "a@f", "translation": "打开"})
            if "b@f" in content:
                out.append({"id": "b@f", "translation": "开启"})
            return json.dumps(out, ensure_ascii=False), Usage(1, 1)

    entries = _to_model([
        {"file_id": "f", "key_path": "a", "original": "Open",
         "meta": {"role": "ui"}},
        {"file_id": "f", "key_path": "b", "original": "Open",
         "meta": {"role": "ui"}},
    ])

    stats = BatchTranslator(DriftClient(), batch_size=2, concurrency=1).run(entries)

    assert stats.done == 2 and stats.failed == 0 and stats.requests == 1
    assert entries[0].status == entries[1].status == "translated"
    assert entries[0].translation == entries[1].translation == "打开"


def test_single_object_json_extract():
    from hanhua.core.translator import extract_json_array
    assert extract_json_array('{"id": "e1", "translation": "你好"}') == \
        [{"id": "e1", "translation": "你好"}]


def test_fallback_line_parse():
    from hanhua.core.translator import extract_json_array_fallback
    out = extract_json_array_fallback(
        '{"id": "e1", "translation": "你好"} {"id": "e2", "translation": "再见"}')
    assert out == [{"id": "e1", "translation": "你好"}, {"id": "e2", "translation": "再见"}]


def test_p0_quality_saves_raw_model_output_evidence():
    """P0-3：质量门保存模型原始输出证据（raw_output），
    归一化后与 raw 相同时不重复存 normalized_output。"""
    entry = _to_model([{
        "file_id": "f", "key_path": "k0", "original": "Open Door",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]

    BatchTranslator(
        FakeClient({"Open Door": "打开门"}), batch_size=1, concurrency=1,
        lang="en→zh-CN",
    ).run([entry])

    assert entry.status == "translated"
    assert entry.meta["raw_output"] == "打开门"
    assert "normalized_output" not in entry.meta


def test_p0_quality_saves_both_raw_and_normalized_when_healed():
    """P0-3：自愈改变了输出（占位符被补全）→ raw 与 normalized 都留存。"""
    class HealingClient(FakeClient):
        def chat(self, system, messages):
            return json.dumps([{
                "id": "k0@f", "translation": "拿着物品",
            }], ensure_ascii=False), Usage(1, 1)

    entry = _to_model([{
        "file_id": "f", "key_path": "k0", "original": "Take {item} now",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]

    BatchTranslator(
        HealingClient(), batch_size=1, concurrency=1, lang="en→zh-CN",
    ).run([entry])

    assert entry.status == "translated"
    assert entry.meta["raw_output"] == "拿着物品"
    assert "{item}" in entry.translation
    assert entry.meta["normalized_output"] == entry.translation


def test_p0_invalid_response_keeps_raw_content_evidence():
    """P0-3：JSON 解析失败时模型原始输出作为证据留存（审校可复盘）。"""
    class GarbageClient(FakeClient):
        def chat(self, system, messages):
            return "不是JSON的模型响应", Usage(1, 1)

    entry = _to_model([{
        "file_id": "f", "key_path": "k0", "original": "Open Door",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]

    BatchTranslator(
        GarbageClient(), batch_size=1, concurrency=1,
    ).run([entry])

    assert entry.status == "failed"
    assert entry.quality_reasons == ("invalid_response",)
    assert entry.meta["raw_output"] == "不是JSON的模型响应"


def test_p0_rejected_translation_keeps_raw_evidence():
    """P0-3：质量门拒绝（untranslated_text）后 raw 证据仍在 meta。"""
    class EchoClient(FakeClient):
        def chat(self, system, messages):
            return json.dumps([{
                "id": "k0@f", "translation": "Open Door",
            }], ensure_ascii=False), Usage(1, 1)

    entry = _to_model([{
        "file_id": "f", "key_path": "k0", "original": "Open Door",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]

    BatchTranslator(
        EchoClient(), batch_size=1, concurrency=1, lang="en→zh-CN",
    ).run([entry])

    assert entry.status == "failed"
    assert "untranslated_text" in entry.quality_reasons
    assert entry.meta["raw_output"] == "Open Door"


def test_batch_prompt_receives_hit_glossary_terms_e2e():
    """P1：术语按条目命中注入——只有本条原文命中的术语进入 prompt。"""
    seen = {}

    class SpyClient(FakeClient):
        def chat(self, system, messages):
            seen["user"] = messages[0]["content"]
            return json.dumps([{
                "id": "dialogue/line/1@f", "translation": "使用月光钥匙",
            }], ensure_ascii=False), Usage(1, 1)

    entry = _to_model([{
        "file_id": "f", "key_path": "dialogue/line/1",
        "original": "Use the Moon Key", "meta": {"role": "display"},
    }])[0]

    BatchTranslator(
        SpyClient(), glossary=[("Moon Key", "月光钥匙"), ("Sword", "长剑")],
        batch_size=1, concurrency=1, lang="en→zh-CN",
    ).run([entry])

    assert "[术语命中] 本条原文包含以下术语" in seen["user"]
    assert "Moon Key → 月光钥匙" in seen["user"]
    assert "Sword" not in seen["user"]


def test_stats_reports_elapsed_and_rate():
    """P3：run 统计耗时与吞吐（条/分）。"""
    entry = _to_model([{
        "file_id": "f", "key_path": "k0", "original": "Open Door",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]

    stats = BatchTranslator(
        FakeClient({"Open Door": "打开门"}), batch_size=1, concurrency=1,
        lang="en→zh-CN",
    ).run([entry])

    assert stats.done == 1
    assert stats.elapsed > 0
    assert stats.rate_per_minute > 0
    assert stats.rate_per_minute == 60.0 / stats.elapsed


def test_stats_rate_zero_without_elapsed():
    from hanhua.core.models import TranslateStats
    assert TranslateStats(done=5, elapsed=0.0).rate_per_minute == 0.0
    assert TranslateStats(done=0, elapsed=1.0).rate_per_minute == 0.0


# ── 多语言源文本双跳 + 同对象译例 + 引文豁免（alisa-demo 0.25.0） ──

def test_multilingual_source_double_hop():
    """多语言源双跳：模型对日语原文输出英语译文（准确但目标语错误，
    质量门拒绝）→ 以英语译文为中间源再译一次中文。

    alisa-demo 实证：右手の鍵 → "Right-hand key" → 右手钥匙。
    """
    class DoubleHop:
        config = SimpleNamespace(timeout=120.0)
        calls = 0

        def translate_text(self, *_args):
            self.calls += 1
            return ("Right-hand key" if self.calls == 1
                    else "右手钥匙"), Usage(1, 1)

    client = DoubleHop()
    entry = _to_model([{
        "file_id": "level18", "key_path": "asset#level18#5656/str/3",
        "original": "右手の鍵",
        "meta": {"role": "display", "disposition": "translate",
                 "asset_file": "level18", "obj": 5656},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])

    assert client.calls == 2
    assert stats.done == 1
    assert entry.translation == "右手钥匙"


def test_same_object_reference_retry():
    """同对象译例：同 obj 兄弟条目已成功 → 失败条目重试注入「同一物品的
    参考译文」→ 模型复用译文。

    alisa-demo 实证：Clé en Fer（法语，模型完全不认识）回显 → 同 obj
    "Iron Key" 已译「铁钥匙」→ 注入后模型输出铁钥匙。
    """
    class ObjRef:
        config = SimpleNamespace(timeout=120.0)
        calls = 0

        def translate_text(self, source, *_args):
            self.calls += 1
            if source == "Iron Key":
                return "铁钥匙", Usage(1, 1)
            return "Clé en Fer", Usage(1, 1)   # 法语回显

        def chat(self, system, messages):
            content = messages[0]["content"]
            assert "Reference translations from the same item" in content
            assert "Iron Key translates to 铁钥匙" in content
            return "铁钥匙", Usage(1, 1)

    client = ObjRef()
    entries = _to_model([
        {"file_id": "level18", "key_path": "asset#level18#5227/str/0",
         "original": "Iron Key",
         "meta": {"role": "display", "disposition": "translate",
                  "asset_file": "level18", "obj": 5227}},
        {"file_id": "level18", "key_path": "asset#level18#5227/str/1",
         "original": "Clé en Fer",
         "meta": {"role": "display", "disposition": "translate",
                  "asset_file": "level18", "obj": 5227}},
    ])

    stats = BatchTranslator(client, batch_size=4, concurrency=1,
                            lang="en→zh-CN").run(entries)

    assert stats.done == 2
    assert entries[0].translation == "铁钥匙"
    assert entries[1].translation == "铁钥匙"


def test_quote_inscription_retained_allowed():
    """原文引号内引文（铭文/题词）保留原文 → 不算英文残留。

    alisa-demo 实证：三语言版同一引文 "To the house of ..." 译文保留
    引文被误判 target_script_mismatch → 豁免（译文已含中文翻译）。
    """
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "s", "key_path": "asset#sharedassets5.assets#36/line/37",
        "original": 'The note reads: "To the house of ..." '
                    'the last word is missing.',
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]

    assert translator._apply_quality(
        entry, '笔记上写着："To the house of ..." 最后一个词缺失了。'
    ) is True
    assert entry.meta["quality_passed"] is True


def test_quote_echo_without_chinese_still_fails():
    """引文豁免要求译文已含中文翻译——纯回显引文仍判失败。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "s", "key_path": "asset#sharedassets5.assets#36/line/37",
        "original": 'The note reads: "To the house of ..."',
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]

    assert translator._apply_quality(entry, 'The note reads: "To the house of ..."') is False
    assert "untranslated_text" in entry.meta["quality_reasons"]


def test_french_item_echo_not_proper_name_exempt():
    """法语 TitleCase 物品名回显不得被 proper_name_echo 豁免（漏网之鱼）。

    alisa-demo 实证：Clé Pomme / Chapeau Cône Vert 回显被当专名豁免
    通过（TitleCase 形态 + 无独立小写词）——多语言打包数组中（同 obj 已
    有成功译文）的多语言源文本必须翻译。
    """
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    # 同 obj 已有成功译文（多语言数组特征：Iron Key 已译铁钥匙）
    translator._record_obj_result(_to_model([{
        "file_id": "level18", "key_path": "asset#level18#5227/str/0",
        "original": "Iron Key",
        "meta": {"asset_file": "level18", "obj": 5227},
    }])[0], "铁钥匙")
    entry = _to_model([{
        "file_id": "level18", "key_path": "asset#level18#5227/str/1",
        "original": "Clé Pomme",
        "meta": {"role": "display", "disposition": "translate",
                 "asset_file": "level18", "obj": 5227},
    }])[0]

    assert translator._apply_quality(entry, "Clé Pomme") is False
    assert {"untranslated_text", "target_script_mismatch"} & set(
        entry.meta["quality_reasons"])


def test_french_proper_name_echo_still_allowed():
    """proper_name 角色的法语专名回显仍豁免（多语言源限制不误伤专名）。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "f", "key_path": "k",
        "original": "Béatrice",
        "meta": {"role": "proper_name", "disposition": "proper_name"},
    }])[0]

    assert translator._apply_quality(entry, "Béatrice") is True


# ── backrooms 修复：数字邻接词豁免 / 词级补译 / 专名 references 重译 ──


@pytest.mark.parametrize(
    ("source", "translation"),
    ((  # 4chan：数字邻接词（chan 紧邻数字 4）→ 专名/网站混合形态保留，
        # 不算英文残留（backrooms 实证：译文保留 4chan 被拆出 chan 误判）
        "Thanks to the anonymous user on 4chan for inspiration",
        "感谢 4chan 上的匿名用户提供的灵感。"),
     (  # 版本号混合形态（beta 紧邻数字）→ 同样豁免
        "Version 1.2beta released",
        "已发布 1.2beta 版本。"),
     ))
def test_digit_adjacent_word_not_target_script_mismatch(source, translation):
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", source,
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, translation) is True
    assert entry.meta["quality_passed"] is True


def test_digit_adjacent_exemption_not_loosened_for_real_residue():
    """数字邻接豁免不放松真残留：空格隔开的 '24 hours'（数字不紧邻）仍判失败。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", "The shop opens 24 hours",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, "营业时间为 24 hours。") is False


def test_word_residue_exempt_requires_meta_marker():
    """词级补译的保留词豁免仅在本条 meta 标记时生效（无标记 → 仍失败）。

    'itch 页面' 的 itch 是 itch.io 专名，补译后模型仍保留 → 标记豁免；
    无补译语义的同形态译文（'please 帮忙' 类）无标记 → 正常拦截。
    """
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    marked = TextEntry(
        "ui", "reported", "available at itch page",
        meta={"role": "display", "disposition": "translate",
              "word_residue_exempt": ["itch"]},
    )
    assert translator._apply_quality(marked, "可在 itch 页面 找到。") is True

    plain = TextEntry(
        "ui", "reported", "available at itch page",
        meta={"role": "display", "disposition": "translate"},
    )
    assert translator._apply_quality(plain, "可在 itch 页面 找到。") is False


class _WordResidueClient(BaseClient):
    """translate_text 模拟词级补译两跳：裸翻译直译误译（痒页面），
    逐词保留引用后模型确认专名保留（itch 页面）。"""

    def __init__(self):
        self.calls = []

    def chat(self, system, messages):
        return "[]", Usage(0, 0)

    def translate_text(self, source, _target_lang, glossary):
        self.calls.append((source.strip(), list(glossary)))
        text = source.strip()
        if text == "itch page":
            if list(glossary) == [("itch", "itch"), ("page", "page")]:
                # 逐词保留引用（第二跳）：模型确认专名保留
                return "itch 页面", Usage(10, 5)
            # 裸翻译：模型把专名 itch 当普通词直译
            return "痒页面", Usage(10, 5)
        return {"was here": "曾来过这里"}.get(text, "译文"), Usage(10, 5)


def test_repair_word_residue_replaces_phrase_and_exempts_kept_name():
    """词级补译：残留短语单独翻译替换回译文；模型保留的词（itch 专名）
    记入本条 meta 豁免 → 质量门放行（backrooms 'itch page' 实证）。"""
    translator = BatchTranslator(
        _WordResidueClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "level0", "key_path": "asset#level0#1298/str/0",
        "original": ("Click here to learn about this game mode in a short "
                     "animation. If you still have problem playing, ask a "
                     "question on Backrooms' community page available at "
                     "itch page or comment on Gamejolt page!"),
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]

    result = translator._repair_word_residue(
        entry, translator.client.translate_text, "zh-CN",
        "点击这里，通过简短的动画了解这种游戏模式。如果您在游玩过程中"
        "仍遇到任何问题，请在 Backrooms 的社区页面上提问，该页面可在 "
        "itch page 上找到，或直接在 Gamejolt 页面上发表评论！")

    assert result is not None and result[2] is True
    assert "itch 页面" in result[1]
    assert "itch" in entry.meta["word_residue_exempt"]
    # 两跳：裸翻译直译（痒页面）→ 逐词引用确认（itch 页面）
    phrases = [c[0] for c in translator.client.calls]
    assert phrases == ["itch page", "itch page"]
    assert ("itch", "itch") in translator.client.calls[1][1]


class _ProperNameClient(BaseClient):
    """translate_text 记录 glossary 并按预设输出返回。"""

    def __init__(self, out):
        self.out = out
        self.glossaries = []

    def chat(self, system, messages):
        return "[]", Usage(0, 0)

    def translate_text(self, source, _target_lang, glossary):
        self.glossaries.append(list(glossary))
        return self.out, Usage(10, 5)


def test_retry_with_proper_name_reference_injects_proper_name():
    """专名 references 重译：'Markiplier was here' 回显 → 注入
    (Markiplier, Markiplier) 引用重译 → 模型译出整句（backrooms 实证）。"""
    client = _ProperNameClient("Markiplier 曾来过这里")
    translator = BatchTranslator(
        client, batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "level1", "key_path": "asset#level1#1928/str/0",
        "original": "Markiplier was here",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]

    result = translator._retry_with_proper_name_reference(
        entry, translator.client.translate_text, "zh-CN",
        "Markiplier is here.")

    assert result is not None and result[2] is True
    assert ("Markiplier", "Markiplier") in client.glossaries[0]
    assert result[1] == "Markiplier 曾来过这里"


def test_retry_with_proper_name_reference_echo_returns_pass():
    """纯专名重译（Crash Bandicoot 注入引用 → 模型回显原文）→ 回显经
    proper_name_echo 放行（物品/游戏名保留合理，baldis 'Shirt Decal'
    被模型补成 'T-shirt Decal' 场景：重译让模型按引用保留专名）。"""
    translator = BatchTranslator(
        _ProperNameClient("Crash Bandicoot"), batch_size=1, concurrency=1,
        lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "f", "key_path": "k",
        "original": "Crash Bandicoot",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]

    result = translator._retry_with_proper_name_reference(
        entry, translator.client.translate_text, "zh-CN", "Crash Bandicoot")

    assert result is not None and result[2] is True
    assert result[1] == "Crash Bandicoot"


def test_retention_term_translated_is_not_glossary_mismatch():
    """保留型术语（term→term 自动沉淀）被翻译成中文 → 不判 mismatch。

    backrooms 实证：自动学习沉淀 'FPS→FPS' 后，质量门拒绝更忠实的
    '输入自定义帧率...'（不含 FPS）。保留型术语是「倾向保留」而非
    「必须保留」——模型翻译了该词是合理行为。
    """
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN",
        glossary=[("FPS", "FPS")])
    entry = TextEntry(
        "ui", "reported", "Enter custom FPS...",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, "输入自定义帧率...") is True
    assert "glossary_mismatch" not in entry.meta["quality_reasons"]


def test_retention_term_echo_without_chinese_still_fails():
    """保留型术语译文纯回显（无中文）→ 仍判失败（untranslated_text）。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN",
        glossary=[("FPS", "FPS")])
    entry = TextEntry(
        "ui", "reported", "Enter custom FPS...",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, "Enter custom FPS...") is False


def test_translation_term_missing_target_still_fails():
    """非保留型术语（真译名 term→译文）译文缺译名 → 仍判 glossary_mismatch。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN",
        glossary=[("Settings", "设置")])
    entry = TextEntry(
        "ui", "reported", "Open Settings menu",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, "打开菜单") is False
    assert "glossary_mismatch" in entry.meta["quality_reasons"]


# ── baldis 修复：rich text 标签参数 / UI 词版本后缀 / 译文引号内专名 ──


def test_rich_text_label_param_not_independent_lower_word():
    """rich text 标签参数（<color=red> 的 red、<size=50> 的 size）不是
    语义词：'<color=red>NULL NULL…' 的 NULL 是游戏内实体名，回显保留
    合理（baldis 实证：修复前 color=red 的 red 被当小写词 → 专名回显
    豁免失败）。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "us#28056", "reported",
        "<color=red>NULL NULL NULL NULL NULL NULL NULL NULL NULL</color>",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, entry.original) is True
    assert entry.meta["quality_passed"] is True


def test_ui_word_check_skips_last_version_word():
    """多词短语末位是版本后缀（'UCLA Gold' 的 Gold 是版本彩蛋名）→
    UI 词检查跳过末位词，回显放行（baldis 实证：Gold 在 UI 词典曾使
    专名回显豁免失败）。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "us#28985", "reported", "UCLA Gold",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, "UCLA Gold") is True

    # 单词仍全查：'Save Game' 首词 Save 在 UI 词典 → 回显仍判失败
    save = TextEntry(
        "us#1", "reported", "Save Game",
        meta={"role": "display", "disposition": "translate"},
    )
    assert translator._apply_quality(save, "Save Game") is False


def test_translated_quote_proper_name_exempt():
    """译文引号内全 TitleCase 专名短语（按钮 "Jump During Playtime" 的
    模型强调标记）→ 整短语豁免（baldis 实证 [6]：Button 类条目模型用
    引号包裹专名保留原文被当英文短语误判）。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "us#18434", "reported",
        "Square Button: Jump During Playtime's Jumprope Minigame",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(
        entry, "方形按钮：在游戏期间进行“Jump During Playtime”小游戏时使用。"
    ) is True


def test_translated_quote_proper_name_requires_source_word():
    """引号豁免要求每个词都在原文出现：'Jump Along' 的 Along 不在原文，
    是模型直译误译的专名 → 不豁免，仍判失败（baldis 实证 [7][8]：
    模型把 Jump During Playtime 误译成 Jump Along，不得放行）。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "us#19596", "reported",
        "X Button: Jump During Playtime's Jumprope Minigame",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(
        entry, "X按钮：在游戏过程中进行“Jump Along”小游戏"
    ) is False


# ── baldis 次轮修复：换行合并兜底 / 专名重译放宽 / 小写化专名 / 标签对 ──


def test_lowercased_proper_name_not_target_script_mismatch():
    """模型把原文 TitleCase 专名小写保留（Bossfight → bossfight）→
    专名保留不是漏翻（baldis [5] 实证：译文 '…在 bossfight 游戏模式
    中退出' 残留小写 bossfight）。UI 词典词除外（Save → save 真漏翻）。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "us#18122", "reported",
        "Triangle Button: Pause (Quit In The Bossfight Gamemode)",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(
        entry, "三角形按钮：暂停（在 bossfight 游戏模式中退出）") is True


def test_lowercased_ui_word_still_fails():
    """小写化专名豁免不放行 UI 词典词：'Save Game' 的 Save 被模型小写
    残留（save 游戏）→ 仍判失败。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "us#2", "reported", "Save Game Settings",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, "保存 save 游戏设置") is False


def test_complete_tag_pair_missing_is_not_mismatch():
    """完整标签对整体丢失（<color=green>Paused</color> → "暂停"，模型用
    引号替代彩色强调）→ 样式整对损失无崩溃风险、译文含中文 → 放行
    （baldis [2] 实证：1.8B 对彩色强调词的稳定行为）。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "asset#level16#140/str/0", "reported",
        "Farming Is Currently <color=green>Paused</color>.\n"
        "Do You Want To <color=red>Quit</color>?",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(
        entry, "农业目前处于“暂停”状态。\n您想<color=red>退出</color>吗？"
    ) is True


def test_partial_tag_missing_still_fails():
    """单个标签缺失（留开标签丢闭合标签）会破坏显示 → 仍判失败。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "k", "reported", "Press <color=red>E</color> to continue",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(
        entry, "按 <color=red>E 继续") is False


def test_brace_placeholder_missing_still_fails():
    """数据占位符 {0} 缺失会破坏运行时展开 → 标签对豁免不放行。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "k", "reported", "Lv.{0} <b>exp</b> needed",
        meta={"role": "display", "disposition": "translate"},
    )

    assert translator._apply_quality(entry, "所需等级经验") is False


def test_proper_name_reference_retry_without_lowercase_tail():
    """纯 TitleCase 专名（'Shirt Decal' 被模型补成 'T-shirt Decal'）→
    专名引用重译仍触发（baldis [1] 实证：模型把 Shirt 联想成 T-shirt）。"""
    translator = BatchTranslator(
        _ProperNameClient("Shirt Decal"), batch_size=1, concurrency=1,
        lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "f", "key_path": "k",
        "original": "Shirt Decal",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]

    retried = translator._retry_with_proper_name_reference(
        entry, translator.client.translate_text, "zh-CN", "T-shirt Decal")
    assert retried is not None
    # 注入的引用含 (Shirt, Shirt) 专名对（此前 has_translatable_tail
    # 拦截导致不触发重译，模型就把 Shirt 补成 T-shirt）
    assert ("Shirt", "Shirt") in translator.client.glossaries[-1]
    # 按引用保留专名的回显经 proper_name_echo 放行
    assert retried[2] is True
    assert retried[1] == "Shirt Decal"


# ── butterflies 修复：VSync 回显 / 引号黑话词 ──


def test_vsync_echo_passes_proper_name_echo():
    """VSync 是驼峰技术缩写 + UI 词典词：回显保留原文合理（butterflies
    实证：'VSync' 回显被判 target_script_mismatch——camel 豁免过了
    quality 门，proper_name_echo 的 UI 词检查却拦截）。驼峰缩写即使进
    UI 词典也允许 proper_name_echo 放行。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "us#2", "reported", "VSync",
        meta={"role": "display", "disposition": "translate"},
    )
    assert translator._apply_quality(entry, "VSync") is True
    # 对照：全大写 ≤3 字母缩写（SFX）是界面标准术语 + 1.8B 模型对单
    # token 稳定回显 → 回显豁免（count-my-coins 实证：重试耗尽仍回显）
    entry2 = TextEntry(
        "us#3", "reported", "SFX",
        meta={"role": "display", "disposition": "translate"},
    )
    assert translator._apply_quality(entry2, "SFX") is True
    # 对照：全大写 4 字母词典词（QUIT）不在缩写豁免 → 回显仍失败
    entry3 = TextEntry(
        "us#4", "reported", "QUIT",
        meta={"role": "display", "disposition": "translate"},
    )
    assert translator._apply_quality(entry3, "QUIT") is False


def test_quoted_blackword_in_chinese_translation_passes():
    """译文引号内保留原文俚语/黑话词 + 中文解释 → 本地化惯例，放行
    （butterflies 实证：'（……她刚才说的"funk"是什么意思？）' 的 funk
    是原文词且非 UI 词典词，模型保留+解释是合理行为）。引号内 UI 词典
    词（"play"）是真半翻 → 仍判失败。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "us#2", "reported", "(...the funk she just call me?)",
        meta={"role": "display", "disposition": "translate"},
    )
    assert translator._apply_quality(
        entry, "（……她刚才说的“funk”是什么意思？）") is True
    # 对照：引号内 UI 词典词（"play"）是真半翻 → 仍失败
    entry2 = TextEntry(
        "us#3", "reported", "Press play to start",
        meta={"role": "display", "disposition": "translate"},
    )
    assert translator._apply_quality(entry2, "按“play”键开始") is False


def test_chinese_source_text_kept_without_model_call():
    """原文即中文（游戏自带中文语言包）→ 首译前直接保留放行，不调用模型
    （containment 实证：Language/CH/*.subs '警卫' 曾被模型回译成英文判
    target_script_mismatch）。CJK 无假名即中文/韩文源。"""
    class CountingClient:
        config = SimpleNamespace(timeout=120.0)
        calls = 0

        def translate_text(self, *_args):
            self.calls += 1
            return "guard", Usage(1, 1)

    client = CountingClient()
    entry = _to_model([{
        "file_id": "ch", "key_path": "guard",
        "original": "警卫",
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])

    assert client.calls == 0
    assert stats.done == 1
    assert entry.translation == "警卫"
    assert entry.meta.get("language_source_kept") is True
    assert entry.meta.get("quality_passed") is True


def test_multilingual_source_kept_after_fallback_chain_exhausted():
    """西语/俄语源是 1.8B 模型能力边界：整条降级链（专名引用/双跳/
    同对象译例）全失败 → 保留原文放行 + language_source_kept 标记
    （containment 实证：37 条 Language/ES|RS 文件，玩家用 CH 语言包
    不可见）。日文源（假名）可译不兜底。"""
    class AlwaysEchoClient:
        config = SimpleNamespace(timeout=120.0)
        calls = 0

        def translate_text(self, *_args):
            self.calls += 1
            return "Obtuviste la Aleacion Anti-Telequinetica.", Usage(1, 1)

    client = AlwaysEchoClient()
    original = 'Obtuviste la "Aleación Anti-Telequinetica".'
    entry = _to_model([{
        "file_id": "es", "key_path": "k", "original": original,
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])

    assert stats.done == 1
    assert entry.translation == original
    assert entry.meta.get("language_source_kept") is True
    assert entry.meta.get("quality_passed") is True
    # 降级链确实试过（首译 + 专名重译 + 双跳），不是直接放行
    assert client.calls >= 3


def test_title_case_action_word_not_used_as_proper_name_reference():
    """TitleCase 动作词（Interact）不是专名：专名 references 重译不得
    注入 (Interact, Interact) 保留引用——模型会把整条短语当术语回显
    （containment 实证：'Interact hold' → 完整回显判 glossary_mismatch）。
    排除后走 UI retry 裸重译，模型直译 '交互保持'。"""
    class EchoThenTranslate:
        config = SimpleNamespace(timeout=120.0)
        calls = 0
        refs_seen = []

        def translate_text(self, source, _lang, glossary):
            self.calls += 1
            self.refs_seen.append(glossary)
            if self.calls == 1:
                return "Interact hold", Usage(1, 1)
            return "交互保持", Usage(1, 1)

    client = EchoThenTranslate()
    entry = _to_model([{
        "file_id": "ui", "key_path": "interact",
        "original": "Interact hold",
        "meta": {"role": "ui", "disposition": "translate"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])

    assert stats.done == 1
    assert entry.translation == "交互保持"
    # 任何一次调用的 references 都不得注入 (Interact, Interact)
    assert not any(
        any(src.casefold() == "interact" and tgt.casefold() == "interact"
            for src, tgt in refs)
        for refs in client.refs_seen)


def test_cyrillic_and_spanish_source_detection():
    """西里尔字母（俄语）与西语功能词 → multilingual 源判定（修复前：
    俄语西里尔完全不在检测中 → 无兜底 → 音译/英语译文判失败恒败；
    'No me veas' 无重音无旧表词 → 西语未被识别）。no/me 是英语高频
    词，单命中不判（'No matter' 仍是英语）。"""
    from hanhua.core.knowledge import _is_multilingual_source
    assert _is_multilingual_source("клипборд")
    assert _is_multilingual_source("Вы вставляете карту-ключ в слот")
    assert _is_multilingual_source("Пожалуйста, нажмите клавишу для выбора {0}")
    assert _is_multilingual_source("No me veas")
    assert _is_multilingual_source("El cazo de hierro")
    assert not _is_multilingual_source("No matter what happens")
    assert not _is_multilingual_source("Tell me the truth")


def test_cyrillic_source_kept_after_fallback_chain_exhausted():
    """俄语源（西里尔字母）是 1.8B 模型能力边界：整条降级链（专名
    引用/双跳/同对象译例）全失败 → 保留原文放行 + language_source_kept
    （containment 实证：клипборд/Привет 21 条真文本——修复前西里尔
    不在 multilingual 检测中 → 模型输出 Klipboard/Hello 音译或英语
    译文判 target_script_mismatch 恒败）。"""
    class AlwaysEchoClient:
        config = SimpleNamespace(timeout=120.0)
        calls = 0

        def translate_text(self, *_args):
            self.calls += 1
            return "Klipboard", Usage(1, 1)

    client = AlwaysEchoClient()
    original = "клипборд"
    entry = _to_model([{
        "file_id": "rs", "key_path": "k", "original": original,
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])

    assert stats.done == 1
    assert entry.translation == original
    assert entry.meta.get("language_source_kept") is True
    assert entry.meta.get("quality_passed") is True
    # 降级链确实试过（首译 + 双跳，单词无专名重译），不是直接放行
    assert client.calls >= 2


def test_language_name_echo_is_allowed():
    """语言名回显（Español：语言选择器显示原名是业界惯例）→ 放行
    （containment level*.assets 实证 6 条：Español 含独立小写词
    has_independent_lower_word → 原 proper_name_echo 分支失败）。"""
    class EchoClient:
        config = SimpleNamespace(timeout=120.0)

        def translate_text(self, *_args):
            return "Español", Usage(1, 1)

    client = EchoClient()
    entry = _to_model([{
        "file_id": "lvl", "key_path": "s", "original": "Español",
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])

    assert stats.done == 1
    assert entry.translation == "Español"
    assert entry.meta.get("quality_passed") is True


def test_source_noise_word_kept_in_chinese_translation():
    """原文非词典小写词（sdfsdfsdfsdfsdfsdf 开发者乱串）在中文译文
    保留 → 豁免放行（containment 实证 6 条：'Invert Mouse Y-Axis
    sdfsdfsdfsdfsdfsdf' 译文保留乱串被判 target_script_mismatch，
    修复前该词不在功能词/UI 词典也无邻接 TitleCase）。"""
    class TranslateClient:
        config = SimpleNamespace(timeout=120.0)

        def translate_text(self, *_args):
            return "反转鼠标 Y 轴 sdfsdfsdfsdfsdfsdf", Usage(1, 1)

    client = TranslateClient()
    entry = _to_model([{
        "file_id": "lvl1", "key_path": "k",
        "original": "Invert Mouse Y-Axis sdfsdfsdfsdfsdfsdf",
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])

    assert stats.done == 1
    assert entry.translation == "反转鼠标 Y 轴 sdfsdfsdfsdfsdfsdf"
    assert entry.meta.get("quality_passed") is True


def test_bullet_extra_placeholder_allowed():
    """行首星号规范化（" *Added bonus" → "* 加空格"）：extra 全为
    bullet 且无缺失 → 放行（containment Changelog 实证 4 条：模型把
    *Added 规范成 * 加空格，判 placeholder_mismatch 恒败）。模型
    新增的非 bullet 占位符（%s）仍判失败。"""
    from hanhua.core.quality import validate_translation_quality

    def _entry(original):
        return _to_model([{
            "file_id": "cl", "key_path": "p", "original": original,
        }])[0]

    bullet_ok = validate_translation_quality(
        _entry(" *Added bonus after beating demo"),
        "* 通关演示后可获得额外奖励。")
    assert bullet_ok.passed

    sigh_ok = validate_translation_quality(
        _entry("*SIGH*"), "* sigh *")
    assert sigh_ok.passed

    extra_fail = validate_translation_quality(
        _entry("save file"), "保存 %s 文件")
    assert not extra_fail.passed


def test_language_name_echo_allowed_with_obj_reference_pairs():
    """语言名回显在「同 obj 已有成功译文」时仍放行（containment
    level1-6 assets 实证 5 条：多语言数组里 English 先译成功 →
    _obj_reference_pairs 拒绝非多语言豁免 → Español 回显被判
    target_script_mismatch 恒败）。语言名保留原名是业界惯例，不受
    同 obj 译例影响。"""
    class MockClient:
        config = SimpleNamespace(timeout=120.0)

        def translate_text(self, text, *_args):
            if text == "English":
                return "英语", Usage(1, 1)
            return "Español", Usage(1, 1)

    client = MockClient()
    # 同 obj 多语言数组：English 先翻译成功填充 _obj_results，Español
    # 后判定时 _obj_reference_pairs 非空——语言名仍豁免
    entries = _to_model([
        {"file_id": "lvl1", "key_path": "a", "original": "English",
         "meta": {"asset_file": "level1", "obj": 4}},
        {"file_id": "lvl1", "key_path": "b", "original": "Español",
         "meta": {"asset_file": "level1", "obj": 4}},
    ])

    stats = BatchTranslator(client, batch_size=2, concurrency=1,
                            lang="en→zh-CN").run(entries)

    assert stats.done == 2
    assert entries[0].translation == "英语"
    assert entries[1].translation == "Español"
    assert entries[1].meta.get("quality_passed") is True


def test_stray_foreign_char_healed_in_translation():
    """译文混入外语单字（'该基金会의官方口号' 的韩文 의，Hy-MT2 中英
    翻译偶发）→ 自愈删除后通过；≥3 个外语字符（实质外语内容）不清洗
    仍判失败；回显的外语专名（原文含 ñ 等）不受影响（containment EN
    语言包实证 1 条）。"""
    class MockClient:
        config = SimpleNamespace(timeout=120.0)

        def translate_text(self, *_args):
            return "“SCP”代表“特殊收容程序”（也是该基金会의官方口号）。", Usage(1, 1)

    client = MockClient()
    entry = _to_model([{
        "file_id": "EN", "key_path": "s",
        "original": '"SCP" stands for "Special Containment Procedures"',
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])

    assert stats.done == 1
    assert "의" not in entry.translation
    assert entry.meta.get("quality_passed") is True


def test_stray_foreign_word_with_punctuation_healed():
    """混入块形态升级：独立韩文实义词带空格隔开、邻中文标点
    （'最致命的 상황；同时' 的 상황 = 情况，containment 字幕第 5 轮
    实证 2 条）→ 块前 8 字符与块后 8 字符内都有汉字即清洗（容忍空格/
    中文标点邻居）；句尾独立词（'爱丽丝 설정' 的 설정 后无汉字）仍
    不清洗——那是译文主体内容而非夹带。"""
    class MockClient:
        config = SimpleNamespace(timeout=120.0)

        def translate_text(self, *_args):
            return ("该生物展现出了非凡的运气，能够完全掌控哪怕是最致命的"
                    " 상황；同时，它还具有惊人的能力。"), Usage(1, 1)

    client = MockClient()
    entry = _to_model([{
        "file_id": "EN", "key_path": "s",
        "original": ("Subject demonstrates extraordinary luck, and is able "
                     "to fully control even the most fatal circumstances."),
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])

    assert stats.done == 1
    assert "상황" not in entry.translation
    assert "最致命的；同时" in entry.translation
    assert entry.meta.get("quality_passed") is True


def test_stray_foreign_char_not_removed_when_in_original():
    """回显的外语专名字符（原文含 ó/ñ，如 Stefánsson）在译文出现 →
    不清洗（非混入）。"""
    class EchoClient:
        config = SimpleNamespace(timeout=120.0)

        def translate_text(self, *_args):
            return "Stefánsson 是最棒的", Usage(1, 1)

    client = EchoClient()
    entry = _to_model([{
        "file_id": "x", "key_path": "s",
        "original": "Stefánsson is the best",
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])

    assert stats.done == 1
    assert "á" in entry.translation
    assert entry.meta.get("quality_passed") is True


# ── crash-back-in-time 修复：连字符拼写变体 / 单残留词补译 ──


def test_hyphenated_spelling_variant_not_target_script_mismatch():
    """连字符拼写变体豁免：原文连写词（hihat）在译文按标准写法拆分
    （Hi-hat 是踩镲标准名）→ 译文连字符词去连字符后等于原文词 →
    分词残留（hat）是合法拼写变体，放行（crash-back-in-time
    'hihat cymbal'→'Hi-hat 钹' 实证：hat 被当普通词残留误判恒败）。
    反例：原文不含连写词时 hat 残留照常失败。"""
    translator = BatchTranslator(
        FakeClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", "hihat cymbal",
        meta={"role": "display", "disposition": "translate"},
    )
    assert translator._apply_quality(entry, "Hi-hat 钹") is True
    # 反例：原文无 hihat 连写词 → hat 残留仍判失败
    entry2 = TextEntry(
        "ui", "reported", "cymbal sound",
        meta={"role": "display", "disposition": "translate"},
    )
    assert translator._apply_quality(entry2, "hat 声音") is False


class _WarpResidueClient(BaseClient):
    """translate_text 模拟单残留词补译：裸翻译回显 warp（模型确认
    保留该术语）→ word_residue_exempt 豁免放行。"""

    def __init__(self):
        self.calls = []

    def chat(self, system, messages):
        return "[]", Usage(0, 0)

    def translate_text(self, source, _target_lang, glossary):
        self.calls.append(source.strip())
        if source.strip() == "warp":
            return "warp", Usage(10, 5)
        return "译文", Usage(10, 5)


def test_repair_word_residue_covers_single_word():
    """词级补译扩展覆盖单残留词：译文残留孤立小写词（'…warp 房间…'，
    模型对游戏术语半保留）→ 补译该词 → 模型回显 → 确认保留豁免放行
    （crash-back-in-time Uka-Uka 审判邀请 实证：首译高质量仅 warp 残留，
    旧判定只处理英文短语 → 重试耗尽恒败）。"""
    translator = BatchTranslator(
        _WarpResidueClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "level0", "key_path": "asset#level0#3893/str/0",
        "original": ("You collected an invitation to an Uka-Uka Trial. "
                     "You can access these levels from the basement, by "
                     "standing in the middle of the warp room."),
        "translation": ("您收到了参加 Uka-Uka 审判的邀请。您可以从地下室"
                        "进入这些关卡，只需站在 warp 房间的中央即可。"),
        "status": "failed",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]
    assert translator._repair_word_residue(
        entry, translator.client.translate_text,
        "zh-CN", entry.translation)[2] is True


def test_single_word_residue_without_translation_context_fails():
    """对照：单残留词补译的豁免要求词在原文（防模型幻觉）——原文不含
    warp 时 warp 残留仍判失败。"""
    translator = BatchTranslator(
        _WarpResidueClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = TextEntry(
        "ui", "reported", "You can access these levels from the basement",
        meta={"role": "display", "disposition": "translate"},
    )
    assert translator._apply_quality(
        entry, "您可以从地下室进入这些关卡，站在 warp 房间的中央即可。") is False


class _LabelValueClient(BaseClient):
    """translate_text 对标签词输出中文译文（HUD 计数标签）。"""

    def __init__(self, out):
        self.out = out
        self.calls = []

    def chat(self, system, messages):
        return "[]", Usage(0, 0)

    def translate_text(self, source, _target_lang, glossary):
        self.calls.append(source.strip())
        return self.out, Usage(5, 3)


def test_repair_word_residue_translates_label_value_format():
    """标签-值格式串（'slash: 999' → 模型 'Slash: 999' 大小写规范化
    回显，deadbeat 实证）：TitleCase 检查把它当专名跳过 → 按原文
    形态恢复标签整词补译，保留 ': 999' 值格式，大小写不敏感替换。"""
    translator = BatchTranslator(
        _LabelValueClient("斩击"), batch_size=1, concurrency=1,
        lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "level1", "key_path": "asset#level1#901/str/0",
        "original": "slash: 999",
        "translation": "Slash: 999",
        "status": "failed",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]
    result = translator._repair_word_residue(
        entry, translator.client.translate_text, "zh-CN", "Slash: 999")
    assert result is not None and result[2] is True
    assert result[1] == "斩击: 999"
    # 裸译 + 逐词引用确认两跳（短语补译第二意见的既有设计）
    assert translator.client.calls == ["slash", "slash"]


def test_repair_word_residue_label_echo_fails_without_translation():
    """对照：标签补译输出非中文（模型回显标签）→ 维持失败（防漏翻）。"""
    translator = BatchTranslator(
        _LabelValueClient("slash"), batch_size=1, concurrency=1,
        lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "level1", "key_path": "asset#level1#901/str/0",
        "original": "slash: 999",
        "translation": "Slash: 999",
        "status": "failed",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]
    result = translator._repair_word_residue(
        entry, translator.client.translate_text, "zh-CN", "Slash: 999")
    assert result is None or result[2] is False


class _MaxResidueClient(BaseClient):
    """translate_text 对全大写词典词 MAX 输出中文（'最大'）。"""

    def __init__(self):
        self.calls = []

    def chat(self, system, messages):
        return "[]", Usage(0, 0)

    def translate_text(self, source, _target_lang, glossary):
        if source.strip() == "MAX":
            return "最大", Usage(10, 5)
        return "译文", Usage(10, 5)


def test_repair_word_residue_covers_upper_ui_dict_word():
    """全大写 UI 词典词补译（F6，deepest-sword 'MAX SEARCH OPTIMIZED'
    实证）：MAX 在 UI 词典（=最大）是普通语义词不是专名——全大写形态
    旧判定跳过补译（当专名）→ 模型对全大写稳定回显 → 恒败。修复：
    全大写 + 词典词 → 可补译，补译输出中文 → 替换放行。"""
    translator = BatchTranslator(
        _MaxResidueClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "Assembly-CSharp.dll", "key_path": "us#11152",
        "original": "MAX SEARCH OPTIMIZED",
        "translation": "MAX 搜索优化版",
        "status": "failed",
        "meta": {"role": "display", "disposition": "translate",
                 "confidence": "medium"},
    }])[0]
    good = translator._repair_word_residue(
        entry, translator.client.translate_text,
        "zh-CN", entry.translation)
    assert good[2] is True
    assert "最大" in entry.translation
    assert "MAX" not in entry.translation


def test_repair_word_residue_skips_upper_non_dict_proper():
    """对照：全大写非词典词（Gamejolt 类专名）维持跳过补译——专名
    形态按专名路径处理，不误进词级补译。"""
    translator = BatchTranslator(
        _MaxResidueClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "level0", "key_path": "asset#level0#1/str/0",
        "original": "Visit GAMEJOLT page",
        "translation": "访问 GAMEJOLT 页面",
        "status": "failed",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]
    # 无残留词可补译（GAMEJOLT 非词典词不提取）→ 返回 None 维持原判定
    assert translator._repair_word_residue(
        entry, translator.client.translate_text,
        "zh-CN", entry.translation) is None


class _SpacedActionClient(BaseClient):
    """translate_text 收到聚合版原文后输出中文（F8-A：1.8B 对原形态
    '* Y A W N *' 稳定回显，聚合 '* YAWN *' 后能译且标签原位保留——
    mock 模拟聚合后的正确模型行为）。"""

    def __init__(self):
        self.calls = []

    def chat(self, system, messages):
        return "[]", Usage(0, 0)

    def translate_text(self, source, _target_lang, _glossary):
        self.calls.append(source)
        return "{punch=3,2}* 哈欠 *{w=3}{x}", Usage(10, 5)


def test_repair_spaced_action_translation_aggregates_and_translates():
    """F8-A：词典未收录的间隔动作词走模型路径——聚合为正常词后模型
    译出中文且标签原位保留。断言：translate_text 收到聚合版原文、
    译文通过质量门（占位符齐全顺序对 + 含中文）。"""
    translator = BatchTranslator(
        _SpacedActionClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "level1", "key_path": "asset#level1#2907/str/0",
        "original": "{punch=3,2}* Z I P P E R *{w=3}{x}",
        "translation": "{punch=3,2}* Z I P P E R *{w=3}{x}",
        "status": "failed",
        "meta": {"role": "display", "disposition": "translate",
                 "confidence": "high"},
    }])[0]
    good = translator._repair_spaced_action_translation(
        entry, translator.client.translate_text, "zh-CN")
    assert good[2] is True
    assert translator.client.calls[0] == "{punch=3,2}* ZIPPER *{w=3}{x}"
    assert "哈欠" in entry.translation
    assert "{punch=3,2}" in entry.translation
    assert "{w=3}{x}" in entry.translation


def test_repair_spaced_action_lexicon_direct_fill_for_catalogued_words():
    """F10-A：词典收录词（YAWN）词典直填优先，模型不被调用
    （1.8B 对聚合形态仍回显，词典是确定性正确路径）。"""
    translator = BatchTranslator(
        _SpacedActionClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "level1", "key_path": "asset#level1#2908/str/0",
        "original": "{punch=3,2}* Y A W N *{w=3}{x}",
        "translation": "{punch=3,2}* Y A W N *{w=3}{x}",
        "status": "failed",
        "meta": {"role": "display", "disposition": "translate",
                 "confidence": "high"},
    }])[0]
    good = translator._repair_spaced_action_translation(
        entry, translator.client.translate_text, "zh-CN")
    assert good[2] is True
    assert translator.client.calls == []      # 词典直填，不走模型
    assert "打哈欠" in entry.translation
    assert "{punch=3,2}" in entry.translation
    assert "{w=3}{x}" in entry.translation


class _EchoAggregatedClient(_SpacedActionClient):
    """模型只回显聚合形态（'* SCOFF *'，2026-08-11 实测 1.8B 对
    SCOFF/SIGH/YAWN/GASP 稳定行为）→ 词典兜底必须接管。"""

    def translate_text(self, source, _target_lang, _glossary):
        self.calls.append(source)
        return source, Usage(10, 5)


def test_repair_spaced_action_lexicon_takes_over_when_model_echoes():
    """F10-A：聚合后模型仍回显（只去空格不翻译）→ 封闭词典直填中文，
    不走模型结果。a-catfiends 剩余 3 条失败中的 SCOFF/SIGH 实证。"""
    translator = BatchTranslator(
        _EchoAggregatedClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "level1", "key_path": "asset#level1#3101/str/0",
        "original": "{punch=3,2}* S C O F F *{w=3}{x}",
        "translation": "{punch=3,2}* S C O F F *{w=3}{x}",
        "status": "failed",
        "meta": {"role": "display", "disposition": "translate",
                 "confidence": "high"},
    }])[0]
    good = translator._repair_spaced_action_translation(
        entry, translator.client.translate_text, "zh-CN")
    assert good[2] is True
    assert "嗤笑" in entry.translation
    assert "{punch=3,2}" in entry.translation
    assert "{w=3}{x}" in entry.translation


def test_repair_spaced_action_translation_skips_non_spaced():
    """对照：无间隔词的原文（普通句子）聚合无变化 → 返回 None 交后续
    降级链（不触发聚合重译）。"""
    translator = BatchTranslator(
        _SpacedActionClient(), batch_size=1, concurrency=1, lang="en→zh-CN")
    entry = _to_model([{
        "file_id": "level1", "key_path": "asset#level1#3046/str/0",
        "original": "I am {punch=3,2}NOT who I used to be.{w=3}{x}",
        "translation": "我已经不再是曾经的我了。{punch=3,2}",
        "status": "failed",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]
    assert translator._repair_spaced_action_translation(
        entry, translator.client.translate_text, "zh-CN") is None
    assert translator.client.calls == []


# ── Q3 失败分类 + attempt 预算 ──
def test_failure_records_category_and_attempt_count():
    """Q3：失败记账写 failure_category（策略类别）+ attempt_count 跨轮累计。"""
    from hanhua.core.batch_translator import (
        _record_failure_attempt, _attempt_exhausted)
    from hanhua.core.models import TextEntry
    e = TextEntry(file_id="f", key_path="k", original="Hello world",
                  status="pending", meta={})
    _record_failure_attempt(e, "untranslated_text")
    assert e.meta["attempt_count"] == 1
    assert e.meta["failure_category"] == "model_behavior"
    # 同轮重复记账幂等（repair 路径防重复计账）
    _record_failure_attempt(e, "placeholder_missing")
    assert e.meta["attempt_count"] == 1
    # 预算未耗尽 → 可再跑
    assert not _attempt_exhausted(e)
    # 跨轮重跑：store 重新加载 → 新对象 → attempt 继续累计
    e2 = TextEntry(file_id="f", key_path="k", original="Hello world",
                   status="failed", meta=dict(e.meta))
    _record_failure_attempt(e2, "untranslated_text")
    assert e2.meta["attempt_count"] == 2
    # model_behavior 预算 2 → 耗尽
    assert _attempt_exhausted(e2)


def test_request_category_has_higher_budget():
    """Q3：request 类（API 故障可恢复）预算 3；content_inherent 预算 1。"""
    from hanhua.core.batch_translator import (
        _record_failure_attempt, _attempt_exhausted, _MAX_ATTEMPTS)
    from hanhua.core.models import TextEntry
    assert _MAX_ATTEMPTS["request"] >= 3
    assert _MAX_ATTEMPTS["model_behavior"] == 2
    assert _MAX_ATTEMPTS["content_inherent"] == 1
    e = TextEntry(file_id="f", key_path="k", original="Hello",
                  status="failed", meta={})
    for _ in range(3):
        # 每轮 store 重新加载 → 新对象 → attempt 累计
        e = TextEntry(file_id="f", key_path="k", original="Hello",
                      status="failed", meta=dict(e.meta))
        _record_failure_attempt(e, "request_error")
    assert e.meta["failure_category"] == "request"
    assert _attempt_exhausted(e)


def test_content_inherent_assigned_for_symbol_only_text():
    """Q3 C1：content_inherent 赋值路径——原文无自然语言（纯符号装饰行/
    纯数字/版本串），失败即归此类，单次验证后不再重试（不白烧 token）。"""
    from hanhua.core.batch_translator import (
        _record_failure_attempt, _attempt_exhausted)
    from hanhua.core.models import TextEntry
    for text in ("-----", "***", "♪ ♪ ♪", "12345", "v1.2.3", "…———…"):
        e = TextEntry(file_id="f", key_path="k", original=text,
                      status="failed", meta={})
        _record_failure_attempt(e, "untranslated_text")
        assert e.meta["failure_category"] == "content_inherent", text
        # content_inherent 预算 1：首次失败即耗尽
        assert _attempt_exhausted(e), text


def test_content_inherent_not_assigned_for_natural_language():
    """Q3 C1 保守边界：带自然语言内容的原文（英文/汉字/假名/URL）不归
    content_inherent——宁可重试，不可误判可译文本为不可译。"""
    from hanhua.core.batch_translator import _record_failure_attempt
    from hanhua.core.models import TextEntry
    for text in ("Hello world", "继续游戏", "こんにちは", "戦争",
                 "https://example.com/", "© 2024 Game Studio"):
        e = TextEntry(file_id="f", key_path="k", original=text,
                      status="failed", meta={})
        _record_failure_attempt(e, "untranslated_text")
        assert e.meta["failure_category"] == "model_behavior", text


def test_attempt_budget_resets_when_rules_version_changes(monkeypatch):
    """Q3 C2：预算挂规则版本戳——版本变化（规则升级）后旧预算自动清零，
    耗尽条目重新进入翻译链（「规则修复→定向重跑」自动生效）。"""
    from hanhua.core.batch_translator import (
        _record_failure_attempt, _attempt_exhausted, _rules_version)
    from hanhua.core.models import TextEntry
    e = TextEntry(file_id="f", key_path="k", original="Hello world",
                  status="failed", meta={})
    _record_failure_attempt(e, "untranslated_text")
    assert e.meta["attempt_count"] == 1
    assert e.meta["_rules_version"] == _rules_version()
    # 版本不一致（规则已升级）→ 旧预算失效，放行重跑
    e.meta["_rules_version"] = _rules_version() - 1
    assert not _attempt_exhausted(e)
    # 版本一致 → 预算照常生效（attempt_count=2 达 model_behavior 上限）
    e.meta["_rules_version"] = _rules_version()
    e.meta["attempt_count"] = 2
    assert _attempt_exhausted(e)
    # 存量条目无版本戳（升级前记账）→ 同样放行重跑，再记账补版本
    e2 = TextEntry(file_id="f", key_path="k", original="Hello world",
                   status="failed", meta={"attempt_count": 9})
    assert not _attempt_exhausted(e2)
    _record_failure_attempt(e2, "untranslated_text")
    assert e2.meta["attempt_count"] == 10
    assert e2.meta["_rules_version"] == _rules_version()


def test_rules_version_changes_with_rule_implementation(monkeypatch):
    """Q3 C2：版本从规则函数字节码派生——规则实现变化（模拟升级）→ 版本变化。"""
    import hanhua.core.batch_translator as bt
    v1 = bt._rules_version()
    monkeypatch.setattr(bt, "_RULES_VERSION_CACHE", None)
    monkeypatch.setattr(bt, "_inherent_untranslatable", lambda text: False)
    v2 = bt._rules_version()
    assert v1 != v2


def test_force_retry_exhausted_overrides_budget():
    """Q3 C2：force_retry_exhausted 显式开关——预算耗尽条目也强制重跑
    （修复后定向重跑，不依赖规则版本变化）。"""
    from hanhua.core.batch_translator import (
        BatchTranslator, _record_failure_attempt)
    from hanhua.core.models import TextEntry
    entry = TextEntry(
        "f", "k", "Hello, my name is Mitch.", status="failed", meta={
            "role": "display", "disposition": "translate", "confidence": "high",
        })
    _record_failure_attempt(entry, "untranslated_text")
    # 跨轮重跑：store 重新加载 → 新对象 → attempt 继续累计
    entry = TextEntry("f", "k", "Hello, my name is Mitch.",
                      status="failed", meta=dict(entry.meta))
    _record_failure_attempt(entry, "untranslated_text")

    client = FakeClient(mapping={"Hello, my name is Mitch.": "你好，我叫米奇。"})
    bt = BatchTranslator(client, batch_size=1, concurrency=1, lang="en→zh-CN")
    stats = bt.run([entry])
    assert client.calls == 0          # 预算耗尽：默认不进 run_scope
    assert stats.total == 0

    entry2 = TextEntry(
        "f", "k", "Hello, my name is Mitch.", status="failed", meta=dict(entry.meta))
    stats = bt.run([entry2], force_retry_exhausted=True)
    assert client.calls == 1          # 强制开关：预算耗尽也重跑
    assert stats.done == 1
    assert entry2.status == "translated"


# ── Q1 语义门（BUILTIN_UI_REFERENCES 进质量门）+ Q2 记忆毒化防护 ──
def test_builtin_ui_wrong_translation_rejected():
    """Q1：'Resume'→'简历'（形式门全过：有中文/占位符齐）被语义门拦截——
    BUILTIN_UI_REFERENCES 参考译文 '继续' 不在译文中。"""
    from hanhua.core.batch_translator import BatchTranslator
    from hanhua.core.models import TextEntry
    bt = BatchTranslator(FakeClient())
    e = TextEntry(file_id="f", key_path="k", original="Resume",
                  status="pending",
                  meta={"role": "display", "disposition": "translate"})
    assert not bt._apply_quality(e, "简历")
    assert e.meta["quality_passed"] is False
    assert e.meta["quality_reasons"] == ["builtin_ui_mismatch"]
    assert e.meta["failure_category"] == "model_behavior"


def test_builtin_ui_correct_translation_passes():
    """Q1：'Resume'→'继续'（参考译文子串）与 '继续游戏'（宽容变体）均通过。"""
    from hanhua.core.batch_translator import BatchTranslator
    from hanhua.core.models import TextEntry
    for translation in ("继续", "继续游戏"):
        # 独立实例（一致性锚定同实例同原文强制一致，属另一机制）
        bt = BatchTranslator(FakeClient())
        e = TextEntry(file_id="f", key_path="k", original="Resume",
                      status="pending",
                      meta={"role": "display", "disposition": "translate"})
        assert bt._apply_quality(e, translation), translation
        assert e.meta["quality_passed"] is True


def test_builtin_ui_gate_respects_user_glossary_override():
    """Q1：用户 glossary 覆盖 'Resume' 后语义门停用（merge 已移除内置 pair）。"""
    from hanhua.core.batch_translator import BatchTranslator
    from hanhua.core.models import TextEntry
    bt = BatchTranslator(FakeClient(), glossary=(("Resume", "恢复"),))
    assert "resume" not in bt.builtin_ui_exact
    e = TextEntry(file_id="f", key_path="k", original="Resume",
                  status="pending",
                  meta={"role": "display", "disposition": "translate"})
    assert bt._apply_quality(e, "恢复")


# ── 2026-08-14 用户实证：play 反复译「播放」——确定性直填 ─────────

def test_play_filled_deterministically_without_model_call():
    """play→播放 根因修复：原文精确命中内置 UI 引用（Play→开始）→
    确定性直填权威译文，零模型调用。

    prompt 注入（references）与 Q1 质量门都只能「引导/拦截标记」——
    4B/1.8B 仍可能无视引用输出「播放」、或重试复败。直填表
    （_glossary_exact 并入 builtin_ui_exact）保证精确命中时根本不
    经过模型，结果必然正确。"""
    from hanhua.core.batch_translator import BatchTranslator
    from hanhua.core.models import TextEntry
    client = FakeClient()
    entry = TextEntry(file_id="f", key_path="k", original="Play",
                      status="pending",
                      meta={"role": "display", "disposition": "translate",
                            "confidence": "high"})
    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])
    assert client.calls == 0, "内置 UI 引用直填必须零模型调用"
    assert stats.done == 1 and stats.failed == 0
    assert entry.status == "translated"
    assert entry.translation == "开始"


def test_play_mismatch_translation_still_gated_by_q1():
    """play 加内置引用后 Q1 语义门同步生效：译「播放」必被拦。"""
    from hanhua.core.batch_translator import BatchTranslator
    from hanhua.core.models import TextEntry
    bt = BatchTranslator(FakeClient())
    e = TextEntry(file_id="f", key_path="k", original="Play",
                  status="pending",
                  meta={"role": "display", "disposition": "translate"})
    assert not bt._apply_quality(e, "播放")
    assert e.meta["quality_reasons"] == ["builtin_ui_mismatch"]


# ── 2026-08-31 用户实证：Disabled 残疾人士 vs 已禁用 ──────────────────

def test_disabled_filled_deterministically_without_model_call():
    """Disabled 精确命中内置 UI 引用 → 确定性直填「已禁用」，零模型调用——
    杜绝本地模型误译「残疾人士」再被审核幻觉 PASS 的污染链。"""
    from hanhua.core.batch_translator import BatchTranslator
    from hanhua.core.models import TextEntry
    client = FakeClient()
    entry = TextEntry(file_id="f", key_path="k", original="Disabled",
                      status="pending",
                      meta={"role": "display", "disposition": "translate",
                            "confidence": "high"})
    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])
    assert client.calls == 0, "内置 UI 引用直填必须零模型调用"
    assert stats.done == 1 and stats.failed == 0
    assert entry.status == "translated"
    assert entry.translation == "已禁用"


def test_disabled_wrong_translation_rejected_by_q1():
    """Q1 语义门：Disabled→残疾人士 必被拦（权威译名 已禁用 不在译文）。"""
    from hanhua.core.batch_translator import BatchTranslator
    from hanhua.core.models import TextEntry
    bt = BatchTranslator(FakeClient())
    e = TextEntry(file_id="f", key_path="k", original="Disabled",
                  status="pending",
                  meta={"role": "display", "disposition": "translate"})
    assert not bt._apply_quality(e, "残疾人士")
    assert e.meta["quality_reasons"] == ["builtin_ui_mismatch"]
    assert e.meta["failure_category"] == "model_behavior"


def test_enabled_filled_deterministically_without_model_call():
    """Enabled → 已启用 确定性直填（与 Disabled 同源）。"""
    from hanhua.core.batch_translator import BatchTranslator
    from hanhua.core.models import TextEntry
    client = FakeClient()
    entry = TextEntry(file_id="f", key_path="k", original="Enabled",
                      status="pending",
                      meta={"role": "display", "disposition": "translate",
                            "confidence": "high"})
    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])
    assert client.calls == 0
    assert entry.translation == "已启用"


# ── AgentMemory 集成（2026-08-12 记忆模块） ──────────────────────────

def _mem_store(tmp_path):
    from hanhua.core.agent_memory import AgentMemory
    mem = AgentMemory(tmp_path / "agent.db")
    mem.init_schema()
    return mem


def test_agent_memory_direct_applies_high_confidence_phrase(tmp_path):
    """跨游戏高置信短语记忆 → 翻译前直接应用（模型不调用）+ 采纳反馈。"""
    mem = _mem_store(tmp_path)
    for g in ("g1", "g2", "g3"):
        mem.propose("Press Start now", "现在按开始", g, role="display")
    entry = TextEntry(
        "f", "k", "Press Start now", meta={
            "role": "display", "disposition": "translate",
            "confidence": "high"})
    client = FakeClient()
    stats = BatchTranslator(
        client, batch_size=1, concurrency=1, memory=None,
        agent_memory=mem, agent_game="demo-game", lang="en→zh-CN",
    ).run([entry])
    assert client.calls == 0            # 未调用模型
    assert stats.done == 1
    assert entry.status == "translated"
    assert entry.translation == "现在按开始"
    assert stats.from_memory >= 1
    assert mem.list_all()[0]["hits"] == 1  # 采纳反馈


def test_agent_memory_rejected_direct_apply_retires(tmp_path):
    """直接应用被质量门拒绝 → 反馈降级 → 2 次拒绝后退休。"""
    mem = _mem_store(tmp_path)
    for g in ("g1", "g2", "g3"):
        mem.propose("TOSS TRASH", "TOSS TRASH",
                    g, role="display")  # 回显译文 = 质量门必拒
    for _ in range(2):
        # 每轮重建条目（模拟跨场次；同一库累计 rejects）
        entry = TextEntry(
            "f", "k", "TOSS TRASH", meta={
                "role": "display", "disposition": "translate",
                "confidence": "high"})
        stats = BatchTranslator(
            FakeClient(), batch_size=1, concurrency=1, memory=None,
            agent_memory=mem, agent_game="demo-game",
        ).run([entry])
        assert stats.done == 1          # 直接应用被拒 → 模型兜底翻译成功
        # 被拒痕迹：该轮先尝试了记忆直接应用（rejects+1）
        assert "agent_memory_rejected_reasons" in entry.meta
    row = mem.list_all()[0]
    assert row["rejects"] == 2
    assert row["status"] == "retired"   # 2 次确认不可信
    # 第三轮：退休后不再尝试直接应用（无被拒痕迹）→ 纯模型翻译
    entry = TextEntry(
        "f", "k", "TOSS TRASH", meta={
            "role": "display", "disposition": "translate",
            "confidence": "high"})
    stats = BatchTranslator(
        FakeClient(mapping={"TOSS TRASH": "丢垃圾"}),
        batch_size=1, concurrency=1, memory=None,
        agent_memory=mem, agent_game="demo-game",
    ).run([entry])
    assert stats.done == 1
    assert entry.translation == "丢垃圾"
    assert "agent_memory_rejected_reasons" not in entry.meta


def test_glossary_force_excludes_reference_pairs_from_quality_gate(tmp_path):
    """glossary_force 只作质量门强制，glossary 其余词对只做参考注入
    （Morfosi 64 条同因全灭实证）：经验记忆词对 ('Locked','锁定') 若
    并入强制词对，"The door is locked" 译文「门锁着」被判
    glossary_mismatch 失败。修复：GUI/runner 只把术语库 active + 知识
    库译例传入 glossary_force，记忆词对保留参考注入与精确直填但不
    强制（reference_pairs 设计即「参考而非强制」）。"""
    entry = TextEntry("f", "k", "The door is locked", meta={
        "role": "display", "disposition": "translate", "confidence": "high"})
    client = FakeClient(mapping={"The door is locked": "门锁着"})
    stats = BatchTranslator(
        client, batch_size=1, concurrency=1, memory=None,
        lang="en→zh-CN",
        glossary=[("Locked", "锁定")],     # 参考注入（记忆词对形态）
        glossary_force=[("Door", "门")],    # 质量门强制仅此
    ).run([entry])
    assert stats.done == 1
    assert entry.status == "translated"
    assert entry.translation == "门锁着"
    assert "glossary_mismatch" not in (entry.quality_reasons or ())


def test_agent_memory_proposes_successful_translations(tmp_path):
    """模型翻译成功（质量门通过+非回显）→ 经验记忆提案。"""
    mem = _mem_store(tmp_path)
    entry = TextEntry("f", "k", "Hello friend", meta={
        "role": "display", "disposition": "translate", "confidence": "high"})
    client = FakeClient(mapping={"Hello friend": "你好朋友"})
    BatchTranslator(
        client, batch_size=1, concurrency=1, memory=None,
        agent_memory=mem, agent_game="demo-game", lang="en→zh-CN",
    ).run([entry])
    assert entry.status == "translated"
    row = mem.list_all()[0]
    assert row["evidence_count"] == 1
    assert row["status"] == "pending"          # 单次翻译只是提案
    assert row["source_game"] == "demo-game"


def test_agent_memory_skips_echo_exempt_entries(tmp_path):
    """回显豁免条目不进经验记忆（Q4：结构串不污染记忆）。"""
    mem = _mem_store(tmp_path)
    entry = TextEntry("f", "k", "Prologue", meta={
        "role": "display", "disposition": "translate", "confidence": "high",
        "echo_exempt": "proper_name"})
    client = FakeClient(mapping={"Prologue": "序章"})
    BatchTranslator(
        client, batch_size=1, concurrency=1, memory=None,
        agent_memory=mem, agent_game="demo-game",
    ).run([entry])
    assert entry.status == "translated"
    assert mem.count() == 0


def test_agent_memory_skips_language_source_kept_entries(tmp_path):
    """C3：语言保持条目（原文已是目标语言，译文=原文）不进经验记忆——
    「原文→原文」沉淀毒化，后续命中直接复用不翻译。"""
    mem = _mem_store(tmp_path)
    entry = TextEntry("f", "k", "继续游戏", meta={
        "role": "display", "disposition": "translate", "confidence": "high",
        "language_source_kept": True})
    client = FakeClient(mapping={"继续游戏": "继续游戏"})
    BatchTranslator(
        client, batch_size=1, concurrency=1, memory=None,
        agent_memory=mem, agent_game="demo-game",
    ).run([entry])
    assert entry.status == "translated"
    assert mem.count() == 0


def test_language_source_kept_not_added_to_working_memory():
    """C3：语言保持条目不进工作记忆（ProjectStore.memory）——与经验
    记忆双门一致，防「原文→原文」污染跨游戏一致性锚定。"""
    import tempfile
    store = ProjectStore(Path(tempfile.mkdtemp()) / "kept.db")
    store.init_schema()
    kept_entry = _to_model([{
        "file_id": "ui", "key_path": "title/1",
        "original": "游戏设置",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]
    normal_entry = _to_model([{
        "file_id": "ui", "key_path": "menu/2",
        "original": "Open Door",
        "meta": {"role": "display", "disposition": "translate"},
    }])[0]
    client = FakeClient({
        "游戏设置": "游戏设置",          # 语言保持（原文已是中文）
        "Open Door": "打开门",
    })
    BatchTranslator(
        client, memory=store, model="m", lang="en→zh-CN",
        batch_size=1, concurrency=1,
    ).run([kept_entry, normal_entry])

    assert kept_entry.meta.get("language_source_kept") is True
    assert "游戏设置" not in store.get_memory_hits(
        ["游戏设置"], "m", "en→zh-CN")
    # Phase B：批记忆 pending 桶，审后结算提交后可见（语言保持仍排除）
    settle_translation_memory(store, [kept_entry, normal_entry],
                              "m", "en→zh-CN")
    assert "游戏设置" not in store.get_memory_hits(
        ["游戏设置"], "m", "en→zh-CN")
    assert store.get_memory_hits(["Open Door"], "m", "en→zh-CN") == {
        "Open Door": "打开门",
    }


class FailingOnceClient(FakeClient):
    """F42：前 2 批抛 ServiceUnavailableError（模拟服务死亡），之后正常。"""

    def __init__(self, mapping=None, fail_batches: int = 2):
        super().__init__(mapping)
        self.batch_calls = 0
        self.fail_batches = fail_batches

    def chat(self, system, messages):
        self.batch_calls += 1
        if self.batch_calls <= self.fail_batches:
            from hanhua.core.translator import ServiceUnavailableError
            raise ServiceUnavailableError("翻译服务不可达（测试模拟）")
        return super().chat(system, messages)


def test_service_restart_callback_invoked_on_unavailable():
    """F42（8morelives 实证）：服务死亡（ServiceUnavailableError 连续
    批次）→ service_restart 回调被调用（调用方重新拉起服务），后续批
    在新服务上继续，不丢失翻译进度。"""
    from hanhua.core.models import STATUS_TRANSLATED
    store = ProjectStore(Path(tempfile.mkdtemp()) / "f42.db")
    store.init_schema()
    entries = [TextEntry(file_id="f", key_path=f"k{i}",
                         original=f"text{i}") for i in range(4)]
    restarts = []

    def on_restart():
        restarts.append(1)

    client = FailingOnceClient({t.original: "译文" for t in entries},
                               fail_batches=3)
    bt = BatchTranslator(client, memory=store, model="m",
                         lang="en→zh-CN", batch_size=1, concurrency=1,
                         service_restart=on_restart)
    bt.run(entries)
    assert restarts, "服务死亡应触发重启回调"
    assert len(restarts) == 1, "连续失败只重启一次"
    assert sum(1 for e in entries if e.status == STATUS_TRANSLATED) >= 1, \
        "重启后剩余条目应翻译成功"


class _BrandEchoDirectiveClient(BaseClient):
    """2026-08-26 'Out of the Loop studio' 回归：native 英文 prompt 稳定
    回显（回显原文），中文显式指令路径（translate_source_directive +
    逐词补译指令）输出正确译文 'Out of the Loop 工作室'。"""

    def __init__(self):
        self.config = SimpleNamespace(timeout=120.0)
        self.calls = 0

    def translate_text(self, *_args):
        self.calls += 1
        return "Out of the Loop studio", Usage(1, 1)

    def chat(self, system, messages):
        self.calls += 1
        content = messages[0]["content"]
        if "请将以下文本翻译为简体中文" in content:
            return "Out of the Loop 工作室", Usage(1, 1)
        if "请将以下名称翻译为简体中文" in content:
            return "Out of the Loop 工作室", Usage(1, 1)
        return "Out of the Loop studio", Usage(1, 1)


def test_brand_echo_translated_via_chinese_directive():
    """品牌/工作室名纯回显（'Out of the Loop studio' containment 实证：
    native 英文 prompt 稳定回显 → 首译 failed → proper_name 引用/双跳
    无中文可译）→ 中文显式指令（翻译意图最强信号）产出 'Out of the Loop
    工作室' → 过质量门落 translated。修复前该条目 BLOCKED 留人工。"""
    client = _BrandEchoDirectiveClient()
    entry = _to_model([{
        "file_id": "level1", "key_path": "asset#level1#104/str/0",
        "original": "Out of the Loop studio",
        "meta": {"role": "display", "disposition": "translate",
                 "kind": "rawstr", "obj": 104, "reason": "single_visible_string",
                 "confidence": "high"},
    }])[0]

    stats = BatchTranslator(client, batch_size=1, concurrency=1,
                            lang="en→zh-CN").run([entry])

    assert stats.done == 1, stats
    assert entry.status == "translated"
    assert entry.translation == "Out of the Loop 工作室"
    assert entry.meta.get("quality_passed") is True
    assert client.calls >= 3, "应先试 native（回显）再走中文指令 + 逐词补译"


def test_retranslate_with_feedback_falls_back_to_chinese_directive():
    """审核反馈重译（retranslate_with_feedback）对纯回显条目：英文反馈
    prompt 仍回显 → 中文显式指令兜底产出正确译文 → 通过质量门。
    （'Out of the Loop studio' 审核反馈重译→回显→BLOCKED 的回归保护）"""
    client = _BrandEchoDirectiveClient()
    entry = _to_model([{
        "file_id": "level1", "key_path": "asset#level1#104/str/0",
        "original": "Out of the Loop studio",
        "meta": {"role": "display", "disposition": "translate",
                 "kind": "rawstr", "obj": 104, "reason": "single_visible_string",
                 "confidence": "high"},
    }])[0]
    bt = BatchTranslator(client, batch_size=1, concurrency=1,
                         lang="en→zh-CN")
    client.calls = 0
    ok, translation = bt.retranslate_with_feedback(entry, "必须译成中文")
    assert ok is True, entry.meta.get("quality_reasons")
    assert entry.translation == "Out of the Loop 工作室"
    assert translation == "Out of the Loop 工作室"
