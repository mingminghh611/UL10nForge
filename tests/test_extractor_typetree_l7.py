"""识别 L7 测试：typetree 覆盖率持续度量 + 字段白名单登记制。

评估报告：Unity 6000 typetree 264/268 失败靠 raw scan 兜底，但无每
容器 typetree 可用率统计（失败率哑信号）；_TYPETREE_DISPLAY_FIELDS
约 40 字段无登记制（新增无依据可审计）。
"""
import struct
from pathlib import Path

from hanhua.core.unity import extractor
from hanhua.core.unity.extractor import _TYPETREE_DISPLAY_FIELD_ROWS


def _with_len(s: str) -> bytes:
    """Unity 序列化对齐字符串（长度前缀 + 4 字节对齐），同 test_v2。"""
    b = s.encode("utf-8")
    padding = b"\x00" * (-len(b) % 4)
    return struct.pack("<I", len(b)) + b + padding


class _FakeObject:
    def __init__(self, path: Path, tree=None, raw: bytes = b"",
                 tname: str = "MonoBehaviour", path_id: int = 7):
        self.path_id = path_id
        self.assets_file = type("AssetFile", (), {"name": path.name})()
        self.type = type("ObjectType", (), {"name": tname})()
        self._tree = tree
        self.raw = raw

    def read_typetree(self):
        if self._tree is None:
            raise ValueError("typetree 不可用")
        return self._tree

    def get_raw_data(self):
        return self.raw


class _FakeEnvironment:
    def __init__(self, objects):
        self.objects = objects
        self.files = {}

    def load(self, paths):
        pass


def _extract(tmp_path, objects, monkeypatch):
    import UnityPy
    from hanhua.core.unity.extractor import extract_asset_file
    p = Path(tmp_path) / "level1"
    p.write_bytes(b"\x00" * 8)
    monkeypatch.setattr(UnityPy, "Environment",
                        lambda: _FakeEnvironment(objects))
    return extract_asset_file(p, "level1")


def _tree_with_text(text: str) -> dict:
    return {"m_Name": "obj", "m_Text": text}


# ── typetree 覆盖率 ──────────────────────────────────────────

def test_typetree_coverage_full_on_success(tmp_path, monkeypatch):
    """全部对象 typetree 成功 → coverage=1.0 入容器 meta。"""
    pf = _extract(tmp_path, [
        _FakeObject(Path("level1"), _tree_with_text("Hello player")),
    ], monkeypatch)
    assert pf.meta["typetree_coverage"] == 1.0
    assert pf.meta["typetree_objects"] == 1
    assert pf.skipped_reasons.get("typetree_failed") is None


def test_typetree_failure_counts_and_raw_fallback(tmp_path, monkeypatch):
    """typetree 失败 → typetree_failed 计数留档 + coverage=0.0 +
    raw scan 兜底（Unity 6000 264/268 失败的量化形态）。"""
    raw = _with_len("Hello player") + b"\x00" * 16
    pf = _extract(tmp_path, [
        _FakeObject(Path("level1"), tree=None, raw=raw),
    ], monkeypatch)
    assert pf.skipped_reasons["typetree_failed"] == 1
    assert pf.meta["typetree_coverage"] == 0.0
    assert pf.meta["typetree_objects"] == 1
    assert any(e.original == "Hello player" for e in pf.entries)


def test_typetree_partial_coverage(tmp_path, monkeypatch):
    """2 对象 1 成功 1 失败 → coverage=0.5（逐容器可用率可查）。"""
    pf = _extract(tmp_path, [
        _FakeObject(Path("level1"), _tree_with_text("A"), path_id=7),
        _FakeObject(Path("level1"), tree=None, raw=b"x", path_id=8),
    ], monkeypatch)
    assert pf.meta["typetree_coverage"] == 0.5
    assert pf.meta["typetree_objects"] == 2
    assert pf.skipped_reasons["typetree_failed"] == 1


# ── 字段白名单登记制 ─────────────────────────────────────────

def test_display_field_registry_derives_set():
    """frozenset 从登记表派生（接口不变），登记行无重复名。"""
    assert extractor._TYPETREE_DISPLAY_FIELDS == frozenset(
        f.name for f in _TYPETREE_DISPLAY_FIELD_ROWS)
    names = [f.name for f in _TYPETREE_DISPLAY_FIELD_ROWS]
    assert len(names) == len(set(names))
    fields = extractor._TYPETREE_DISPLAY_FIELDS
    assert "text" in fields and "dialoguetext" in fields
    assert "name" not in fields  # m_Name 标识名有意排除


def test_display_field_rows_carry_source_group():
    """每字段带出处分组（ui/dialogue/locale/misc）——新增必须登记。"""
    groups = {f.group for f in _TYPETREE_DISPLAY_FIELD_ROWS}
    assert groups <= {"ui", "dialogue", "locale", "misc"}
    assert groups == {"ui", "dialogue", "locale", "misc"}
    assert len(_TYPETREE_DISPLAY_FIELD_ROWS) >= 40  # 原清单规模保持


# ── 事件绑定元数据字段（F53c，Dobraminhos 实证 2026-09-02）──────────
# m_TargetAssemblyTypeName/m_ActionEvents 等字段值（'GameMaster,
# Assembly-CSharp'/'PlayerActionsXbox/Move'）是反射/InputSystem 按名绑定键，
# 曾因『值恰似短短语』被 typetree_display_evidence 放行（642+389 条）。
# 字段名是确定性结构证据 → 字段命中即整子树跳过，值形态不参与。

def _onclick_tree() -> dict:
    """Unity Button/事件回调的 typetree：m_OnClick 持久化回调含 m_Target
    AssemblyTypeName（目标脚本类型引用）+ m_MethodName（绑定方法名）。"""
    return {
        "m_Name": "btn",
        "m_OnClick": {
            "m_PersistentCalls": {
                "m_Calls": [
                    {
                        "m_TargetAssemblyTypeName": "GameMaster, Assembly-CSharp",
                        "m_MethodName": "StartGame",
                        "m_Arguments": {
                            "m_ObjectArgumentAssemblyTypeName":
                                "UnityEngine.Object, UnityEngine"},
                    }
                ]
            }
        },
    }


def test_unityevent_target_assembly_type_field_skipped(tmp_path, monkeypatch):
    """UnityEvent m_TargetAssemblyTypeName 值（目标脚本程序集限定类名）→
    不产生 pending display 条目（字段名命中 _EVENT_BINDING_FIELDS 整枝跳过）。"""
    pf = _extract(tmp_path, [
        _FakeObject(Path("level1"), _onclick_tree(), path_id=445),
    ], monkeypatch)
    assert not any(
        e.status == "pending" and "GameMaster" in (e.original or "")
        for e in pf.entries), "UnityEvent 目标类型引用不得 pending"
    assert not any(
        e.meta.get("role") == "display"
        for e in pf.entries if "Assembly-CSharp" in (e.original or ""))


def test_action_events_map_paths_skipped(tmp_path, monkeypatch):
    """自定义输入 m_ActionEvents[].m_ActionName 值（'PlayerActionsXbox/Move'
    输入动作映射路径 + 编辑器默认 'New action'）→ 不得 pending display。"""
    tree = {
        "m_Name": "act",
        "m_ActionEvents": [
            {"m_ActionName": "PlayerActions/Move[/Keyboard/a,/Keyboard/d]",
             "m_ActionId": "abc"},
            {"m_ActionName": "PlayerActionsXbox/Jump", "m_ActionId": "def"},
            {"m_ActionName": "PlayerInUI/New action", "m_ActionId": "ghi"},
        ],
    }
    pf = _extract(tmp_path, [
        _FakeObject(Path("level2"), tree, path_id=2326),
    ], monkeypatch)
    pend = [e for e in pf.entries
            if e.status == "pending" and "Action" in e.key_path]
    assert pend == [], "输入动作映射路径/默认名不得进队列"


def test_event_binding_field_blocked_even_in_value_evidence_object(
        tmp_path, monkeypatch):
    """事件绑定字段命中优先于对象级值特征（即使同树有 m_Text 显示证据，
    m_TargetAssemblyTypeName 子树仍不得升格）。"""
    tree = {
        "m_Name": "root",
        "m_OnClick": {"m_PersistentCalls": {"m_Calls": [
            {"m_TargetAssemblyTypeName": "MenuMaster, Assembly-CSharp",
             "m_MethodName": "OpenPause"}]}},
        "m_Text": "Press J to interact.",   # 真显示证据在同对象
    }
    pf = _extract(tmp_path, [
        _FakeObject(Path("level1"), tree, path_id=451),
    ], monkeypatch)
    # 真 UI 文本保留可译
    assert any(e.status == "pending"
               and e.original == "Press J to interact." for e in pf.entries)
    # 类型引用不得升格
    assert not any(
        e.meta.get("role") == "display"
        and "Assembly-CSharp" in (e.original or "") for e in pf.entries)


# ── 文本镜像/动画字段（B10b，doubleshake SuperTextMesh 实证 2026-09-02）──
# SuperTextMesh 组件序列化：_text（权威显示文本）+ drawText/preParsedText/
# hyphenedText（_text 的运行时镜像缓存，同值 ×118）+ drawAnimName/undrawAnimName
# （'Appear'/'stamp' 文字进出场动画名 ×1166）。此前 typetree 全字段漏白名单 →
# raw scan 兜底把 Appear/PAUSED 重复放行（word_list 100+single 大量）。修复：
# 下划线私有字段归一化（_text→text 命中白名单）；镜像/动画字段登记结构跳过。

def test_underscore_field_normalization():
    """_text → text（私有序列化字段归一到白名单），__ 双下划线不剥。"""
    assert extractor._normalized_field_name("_text") == "text"
    assert extractor._normalized_field_name("_displayedText") == "displayedtext"
    assert extractor._normalized_field_name("__text") == "__text"
    assert extractor._normalized_field_name("_name") == "name"


def test_supertextmesh_only_authoritative_text_field_pending(tmp_path, monkeypatch):
    """SuperTextMesh：_text 是显示文本（可译）；drawText/preParsedText/
    hyphenedText 镜像与 drawAnimName/undrawAnimName 动画键不得 pending。"""
    tree = {
        "m_Name": "",
        "_text": "PAUSED",
        "drawText": "PAUSED",
        "preParsedText": "PAUSED",
        "hyphenedText": "PAUSED",
        "drawAnimName": "Appear",
        "undrawAnimName": "Appear",
    }
    pf = _extract(tmp_path, [
        _FakeObject(Path("level1"), tree, path_id=7369),
    ], monkeypatch)
    pend = [e for e in pf.entries if e.status == "pending"]
    # 只 _text 一条 pending，镜像/动画字段跳过
    assert len(pend) == 1, f"应只剩权威 _text 一条 pending，实际 {len(pend)}"
    assert pend[0].original == "PAUSED"
    fp = pend[0].meta.get("field_path") or []
    assert fp and fp[-1] == "_text"


def test_animname_field_skipped_even_when_display_evidence_present(
        tmp_path, monkeypatch):
    """动画状态名（drawAnimName='Appear'）字段名级跳过——即使同树有 _text
    显示证据也不得升格（翻译断 SuperTextMesh 进出场动画）。"""
    tree = {
        "_text": "Use this item?",
        "drawAnimName": "Appear",
        "undrawAnimName": "Appear",
    }
    pf = _extract(tmp_path, [
        _FakeObject(Path("level1"), tree, path_id=7401),
    ], monkeypatch)
    anim = [e for e in pf.entries
            if (e.meta.get("field_path") or [""])[-1] in
            ("drawAnimName", "undrawAnimName")]
    assert all(e.status != "pending" for e in anim), "动画状态名不得 pending"
    # 真 UI 文本保留
    assert any(e.status == "pending" and e.original == "Use this item?"
               for e in pf.entries)
