"""结构字段黑名单单一源（M1，2026-09-05 0.39.0）。

写回安全的核心清单此前散落三处手工同步（问题集 A7 复审发现的隐患）：
  - writer._IMMUTABLE_FIELD_NAMES        （写回闸门，原始大小写）
  - extractor._TYPETREE_IMMUTABLE_FIELD_NAMES（扫描端预拦，casefold 变体）
  - logic_audit.INPUT_BINDING_FIELD_PATHS（语义回退判定，casefold 全变体）
任何一处漏加新字段 = 防线缺口（B15 的 m_ExpectedControlType/m_Groups
当时就是三处分别补的）。本模块成为唯一权威源，三处全部改为 import，
新增结构字段只改这里，回归测试 `test_structural_fields_single_source`
强制三处使用同一来源，防再次分叉。

清单内容契约（与 writer._IMMUTABLE_FIELD_NAMES 历史集合完全一致，
追加字段必须附来源事故/依据注释）：
  - Object 名 / 稳定 ID / GUID / PPtr 引用 / 地址
  - Input System 绑定（B15 snowday 按键失灵实证）
  - MonoBehaviour 脚本引用 / 类名 / 命名空间
  - Localization locale / StringTable 共享引用

变体规则：
  - m_Xxx 原始大小写（写回层按字典序精确匹配，typetree 键即此形态）
  - mXxx camelCase 无下划线变体（NGUI/旧序列化）
  - casefold 全小写（逻辑回退/扫描端拦截形态变体用）
"""

from __future__ import annotations

# 原始字段名（写回闸门 _is_immutable_field_name 用，成员判定按 typetree
# 键原文精确匹配——typetree 键从不 casefold）。
IMMUTABLE_FIELD_NAMES = frozenset({
    "m_Name",                          # Object 名（A1 按钮失灵根因）
    "m_Key", "m_Id", "m_EntryID",      # StringTable Entry / 各类稳定 ID
    "m_GUID",                          # 资产 GUID
    "m_FileID", "m_PathID",            # PPtr 引用
    "m_Path", "m_Address",             # 资源/文件地址
    "m_ControlPath", "m_Action", "m_ActionMap",   # Input System 绑定
    "m_ExpectedControlType", "m_Groups",           # B15：InputActionAsset
                                       # 控件类型/控制方案组（snowday 实证
                                       # 'Button'→'按钮' 全部按键失灵）
    "m_Script",                        # MonoBehaviour 脚本引用
    "m_ClassName", "m_Namespace",      # 脚本类名
    "m_LocaleIdentifier", "m_LocaleCode",          # Localization locale
    "m_SharedData",                    # StringTable 表级共享引用
})

# camelCase 无下划线变体（NGUI/旧序列化：mName/mGUID/mScript 等）。
# 仅用于扫描端/逻辑回退的宽松拦截；写回闸门保持原始大小写精确匹配。
IMMUTABLE_FIELD_CAMEL_VARIANTS = frozenset({
    "mName", "mKey", "mId", "mEntryID", "mGUID",
    "mFileID", "mPathID", "mPath", "mAddress",
    "mControlPath", "mAction", "mActionMap", "mScript",
    "mClassName", "mNamespace", "mSharedData",
    "mExpectedControlType", "mGroups",
})

# casefold 全形态（含原始名 + camel 变体，扫描端/逻辑回退用）。
IMMUTABLE_FIELD_NAMES_FOLDED = frozenset(
    name.casefold() for name in
    (IMMUTABLE_FIELD_NAMES | IMMUTABLE_FIELD_CAMEL_VARIANTS))

# Input System 绑定相关字段叶子名（casefold）。logic_audit 的
# INPUT_BINDING_FIELD_PATHS 由这些叶子 × {裸 / m_ / m前缀无下划线} 三变体
# 派生——B15 按键失灵的字段路径信号必须与写回闸门同源。
INPUT_BINDING_FIELD_PATH_LEAVES = frozenset({
    "action", "actionmap", "controlpath", "expectedcontroltype", "groups",
})


def is_immutable_field(name: str) -> bool:
    """写回闸门成员判定：原始大小写精确匹配。"""
    return name in IMMUTABLE_FIELD_NAMES


def is_immutable_field_folded(name: str) -> bool:
    """宽松成员判定（casefold 变体拦截）：扫描端/逻辑回退用。

    裸字段名（如 "name"/"action"）在自定义对象里可能是真实显示文本，
    本判定只吃带 m 前缀的变体，与历史行为一致。
    """
    return name in IMMUTABLE_FIELD_NAMES_FOLDED
