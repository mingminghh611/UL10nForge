"""识别 L9 测试：脚本类注册表（确定性类名 → config 跳过 + 待登记队列）。"""
from __future__ import annotations

import struct

from hanhua.core.unity.class_registry import (CONFIG_CLASSES,
                                              DISPLAY_CLASSES, disposition)
from hanhua.core.unity.extractor import (_script_class_of,
                                         extract_asset_file)


def _with_len(s: str) -> bytes:
    b = s.encode("utf-8")
    padding = b"\x00" * (-len(b) % 4)
    return struct.pack("<I", len(b)) + b + padding


class _MonoScriptObj:
    def __init__(self, name: str):
        self.type = type("ObjectType", (), {"name": "MonoScript"})()
        self._name = name

    def read_typetree(self):
        return {"m_Name": self._name}


class _FakeObject:
    def __init__(self, path, tree=None, raw: bytes = b"", path_id: int = 7,
                 objects: dict | None = None):
        self.path_id = path_id
        self.assets_file = type("AssetFile", (), {
            "name": path.name, "objects": objects or {}})()
        self.type = type("ObjectType", (), {"name": "MonoBehaviour"})()
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


class TestRegistry:
    def test_dispositions(self):
        assert disposition("TMP_FontAsset") == "config"
        assert disposition("TextMeshProUGUI") == "display"
        assert disposition("MyCustomGameScript") is None

    def test_config_classes_subset(self):
        assert {"TMP_FontAsset", "TMP_SpriteAsset",
                "InputActionAsset", "TimelineAsset"} <= CONFIG_CLASSES

    def test_display_classes_subset(self):
        assert {"TextMeshProUGUI", "TMP_InputField",
                "TextMeshPro"} <= DISPLAY_CLASSES


class TestFmodFamily:
    """give-me-strength 实证（2026-08-29）：FMODUnity.Settings 的 bank 名
    （Master/Music…）与 Platform* 的 parentIdentifier（'default'）被翻译，
    RuntimeManager 加载银行失败 → 全游戏静音。类注册表按命名空间前缀
    整体判定 FMOD 家族；通用词类名（Settings/Platform）不裸名登记，
    防游戏自有同名类（设置界面 Settings）误杀。"""

    def test_fmod_namespace_prefix_is_config(self):
        assert disposition("FMODUnity.Settings") == "config"
        assert disposition("FMODUnity.PlatformWindows") == "config"
        assert disposition("FMODUnity.PlatformAndroid") == "config"
        assert disposition("FMODUnityResonance.FmodResonanceAudio") == "config"

    def test_fmod_proprietary_words_are_config(self):
        assert disposition("StudioBankLoader") == "config"
        assert disposition("StudioEventEmitter") == "config"
        assert disposition("FMODEventTrack") == "config"

    def test_generic_class_names_not_config(self):
        # 游戏自有同名类不得被误杀（裸名不登记，只认 FMOD 命名空间）
        assert disposition("Settings") is None
        assert disposition("Platform") is None
        assert disposition("EventHandler") is None
        assert disposition("RuntimeManager") is None

    def test_tmpro_namespace_prefix_stripped(self):
        assert disposition("TMPro.TMP_StyleSheet") == "config"
        assert disposition("TMPro.TMP_FontAsset") == "config"
        assert disposition("TMPro.TextMeshProUGUI") == "display"
        # 未登记类带 TMPro 前缀仍不判定
        assert disposition("TMPro.TMP_Settings") is None


class TestWritebackFmodFallback:
    """写回侧兜底：旧库（修复前提取）残留的 FMOD/TMP 配置类已翻译条目，
    写回时按 meta.script_class 整体回退保留原文（logic_audit 2d 段）。"""

    def test_fmod_config_object_reverts(self):
        from hanhua.core.unity.logic_audit import (
            logic_key_evidence, typetree_logic_key_evidence)
        r = logic_key_evidence(
            "Master", {"script_class": "FMODUnity.Settings"})
        assert r == ("revert", "fmod_config_object")
        r = logic_key_evidence(
            "默认", {"script_class": "FMODUnity.PlatformWindows"})
        assert r == ("revert", "fmod_config_object")
        r = typetree_logic_key_evidence(
            {"script_class": "StudioBankLoader", "field_path": []}, "Master")
        assert r == ("revert", "fmod_config_object")

    def test_display_and_unknown_classes_not_reverted(self):
        from hanhua.core.unity.logic_audit import (
            logic_key_evidence, typetree_logic_key_evidence)
        # 显示组件类不受影响
        r = logic_key_evidence("Hello", {"script_class": "TextMeshProUGUI"})
        assert r is None
        # 游戏自有 Settings 类不误杀（走到普通 report 规则是既有行为）
        r = typetree_logic_key_evidence(
            {"script_class": "PlayerController", "field_path": ["m_text"]},
            "Hello")
        assert r is None
        # 无 script_class 不判定
        r = logic_key_evidence("Hello", {})
        assert r is None


class TestConfigClassSkip:
    def _extract(self, tmp_path, objects, monkeypatch):
        import UnityPy
        p = tmp_path / "level1"
        p.write_bytes(b"\x00" * 8)
        monkeypatch.setattr(UnityPy, "Environment",
                            lambda: _FakeEnvironment(objects))
        return extract_asset_file(p, "level1")

    def test_config_class_object_skipped_with_reason(self, tmp_path,
                                                     monkeypatch):
        # TMP_FontAsset 对象（typetree 有 m_Script → MonoScript 类名），
        # raw scan 里是字体名/精灵名（按名引用键）——必须整体跳过
        script = _MonoScriptObj("TMP_FontAsset")
        # raw 里放 "MySprite Asset"（有空格会过句子形态防线，
        # 类名证据必须拦下；BaiJamjuree-Medium SDF 会被 engine_string
        # 预过滤先行拦截，测不到类判定分支）
        raw = b"\x00" * 16 + _with_len("MySprite Asset")
        tree = {"m_Script": {"m_FileID": 0, "m_PathID": 9},
                "m_Name": "LiberationSans SDF"}
        obj = _FakeObject(tmp_path / "level1", tree=tree, raw=raw,
                          objects={9: script})
        pf = self._extract(tmp_path, [obj], monkeypatch)
        config_entries = [e for e in pf.entries
                          if e.meta.get("reason") == "script_class_config"]
        assert config_entries, [e.meta.get("reason") for e in pf.entries]
        assert all(e.status == "skipped" for e in config_entries)

    def test_l6_classes_keep_legacy_reason_vocabulary(self, tmp_path,
                                                      monkeypatch):
        # L6 已覆盖的类（InputActionAsset）保持既有 reason
        # input_system_object（审计连续性，不改为 script_class_config）
        script = _MonoScriptObj("InputActionAsset")
        raw = b"\x00" * 16 + _with_len("Jump")
        tree = {"m_Script": {"m_FileID": 0, "m_PathID": 9},
                "m_Name": "obj"}
        obj = _FakeObject(tmp_path / "level1", tree=tree, raw=raw,
                          objects={9: script})
        pf = self._extract(tmp_path, [obj], monkeypatch)
        jump = [e for e in pf.entries if e.original == "Jump"]
        assert jump and jump[0].meta["reason"] == "input_system_object"

    def test_unknown_class_reported_in_meta(self, tmp_path, monkeypatch):
        # 未登记类名 → 容器 meta.script_classes 收集（待登记队列）
        script = _MonoScriptObj("MyCustomGameScript")
        tree = {"m_Script": {"m_FileID": 0, "m_PathID": 9},
                "m_Name": "Thing"}
        obj = _FakeObject(tmp_path / "level1", tree=tree, raw=b"\x00" * 16,
                          objects={9: script})
        pf = self._extract(tmp_path, [obj], monkeypatch)
        assert "MyCustomGameScript" in pf.meta.get("script_classes", ())
