"""序列化脚本类注册表：确定性类名 → 对象级 disposition（识别 L9）。

L6 已能从 MonoBehaviour 的 m_Script PPtr 解析出确定性脚本类名
（extractor._script_class_of），但此前只用在 InputSystem/Timeline 两个
信号集合上。本注册表把它推广为登记制：

- config：引擎/资源配置类——对象内字符串是运行时按名查找的键或
  资产元数据（字体名/精灵名/动作名），翻译必断引用。确定性跳过
  （取代/先于 is_tmp_asset_object 等串池信号猜测，证据分层）；
- display：显示组件类——对象内字符串多为显示文本，确定性证据
  优先于「小配置对象」等形态猜测（猜测不得推翻确定性）；
- 未登记类名：不判定（走既有启发式链），由提取器收集进报告
  「待登记类队列」——每遇一个新游戏类名，人工过一遍后加一行，
  而非新增一条正则（与 morphology.py 形态注册表同模式）。

每行带出处分组（与 L7 字段白名单登记制同模式），新增必须可审计。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class ClassEntry:
    name: str
    disposition: str   # config / display
    group: str         # 出处分组（可审计）


# config 出处分组：
#  tmp_asset  = TMP 字体/精灵/样式资产（headache 实证：资产名是 <font>/
#               <sprite> 按名引用键，翻译断引用→字体/表情丢失；
#               give-me-strength 实证 2026-08-29：TMP_StyleSheet 的
#               Quote/Link/Title 是 <style="..."> 标签查找键）
#  input      = InputSystem 配置（morfosigame/deadbeat 实证：动作名
#               按原名查找，翻译破坏按键交互）
#  timeline   = Timeline 演出配置（morfosigame 实证：轨道/剪辑名
#               翻译破坏反序列化）
#  fmod       = FMOD 音频集成配置（give-me-strength 实证 2026-08-29：
#               Settings.m_Banks 的 bank 名（Master/Music…）是 .bank
#               文件加载键、Platform*.parentIdentifier（'default'）是
#               平台继承查找键——翻译后 RuntimeManager 加载银行失败，
#               全游戏静音。StudioEventEmitter 的 event 路径已由
#               engine_strings event:/ 规则拦截，其余字段同类整体跳过）
# display 出处分组：
#  ui_text    = TMP/UI 文本组件（指南 §3.2 显示组件）
_CLASS_ROWS: tuple[ClassEntry, ...] = (
    # ── config ──
    ClassEntry("TMP_FontAsset", "config", "tmp_asset"),
    ClassEntry("TMP_SpriteAsset", "config", "tmp_asset"),
    ClassEntry("TMP_StyleSheet", "config", "tmp_asset"),
    ClassEntry("InputActionAsset", "config", "input"),
    ClassEntry("InputActionMap", "config", "input"),
    ClassEntry("InputActionReference", "config", "input"),
    ClassEntry("PlayerInput", "config", "input"),
    ClassEntry("InputControlScheme", "config", "input"),
    ClassEntry("TimelineAsset", "config", "timeline"),
    ClassEntry("PlayableDirector", "config", "timeline"),
    # FMODUnity 家族：只登记无歧义类名（Studio*/FMODEvent* 前缀是 FMOD
    # 专有词）。通用词类名（Settings/Platform/EventHandler/RuntimeManager
    # 等）不裸名登记——游戏自有同名类（设置界面 Settings）会被误杀；
    # 它们经命名空间限定名（FMODUnity.Settings，_script_class_of /
    # _script_class_from_head 产出）由下方前缀匹配整体判定。
    ClassEntry("StudioBankLoader", "config", "fmod"),
    ClassEntry("StudioEventEmitter", "config", "fmod"),
    ClassEntry("StudioListener", "config", "fmod"),
    ClassEntry("StudioParameterTrigger", "config", "fmod"),
    ClassEntry("StudioGlobalParameterTrigger", "config", "fmod"),
    ClassEntry("FMODEventTrack", "config", "fmod"),
    ClassEntry("FMODEventPlayable", "config", "fmod"),
    # ── display ──
    ClassEntry("TextMeshProUGUI", "display", "ui_text"),
    ClassEntry("TMP_InputField", "display", "ui_text"),
    ClassEntry("TextMeshPro", "display", "ui_text"),
)

CONFIG_CLASSES: frozenset[str] = frozenset(
    e.name for e in _CLASS_ROWS if e.disposition == "config")
DISPLAY_CLASSES: frozenset[str] = frozenset(
    e.name for e in _CLASS_ROWS if e.disposition == "display")

# FMOD 命名空间前缀（script_class 为命名空间限定名时整体判定：
# FMODUnity.Settings / FMODUnity.PlatformWindows /
# FMODUnityResonance.FmodResonanceAudio）。Platform 前缀家族随 FMOD
# SDK 版本增减（PlatformWindows/PlatformAndroid/…），命名空间前缀
# 匹配覆盖全部，无需逐一登记。
_FMOD_NAMESPACE_PREFIXES = ("FMODUnity.", "FMODUnityResonance.")

# TMPro 命名空间前缀：TMPro.TMP_StyleSheet 等命名空间限定名。裸名
# TMP_* 已逐一登记，带 TMPro 前缀时剥掉再查（TMP_ 开头是 TMP 专有词，
# 无游戏同名类误杀风险）。
_TMP_NAMESPACE_PREFIX = "TMPro."

# Rewired/InControl 输入插件命名空间前缀（脚本类为命名空间限定名时
# 整体判定为 config）：设备配置类家族随插件版本增减（InputManager/
# Data.Mapping.HardwareJoystickMap/Data.Mapping.HardwareJoystickTemplateMap/
# UI.ControlMapper.LanguageData/ComponentControls.TouchButton…），命名
# 空间前缀匹配覆盖全部，无需逐一登记。
#
# 判定理由（ffs-legacy 实证 2026-08-31，sharedassets0.assets 6470 条
# pending 中 5054 条来自 Rewired.*）：HardwareJoystickMap/TemplateMap 的
# 设备名（'CH Eclipse Yoke'/'Saitek Pro Flight Yoke'）是运行时按字符串
# 匹配硬件的键；元素标签（'Left Stick X'/'Throttle 1 Up'）是映射 UI 的
# 轴/按钮名；InputManager 的动作名/映射名/类别名是 Rewired 按名查找键——
# 翻译必然破坏输入匹配与重绑定。ControlMapper.LanguageData 的 56 条
# （'Yes'/'Choose Controller'/'Press any button…'）是 Rewired 自带重映射
# UI 的显示串，同一批串跨游戏相同——宁漏勿坏：整屏 UI 保持英文是可见
# 缺失，翻译后匹配错乱是功能断裂（识别的风险不对称原则）。
_REWIRED_NAMESPACE_PREFIXES = ("Rewired.", "InControl.")


@lru_cache(maxsize=None)
def disposition(script_class: str) -> str | None:
    """脚本类名 → 'config' / 'display' / None（未登记，走启发式链）。

    script_class 可为裸类名（StudioEventEmitter）或命名空间限定名
    （FMODUnity.Settings）——FMOD 家族按命名空间前缀整体判定（含
    Platform 前缀家族），通用词类名（Settings/Platform）只有带 FMOD
    命名空间才命中，防游戏自有同名类误杀。
    """
    if not script_class:
        return None
    if script_class.startswith(_TMP_NAMESPACE_PREFIX):
        script_class = script_class[len(_TMP_NAMESPACE_PREFIX):]
    if script_class in CONFIG_CLASSES:
        return "config"
    if script_class in DISPLAY_CLASSES:
        return "display"
    if script_class.startswith(_FMOD_NAMESPACE_PREFIXES):
        # FMODUnity/FMODUnityResonance 命名空间内全部是音频集成配置类
        return "config"
    if script_class.startswith(_REWIRED_NAMESPACE_PREFIXES):
        # Rewired/InControl 命名空间内全部是输入插件配置类
        return "config"
    return None
