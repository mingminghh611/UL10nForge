"""形态注册表完整性：每形态必须声明锚点覆盖，否则元测试失败。

「形态覆盖可审计」的制度化保障：新增形态必须在 _COVERAGE 登记对应
锚点（fixture 测试或真实样本验证），未登记 → 测试失败强制登记。
流程见 docs/识别形态覆盖与遗漏处理.md。
"""
from hanhua.core.tooling.morphology import REGISTRY, classify_morphology

# 形态 → 锚点覆盖（fixture 测试 / 真实样本验证，含修复版本号）
_COVERAGE = {
    "mono_csharp":
        "tests/test_mono_ui_wrapper.py（C# 传递验证链 fixture，历史锚点）"
        "+ tests/test_mono_debug_sinks.py（0.36.10 调试/结构/按键 sink："
        "MonoBehaviour.print / AnimatorStateInfo.IsName / LayerMask.GetMask "
        "params / 输入按键名标签 / PlayerPrefs 键）",
    "mono_unityscript":
        "tests/test_mono_ui_wrapper.py 双名 fixture + lilys-day-off 真实样本"
        "（0.14.0 含空格 825 条恢复 / 0.14.1 语气词 29 条恢复）",
    "mono_boo":
        "tests/test_game_fingerprint.py::test_manifest_fallback_matches_boo_assembly"
        "（0.14.0 补 fallback 前缀）",
    "mono_other":
        "tests/test_game_fingerprint.py::test_manifest_type16_"
        "filters_framework_and_package_assemblies（StgAssembly_* 自定义名）",
    "asset_unity":
        "tests/test_v2.py typetree/rawstr/TextAsset 系列 + 0.14.1 证据分层"
        "测试（'A game by Kyuppin' 锚点）",
    "il2cpp_metadata":
        "tests/test_v2.py parse_string_literals 系列 + Il2CppDumper 交叉验证",
}


def test_registry_coverage_complete():
    registered = {m.name for m in REGISTRY}
    covered = set(_COVERAGE)
    assert registered == covered, (
        "形态注册表与锚点覆盖不一致："
        f"未登记覆盖 {sorted(registered - covered)}；"
        f"覆盖了未注册形态 {sorted(covered - registered)}")


def test_registry_prior_semantics():
    # 语言形态文本先验必须显式声明：dense = 字面量几乎全是显示文本
    priors = {m.name: m.prior for m in REGISTRY}
    assert priors["mono_unityscript"] == "dense"
    assert priors["mono_boo"] == "dense"
    assert priors["mono_csharp"] == "mixed"
    assert priors["mono_other"] == "mixed"
    assert priors["asset_unity"] == "mixed"
    assert priors["il2cpp_metadata"] == "mixed"
    assert all(m.extractor for m in REGISTRY)  # 提取器绑定可审计


def test_classify_morphology_paths():
    assert classify_morphology(
        "Managed/Assembly-CSharp.dll") == "mono_csharp"
    assert classify_morphology(
        "lilys-day-off_Data/Managed/"
        "Assembly-UnityScript-firstpass.dll") == "mono_unityscript"
    assert classify_morphology(
        "Managed/Assembly-Boo.dll") == "mono_boo"
    assert classify_morphology(
        "Custom_Data/Managed/StgAssembly_1.dll") == "mono_other"
    assert classify_morphology("level13") == "asset_unity"
    assert classify_morphology("sharedassets0.assets") == "asset_unity"
    assert classify_morphology("Assets/bundle.unity3d") == "asset_unity"
    assert classify_morphology(
        "Custom_Data/il2cpp_data/Metadata/"
        "global-metadata.dat") == "il2cpp_metadata"
    assert classify_morphology("Managed/metadata.dat") == "il2cpp_metadata"
