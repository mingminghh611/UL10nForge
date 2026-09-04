import json
import tempfile
from pathlib import Path
import hashlib
import struct
from types import SimpleNamespace

import pytest

import hanhua.core.project as project_module
from hanhua.core.memory import ProjectStore
from hanhua.core.models import FontConfig
from hanhua.core.project import Project
from hanhua.core.tooling.il2cpp_dumper import Il2CppLiteral
from hanhua.core.settings import SettingsStore
from tests.test_scanner import _make_tree
from tests.test_tooling_runner import _make_junction


def test_settings_roundtrip():
    p = Path(tempfile.mkdtemp()) / "s.json"
    s = SettingsStore(p)
    s.load()
    s.api.base_url = "https://x/v1"
    s.api.api_key = "k"
    s.api.model = "m"
    s.save()
    s2 = SettingsStore(p)
    s2.load()
    assert s2.api.base_url == "https://x/v1" and s2.api.model == "m"


def test_settings_without_font_section_defaults_to_lenovo():
    p = Path(tempfile.mkdtemp()) / "s.json"
    p.write_text('{"api": {}, "recent": []}', encoding="utf-8")

    settings = SettingsStore(p)
    settings.load()

    assert settings.font.enabled is True
    assert settings.font.filename == "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf"


def test_settings_font_roundtrip():
    p = Path(tempfile.mkdtemp()) / "s.json"
    settings = SettingsStore(p)
    settings.font.filename = "DingTalk JinBuTi.ttf"
    settings.save()

    loaded = SettingsStore(p)
    loaded.load()

    assert loaded.font == settings.font


def test_project_profile_is_per_project():
    from hanhua.core.models import GameProfile
    d = _make_tree()
    app_dir = Path(tempfile.mkdtemp()) / "app"
    proj = Project.open_game_dir(d, app_dir)
    proj.scan()
    assert proj.profile.game_name == ""
    proj.save_profile(GameProfile(game_name="我的游戏", world_setting="幽谷"))
    assert proj.profile.game_name == "我的游戏"
    # 另一个项目（同一 app_dir 不同游戏目录）档案独立
    d2 = Path(tempfile.mkdtemp())
    (d2 / "a.txt").write_text("hi=hello\n", encoding="utf-8")
    proj2 = Project.open_game_dir(d2, app_dir)
    proj2.scan()
    assert proj2.profile.game_name == ""


def test_unselected_project_keeps_legacy_database_identity(tmp_path):
    game = tmp_path / "Legacy Game"
    game.mkdir()
    app_dir = tmp_path / "app"
    expected_slug = hashlib.md5(str(game).encode("utf-8")).hexdigest()[:10]

    project = Project.open_game_dir(game, app_dir)

    assert project.store.db == app_dir / "projects" / expected_slug / "project.db"


def test_settings_recent():
    p = Path(tempfile.mkdtemp()) / "s.json"
    s = SettingsStore(p)
    s.load()
    s.add_recent("C:/games/a")
    s.add_recent("C:/games/b")
    s2 = SettingsStore(p)
    s2.load()
    assert s2.recent == ["C:/games/b", "C:/games/a"]


def test_project_scan():
    d = _make_tree()
    app_dir = Path(tempfile.mkdtemp()) / "app"
    proj = Project.open_game_dir(d, app_dir)
    assert proj.scan() == 3
    assert proj.store.count("pending") == 3   # 1 json + 1 csv + 1 txt
    assert proj.out_dir == d.parent / (d.name + "_汉化")


def test_scan_aggregates_extractor_skipped_reasons(monkeypatch):
    """R5：提取侧静默跳过聚合进扫描状态（哑识别可见化）——
    静默 continue 不产生条目也不留痕，聚合后进分析报告供审查。"""
    from hanhua.core.extractor import ParsedFile
    d = _make_tree()
    app_dir = Path(tempfile.mkdtemp()) / "app"
    proj = Project.open_game_dir(d, app_dir)

    def fake_parse_file(path, file_id=None):
        pf = ParsedFile(
            file_id or str(path), str(path), "txt", [],
            "utf-8", "\n", {}, False)
        pf.skipped_reasons["code_identifier"] = 42
        pf.skipped_reasons["engine_morph"] = 7
        return pf

    monkeypatch.setattr("hanhua.core.project.parse_file", fake_parse_file)
    kept = proj.scan()
    # _make_tree 有 kept 个文件，计数逐文件聚合
    assert proj._last_scan_skipped == {"code_identifier": 42 * kept,
                                       "engine_morph": 7 * kept}


def _write_valid_pe(path: Path, *, cli: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = bytearray(0x400)
    blob[:2] = b"MZ"
    struct.pack_into("<I", blob, 0x3C, 0x80)
    blob[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", blob, 0x84, 0x8664, 1, 0, 0, 0, 0xF0, 0x22)
    struct.pack_into("<H", blob, 0x98, 0x20B)
    struct.pack_into("<I", blob, 0x98 + 108, 16)
    section = 0x80 + 4 + 20 + 0xF0
    blob[section:section + 8] = b".text\0\0\0"
    struct.pack_into("<IIII", blob, section + 8, 0x200, 0x2000, 0x200, 0x200)
    if cli:
        struct.pack_into("<II", blob, 0x98 + 112 + 14 * 8, 0x2000, 0x48)
        struct.pack_into("<IHHII", blob, 0x200, 0x48, 2, 5, 0x2080, 0x20)
        struct.pack_into("<I", blob, 0x210, 1)
        blob[0x280:0x284] = b"BSJB"
    path.write_bytes(blob)


def _make_nested_mono_players(tmp_path: Path) -> Path:
    source = tmp_path / "Player Package"
    for name in ("A", "B"):
        root = source / name
        data = root / f"{name}_Data"
        _write_valid_pe(root / f"{name}.exe")
        _write_valid_pe(data / "Managed" / "Assembly-CSharp.dll", cli=True)
        (data / "globalgamemanagers").write_bytes(b"2022.3.34f1")
        localization = root / "Localization"
        localization.mkdir()
        (localization / "en.json").write_text(
            '{"title":"Player ' + name + '"}', encoding="utf-8")
    return source


def _make_nested_il2cpp_players(tmp_path: Path) -> Path:
    source = tmp_path / "IL2CPP Package"
    for name in ("A", "B"):
        root = source / name
        data = root / f"{name}_Data"
        metadata = data / "il2cpp_data" / "Metadata" / "global-metadata.dat"
        _write_valid_pe(root / f"{name}.exe")
        _write_valid_pe(root / "GameAssembly.dll")
        metadata.parent.mkdir(parents=True)
        literal = "Hello player"
        raw = bytearray(0x200 + len(literal))
        struct.pack_into("<II", raw, 0, 0xFAB11BAF, 29)
        struct.pack_into("<II", raw, 0x08, 0x100, 8)
        struct.pack_into("<II", raw, 0x10, 0x200, len(literal))
        struct.pack_into("<II", raw, 0x100, len(literal), 0)
        raw[0x200:] = literal.encode("utf-8")
        metadata.write_bytes(raw)
        (data / "globalgamemanagers").write_bytes(b"2022.3.34f1")
        localization = data / "Localization"
        localization.mkdir()
        (localization / "en.json").write_text(
            '{"title":"Player ' + name + '"}', encoding="utf-8")
    return source


def _make_same_root_mono_players(tmp_path: Path) -> Path:
    source = tmp_path / "Shared Player Root"
    source.mkdir()
    for name in ("A", "B"):
        data = source / f"{name}_Data"
        _write_valid_pe(source / f"{name}.exe")
        _write_valid_pe(data / "Managed" / "Assembly-CSharp.dll", cli=True)
        (data / "globalgamemanagers").write_bytes(b"2022.3.34f1")
        localization = data / "Localization"
        localization.mkdir()
        (localization / "en.json").write_text(
            '{"title":"Player ' + name + '"}', encoding="utf-8")
        raw = ("Settings " + name).encode("utf-8")
        (data / "sharedassets0.assets").write_bytes(
            len(raw).to_bytes(4, "little") + raw + b"\0" * (-len(raw) % 4))
        level_raw = ("Level " + name).encode("utf-8")
        (data / "level0").write_bytes(
            len(level_raw).to_bytes(4, "little")
            + level_raw + b"\0" * (-len(level_raw) % 4))
    return source


def _make_flat_with_nested_sibling(tmp_path: Path) -> Path:
    source = tmp_path / "Flat Package"
    data = source / "Flat_Data"
    _write_valid_pe(source / "Flat.exe")
    _write_valid_pe(data / "Managed/Assembly-CSharp.dll", cli=True)
    (data / "globalgamemanagers").write_bytes(b"2022.3.34f1")
    (source / "Localization").mkdir(parents=True)
    (source / "Localization/en.json").write_text(
        '{"title":"Flat"}', encoding="utf-8")

    nested = source / "Nested"
    nested_data = nested / "Nested_Data"
    _write_valid_pe(nested / "Nested.exe")
    _write_valid_pe(nested_data / "Managed/Assembly-CSharp.dll", cli=True)
    (nested_data / "globalgamemanagers").write_bytes(b"2022.3.34f1")
    (nested / "Localization").mkdir(parents=True)
    (nested / "Localization/en.json").write_text(
        '{"title":"Nested"}', encoding="utf-8")
    return source


def _install_fake_raw_asset_environment(monkeypatch) -> None:
    import UnityPy

    class FakeObject:
        def __init__(self, path: Path, raw: bytes):
            self.path_id = 7
            self.assets_file = type("AssetFile", (), {"name": path.name})()
            self.type = type("ObjectType", (), {"name": "MonoBehaviour"})()
            self.raw = raw

        def get_raw_data(self):
            return self.raw

        def set_raw_data(self, raw):
            self.raw = bytes(raw)

    class SerializedFile:
        def __init__(self, environment):
            self.environment = environment
            self.reader = None

        def save(self):
            return self.environment.objects[0].get_raw_data()

    class FakeEnvironment:
        def __init__(self):
            self.objects = []
            self.files = {}

        def load(self, paths):
            path = Path(paths[0] if isinstance(paths, (list, tuple)) else paths)
            self.objects = [FakeObject(path, path.read_bytes())]
            self.files = {"main": SerializedFile(self)}

    monkeypatch.setattr(UnityPy, "Environment", FakeEnvironment)


def test_project_selectors_isolate_nested_player_and_database(tmp_path, monkeypatch):
    source = _make_nested_mono_players(tmp_path)
    app_dir = tmp_path / "app"

    selected_a = Project(
        source, app_dir,
        player_root=Path("A"), player_executable=Path("A/A.exe"),
    )
    selected_b = Project.open_game_dir(
        source, app_dir,
        player_root=source / "B", player_executable=source / "B/B.exe",
    )
    monkeypatch.setattr(selected_a, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)
    real_resolve_font = project_module.resolve_font_provider
    font_roots = []

    def scoped_resolve_font(game_dir, runtime, **kwargs):
        font_roots.append(kwargs.get("player_root"))
        return real_resolve_font(game_dir, runtime, **kwargs)

    monkeypatch.setattr(
        project_module, "resolve_font_provider", scoped_resolve_font)

    report = selected_a.scan_all()

    assert report.fingerprint.player_root == (source / "A").resolve()
    assert [row["rel_path"] for row in selected_a.store.get_files()
            if not row["format"].startswith("v2_")] == ["A/Localization/en.json"]
    assert all(not row["rel_path"].startswith("B/")
               for row in selected_a.store.get_files())
    assert selected_a.store.db != selected_b.store.db
    assert font_roots == [(source / "A").resolve()]


def test_selected_nested_player_writeback_preserves_source_and_sibling(
        tmp_path, monkeypatch):
    import UnityPy

    source = _make_nested_mono_players(tmp_path)
    asset = source / "A/A_Data/sharedassets0.assets"
    asset_text = b"Settings"
    asset.write_bytes(len(asset_text).to_bytes(4, "little") + asset_text + b"\0" * 3)

    class FakeObject:
        def __init__(self, raw):
            self.path_id = 7
            self.assets_file = type("AssetFile", (), {"name": asset.name})()
            self.type = type("ObjectType", (), {"name": "MonoBehaviour"})()
            self.raw = raw

        def get_raw_data(self):
            return self.raw

        def set_raw_data(self, raw):
            self.raw = bytes(raw)

    class SerializedFile:
        def __init__(self, environment):
            self.environment = environment
            self.reader = None

        def save(self):
            return self.environment.objects[0].get_raw_data()

    class FakeEnvironment:
        def __init__(self):
            self.objects = []
            self.files = {}

        def load(self, paths):
            path = paths[0] if isinstance(paths, (list, tuple)) else paths
            self.objects = [FakeObject(Path(path).read_bytes())]
            self.files = {"main": SerializedFile(self)}

    monkeypatch.setattr(UnityPy, "Environment", FakeEnvironment)
    project = Project.open_game_dir(
        source, tmp_path / "app",
        player_root=Path("A"), player_executable=Path("A/A.exe"),
    )
    monkeypatch.setattr(
        project_module.mono_extractor, "extract_dll_user_strings",
        lambda *_args, **_kwargs: SimpleNamespace(noise=True))
    source_manifest = project_module._tree_hashes(source)
    sibling_manifest = project_module._tree_hashes(source / "B")
    from hanhua.core.font import pipeline as pipeline_module
    real_install_font = pipeline_module.install_font_override
    font_roots = []

    def scoped_install_font(game_dir, out_dir, config, **kwargs):
        font_roots.append(kwargs.get("player_root"))
        return real_install_font(game_dir, out_dir, config, **kwargs)

    monkeypatch.setattr(
        pipeline_module, "install_font_override", scoped_install_font)

    assert project.scan_all().unblocked is True
    project.store.set_manual("A/Localization/en.json", "title", "玩家甲")
    asset_file_id = "A/A_Data/sharedassets0.assets"
    asset_entry = next(
        row for row in project.store.get_entries()
        if row["file_id"] == asset_file_id and row["original"] == "Settings")
    project.store.set_manual(asset_file_id, asset_entry["key_path"], "设置")

    result = project.write_all()

    output = project.out_dir / "A" / "Localization" / "en.json"
    assert '"玩家甲"' in output.read_text(encoding="utf-8")
    assert "设置".encode("utf-8") in (
        project.out_dir / "A/A_Data/sharedassets0.assets").read_bytes()
    assert project_module._tree_hashes(source) == source_manifest
    assert project_module._tree_hashes(source / "B") == sibling_manifest
    assert result["verification"]["reopen_verified"] is True
    assert font_roots == [(source / "A").resolve()]


def test_write_all_resyncs_catalog_crc_after_static_font_replace(
        tmp_path, monkeypatch):
    """C5：静态字体替换整容器重建 bundle 后，catalog.bin 的 CRC 必须
    二次同步（write_back_v2 末尾更新过一次，替换后又变了）。"""
    import UnityPy

    source = _make_nested_mono_players(tmp_path)
    asset = source / "A/A_Data/sharedassets0.assets"
    asset_text = b"Settings"
    asset.write_bytes(len(asset_text).to_bytes(4, "little") + asset_text + b"\0" * 3)

    class FakeObject:
        def __init__(self, raw):
            self.path_id = 7
            self.assets_file = type("AssetFile", (), {"name": asset.name})()
            self.type = type("ObjectType", (), {"name": "MonoBehaviour"})()
            self.raw = raw

        def get_raw_data(self):
            return self.raw

        def set_raw_data(self, raw):
            self.raw = bytes(raw)

    class SerializedFile:
        def __init__(self, environment):
            self.environment = environment
            self.reader = None

        def save(self):
            return self.environment.objects[0].get_raw_data()

    class FakeEnvironment:
        def __init__(self):
            self.objects = []
            self.files = {}

        def load(self, paths):
            path = paths[0] if isinstance(paths, (list, tuple)) else paths
            self.objects = [FakeObject(Path(path).read_bytes())]
            self.files = {"main": SerializedFile(self)}

    monkeypatch.setattr(UnityPy, "Environment", FakeEnvironment)
    project = Project.open_game_dir(
        source, tmp_path / "app",
        player_root=Path("A"), player_executable=Path("A/A.exe"),
    )
    monkeypatch.setattr(
        project_module.mono_extractor, "extract_dll_user_strings",
        lambda *_args, **_kwargs: SimpleNamespace(noise=True))

    from hanhua.core.font import pipeline as pipeline_module
    from hanhua.core.unity.font_replace import FontReplaceResult
    replaced_paths = ["A/A_Data/StreamingAssets/aa/fonts.bundle"]
    monkeypatch.setattr(
        pipeline_module, "install_static_fonts",
        lambda *_args, **_kwargs: FontReplaceResult(
            replaced=1, replaced_paths=list(replaced_paths)))
    monkeypatch.setattr(
        pipeline_module, "install_font_override",
        lambda *_args, **_kwargs: None)
    catalog_syncs = []
    monkeypatch.setattr(
        project_module, "_update_addressables_catalogs",
        lambda *_args: catalog_syncs.append(_args) or [])

    assert project.scan_all().unblocked is True
    project.store.set_manual("A/Localization/en.json", "title", "玩家甲")
    asset_file_id = "A/A_Data/sharedassets0.assets"
    asset_entry = next(
        row for row in project.store.get_entries()
        if row["file_id"] == asset_file_id and row["original"] == "Settings")
    project.store.set_manual(asset_file_id, asset_entry["key_path"], "设置")

    project.write_all(font_config=FontConfig(enabled=True))

    # 字体替换后必须以被重建的 bundle 相对路径二次同步 catalog CRC
    assert len(catalog_syncs) == 1
    game_dir, staging, file_infos = catalog_syncs[0]
    assert Path(game_dir).resolve() == source.resolve()
    assert Path(staging).name.startswith(".Player Package_汉化.staging-")
    assert [info["rel_path"] for info in file_infos] == replaced_paths


def _make_publish_ready_project(tmp_path, monkeypatch):
    """写回 C7 测试装置：造出能一路走到发布段的 project。

    返回 (project, source)：source 内含 A/Localization/en.json +
    sharedassets0.assets（Settings 字符串），一条手动译文 + 一条
    asset 译文，scan_all 后 write_all 可完整发布。
    """
    import UnityPy

    source = _make_nested_mono_players(tmp_path)
    asset = source / "A/A_Data/sharedassets0.assets"
    asset_text = b"Settings"
    asset.write_bytes(
        len(asset_text).to_bytes(4, "little") + asset_text + b"\0" * 3)

    class FakeObject:
        def __init__(self, raw):
            self.path_id = 7
            self.assets_file = type("AssetFile", (), {"name": asset.name})()
            self.type = type("ObjectType", (), {"name": "MonoBehaviour"})()
            self.raw = raw

        def get_raw_data(self):
            return self.raw

        def set_raw_data(self, raw):
            self.raw = bytes(raw)

    class SerializedFile:
        def __init__(self, environment):
            self.environment = environment
            self.reader = None

        def save(self):
            return self.environment.objects[0].get_raw_data()

    class FakeEnvironment:
        def __init__(self):
            self.objects = []
            self.files = {}

        def load(self, paths):
            path = paths[0] if isinstance(paths, (list, tuple)) else paths
            self.objects = [FakeObject(Path(path).read_bytes())]
            self.files = {"main": SerializedFile(self)}

    monkeypatch.setattr(UnityPy, "Environment", FakeEnvironment)
    project = Project.open_game_dir(
        source, tmp_path / "app",
        player_root=Path("A"), player_executable=Path("A/A.exe"),
    )
    monkeypatch.setattr(
        project_module.mono_extractor, "extract_dll_user_strings",
        lambda *_args, **_kwargs: SimpleNamespace(noise=True))
    project.scan_all()
    project.store.set_manual("A/Localization/en.json", "title", "玩家甲")
    asset_entry = next(
        row for row in project.store.get_entries()
        if row["file_id"] == "A/A_Data/sharedassets0.assets")
    project.store.set_manual(
        asset_entry["file_id"], asset_entry["key_path"], "设置")
    return project, source


def test_write_all_manifest_written_into_staging_before_rename(
        tmp_path, monkeypatch):
    """C7：manifest（回滚凭据）先写入 staging，随 rename 原子落位——
    消除 rename→写 manifest 之间的崩溃窗口（新版本已发布、凭据却未
    落盘）。第二次发布必须记录 backup 恢复路径。"""
    project, _source = _make_publish_ready_project(tmp_path, monkeypatch)
    project.write_all()  # 第一次发布：产生旧输出

    captured: list[Path] = []
    real_manifest = project._write_publish_manifest  # 绑定方法（monkeypatch 前）

    def scoped_manifest(self, out_dir, *args, **kwargs):
        captured.append(Path(out_dir))
        return real_manifest(out_dir, *args, **kwargs)

    monkeypatch.setattr(Project, "_write_publish_manifest", scoped_manifest)
    project.write_all()  # 第二次发布：backup 路径存在

    # manifest 的目标必须是 staging（rename 前），而不是发布后的 out_dir
    assert captured, "第二次发布必须调用 _write_publish_manifest"
    assert captured[0].name.startswith(".Player Package_汉化.staging-")
    # 发布后 manifest 随 rename 落位 + 记录 backup 回滚凭据
    manifest_path = project.out_dir / ".hanhua-manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert ".backup-" in manifest["backup"]["path"]
    assert "改名为" in manifest["backup"]["restore"]
    # 备份目录确实在磁盘（回滚凭据真实可用）
    backups = list(project.out_dir.parent.glob(
        f".{project.out_dir.name}.backup-*"))
    assert len(backups) == 1


def test_write_all_post_publish_failure_keeps_backup_and_manifest(
        tmp_path, monkeypatch):
    """C7：发布 rename 成功后若后续异常，backup 不再被 finally 无脑
    删除（原实现 rmtree backup 丢回滚凭据）；manifest 已在 out_dir，
    回滚凭据完整。"""
    project, _source = _make_publish_ready_project(tmp_path, monkeypatch)
    project.write_all()  # 第一次发布：产生旧输出
    assert (project.out_dir / ".hanhua-manifest.json").exists()

    real_cleanup = project_module._schedule_backup_cleanup

    def boom_cleanup(*args, **kwargs):
        raise RuntimeError("清理线程启动失败（模拟 rename 后异常）")

    monkeypatch.setattr(project_module, "_schedule_backup_cleanup",
                        boom_cleanup)
    with pytest.raises(RuntimeError, match="清理线程启动失败"):
        project.write_all()

    # rename 已成功：新版本在 out_dir、manifest 落位
    assert (project.out_dir / ".hanhua-manifest.json").exists()
    # backup 保留在磁盘（回滚凭据未丢）
    backups = list(project.out_dir.parent.glob(
        f".{project.out_dir.name}.backup-*"))
    assert len(backups) == 1
    # 备份内容 = 发布前版本（含 manifest 与英文原文）
    assert (backups[0] / ".hanhua-manifest.json").exists()


def test_write_all_publish_rename_failure_keeps_diagnostic_staging(
        tmp_path, monkeypatch):
    """C7：rename 失败时 staging 改名 .diagnostic-* 保留供诊断（原
    实现 finally 直接删除，修复失败的根因排查无现场）；旧版本恢复
    回 out_dir，backup 内容不丢失。"""
    project, _source = _make_publish_ready_project(tmp_path, monkeypatch)
    project.write_all()  # 第一次发布：产生旧输出

    real_replace = project_module._replace_directory
    failures = []

    def failing_replace(source, target):
        if (Path(source).name.startswith(".Player Package_汉化.staging-")
                and Path(target) == project.out_dir):
            if not failures:
                failures.append("boom")
                raise PermissionError("模拟 staging→out_dir rename 失败")
        return real_replace(source, target)

    monkeypatch.setattr(project_module, "_replace_directory",
                        failing_replace)
    with pytest.raises(PermissionError, match="模拟 staging"):
        project.write_all()

    # staging 改名保留供诊断
    diagnostics = list(project.out_dir.parent.glob(
        f".{project.out_dir.name}.diagnostic-*"))
    assert len(diagnostics) == 1
    # 旧版本已恢复回 out_dir（except 恢复路径），内容完整
    assert (project.out_dir / "A/Localization/en.json").exists()
    # 无 backup 残留（恢复路径已把内容还回 out_dir）
    backups = list(project.out_dir.parent.glob(
        f".{project.out_dir.name}.backup-*"))
    assert backups == []


def test_same_root_player_selection_isolates_scan_database_and_writeback(
        tmp_path, monkeypatch):
    source = _make_same_root_mono_players(tmp_path)
    app_dir = tmp_path / "app"
    _install_fake_raw_asset_environment(monkeypatch)

    selected_a = Project.open_game_dir(
        source, app_dir, player_root=source,
        player_executable=source / "A.exe")
    selected_b = Project.open_game_dir(
        source, app_dir, player_root=source,
        player_executable=source / "B.exe")

    assert selected_a.player_root == Path(".")
    assert selected_a.player_executable == Path("A.exe")
    assert selected_a.store.db != selected_b.store.db
    monkeypatch.setattr(
        project_module.mono_extractor, "extract_dll_user_strings",
        lambda *_args, **_kwargs: SimpleNamespace(noise=True))
    real_find_assets = project_module.unity_extractor.find_asset_files
    asset_roots = []

    def scoped_assets(root, *, data_dir=None, exclude_roots=()):
        assert root == (source / "A_Data").resolve()
        assert data_dir == (source / "A_Data").resolve()
        assert tuple(exclude_roots) == ()
        asset_roots.append(root)
        return real_find_assets(root, data_dir=data_dir)

    monkeypatch.setattr(
        project_module.unity_extractor, "find_asset_files", scoped_assets)
    source_manifest = project_module._tree_hashes(source)
    sibling_manifest = project_module._tree_hashes(source / "B_Data")

    assert selected_a.scan_all().unblocked is True
    assert asset_roots == [(source / "A_Data").resolve()]
    files = {row["rel_path"] for row in selected_a.store.get_files()}
    assert "A_Data/Localization/en.json" in files
    assert "A_Data/sharedassets0.assets" in files
    assert "A_Data/level0" in files
    assert not any(path.startswith("B_Data/") for path in files)
    selected_a.store.set_manual(
        "A_Data/Localization/en.json", "title", "玩家甲")
    asset_entry = next(
        row for row in selected_a.store.get_entries()
        if row["file_id"] == "A_Data/sharedassets0.assets")
    selected_a.store.set_manual(
        asset_entry["file_id"], asset_entry["key_path"], "设置甲")

    selected_a.write_all()

    assert '"玩家甲"' in (
        selected_a.out_dir / "A_Data/Localization/en.json").read_text(
            encoding="utf-8")
    assert "设置甲".encode("utf-8") in (
        selected_a.out_dir / "A_Data/sharedassets0.assets").read_bytes()
    assert project_module._tree_hashes(source) == source_manifest
    assert project_module._tree_hashes(source / "B_Data") == sibling_manifest
    assert project_module._tree_hashes(selected_a.out_dir / "B_Data") == sibling_manifest


def test_flat_selected_player_excludes_nested_sibling_from_scan_and_write(
        tmp_path, monkeypatch):
    source = _make_flat_with_nested_sibling(tmp_path)
    _install_fake_raw_asset_environment(monkeypatch)
    project = Project.open_game_dir(
        source, tmp_path / "app",
        player_root=source, player_executable=source / "Flat.exe")
    monkeypatch.setattr(
        project_module.mono_extractor, "extract_dll_user_strings",
        lambda *_args, **_kwargs: SimpleNamespace(noise=True))
    sibling_manifest = project_module._tree_hashes(source / "Nested")

    assert project.scan_all().unblocked is True
    files = {row["rel_path"] for row in project.store.get_files()}
    assert "Localization/en.json" in files
    assert not any(path.startswith("Nested/") for path in files)
    project.store.set_manual("Localization/en.json", "title", "平面")

    project.write_all()

    assert project_module._tree_hashes(source / "Nested") == sibling_manifest
    assert project_module._tree_hashes(project.out_dir / "Nested") == sibling_manifest


def test_stray_data_directory_does_not_hide_root_localization(tmp_path, monkeypatch):
    game = tmp_path / "Single Player"
    data = game / "Single Player_Data"
    _write_valid_pe(game / "Single Player.exe")
    _write_valid_pe(data / "Managed/Assembly-CSharp.dll", cli=True)
    (data / "globalgamemanagers").write_bytes(b"2022.3.34f1")
    (game / "Old_Data").mkdir()
    localization = game / "Localization"
    localization.mkdir()
    (localization / "en.json").write_text(
        '{"title":"Root localization"}', encoding="utf-8")
    project = Project.open_game_dir(game, tmp_path / "app")
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)

    project.scan_all()

    assert {row["rel_path"] for row in project.store.get_files()} == {
        "Localization/en.json",
    }


def test_selected_il2cpp_scan_uses_exact_player_inputs(tmp_path, monkeypatch):
    source = _make_nested_il2cpp_players(tmp_path)
    selected = Project.open_game_dir(
        source, tmp_path / "app",
        player_root=Path("B"), player_executable=Path("B/B.exe"),
    )
    expected_metadata = (
        source / "B/B_Data/il2cpp_data/Metadata/global-metadata.dat").resolve()
    calls = []

    def selected_assets(root, *, data_dir=None, exclude_roots=()):
        assert root == (source / "B").resolve()
        assert data_dir == (source / "B/B_Data").resolve()
        assert tuple(exclude_roots) == ()
        return []

    def selected_metadata(path, file_id=None):
        assert path == expected_metadata
        assert file_id == "B/B_Data/il2cpp_data/Metadata/global-metadata.dat"
        calls.append(path)
        return SimpleNamespace(noise=True)

    monkeypatch.setattr(project_module.unity_extractor, "find_asset_files", selected_assets)
    monkeypatch.setattr(
        project_module.mono_extractor, "find_dll_files",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not rediscover DLLs")))
    monkeypatch.setattr(
        project_module.il2cpp_extractor, "find_metadata_file",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not rediscover metadata")))
    monkeypatch.setattr(
        project_module.il2cpp_extractor, "extract_metadata_strings",
        selected_metadata)

    assert selected.scan_v2() == 0
    assert calls == [expected_metadata]


def test_selected_il2cpp_scan_all_and_writeback_preserve_sibling(
        tmp_path, monkeypatch):
    source = _make_nested_il2cpp_players(tmp_path)
    project = Project.open_game_dir(
        source, tmp_path / "app",
        player_root=Path("B"), player_executable=Path("B/B.exe"),
    )
    selected_assembly = (source / "B/GameAssembly.dll").resolve()
    selected_metadata = (
        source / "B/B_Data/il2cpp_data/Metadata/global-metadata.dat").resolve()
    source_manifest = project_module._tree_hashes(source)
    sibling_manifest = project_module._tree_hashes(source / "A")
    tool_calls = []

    def verified_tool(_runner, _spec, game_assembly, metadata, _config):
        assert game_assembly == selected_assembly
        assert metadata == selected_metadata
        tool_calls.append((game_assembly, metadata))
        return (
            SimpleNamespace(cache_hit=False, elapsed_ms=3),
            (Il2CppLiteral("Hello player", 0x1000),),
        )

    monkeypatch.setattr(project_module, "run_il2cpp_dumper", verified_tool)

    report = project.scan_all()

    assert report.unblocked is True
    assert tool_calls == [(selected_assembly, selected_metadata)]
    assert all(not row["rel_path"].startswith("A/")
               for row in project.store.get_files())
    metadata_id = "B/B_Data/il2cpp_data/Metadata/global-metadata.dat"
    project.store.set_manual(metadata_id, "meta#0", "你好")

    result = project.write_all()

    output_metadata = (
        project.out_dir / "B/B_Data/il2cpp_data/Metadata/global-metadata.dat")
    output_raw = output_metadata.read_bytes()
    reopened = [
        output_raw[offset:offset + length].decode("utf-8").rstrip("\0")
        for _index, length, offset
        in project_module.il2cpp_extractor.parse_string_literals(output_raw)
    ]
    assert reopened == ["你好"]
    assert result["v2"].entries == 1
    assert project_module._tree_hashes(source) == source_manifest
    assert project_module._tree_hashes(source / "A") == sibling_manifest
    assert project_module._tree_hashes(project.out_dir / "A") == sibling_manifest


def test_selected_mono_scan_uses_only_fingerprinted_application_assemblies(
        tmp_path, monkeypatch):
    source = _make_nested_mono_players(tmp_path)
    selected = Project.open_game_dir(
        source, tmp_path / "app",
        player_root=Path("A"), player_executable=Path("A/A.exe"),
    )
    expected = (source / "A/A_Data/Managed/Assembly-CSharp.dll").resolve()
    calls = []

    monkeypatch.setattr(
        project_module.unity_extractor, "find_asset_files",
        lambda root, *, data_dir=None, exclude_roots=(): []
        if (root == (source / "A").resolve()
            and data_dir == (source / "A/A_Data").resolve()
            and tuple(exclude_roots) == ())
        else (_ for _ in ()).throw(AssertionError("wrong asset root")))
    monkeypatch.setattr(
        project_module.mono_extractor, "find_dll_files",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not rediscover DLLs")))

    def selected_assembly(path, file_id=None, *, cross_sinks=frozenset()):
        assert path == expected
        assert file_id == "A/A_Data/Managed/Assembly-CSharp.dll"
        calls.append(path)
        return SimpleNamespace(noise=True)

    monkeypatch.setattr(
        project_module.mono_extractor, "extract_dll_user_strings",
        selected_assembly)

    assert selected.scan_v2() == 0
    assert calls == [expected]


def test_unselected_ambiguous_project_blocks_before_scanner_or_store(
        tmp_path, monkeypatch):
    source = _make_nested_mono_players(tmp_path)
    project = Project.open_game_dir(source, tmp_path / "app")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("ambiguous project must not scan or mutate store")

    monkeypatch.setattr(project_module, "discover", forbidden)
    monkeypatch.setattr(project_module.unity_extractor, "find_asset_files", forbidden)

    report = project.scan_all()

    assert report.fingerprint.layout_kind == "ambiguous"
    assert report.unblocked is False
    assert report.text_files == 0
    assert report.v2_files == 0
    # schema 必须已初始化：blocked 项目后续 get_entries 可安全调用
    # （ned-flanders 真实案例：未建表时 get_entries 抛 OperationalError）
    assert project.store.get_entries() == []


def test_write_preflight_rejects_selected_backend_layout_drift_before_copy(
        tmp_path, monkeypatch):
    source = _make_nested_mono_players(tmp_path)
    project = Project.open_game_dir(
        source, tmp_path / "app",
        player_root=Path("A"), player_executable=Path("A/A.exe"),
    )
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)
    assert project.scan_all().unblocked is True
    project.store.set_manual("A/Localization/en.json", "title", "玩家甲")
    _write_valid_pe(
        source / "A/A_Data/Managed/Assembly-CSharp-Extra.dll", cli=True)
    copy_started = False

    def forbidden_copy(*_args, **_kwargs):
        nonlocal copy_started
        copy_started = True

    monkeypatch.setattr(project_module, "copy_game_dir", forbidden_copy)

    with pytest.raises(RuntimeError, match="layout/backend inputs changed"):
        project.write_all()

    assert copy_started is False


def _make_mono_unity_game(tmp_path: Path) -> Path:
    game = tmp_path / "Fixture Game"
    data = game / "Fixture Game_Data"
    (data / "Managed").mkdir(parents=True)
    _write_valid_pe(game / "Fixture Game.exe")
    (game / "MonoBleedingEdge").mkdir()
    (data / "globalgamemanagers").write_bytes(b"header 2022.3.34f1 trailer")
    _write_valid_pe(data / "Managed" / "Assembly-CSharp.dll", cli=True)
    (data / "Managed" / "UnityEngine.CoreModule.dll").write_bytes(b"fixture")
    return game


def test_project_analyze_reports_runtime_tools_and_deterministic_route(tmp_path):
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")

    report = project.analyze()

    assert report.fingerprint.runtime == "mono"
    assert report.fingerprint.unity_version == "2022.3.34f1"
    assert {status.tool_id for status in report.tool_statuses} == {
        "il2cpp_dumper", "bmfont",
    }
    route = {step.step_id: step for step in report.route}
    assert route["detection"].status == "succeeded"
    assert route["tool_analysis"].status == "skipped"
    assert report.unblocked is True
    assert report.completable is False
    assert report.input_protected is True
    assert report.font_capability.provider_id == "bepinex5_mono_x64"
    assert report.font_capability.provider_supported is True
    assert report.font_capability.payload_available is True


def _make_il2cpp_unity_game(tmp_path: Path) -> Path:
    game = tmp_path / "IL2CPP Fixture"
    data = game / "IL2CPP Fixture_Data"
    metadata_dir = data / "il2cpp_data" / "Metadata"
    metadata_dir.mkdir(parents=True)
    _write_valid_pe(game / "IL2CPP Fixture.exe")
    _write_valid_pe(game / "GameAssembly.dll")
    (data / "globalgamemanagers").write_bytes(b"2022.3.11f1")
    (metadata_dir / "global-metadata.dat").write_bytes(
        struct.pack("<II", 0xFAB11BAF, 29))
    return game


def test_project_analyze_exposes_optional_il2cpp_font_boundary(tmp_path):
    project = Project.open_game_dir(
        _make_il2cpp_unity_game(tmp_path), tmp_path / "app")

    report = project.analyze()
    font = {step.step_id: step for step in report.route}["font"]

    assert report.font_capability.provider_id == "bepinex6_il2cpp_x64"
    assert report.font_capability.provider_supported is False
    assert report.font_capability.payload_available is False
    assert report.font_capability.runtime_verified is False
    assert font.backend == "static_replace"
    assert font.status == "pending"
    assert font.required is True
    assert "静态字体替换" in font.reason


def _add_write_ready_text(project: Project) -> None:
    text = project.game_dir / "Localization" / "en.json"
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_text('{"title":"Hello"}', encoding="utf-8")
    project.scan()
    project.store.set_manual("Localization/en.json", "title", "你好")


def test_runtime_exact_translations_include_only_unambiguous_write_ready_text(
        tmp_path):
    store = ProjectStore(tmp_path / "runtime-map.sqlite")
    store.init_schema()
    store.add_file("ui", "ui.asset", "v2_unity", "binary", "", {})
    rows = [
        ("settings", "Settings", "translate", "设置", "translated"),
        ("structural", "Internal Key", "structural", "内部键", "translated"),
        ("failed", "Retry", "translate", "重试", "failed"),
        ("conflict-a", "Dynamic Label", "translate", "动态标签", "translated"),
        ("conflict-b", "Dynamic Label", "translate", "变化标签", "translated"),
    ]
    store.upsert_entries([
        {
            "file_id": "ui",
            "key_path": key_path,
            "original": original,
            "meta": {"disposition": disposition, "confidence": "high"},
        }
        for key_path, original, disposition, _translation, _status in rows
    ])
    for key_path, _original, _disposition, translation, status in rows:
        store.update_translation("ui", key_path, translation, status)

    assert project_module._runtime_exact_translations(store) == {
        "Settings": "设置",
    }


def test_runtime_exact_translations_remove_chain_and_cycle_edges(tmp_path):
    store = ProjectStore(tmp_path / "runtime-graph.sqlite")
    store.init_schema()
    store.add_file("ui", "ui.asset", "v2_unity", "binary", "", {})
    mappings = [
        ("chain-a", "A", "B"),
        ("chain-b", "B", "C"),
        ("cycle-x", "X", "Y"),
        ("cycle-y", "Y", "X"),
        ("stable", "Settings", "设置"),
    ]
    store.upsert_entries([{
        "file_id": "ui",
        "key_path": key_path,
        "original": original,
        "meta": {"disposition": "translate", "confidence": "high"},
    } for key_path, original, _translation in mappings])
    for key_path, _original, translation in mappings:
        store.update_translation("ui", key_path, translation, "translated")

    assert project_module._runtime_exact_translations(store) == {
        "B": "C",
        "Settings": "设置",
    }


def test_scan_all_keeps_native_results_when_required_tool_fails(tmp_path, monkeypatch):
    game = _make_il2cpp_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    project.store.init_schema()
    metadata = game / "IL2CPP Fixture_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    before = metadata.read_bytes()
    monkeypatch.setattr(project, "scan", lambda: 4)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 2)

    def fail_tool(*args, **kwargs):
        raise RuntimeError("cross-check failed")

    monkeypatch.setattr("hanhua.core.project.run_il2cpp_dumper", fail_tool)
    events = []

    report = project.scan_all(events.append)

    assert report.text_files == 4 and report.v2_files == 2
    assert report.tool_results[0].status == "failed"
    assert {step.step_id: step.status for step in report.route}["tool_analysis"] == "failed"
    assert report.completable is False
    assert [event.phase for event in events] == [
        "detection", "text_scan", "binary_scan", "tool_analysis", "complete",
    ]
    assert metadata.read_bytes() == before


def test_scan_all_reports_recognition_coverage_gaps(tmp_path, monkeypatch):
    """覆盖率接线（0.38.0）：scan_all 末尾跑轻量 census 差集——census
    看到但提取池没有的文本按 _string_disposition 归因聚合进
    report.recognition_gaps，未解释缺口是识别盲区的工作队列（哑信号
    可观测）。池内文本不产生缺口。"""
    game = _make_mono_unity_game(tmp_path)
    # census 载体：.bin 不在已覆盖后缀清单，字节文本会被普查命中；
    # "Player dead" 进不了任何提取池（无 KV/JSON 结构）→ unexplained；
    # "Localized Name Here" 同时写进 JSON（scan 会提取）→ 不成缺口
    probe = game / "Fixture Game_Data" / "strings_probe.bin"
    probe.write_bytes(b"Player dead here\x00")
    json_path = game / "Localization" / "en.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text('{"title":"Visible Naming"}', encoding="utf-8")
    project = Project.open_game_dir(game, tmp_path / "app")
    monkeypatch.setattr(project, "scan", lambda: 1)
    monkeypatch.setattr(
        project, "scan_v2",
        lambda progress_cb=None, csv_overwrite_source=False: 0)

    report = project.scan_all()

    gaps = report.recognition_gaps
    assert gaps, "census 载体存在时缺口摘要不得为空"
    assert gaps["gap_total"] >= 1
    assert gaps["unexplained"] >= 1
    assert any("Player dead" in sample
               for sample in gaps["unexplained_samples"])
    # 池内文本（JSON 已提取）不应成为缺口
    assert all("Visible Naming" not in sample
               for sample in gaps["unexplained_samples"])
    # 持久化 + resume 恢复（与 il2cpp_cross_check 同模式）
    assert project.store.get_profile_value("recognition_coverage") == gaps
    reopened = Project.open_game_dir(game, tmp_path / "app")
    assert reopened._last_scan_gaps == gaps


def test_scan_all_coverage_gap_failure_degrades_silently(tmp_path, monkeypatch):
    """覆盖率是观测通道不是闸门：census 抛异常时静默降级（空摘要），
    scan_all 正常完成、报告其余字段不受影响（宁漏勿坏）。"""
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    monkeypatch.setattr(project, "scan", lambda: 1)
    monkeypatch.setattr(
        project, "scan_v2",
        lambda progress_cb=None, csv_overwrite_source=False: 0)

    import hanhua.core.recognition_report as rr
    def broken_gaps(*args, **kwargs):
        raise RuntimeError("census boom")

    monkeypatch.setattr(rr, "coverage_gaps", broken_gaps)
    report = project.scan_all()

    assert report.recognition_gaps == {}
    assert report.unblocked is True


def test_scan_all_degrades_tool_analysis_on_dumper_version_gap(tmp_path, monkeypatch):
    """Il2CppDumper 二进制不支持 metadata v39，而 native 解析器已验证支持时，
    tool_analysis 降级为 skipped（审计保留 failed 记录），不阻断流水线。#183"""
    game = _make_il2cpp_unity_game(tmp_path)
    metadata = game / "IL2CPP Fixture_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    metadata.write_bytes(struct.pack("<II", 0xFAB11BAF, 39) + b"\x00" * 64)
    project = Project.open_game_dir(game, tmp_path / "app")
    project.store.init_schema()
    monkeypatch.setattr(project, "scan", lambda: 4)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 2)

    def version_gap(*args, **kwargs):
        raise RuntimeError(
            "System.NotSupportedException: ERROR: Metadata file supplied "
            "is not a supported version[39].")

    monkeypatch.setattr("hanhua.core.project.run_il2cpp_dumper", version_gap)

    report = project.scan_all()

    assert {step.step_id: step.status for step in report.route}["tool_analysis"] == "skipped"
    assert report.tool_results[0].status == "failed"      # 可见性保留
    assert "not a supported version" in report.tool_results[0].reason
    assert report.unblocked is True                        # 不阻断流水线
    assert report.completable is False                     # 交叉验证未完成


def test_scan_all_keeps_blocked_on_generic_tool_failure(tmp_path, monkeypatch):
    """非版本缺口的通用工具失败必须保持 blocked，不得误放行。"""
    game = _make_il2cpp_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    project.store.init_schema()
    monkeypatch.setattr(project, "scan", lambda: 4)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 2)

    def fail_tool(*args, **kwargs):
        raise RuntimeError("game assembly unreadable")

    monkeypatch.setattr("hanhua.core.project.run_il2cpp_dumper", fail_tool)

    report = project.scan_all()

    assert {step.step_id: step.status for step in report.route}["tool_analysis"] == "failed"
    assert report.unblocked is False


def test_scan_all_surfaces_verified_tool_cache_hit(tmp_path, monkeypatch):
    game = _make_il2cpp_unity_game(tmp_path)
    metadata = game / "IL2CPP Fixture_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    metadata.write_bytes(metadata.read_bytes() + b"Hello")
    project = Project.open_game_dir(game, tmp_path / "app")
    monkeypatch.setattr(project, "scan", lambda: 1)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 1)
    monkeypatch.setattr(
        "hanhua.core.project.il2cpp_extractor.parse_string_literals",
        lambda raw: [(0, 5, 8)],
    )
    calls = 0

    def fake_tool(*args, **kwargs):
        nonlocal calls
        calls += 1
        return (SimpleNamespace(cache_hit=calls > 1, elapsed_ms=7),
                (Il2CppLiteral("Hello", 0x1000),))

    monkeypatch.setattr("hanhua.core.project.run_il2cpp_dumper", fake_tool)

    first = project.scan_all()
    second = project.scan_all()

    assert first.tool_results[0].status == "succeeded"
    assert first.tool_results[0].cache_hit is False
    assert second.tool_results[0].cache_hit is True
    assert second.unblocked is True
    assert second.completable is False


def test_write_all_preflights_all_persisted_paths_before_copy(tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    project.store.init_schema()
    project.store.add_file(
        "evil", "../outside.txt", "txt", "utf-8", "\n", {})
    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("copy must not start before rel_path preflight")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(ValueError, match="不安全的相对路径"):
        project.write_all()

    assert copy_started is False
    assert not project.out_dir.exists()


def test_write_all_rejects_unknown_runtime_before_copy(tmp_path, monkeypatch):
    game = tmp_path / "Unknown Game"
    game.mkdir()
    (game / "strings.txt").write_text("title=Hello\n", encoding="utf-8")
    project = Project.open_game_dir(game, tmp_path / "app")
    project.scan()
    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("unknown runtime must be rejected before copy")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(RuntimeError, match="运行时|writer|写回"):
        project.write_all()

    assert copy_started is False
    assert not project.out_dir.exists()


def test_write_all_rejects_il2cpp_v29_without_successful_cross_check(
        tmp_path, monkeypatch):
    game = _make_il2cpp_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    project.store.init_schema()
    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("IL2CPP evidence gate must run before copy")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(RuntimeError, match="IL2CPP.*交叉验证"):
        project.write_all()

    assert copy_started is False


def test_write_all_accepts_v39_version_gap_degrade(tmp_path, monkeypatch):
    """版本缺口降级（dumper 不支持 v39，native 解析器已验证）必须允许
    写回：#183。通用工具失败仍保持拒绝。"""
    game = _make_il2cpp_unity_game(tmp_path)
    metadata = (
        game / "IL2CPP Fixture_Data" / "il2cpp_data" / "Metadata"
        / "global-metadata.dat"
    )
    metadata.write_bytes(
        struct.pack("<II", 0xFAB11BAF, 39) + metadata.read_bytes()[8:])
    project = Project.open_game_dir(game, tmp_path / "app")
    project.store.init_schema()
    _add_write_ready_text(project)
    monkeypatch.setattr(project, "scan", lambda: 0)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)
    monkeypatch.setattr(
        "hanhua.core.project.il2cpp_extractor.parse_string_literals",
        lambda raw: [(0, 5, 8)],
    )

    def version_gap(*args, **kwargs):
        raise RuntimeError(
            "System.NotSupportedException: ERROR: Metadata file supplied "
            "is not a supported version[39].")

    monkeypatch.setattr("hanhua.core.project.run_il2cpp_dumper", version_gap)
    report = project.scan_all()
    assert {s.step_id: s.status for s in report.route}["tool_analysis"] == "skipped"
    assert report.unblocked is True

    def copy_reached(*args, **kwargs):
        raise RuntimeError("COPY_REACHED")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", copy_reached)

    with pytest.raises(RuntimeError, match="COPY_REACHED"):
        project.write_all()


def test_write_all_accepts_current_successful_il2cpp_v29_cross_check(
        tmp_path, monkeypatch):
    game = _make_il2cpp_unity_game(tmp_path)
    metadata = (
        game / "IL2CPP Fixture_Data" / "il2cpp_data" / "Metadata"
        / "global-metadata.dat"
    )
    metadata.write_bytes(metadata.read_bytes() + b"Hello")
    project = Project.open_game_dir(game, tmp_path / "app")
    _add_write_ready_text(project)
    monkeypatch.setattr(project, "scan", lambda: 0)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)
    monkeypatch.setattr(
        "hanhua.core.project.il2cpp_extractor.parse_string_literals",
        lambda raw: [(0, 5, 8)],
    )

    def fake_tool(*args, **kwargs):
        return (
            SimpleNamespace(cache_hit=False, elapsed_ms=5),
            (Il2CppLiteral("Hello", 0x1000),),
        )

    monkeypatch.setattr("hanhua.core.project.run_il2cpp_dumper", fake_tool)
    report = project.scan_all()
    assert report.tool_results[0].status == "succeeded"

    def copy_reached(*args, **kwargs):
        raise RuntimeError("COPY_REACHED")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", copy_reached)

    with pytest.raises(RuntimeError, match="COPY_REACHED"):
        project.write_all()


def test_il2cpp_v29_static_writeback_uses_static_font_replace(
        tmp_path, monkeypatch):
    game = _make_il2cpp_unity_game(tmp_path)
    metadata = (
        game / "IL2CPP Fixture_Data" / "il2cpp_data" / "Metadata"
        / "global-metadata.dat"
    )
    metadata.write_bytes(metadata.read_bytes() + b"Hello")
    project = Project.open_game_dir(
        game, tmp_path / "app", FontConfig(enabled=True))
    _add_write_ready_text(project)
    monkeypatch.setattr(project, "scan", lambda: 0)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)
    monkeypatch.setattr(
        "hanhua.core.project.il2cpp_extractor.parse_string_literals",
        lambda raw: [(0, 5, 8)],
    )
    monkeypatch.setattr(
        "hanhua.core.project.run_il2cpp_dumper",
        lambda *args, **kwargs: (
            SimpleNamespace(cache_hit=False, elapsed_ms=5),
            (Il2CppLiteral("Hello", 0x1000),),
        ),
    )
    report = project.scan_all()
    font_step = {step.step_id: step for step in report.route}["font"]
    assert font_step.status == "pending"
    assert font_step.backend == "static_replace"

    def reject_font_install(*args, **kwargs):
        raise AssertionError("unsupported IL2CPP font installer must not run")

    monkeypatch.setattr(
        "hanhua.core.font.pipeline.install_font_override",
        reject_font_install)

    result = project.write_all()

    # fixture 游戏无 Font/TMP 对象：静态替换执行但替换数 0，
    # 退回 capability 本身（unsupported 但不阻塞写回）
    assert result["font"].provider_id == "bepinex6_il2cpp_x64"
    assert result["font"].provider_supported is False
    assert result["font"].payload_deployed is False
    assert result["font"].runtime_verified is False
    assert result["verification"]["font_level"] == "unsupported"
    assert result["verification"]["font_payload_deployed"] is False
    assert result["verification"]["font_runtime_verified"] is False
    assert result["analysis_report"].completable is True
    assert (project.out_dir / "Localization" / "en.json").read_text(
        encoding="utf-8") == '{"title":"你好"}'


@pytest.mark.parametrize("changed_input", ("game_assembly", "metadata"))
def test_write_all_rejects_il2cpp_input_drift_after_successful_cross_check(
        tmp_path, monkeypatch, changed_input):
    game = _make_il2cpp_unity_game(tmp_path)
    metadata = (
        game / "IL2CPP Fixture_Data" / "il2cpp_data" / "Metadata"
        / "global-metadata.dat"
    )
    metadata.write_bytes(metadata.read_bytes() + b"Hello")
    project = Project.open_game_dir(game, tmp_path / "app")
    monkeypatch.setattr(project, "scan", lambda: 0)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)
    monkeypatch.setattr(
        "hanhua.core.project.il2cpp_extractor.parse_string_literals",
        lambda raw: [(0, 5, 8)],
    )
    monkeypatch.setattr(
        "hanhua.core.project.run_il2cpp_dumper",
        lambda *args, **kwargs: (
            SimpleNamespace(cache_hit=False, elapsed_ms=5),
            (Il2CppLiteral("Hello", 0x1000),),
        ),
    )
    report = project.scan_all()
    assert report.tool_results[0].status == "succeeded"

    target = (
        game / "GameAssembly.dll" if changed_input == "game_assembly"
        else metadata
    )
    original = target.read_bytes()
    target.write_bytes(original[:-1] + bytes([original[-1] ^ 0x01]))
    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("input drift must be rejected before copy")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(RuntimeError, match="IL2CPP.*输入.*变化"):
        project.write_all()

    assert copy_started is False


def test_write_all_rejects_unsupported_il2cpp_metadata_v30_before_copy(
        tmp_path, monkeypatch):
    game = _make_il2cpp_unity_game(tmp_path)
    metadata = (
        game / "IL2CPP Fixture_Data" / "il2cpp_data" / "Metadata"
        / "global-metadata.dat"
    )
    metadata.write_bytes(struct.pack("<II", 0xFAB11BAF, 30))
    project = Project.open_game_dir(game, tmp_path / "app")
    project.store.init_schema()
    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("unsupported metadata must be rejected before copy")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(RuntimeError, match="writer 路由不可用"):
        project.write_all()

    assert copy_started is False


def test_write_all_rejects_required_failed_route_before_copy(tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    text = game / "Localization" / "en.json"
    text.parent.mkdir()
    text.write_text('{"title":"Hello"}', encoding="utf-8")
    project = Project.open_game_dir(game, tmp_path / "app")
    project.scan()
    project.store.set_manual("Localization/en.json", "title", "你好")
    monkeypatch.setattr(
        "hanhua.core.project.plan_backends",
        lambda *args, **kwargs: (
                SimpleNamespace(
                    required=True,
                    status="failed",
                    step_id="translation_quality",
                    reason="synthetic required failure",
                    backend="quality_gate",
                    confidence="low",
                ),
        ),
    )
    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("required failure must be rejected before copy")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(RuntimeError, match="translation_quality"):
        project.write_all()

    assert copy_started is False


def test_write_all_rejects_zero_write_ready_translations_before_copy(
        tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    text = game / "Localization" / "en.json"
    text.parent.mkdir()
    text.write_text('{"title":"Hello"}', encoding="utf-8")
    project = Project.open_game_dir(game, tmp_path / "app")
    project.scan()
    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("zero write-ready must be rejected before copy")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(RuntimeError, match="可写译文|write-ready"):
        project.write_all()

    assert copy_started is False


def test_write_all_rejects_required_pending_prerequisite_before_copy(
        tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    text = game / "Localization" / "en.json"
    text.parent.mkdir()
    text.write_text('{"title":"Hello"}', encoding="utf-8")
    project = Project.open_game_dir(game, tmp_path / "app")
    project.scan()
    project.store.set_manual("Localization/en.json", "title", "你好")
    monkeypatch.setattr(
        "hanhua.core.project.plan_backends",
        lambda *args, **kwargs: (
            SimpleNamespace(
                required=True, status="succeeded", step_id="detection",
                reason="detected", backend="native", confidence="high"),
            SimpleNamespace(
                required=True, status="pending", step_id="custom_prerequisite",
                reason="not executed", backend="fixture", confidence="low"),
            SimpleNamespace(
                required=True, status="pending", step_id="writeback",
                reason="not started", backend="writer", confidence="high"),
        ),
    )
    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("required pending prerequisite must block copy")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(RuntimeError, match="custom_prerequisite"):
        project.write_all()

    assert copy_started is False


def test_write_all_rechecks_il2cpp_evidence_after_copy_before_any_writer(
        tmp_path, monkeypatch):
    game = _make_il2cpp_unity_game(tmp_path)
    metadata = (
        game / "IL2CPP Fixture_Data" / "il2cpp_data" / "Metadata"
        / "global-metadata.dat"
    )
    metadata.write_bytes(metadata.read_bytes() + b"Hello")
    project = Project.open_game_dir(game, tmp_path / "app")
    _add_write_ready_text(project)
    monkeypatch.setattr(project, "scan", lambda: 0)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)
    monkeypatch.setattr(
        "hanhua.core.project.il2cpp_extractor.parse_string_literals",
        lambda raw: [(0, 5, 8)],
    )
    monkeypatch.setattr(
        "hanhua.core.project.run_il2cpp_dumper",
        lambda *args, **kwargs: (
            SimpleNamespace(cache_hit=False, elapsed_ms=5),
            (Il2CppLiteral("Hello", 0x1000),),
        ),
    )
    assert project.scan_all().tool_results[0].status == "succeeded"
    real_copy = __import__(
        "hanhua.core.unity.writer", fromlist=["copy_game_dir"]
    ).copy_game_dir
    writer_started = False

    def mutate_then_copy(source, staging, progress_cb=None):
        original = metadata.read_bytes()
        metadata.write_bytes(original[:-1] + bytes([original[-1] ^ 0x01]))
        return real_copy(source, staging, progress_cb)

    def reject_writer(*args, **kwargs):
        nonlocal writer_started
        writer_started = True
        raise AssertionError("writer must not run with stale IL2CPP evidence")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", mutate_then_copy)
    monkeypatch.setattr("hanhua.core.project.write_back_text", reject_writer)

    with pytest.raises(RuntimeError, match="IL2CPP.*输入.*变化"):
        project.write_all()

    assert writer_started is False


def test_write_all_rejects_json_modified_after_successful_scan_before_copy(
        tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    _add_write_ready_text(project)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)
    assert project.scan_all().input_protected is True
    source = game / "Localization" / "en.json"
    source.write_text('{"title":"Changed"}', encoding="utf-8")
    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("drift must be rejected before copy")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(RuntimeError, match="扫描.*输入|输入.*变化"):
        project.write_all()

    assert copy_started is False


@pytest.mark.parametrize("operation,relative", [
    ("modify", "Localization/en.json"),
    ("modify", "Fixture Game_Data/cache/data.bundle"),
    ("modify", "Fixture Game_Data/Managed/Fixture.dll"),
    ("add", "Localization/added.json"),
    ("add", "Fixture Game_Data/cache/added.bundle"),
    ("add", "Fixture Game_Data/Managed/Added.dll"),
    ("delete", "Localization/en.json"),
    ("delete", "Fixture Game_Data/cache/data.bundle"),
    ("delete", "Fixture Game_Data/Managed/Fixture.dll"),
])
def test_write_all_rejects_any_full_tree_drift_before_copy_and_keeps_output(
        tmp_path, monkeypatch, operation, relative):
    game = _make_mono_unity_game(tmp_path)
    json_path = game / "Localization" / "en.json"
    json_path.parent.mkdir(parents=True)
    json_path.write_text('{"title":"Hello"}', encoding="utf-8")
    bundle = game / "Fixture Game_Data" / "cache" / "data.bundle"
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"UnityFS fixture")
    dll = game / "Fixture Game_Data" / "Managed" / "Fixture.dll"
    dll.write_bytes(b"MZ fixture dll")
    project = Project.open_game_dir(game, tmp_path / "app")
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)
    assert project.scan_all().input_protected is True
    project.store.set_manual("Localization/en.json", "title", "你好")
    project.out_dir.mkdir(parents=True)
    marker = project.out_dir / "KEEP-ME.txt"
    marker.write_text("old output", encoding="utf-8")
    target = game / relative
    if operation == "modify":
        target.write_bytes(target.read_bytes() + b" changed")
    elif operation == "add":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"added")
    else:
        target.unlink()
    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("full-tree drift must be rejected before copy")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(RuntimeError, match="扫描后的.*输入.*变化"):
        project.write_all()

    assert copy_started is False
    assert marker.read_text(encoding="utf-8") == "old output"


def test_scan_all_marks_full_tree_change_during_scan_unprotected(
        tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    source = game / "Localization" / "en.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"title":"Hello"}', encoding="utf-8")
    project = Project.open_game_dir(game, tmp_path / "app")
    real_scan = project.scan

    def mutate_during_scan():
        result = real_scan()
        source.write_text('{"title":"Changed"}', encoding="utf-8")
        return result

    monkeypatch.setattr(project, "scan", mutate_during_scan)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)

    report = project.scan_all()

    assert report.input_protected is False
    assert report.unblocked is False
    assert any("完整输入文件树变化" in item for item in report.warnings)


def test_write_all_rejects_copy_time_drift_before_writer_and_keeps_output(
        tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    _add_write_ready_text(project)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)
    assert project.scan_all().input_protected is True
    project.out_dir.mkdir(parents=True)
    marker = project.out_dir / "KEEP-ME.txt"
    marker.write_text("old output", encoding="utf-8")
    source = game / "Localization" / "en.json"
    real_copy = __import__(
        "hanhua.core.unity.writer", fromlist=["copy_game_dir"]
    ).copy_game_dir
    writer_started = False

    def mutate_after_copy(source_root, staging, progress_cb=None):
        copied = real_copy(source_root, staging, progress_cb)
        source.write_text('{"title":"Changed"}', encoding="utf-8")
        return copied

    def reject_writer(*args, **kwargs):
        nonlocal writer_started
        writer_started = True
        raise AssertionError("writer must not run after copy-time drift")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", mutate_after_copy)
    monkeypatch.setattr("hanhua.core.project.write_back_text", reject_writer)

    with pytest.raises(RuntimeError, match="复制期间.*扫描清单"):
        project.write_all()

    assert writer_started is False
    assert marker.read_text(encoding="utf-8") == "old output"


def test_write_all_rejects_text_writer_that_does_not_apply_translation(
        tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    _add_write_ready_text(project)
    project.out_dir.mkdir(parents=True)
    marker = project.out_dir / "KEEP-ME.txt"
    marker.write_text("old output", encoding="utf-8")
    monkeypatch.setattr(
        "hanhua.core.project.write_back_text", lambda *args, **kwargs: 1)

    with pytest.raises(RuntimeError, match="重开验证|译文.*未写入"):
        project.write_all()

    assert marker.read_text(encoding="utf-8") == "old output"


def test_write_all_rechecks_source_immediately_before_publish_and_keeps_output(
        tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    _add_write_ready_text(project)
    project.out_dir.mkdir(parents=True)
    marker = project.out_dir / "KEEP-ME.txt"
    marker.write_text("old output", encoding="utf-8")
    source = game / "Localization" / "en.json"
    real_reopen = __import__(
        "hanhua.core.project", fromlist=["_reopen_written_outputs"]
    )._reopen_written_outputs

    def mutate_after_reopen(store, staging):
        verified = real_reopen(store, staging)
        source.write_text('{"title":"Changed before publish"}', encoding="utf-8")
        return verified

    monkeypatch.setattr(
        "hanhua.core.project._reopen_written_outputs", mutate_after_reopen)

    with pytest.raises(RuntimeError, match="发布前.*输入.*变化"):
        project.write_all()

    assert marker.read_text(encoding="utf-8") == "old output"


def test_public_text_scan_invalidates_mixed_locator_manifest_until_scan_all(
        tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    _add_write_ready_text(project)
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)
    assert project.scan_all().input_protected is True
    project.store.add_file(
        "binary", "Fixture Game_Data/Managed/Assembly-CSharp.dll",
        "v2_mono", "binary", "", {})

    project.scan()
    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("mixed locator public scan must invalidate baseline")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(RuntimeError, match="完整输入清单|统一扫描"):
        project.write_all()

    assert copy_started is False


def _fake_dll_extractor(monkeypatch):
    """scan_v2 的 DLL 提取用 fake 替换。

    fixture DLL 的极简 CLI metadata（NumberOfStreams=0）会触发
    dnfile 0.18.0 的 streams_list 未初始化 bug；且真实解析对绑定
    状态机测试无意义。注入 1 条 pending #US 条目，让 v2 文件记录
    与条目在 scan_all/scan_v2 中真实入库。
    """
    from hanhua.core.extractor import ParsedFile
    from hanhua.core.models import TextEntry

    def fake_extract(path, file_id=None, progress_cb=None, *,
                     cross_sinks=frozenset()):
        return ParsedFile(
            file_id=file_id, rel_path=str(path), format="v2_mono",
            entries=[TextEntry(
                file_id=file_id, key_path="us/1", original="Hello player",
                status="pending",
                meta={"kind": "us", "heap_offset": 0, "utf16_len": 12})],
            encoding="utf-8", eol="\n", meta={"kind": "mono"}, noise=False)

    monkeypatch.setattr(
        "hanhua.core.project.mono_extractor.extract_dll_user_strings",
        fake_extract)


def test_scan_v2_refuses_binding_when_text_entries_are_stale(
        tmp_path, monkeypatch):
    # review High：scan() 绑定 H0 后输入树被改到 H1，单独 scan_v2 不得
    # 用 H1 覆盖绑定——文本条目仍解析自 H0，会对新树写回错位
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    _add_write_ready_text(project)
    _fake_dll_extractor(monkeypatch)
    assert project.scan_all().input_protected is True
    project.scan()
    assert project._last_source_manifest is not None
    # 输入树变化（模拟 Steam 更新/用户编辑）
    text = game / "Localization" / "en.json"
    text.write_text('{"title":"Changed"}', encoding="utf-8")
    # scan_v2 不重扫文本——文本条目来源树与当前树不同源 → 拒绝绑定
    project.scan_v2()
    assert project._last_source_manifest is None
    with pytest.raises(RuntimeError, match="完整输入清单"):
        project.write_all()


def test_scan_after_scan_v2_keeps_binding_when_tree_unchanged(
        tmp_path, monkeypatch):
    # review Medium：scan_v2 后 scan 的顺序不得因先清空再条件重绑而
    # 假性拒绝——树未变且 v2 已绑定同树时应保持绑定
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    _add_write_ready_text(project)
    _fake_dll_extractor(monkeypatch)
    assert project.scan_all().input_protected is True
    project.scan_v2()
    assert project._last_source_manifest is not None
    project.scan()  # 树未变 → 保持绑定
    assert project._last_source_manifest is not None


def test_project_rejects_v2_candidate_when_writer_reports_zero_verified_patches(
        tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    monkeypatch.setattr(project, "scan_v2", lambda progress_cb=None, csv_overwrite_source=False: 0)
    assert project.scan_all().input_protected is True
    project.store.add_file(
        "binary", "Fixture Game_Data/Managed/Assembly-CSharp.dll",
        "v2_mono", "binary", "", {})
    project.store.upsert_entries([{
        "file_id": "binary", "key_path": "us/1", "original": "Hello player",
        "meta": {"kind": "us", "heap_offset": 0, "utf16_len": 12},
    }])
    project.store.set_manual("binary", "us/1", "你好")
    project.out_dir.mkdir(parents=True)
    marker = project.out_dir / "KEEP-ME.txt"
    marker.write_text("old output", encoding="utf-8")
    monkeypatch.setattr(
        "hanhua.core.project.write_back_v2",
        lambda *args, **kwargs: __import__(
            "hanhua.core.unity.writer", fromlist=["WriteResult"]
        ).WriteResult(),
    )

    with pytest.raises(RuntimeError, match="实际译文补丁"):
        project.write_all()

    assert marker.read_text(encoding="utf-8") == "old output"


def test_project_unicode_json_fallback_reports_one_reopened_actual_patch(tmp_path):
    import json

    game = _make_mono_unity_game(tmp_path)
    source = game / "Localization" / "en.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        '{"title":"\\u0048\\u0065\\u006c\\u006c\\u006f"}\n',
        encoding="utf-8",
    )
    project = Project.open_game_dir(game, tmp_path / "app")
    project.scan()
    project.store.set_manual("Localization/en.json", "title", "你好")

    result = project.write_all()

    output = project.out_dir / "Localization" / "en.json"
    assert json.loads(output.read_text(encoding="utf-8"))["title"] == "你好"
    assert result["verification"]["reopen_verified"] is True
    assert result["verification"]["written_translations"] == 1


def test_project_preflight_rejects_junction_parent_before_copy(tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "file.txt"
    external.write_text("DO NOT TOUCH", encoding="utf-8")
    _make_junction(game / "linked", outside)
    project = Project.open_game_dir(game, tmp_path / "app")
    project.store.init_schema()
    project.store.add_file("safe", "linked/file.txt", "txt", "utf-8", "\n", {})
    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("Project preflight must reject junction before copy")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(ValueError, match="reparse|重解析"):
        project.write_all()

    assert copy_started is False
    assert external.read_text(encoding="utf-8") == "DO NOT TOUCH"


def test_project_rejects_private_staging_root_junction_before_copy(
        tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    project.store.init_schema()
    _add_write_ready_text(project)
    outside = tmp_path / "outside-staging"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("DO NOT TOUCH", encoding="utf-8")
    junction = tmp_path / "staging-junction"

    def junction_mkdtemp(*args, **kwargs):
        _make_junction(junction, outside)
        return str(junction)

    copy_started = False

    def reject_copy(*args, **kwargs):
        nonlocal copy_started
        copy_started = True
        raise AssertionError("staging reparse root must be rejected before copy")

    monkeypatch.setattr("hanhua.core.project.tempfile.mkdtemp", junction_mkdtemp)
    monkeypatch.setattr("hanhua.core.project.copy_game_dir", reject_copy)

    with pytest.raises(ValueError, match="reparse|重解析"):
        project.write_all()

    assert copy_started is False
    assert marker.read_text(encoding="utf-8") == "DO NOT TOUCH"


def test_project_rechecks_staging_root_after_copy_before_any_writer(
        tmp_path, monkeypatch):
    game = _make_mono_unity_game(tmp_path)
    project = Project.open_game_dir(game, tmp_path / "app")
    project.store.init_schema()
    _add_write_ready_text(project)
    outside = tmp_path / "outside-after-copy"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("DO NOT TOUCH", encoding="utf-8")
    writer_started = False

    def replace_staging_root(source, staging, progress_cb=None):
        staging.rmdir()
        _make_junction(staging, outside)
        return 0

    def reject_writer(*args, **kwargs):
        nonlocal writer_started
        writer_started = True
        raise AssertionError("writer must not run under replaced staging root")

    monkeypatch.setattr("hanhua.core.project.copy_game_dir", replace_staging_root)
    monkeypatch.setattr("hanhua.core.project.write_back_text", reject_writer)

    with pytest.raises(ValueError, match="reparse|重解析"):
        project.write_all()

    assert writer_started is False
    assert marker.read_text(encoding="utf-8") == "DO NOT TOUCH"


def test_write_all_rejects_store_inside_out_dir(tmp_path):
    """发布时 out_dir 整体改名；store 位于其内会被 SQLite 句柄阻止（S196）。"""
    from hanhua.core.project import _reject_store_inside_out_dir

    # app_dir 在 out_dir 内 → 明确报错
    app_dir = tmp_path / "out" / "app"
    out_dir = tmp_path / "out"
    with pytest.raises(RuntimeError, match="输出目录"):
        _reject_store_inside_out_dir(app_dir, out_dir)

    # 独立目录 → 通过
    app_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _reject_store_inside_out_dir(app_dir, out_dir)
    assert True


def test_reopen_txt_tolerates_plain_translation_structure_shift(tmp_path):
    """txt 整行翻译改变行首结构（如去掉前导 tab）后，重开解析的 locator
    会从 plain/N 变成 kv/<key>/N。重开验证按行号+行内容检查，
    译文已写入对应行即通过（真实案例：paintgame 的 Wwise Init.txt）。"""
    game = _make_mono_unity_game(tmp_path)
    txt = game / "Localization" / "notes.txt"
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text("State Group\tID\tName\tNotes\n\t1001\tTest\t\n", encoding="utf-8")
    project = Project.open_game_dir(game, tmp_path / "app")
    project.scan()
    # 第 2 行（0-based 1）以 tab 开头 → plain/1 整行；给它一条去掉前导
    # tab 的整行译文（写回后重开解析会变成 kv/1001/1）
    project.store.set_manual("Localization/notes.txt", "plain/1", "测试1001\tTest\t")

    from hanhua.core.project import _reopen_written_outputs
    from hanhua.core.writer import write_back
    out = tmp_path / "out"
    write_back(project.store, game, out)
    assert _reopen_written_outputs(project.store, out) == 1


def test_reopen_txt_still_detects_missing_translation(tmp_path):
    """译文未写入对应行时重开验证仍失败（防护不因行内容检查而失效）。"""
    game = _make_mono_unity_game(tmp_path)
    txt = game / "Localization" / "notes.txt"
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text("first line\nsecond line\n", encoding="utf-8")
    project = Project.open_game_dir(game, tmp_path / "app")
    project.scan()
    project.store.set_manual("Localization/notes.txt", "plain/0", "译文未写入")

    from hanhua.core.project import _reopen_written_outputs
    from hanhua.core.writer import write_back
    out = tmp_path / "out"
    write_back(project.store, game, out)
    # 手工把写回后的第 0 行改回原文（模拟译文没落位）
    patched = out / "Localization" / "notes.txt"
    patched.write_text("first line\nsecond line\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="重开不一致"):
        _reopen_written_outputs(project.store, out)
