"""Tests for pure-Python Unity game detection and real Hook deployment / uninstallation."""
import struct
from pathlib import Path

from ul10n.core.game_detector import detect_game, select_tmp_bundle_name
from ul10n.core.hook_deployer import deploy_hook, get_xuat_paths, uninstall_hook

APP_ROOT = Path(__file__).resolve().parents[1]


def _make_fake_pe(path: Path, machine: int) -> None:
    buf = bytearray(0x100)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x80)
    buf[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", buf, 0x84, machine)
    path.write_bytes(bytes(buf))


def test_detect_and_deploy_mono_x64(tmp_path: Path):
    game_dir = tmp_path / "DemoMono"
    data_dir = game_dir / "DemoMono_Data"
    (data_dir / "Managed").mkdir(parents=True)
    _make_fake_pe(game_dir / "DemoMono.exe", 0x8664)
    (data_dir / "globalgamemanagers").write_bytes(b"\x00\x002022.3.15f1\x00")

    info = detect_game(game_dir)
    assert info.is_unity is True
    assert info.runtime_type == "mono"
    assert info.arch == "x64"
    assert info.unity_version == "2022.3.15f1"
    # 2022.3.15 < 2022.3.61 (u2022 build version), so must pick forward-compatible u2021
    assert select_tmp_bundle_name(info.unity_version) == "notoserif_sdf_u2021"
    assert select_tmp_bundle_name("2021.1.23f1") == "notoserif_sdf_u2019"
    assert select_tmp_bundle_name("6000.4.3f1") == "notoserif_sdf_u6000"

    res = deploy_hook(
        info,
        app_root=APP_ROOT,
        project_id="abc123",
        server_port=18765,
    )
    assert res.loader == "bepinex"
    assert res.files_written > 10
    assert (game_dir / "winhttp.dll").is_file()
    paths = get_xuat_paths(game_dir, "bepinex")
    assert paths["config"].is_file()
    assert paths["auto_file"].is_file()  # Must exist to prevent XUAT ReloadTranslations FileNotFoundException
    cfg = paths["config"].read_text(encoding="utf-8")
    assert "http://127.0.0.1:18765/xuat/translate/abc123" in cfg
    assert "EnableShortDelay=True" in cfg
    assert "DisableSpamChecks=True" in cfg
    assert "EnableUIElements=True" in cfg
    assert "notoserif_sdf_u2021" in cfg

    removed = uninstall_hook(game_dir, res.manifest)
    assert removed > 0
    assert not (game_dir / "BepInEx").exists()
    assert not (game_dir / "winhttp.dll").exists()
    # Original game files untouched
    assert (game_dir / "DemoMono.exe").is_file()
    assert (data_dir / "globalgamemanagers").is_file()


def test_detect_and_deploy_il2cpp_x64_bepinex_and_melon(tmp_path: Path):
    game_dir = tmp_path / "DemoIl2cpp"
    data_dir = game_dir / "DemoIl2cpp_Data"
    (data_dir / "il2cpp_data").mkdir(parents=True)
    _make_fake_pe(game_dir / "DemoIl2cpp.exe", 0x8664)
    (game_dir / "GameAssembly.dll").write_bytes(b"MZ")
    (data_dir / "globalgamemanagers").write_bytes(b"\x006000.0.23f1\x00")

    info = detect_game(game_dir)
    assert info.runtime_type == "il2cpp"
    # 6000.0.23 < 6000.3.0 (u6000 build version), so must pick u2022 for forward compatibility
    assert select_tmp_bundle_name(info.unity_version) == "notoserif_sdf_u2022"

    res_bep = deploy_hook(info, app_root=APP_ROOT, project_id="il1", server_port=18765, loader="bepinex")
    assert (game_dir / "dotnet").is_dir()
    uninstall_hook(game_dir, res_bep.manifest)
    assert not (game_dir / "dotnet").exists()

    res_ml = deploy_hook(info, app_root=APP_ROOT, project_id="il1", server_port=18765, loader="melonloader")
    assert (game_dir / "MelonLoader").is_dir()
    assert (game_dir / "Mods").is_dir()
    uninstall_hook(game_dir, res_ml.manifest)
    assert not (game_dir / "MelonLoader").exists()


def test_detect_nested_wrapper_folder(tmp_path: Path):
    outer = tmp_path / "Flabby Pizza"
    inner = outer / "Flabby Pizza"
    data_dir = inner / "Flabby Pizza_Data"
    (data_dir / "Managed").mkdir(parents=True)
    _make_fake_pe(inner / "Flabby Pizza.exe", 0x8664)
    (data_dir / "globalgamemanagers").write_bytes(b"\x006000.4.3f1\x00")

    info = detect_game(outer)
    assert info.is_unity is True
    assert Path(info.game_dir) == inner.resolve()
    assert info.runtime_type == "mono"
    assert info.unity_version == "6000.4.3f1"
