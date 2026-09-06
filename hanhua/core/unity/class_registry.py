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
#  render_layer = 渲染层/排序组件（hrana 实证：OrderInLayer 的
#               Front/Behind 层名是排序引用）
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
    # ── config：URP 渲染管线配置（hickory 实证 2026-09-02）──
    # Renderer2DData 的混合模式枚举（'Multiply'/'Additive'/'Multiply with
    # Mask'，2D 光照渲染模式）、UniversalRenderPipelineGlobalSettings 的
    # 'Default' 等——渲染管线配置按名引用（URP 资产是引擎渲染设置，非
    # 玩家 UI 文本；'Multiply' 若翻成「相乘」会断 2D 光照混合渲染）。与
    # Timeline/FMOD 同属引擎渲染/演出配置类。UnityEngine.Rendering.*
    # 命名空间内还有大量 DebugUIHandler*/VolumeProfile/PostProcess 类，
    # 命名空间前缀匹配覆盖全部。
    ClassEntry("Renderer2DData", "config", "urp_render"),
    ClassEntry("UniversalRenderPipelineGlobalSettings", "config", "urp_render"),
    ClassEntry("UniversalRenderPipelineAsset", "config", "urp_render"),
    ClassEntry("VolumeProfile", "config", "urp_render"),
    ClassEntry("PostProcessData", "config", "urp_render"),
    # ── config：Photon Pun 联机同步组件（bottle-cracks 实证 2026-09-02）──
    # PhotonAnimatorView 的 m_SynchronizeParameters[].Name 值（'Speed'/
    # 'Direction'/'Jump'——Animator 参数名）是 Photon 按名同步的网络键，
    # 翻译断联机动画同步（所有客户端动画参数不同步）；PhotonView 的
    # RPC/变量名同理。命名空间前缀 Photon.Pun. 内全是联机配置/同步类。
    ClassEntry("PhotonAnimatorView", "config", "photon_net"),
    ClassEntry("PhotonView", "config", "photon_net"),
    ClassEntry("PhotonTransformView", "config", "photon_net"),
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
    # ── config：渲染层/排序组件（hrana 实证 2026-09-02）──
    # OrderInLayer（命名空间无）：组件的序列化字段是渲染层 ID——'Front'/
    # 'Behind' 是游戏对象排序层引用（默认渲染顺序的前/后），翻译成「前面/
    # 后面」写回后游戏按层名查找断裂，对象渲染/遮挡顺序错乱。组件字段值
    # 是确定性结构（层名），非玩家可见文本；UI 文本组件（Text/TMP）不在
    # 本类，由 display 分组放行。obj86417 实证：单 MonoBehaviour 值
    # Front/Behind 成对出现（= 图层序列化），无 UI 字段证据，此前被
    # single_visible_string/f38_released 放行进池。
    ClassEntry("OrderInLayer", "config", "render_layer"),
    # ── config：Fungus 对话系统结构类（a-catfiends 实证 2026-09-02）──
    # Fungus 对话组件序列化字段值是运行时按名查找键，翻译必断对话流程：
    # BooleanVariable/FloatVariable 等 *Variable 的 Key（'Menu'/'milk'/
    # 'LYNCH'/'LOCATION' 是变量名——对话 SetVariable/Compare 按名引用）；
    # Block 的 BlockName（'Opening (Copy)' 对话块跳转引用）；PlayAnimState
    # 的 AnimState（'end transition'/'kalkam' 动画状态名）；MessageReceived/
    # SendMessage 的 Message（'DEATH' 消息名，Block 触发按名匹配）；StopBlock/
    # StopFlowchart 的 BlockName（被停止块引用）；Flowchart 名称/描述
    # （编辑器定位，非游戏内显示）。
    # **不在本组的 Fungus 类**（其序列化值是真显示文本，必须保留翻译）：
    # Fungus.Say（storyText 对话正文 178 条 actionable 实证）、
    # Fungus.Character（角色显示名）、Fungus.InfoText（Info 字段是游戏内
    # 说明文本，'Information text' 只是默认占位）——精确到类的登记保证
    # 只拦结构类、不误杀对话/显示类。typetree 解析出命名空间限定名
    # （Fungus.X）精确匹配。
    ClassEntry("Fungus.BooleanVariable", "config", "fungus_struct"),
    ClassEntry("Fungus.FloatVariable", "config", "fungus_struct"),
    ClassEntry("Fungus.IntegerVariable", "config", "fungus_struct"),
    ClassEntry("Fungus.StringVariable", "config", "fungus_struct"),
    ClassEntry("Fungus.AudioSourceVariable", "config", "fungus_struct"),
    ClassEntry("Fungus.GameObjectVariable", "config", "fungus_struct"),
    ClassEntry("Fungus.MaterialVariable", "config", "fungus_struct"),
    ClassEntry("Fungus.SpriteVariable", "config", "fungus_struct"),
    ClassEntry("Fungus.Block", "config", "fungus_struct"),
    ClassEntry("Fungus.Flowchart", "config", "fungus_struct"),
    ClassEntry("Fungus.PlayAnimState", "config", "fungus_struct"),
    ClassEntry("Fungus.MessageReceived", "config", "fungus_struct"),
    ClassEntry("Fungus.SendMessage", "config", "fungus_struct"),
    ClassEntry("Fungus.StopBlock", "config", "fungus_struct"),
    ClassEntry("Fungus.StopFlowchart", "config", "fungus_struct"),
    # FungusTrigger：触发器名（'DEATH' 等按键/事件触发名，Block 启动按名匹配）
    ClassEntry("FungusTrigger", "config", "fungus_struct"),
    # ── config：装备/AI 决策配置类（drova 实证 2026-09-02）──
    # AI_EquipItem 的 Values/Primary/Secondary/Range/Melee（AI 判断装备优劣
    # 的评分类型枚举）与 TAL_Req_CheckItemTag 的 Values/Handedness/Range
    # （物品标签匹配键）——游戏代码按 TagString 匹配物品（检查手上武器是否
    # Melee 类型决定 AI 行为），翻译断物品标签匹配。UI 文本（物品名/属性标签
    # 德语/英语显示）在 CustomFramework.Localization 的 Value 表单独提取，
    # 不受影响。裸类名精确登记（防误杀同名前缀游戏类）。
    ClassEntry("AI_EquipItem", "config", "drova_item_tag"),
    ClassEntry("TAL_Req_CheckItemTag", "config", "drova_item_tag"),
    # ── display ──
    ClassEntry("TextMeshProUGUI", "display", "ui_text"),
    ClassEntry("TMP_InputField", "display", "ui_text"),
    ClassEntry("TextMeshPro", "display", "ui_text"),
    # ── display：文本内容 ScriptableObject（fake-it 实证 2026-09-07）──
    # Experimental.ScriptableText（sharedassets 内 ContentFr/ContentEn 双语
    # 字段）持有整个主菜单 UI 文本（'New Game'/'Nouveau jeu'、'To Do'/
    # 'A faire' 70 对）——此前被「共享资源小配置对象」规则当 Timeline 剪辑
    # 名误跳过（140 条漏网，reason=shared_resource_config_object，AI 召回
    # 面也不收）。类名含 Text 且字段是内容字段是确定性文本证据，登记为
    # display：对象内字符串按值形态正常分类，不受小配置对象规则整跳。
    # 裸名精确登记（Experimental. 命名空间经 rsplit 兼容路径命中），跨游戏
    # 重名风险极低（Text 词义明确指向文本内容）。
    ClassEntry("Experimental.ScriptableText", "display", "scriptable_text"),
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
    # 裸类名登记兼容命名空间限定名（AI.AI_EquipItem → AI_EquipItem）：
    # 精确登记行用裸名（drova AI_EquipItem 实证 script_class 带 AI. 前缀），
    # 带命名空间时剥到最后一段再查，防跨游戏误杀（只影响已精确登记的类）。
    _simple = script_class.rsplit(".", 1)[-1]
    if _simple != script_class and _simple in CONFIG_CLASSES:
        return "config"
    if script_class.startswith(_FMOD_NAMESPACE_PREFIXES):
        # FMODUnity/FMODUnityResonance 命名空间内全部是音频集成配置类
        return "config"
    if script_class.startswith(_REWIRED_NAMESPACE_PREFIXES):
        # Rewired/InControl 命名空间内全部是输入插件配置类
        return "config"
    if script_class.startswith("Photon."):
        # Photon Pun/Photon 联机命名空间（PhotonView/PhotonAnimatorView/
        # PhotonTransformView/PhotonNetwork…）：网络同步/RPC 配置——按名
        # 同步键翻译断联机（bottle-cracks 实证 2026-09-02 PhotonAnimatorView
        # m_SynchronizeParameters Name 'Speed'/'Jump' 是 Animator 参数同步名）。
        return "config"
    if script_class.startswith("UnityEngine.Rendering."):
        # URP/HDRP 渲染管线命名空间（RenderPipelineAsset/VolumeProfile/
        # Renderer2DData/DebugUIHandler* 等）：引擎渲染配置类——管线设置/
        # 混合模式/后处理卷的字符串是引擎引用（hickory 实证 2026-09-02：
        # Renderer2DData 'Multiply'/'Additive' 2D 光照混合枚举被当显示词放行），
        # 翻译断渲染。命名空间前缀覆盖全部，无需逐一登记。
        return "config"
    return None
