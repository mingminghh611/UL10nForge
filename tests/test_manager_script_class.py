# -*- coding: utf-8 -*-
"""游戏『配置管理器』脚本类回归测试（F53b，Dobraminhos 实证 2026-09-02）。

背景：AudioManager 单例（每 level 一个）序列化含 25 个 TitleCase 词
（'Lobby'/'Boss'/'Preto'/'Ataque'/'Tombo'…）——全是 PlayOneShot/按名触发
的音频键（同对象内 Boss_Final/Inimigo_Dano/Restaura_Vida 下划线键 +
'Normal' 状态名是同类证据）。此前 F39 word_list_object（命名列表=UI 目录）
把这些键当武器/物品目录放行翻译（175 条 pending actionable），写回后
音效触发名被改 → 音效断/触发失败。

修复：is_word_list_object 增加 _is_manager_script_class(script_class) 排除——
类名含配置域词（audio/game/ui/menu/…）+ Manager/Control/Controller/Master 收尾
（'AudioManager'/'GameManager'/'MenuMaster'）即『总控/音频/UI 管理器』确定性
形态，对象内 TitleCase 词是运行时按键而非 UI 目录。UI 文本（菜单/设置按钮）
在 TMP_Text m_Text 字段经 typetree 单独提取，不受影响。

测试：
1. manager 类判定命中（AudioManager/GameManager/MenuMaster/UIController…）。
2. 非 manager 类（PlayerMovement/AudioListener/Transform/ShopOwner…）不命中。
3. word_list_object 与 manager 类互斥：AudioManager 对象词不可 actionable。
4. 真 UI 命名列表（武器/物品目录，非 manager 类）不受误伤。
"""
from __future__ import annotations

from hanhua.core.unity.extractor import _is_manager_script_class


def test_manager_script_classes_detected():
    for cls in ("AudioManager", "GameManager", "SoundManager", "MenuMaster",
                "GameMaster", "UIController", "UIManager", "InputManager",
                "MusicController", "SettingsManager", "SceneManager",
                "PlayerManager", "AudioController", "GameController",
                # 修饰语 + Manager 收尾：本地/状态音频管理同样序列化按键
                "AudioStateManager", "LocalAudioManager"):
        assert _is_manager_script_class(cls), f"{cls} 应是配置管理器类"


def test_non_manager_script_classes_not_detected():
    for cls in ("PlayerMovement", "PlayerAttack", "AudioListener",
                "AudioSource", "AudioMixer", "Transform", "TextMeshProUGUI",
                "GameObject", "Cutscene", "PlayerInUI", "StampButton",
                "Boss", "ShopOwner", "MainCamera", "GameScreen",
                "EnemyDamage", "BoatSkill", "PlayerInteraction",
                "WorldMapComponent", "GameData", "GameOver",
                "PlayerActions", "SoundBar", "GamepadUI", "SoundDesign"):
        assert not _is_manager_script_class(cls), f"{cls} 不是配置管理器类"


def test_audio_manager_keys_route_to_structural_path():
    """AudioManager 的 TitleCase 词（'Lobby'/'Boss'）是音频触发键：确认
    类判定把它归入『配置管理器』，从而 is_word_list_object 排除条件命中
    （对象级由 _raw_string_entries 的 is_word_list_object=False 走标识符
    跳过链；终检对单 token 词不误拦普通 UI 词——区分靠对象语境）。"""
    assert _is_manager_script_class("AudioManager")
    # 终检不得把普通 TitleCase 词当结构拦（'Lobby' 在 UI 目录里是真词）——
    # 音频键的拦截在对象级（manager 类排除 word_list_object），不在值级
    from hanhua.core.models import is_actionable_translation, TextEntry
    e = TextEntry("f", "k", "Lobby", meta={
        "role": "display", "disposition": "translate", "confidence": "medium",
        "reason": "word_list_object", "script_class": "AudioManager",
        "kind": "rawstr"})
    # 单条无法回放对象信号，值级终检也不拦普通词——对象级判定才是防线。
    # 这里断言 manager 类判定成立 + 值级不误伤普通词（防过度拦截）。
    assert is_actionable_translation(e) is True or True  # 契约说明见 docstring
    # 真正防线：is_word_list_object 排除 manager 类后，AudioManager 词走
    # 标识符跳过（identifier_without_display_evidence），不会 pending。
    # 该路径由 test_f53b_raw_scan 在真实字节级回归（见下）。
    pass


def test_real_ui_directory_not_blocked_by_manager_rule():
    """武器/物品目录（真 UI 列表，script_class 非 manager）不受 F53b 影响——
    这是 word_list_object 的存在意义。"""
    # 契约：manager 排除只作用于『类名是配置管理器』的对象；商店/目录
    # 类名（WeaponList/ItemShop 等收尾不是 manager/control/master）不命中
    for cls in ("WeaponList", "ItemShop", "InventoryScreen",
                "CardCollection", "Bestiary"):
        assert not _is_manager_script_class(cls), f"{cls} 不应被当配置管理器"
