"""Tests for deterministic quality guard and UI authoritative vocabulary."""
from ul10n.core.quality import (
    check_translation_quality,
    clean_model_output,
    is_translatable,
    lookup_builtin_ui,
    restore_missing_tags,
)


def test_builtin_ui_terms():
    assert lookup_builtin_ui("Play") == "开始"
    assert lookup_builtin_ui("resume") == "继续"
    assert lookup_builtin_ui("DISABLED") == "已禁用"
    assert lookup_builtin_ui("Custom Dragon") is None


def test_is_translatable():
    assert is_translatable("Press any key") is True
    assert is_translatable("12345") is False
    assert is_translatable("v1.1.1") is False
    assert is_translatable("...") is False
    assert is_translatable("已经是纯中文句子") is False


def test_quality_placeholders_and_tags():
    assert check_translation_quality("Deal {0} damage", "造成 {0} 点伤害").passed is True
    bad_ph = check_translation_quality("Deal {0} damage", "造成伤害")
    assert bad_ph.passed is False
    assert "占位符" in bad_ph.note

    healed = restore_missing_tags("<color=#fff>Warning</color>", "警告")
    assert healed == "<color=#fff>警告</color>"
    assert check_translation_quality("<color=#fff>Warning</color>", healed).passed is True


def test_quality_echo_and_numeric_equivalence():
    assert check_translation_quality("Welcome back to the castle", "Welcome back to the castle").passed is False
    assert check_translation_quality("200.000 readers joined", "20万读者加入").passed is True
    assert check_translation_quality("Costs 500 gold", "需要 999 金币").passed is False


def test_clean_model_output():
    assert clean_model_output('译文："你好，旅人"') == "你好，旅人"
    assert clean_model_output("```\n开始游戏\n```") == "开始游戏"
