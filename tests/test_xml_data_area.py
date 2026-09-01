# -*- coding: utf-8 -*-
"""XML TextAsset 数据区过滤测试（operation-ops 实证 2026-09-01）。

背景：operation-ops 的关卡文件是 <PartData>…<DesignSaveData>…</DesignSaveData>
</PartData> 式机器数据 XML——数值（x/y/z/rotX/scale）、预制体实例名
（DesignSaveData/name='Wall1(Clone)'/name='Switch2'）、父对象挂载名
（parentName='MapEditor'）、开关目标对象引用数组（switchObjects/string[N] =
'rd0 rd1 rd2'）、任务目标对象名（objectiveMapObject='Prof. Plum' 跨字段引用
DesignSaveData/name）。这些翻译写坏关卡加载/交互，必须按结构拦截。

本测试锁定 _NUMERIC_TEXT / _XML_REF_FIELDS / DesignSaveData.name /
switchObjects 父路径 / 良构空结果不落行拆分 的结构判定，防回归。
"""
import xml.etree.ElementTree as ET

from hanhua.core.formats.xml_format import extract_xml_text

# ── operation-ops 关卡数据抽样 ──────────────────────────────
_LEVEL = """<?xml version="1.0" encoding="utf-8"?>
<PartData xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <parts>
    <DesignSaveData>
      <x>16.5</x><y>0.5</y><z>7.5</z>
      <rotX>0</rotX><scale>1</scale>
      <prefabIndex>3</prefabIndex>
      <name>Wall1(Clone)</name>
      <parentName>MapEditor</parentName>
    </DesignSaveData>
    <DesignSaveData>
      <x>21.5</x><y>0.5</y><z>17.5</z>
      <name>Switch2</name>
      <parentName>MapEditor</parentName>
    </DesignSaveData>
  </parts>
  <objectives>
    <MissionObjective>
      <description>Destroy specimens</description>
      <objectiveMapObject>Prof. Plum</objectiveMapObject>
    </MissionObjective>
  </objectives>
  <switchObjects>
    <string>Switch2</string>
    <string>Outer1 Outer2 Outer3</string>
  </switchObjects>
  <switchObjectsManipulated>
    <string>rd0 rd1 rd2</string>
  </switchObjectsManipulated>
  <intelStrings>
    <string>The code for the armoury has changed again to %01.</string>
    <string>NOTICE: ALL PERSONNEL</string>
  </intelStrings>
</PartData>
"""


def _paths(entries):
    return [(e.key_path, e.original) for e in entries]


def test_numeric_text_node_skipped():
    """纯数字文本节点（x/y/z/rotX/scale）不提取。"""
    assert not any(p.endswith("/x") for p, _ in _paths(extract_xml_text(_LEVEL)))


def test_designsave_name_skipped():
    """DesignSaveData/name（对象实例名，含 (Clone)）不提取。"""
    es = extract_xml_text(_LEVEL)
    assert not any(v in ("Wall1(Clone)", "Switch2") for _, v in _paths(es))


def test_parentname_skipped():
    """parentName（父对象挂载名）不提取。"""
    assert not any(p.endswith("/parentName") for p, _ in _paths(extract_xml_text(_LEVEL)))


def test_objectivemapobject_skipped():
    """objectiveMapObject（任务目标对象名，跨字段引用 name）不提取。"""
    assert not any(p.endswith("/objectiveMapObject") for p, _ in _paths(extract_xml_text(_LEVEL)))


def test_switch_arrays_skipped():
    """switchObjects*/string（开关目标对象名列表）不提取。"""
    es = extract_xml_text(_LEVEL)
    assert not any("/switchObjects" in p for p, _ in _paths(es))


def test_real_text_kept():
    """description/intelStrings 是真显示文本，保留。"""
    es = extract_xml_text(_LEVEL)
    by_path = {e.key_path: e.original for e in es}
    assert "Destroy specimens" in by_path.values()
    assert "The code for the armoury has changed again to %01." in by_path.values()
    assert "NOTICE: ALL PERSONNEL" in by_path.values()


def test_playerstats_single_line_xml_structured_empty():
    """单行良构 XML 全机器值 → 结构化空结果，不落 line 拆分。

    operation-ops PlayerStats 实证：<name>Player</name><hp>3</hp>…
    叶子全部被机器值过滤合法跳过，若 extract 返回 [] 会被调用方当作
    “非 XML”落到按行拆分，把整段 XML 当一个显示文本条目进池，翻译即
    毁掉整个存档 XML。extract_xml_text 必须区分“良构但零条目”与
    “非良构解析失败”（后者仍应走行拆分）。
    """
    stats = ('<PlayerStats xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
             "<name>Player</name><hp>3</hp><speed>8</speed><stealth>3</stealth>"
             "<strength>3</strength><primaryWeapon>0</primaryWeapon>"
             "<demoCharges>3</demoCharges></PlayerStats>")
    entries = extract_xml_text(stats)
    # 叶子（name/hp/speed…）全被过滤；name='Player' 大写开头也应被拦
    assert entries == [], f"单行存档 XML 应零条目，得到 {entries}"


def test_apply_xml_machine_values_unchanged():
    """即使异常带译文，机器值字段写回侧也拒绝改动（apply 按 key_path 匹配）。"""
    entries = extract_xml_text(_LEVEL)
    for e in entries:
        e.translation = "X"
    out = apply_xml(entries, _LEVEL)
    root = ET.fromstring(out)
    parts = root.find("parts")
    d0 = parts.findall("DesignSaveData")[0]
    assert d0.findtext("name") == "Wall1(Clone)", "机器 name 值写回侧不得改动"
    assert d0.findtext("x") == "16.5", "机器数值写回侧不得改动"
    assert root.findtext("objectives/MissionObjective/objectiveMapObject") == "Prof. Plum"


def apply_xml(entries, source_text):
    # 本地导入避免循环（测试用轻量实现引用生产函数）
    from hanhua.core.formats.xml_format import apply_xml as _a
    return _a(entries, source_text)
