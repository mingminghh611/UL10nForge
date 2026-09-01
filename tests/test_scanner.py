import os
import struct
import tempfile
from pathlib import Path

from hanhua.core.scanner import discover
from hanhua.core.extractor import parse_file
from hanhua.core.unity.extractor import find_asset_files
from hanhua.core.unity.mono_dll import find_dll_files
from hanhua.core.tooling.fingerprint import fingerprint_game


def _write_pe(path: Path, *, cli: bool = False) -> None:
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


def _make_tree():
    d = Path(tempfile.mkdtemp())
    data_dir = d / f"{d.name}_Data"
    managed = data_dir / "Managed"
    managed.mkdir(parents=True)
    _write_pe(d / f"{d.name}.exe")
    _write_pe(managed / "Assembly-CSharp.dll", cli=True)
    (data_dir / "globalgamemanagers").write_bytes(b"2022.3.34f1")
    (d / "Localization").mkdir()
    (d / "Localization" / "en.json").write_text('{"a": "Hi"}', encoding="utf-8")
    (d / "Localization" / "text.csv").write_text("Key,Type,English\nk1,Text,Hello\n", encoding="utf-8")
    (d / "strings.txt").write_text("a=hello\n", encoding="utf-8")
    (d / "logo.png").write_bytes(b"\x89PNG")
    (d / "data.bin").write_bytes(b"\x00\x01")
    return d


def test_discover():
    d = _make_tree()
    files = discover(d)
    names = sorted(p.name for p in files)
    assert names == ["en.json", "strings.txt", "text.csv"]


def test_discover_skips_build_dir():
    d = _make_tree()
    (d / "Build").mkdir()
    (d / "Build" / "a.txt").write_text("x=1\n", encoding="utf-8")
    files = discover(d)
    assert all("Build" not in p.parts for p in files)


def test_discover_skips_il2cpp_conversion_output():
    # backrooms 实证：<Game>_BackUpThisFolder_ButDontShipItWithYourGame/
    # il2cppOutput/*.c 被文本启发式误收，单游戏 639 万条目（开发残留目录）。
    d = _make_tree()
    backup = d / f"{d.name}_BackUpThisFolder_ButDontShipItWithYourGame"
    output = backup / "il2cppOutput"
    output.mkdir(parents=True)
    (output / "Il2CppTypeDefinitions.c").write_text(
        '#include "pch-c.h"\n', encoding="utf-8")
    (output / "Il2CppGenericMethodTable.cpp").write_text(
        "#ifndef _MSC_VER\n", encoding="utf-8")
    (backup / "symbols.zip").write_bytes(b"PK\x03\x04fake")
    files = discover(d)
    assert not any("BackUpThisFolder" in p.parts for p in files)
    assert not any("il2cppOutput" in p.parts for p in files)
    assert "en.json" in {p.name for p in files}


def test_discover_skips_source_code_files_everywhere():
    # 编译产物源码（含工程残留）：可打印率高会被文本启发式误收；
    # .json 等本地化格式绝不能进黑名单。
    d = _make_tree()
    for name, content in (
            ("main.c", "int main() {}\n"),
            ("util.cpp", "#include <vector>\n"),
            ("lib.h", "#pragma once\n"),
            ("logic.cs", "public class Logic {}\n"),
            ("localization.json", '{"title": "Play"}\n'),
    ):
        (d / name).write_text(content, encoding="utf-8")
    files = discover(d)
    names = sorted(p.name for p in files)
    assert "localization.json" in names
    assert all(name not in names for name in ("main.c", "util.cpp", "lib.h", "logic.cs"))


def test_discover_skips_unity_runtime_files():
    d = _make_tree()
    mono = d / "MonoBleedingEdge" / "etc" / "mono"
    mono.mkdir(parents=True)
    (mono / "browscap.ini").write_text("Ask=true\n", encoding="utf-8")
    (mono / "mconfig" ).mkdir()
    (mono / "mconfig" / "config.xml").write_text("<config/>", encoding="utf-8")
    files = discover(d)
    names = sorted(p.name for p in files)
    assert names == ["en.json", "strings.txt", "text.csv"]
    assert not any("MonoBleedingEdge" in p.parts for p in files)
    assert not any(p.name == "browscap.ini" for p in files)


def test_discover_finds_unity5_data_unity3d_asset():
    # Unity 5.x 合并场景 data.unity3d（GameName_Data/ 下）是游戏内容（场景文本），
    # 不是运行时噪音（crash-back-in-time/hickory 真实识别不全的根因）
    d = _make_tree()
    data_dir = d / f"{d.name}_Data"
    (data_dir / "data.unity3d").write_bytes(b"\x00" * 64)
    files = discover(d, include_assets=True)
    assert any(p.name == "data.unity3d" for p in files)


def test_discover_runtime_exclusions_are_case_insensitive():
    d = _make_tree()
    build_dir = d / "BUILD"
    build_dir.mkdir()
    (build_dir / "build-log.txt").write_text("noise", encoding="utf-8")
    mono_dir = d / "monobleedingedge"
    mono_dir.mkdir()
    (mono_dir / "runtime.xml").write_text("<noise/>", encoding="utf-8")
    (d / "BROWSCAP.INI").write_text("Ask=true", encoding="utf-8")

    files = discover(d)

    assert sorted(path.name for path in files) == [
        "en.json", "strings.txt", "text.csv",
    ]


def test_discover_skips_burst_debug_information_directory():
    d = _make_tree()
    burst_dir = d / "Example_BurstDebugInformation_DoNotShip"
    burst_dir.mkdir()
    (burst_dir / "symbols.txt").write_text("debug symbols", encoding="utf-8")

    files = discover(d)

    assert all("Example_BurstDebugInformation_DoNotShip" not in p.parts for p in files)


def test_discover_skips_unity_logs_and_engine_api_docs():
    # output_log.txt（Unity 运行时日志）与 Managed/*.xml（引擎 API 文档注释）
    # 是打包必带的运行时噪音，绝非本地化文本（真实失败样本：14 + 42 条）
    d = _make_tree()
    (d / "output_log.txt").write_text("Direct3D:\nNVIDIA GeForce GTX 960\n", encoding="utf-8")
    (d / "Player.log").write_text("Loading scene 1\n", encoding="utf-8")
    managed = d / f"{d.name}_Data" / "Managed"
    (managed / "UnityEngine.AIModule.xml").write_text(
        "<member name=\"T:UnityEngine.NavMesh\"><summary>Navigation mesh.</summary></member>",
        encoding="utf-8")
    (managed / "UnityEngine.xml").write_text("<doc/>", encoding="utf-8")
    (managed / "Assembly-CSharp.xml").write_text(
        "<member name=\"T:Game.Logic\"><summary>Main logic.</summary></member>",
        encoding="utf-8")

    files = discover(d)

    names = sorted(p.name for p in files)
    assert "output_log.txt" not in names
    assert "Player.log" not in names
    assert "UnityEngine.AIModule.xml" not in names
    assert "UnityEngine.xml" not in names
    assert "Assembly-CSharp.xml" not in names
    assert names == ["en.json", "strings.txt", "text.csv"]


def test_discover_skips_credit_roster_and_license_files():
    # CREDITS.txt（credit 名单，如 "- from AudioBlocks.com"）与 LICENSE.txt 是
    # 打包元文件：credit 人名/品牌应保留原文、许可证文本翻译无意义
    d = _make_tree()
    (d / "CREDITS.txt").write_text("A* pathfind project by Aron Granberg\n", encoding="utf-8")
    (d / "CREDITS_en.txt").write_text("- from AudioBlocks.com\n", encoding="utf-8")
    (d / "LICENSE.txt").write_text("MIT License\nCopyright (c) 2020\n", encoding="utf-8")
    (d / "License_en.txt").write_text("GPL", encoding="utf-8")

    files = discover(d)

    names = sorted(p.name for p in files)
    assert "CREDITS.txt" not in names
    assert "CREDITS_en.txt" not in names
    assert "LICENSE.txt" not in names
    assert "License_en.txt" not in names
    assert names == ["en.json", "strings.txt", "text.csv"]


def test_discover_never_returns_companion_resource_files_as_assets():
    d = _make_tree()
    asset = d / "sharedassets0.assets"
    asset.write_bytes(b"asset")
    (d / "sharedassets0.assets.resS").write_bytes(b"resource")
    (d / "level0.ress").write_bytes(b"resource")

    files = discover(d, include_assets=True)

    assert asset in files
    assert all(path.suffix.lower() != ".ress" for path in files)


def test_extensionless_unityfs_magic_participates_in_asset_discovery(tmp_path):
    data_dir = tmp_path / "Example_Data" / "StreamingAssets"
    data_dir.mkdir(parents=True)
    bundle = data_dir / "0123456789abcdef"
    plain = data_dir / "ordinary"
    bundle.write_bytes(b"UnityFS\x00fixture")
    plain.write_bytes(b"\x00\x01ordinary data")

    assert discover(tmp_path, include_assets=True) == [bundle]
    assert find_asset_files(tmp_path) == [bundle]


def test_asset_discovery_explicit_data_root_keeps_direct_level_scene_isolated(
        tmp_path):
    selected_data = tmp_path / "A_Data"
    sibling_data = tmp_path / "B_Data"
    selected_data.mkdir()
    sibling_data.mkdir()
    selected_level = selected_data / "level0"
    sibling_level = sibling_data / "level0"
    selected_level.write_bytes(b"selected")
    sibling_level.write_bytes(b"sibling")

    assert find_asset_files(
        selected_data, data_dir=selected_data) == [selected_level]


def test_discovery_exclude_roots_prunes_nested_player_before_read(tmp_path):
    selected = tmp_path / "selected"
    sibling = tmp_path / "sibling"
    selected.mkdir()
    sibling.mkdir()
    selected_text = selected / "Localization" / "en.json"
    sibling_text = sibling / "Localization" / "en.json"
    selected_text.parent.mkdir()
    sibling_text.parent.mkdir()
    selected_text.write_text('{"title":"A"}', encoding="utf-8")
    sibling_text.write_text('{"title":"B"}', encoding="utf-8")

    assert discover(tmp_path, exclude_roots=(sibling,)) == [selected_text]


def test_unknown_il2cpp_metadata_keeps_static_and_managed_routes(tmp_path):
    game = tmp_path / "Hybrid Game"
    data = game / "Hybrid Game_Data"
    managed = data / "Managed"
    metadata = data / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    managed.mkdir(parents=True)
    metadata.parent.mkdir(parents=True)
    _write_pe(game / "Hybrid Game.exe")
    _write_pe(game / "GameAssembly.dll")
    _write_pe(managed / "Assembly-CSharp.dll", cli=True)
    metadata.write_bytes(struct.pack("<II", 0xFAB11BAF, 30) + b"\x00" * 64)
    bundle = data / "abcdef0123456789"
    bundle.write_bytes(b"UnityFS\x00fixture")

    fingerprint = fingerprint_game(game)

    assert "unsupported_il2cpp_metadata_version" in fingerprint.evidence
    assert "native_text_extract" in fingerprint.capabilities
    assert "native_asset_extract" in fingerprint.capabilities
    assert "native_il2cpp_literal_extract" not in fingerprint.capabilities
    assert find_dll_files(game) == [managed / "Assembly-CSharp.dll"]
    assert find_asset_files(game) == [bundle]


def test_discovery_prunes_runtime_directories_during_shared_walk(
        tmp_path, monkeypatch):
    localization = tmp_path / "Localization"
    localization.mkdir()
    text_file = localization / "en.json"
    text_file.write_text('{"title": "Hello"}', encoding="utf-8")
    data_dir = tmp_path / "Example_Data"
    data_dir.mkdir()
    scene = data_dir / "level0"
    scene.write_bytes(b"UnityFS")
    pruned_snapshots = []

    def guarded_walk(root, topdown=True):
        assert topdown is True
        dirs = [
            "Localization", "Example_Data", "build", "MonoBleedingEdge",
            "Example_BurstDebugInformation_DoNotShip",
        ]
        yield str(root), dirs, []
        pruned_snapshots.append(tuple(dirs))
        yield str(localization), [], [text_file.name]
        yield str(data_dir), [], [scene.name]

    monkeypatch.setattr(os, "walk", guarded_walk)

    assert discover(tmp_path) == [text_file]
    assert find_asset_files(tmp_path) == [scene]
    assert pruned_snapshots == [
        ("Example_Data", "Localization"),
        ("Example_Data", "Localization"),
    ]


def test_find_asset_files_finds_legacy_root_scenes(tmp_path):
    # 老式布局（Unity ≤4.x）：游戏根目录的 mainData 是无后缀序列化场景索引，
    # 含全部场景文本（hotel-paradise 真实识别不全的根因，ISSUES #192）。
    # 同目录的 levelN 因有 mainData 作为老式布局证据而一并收入。
    d = tmp_path
    (d / "mainData").write_bytes(b"\x00" * 64)
    (d / "level0").write_bytes(b"\x00" * 64)
    (d / "level1").write_bytes(b"\x00" * 64)
    (d / "random_no_suffix").write_bytes(b"\x00" * 64)
    found = find_asset_files(d)
    names = sorted(p.name for p in found)
    assert names == ["level0", "level1", "mainData"]


def test_find_asset_files_rejects_orphan_legacy_level(tmp_path):
    # 根目录裸 level1 无 mainData 老式布局证据：可能是游戏自有数据文件，
    # 必须拒绝（与 rejects_level_scene_outside_data_tree 同一设计意图）
    d = tmp_path
    (d / "level1").write_bytes(b"not a unity legacy scene")
    assert find_asset_files(d) == []


def test_public_discovery_results_are_globally_path_sorted(tmp_path):
    nested_dir = tmp_path / "a"
    nested_dir.mkdir()
    nested_text = nested_dir / "nested.txt"
    nested_text.write_text("nested", encoding="utf-8")
    root_text = tmp_path / "z.txt"
    root_text.write_text("root", encoding="utf-8")
    nested_asset = nested_dir / "nested.assets"
    nested_asset.write_bytes(b"\x00\x01nested")
    root_asset = tmp_path / "z.assets"
    root_asset.write_bytes(b"\x00\x01root")

    assert discover(tmp_path) == [nested_text, root_text]
    assert find_asset_files(tmp_path) == [nested_asset, root_asset]


def test_parse_file_dispatches():
    d = _make_tree()
    parsed = parse_file(d / "Localization" / "en.json", "f1")
    assert parsed.format == "json" and len(parsed.entries) == 1
    parsed_txt = parse_file(d / "strings.txt", "f2")
    assert parsed_txt.format == "txt"
    assert parsed_txt.entries[0].status == "pending"


def test_parse_file_unknown_extension_routes_by_content():
    # 未知扩展名文本文件按内容路由：JSON 内容 → json（不再 txt 行拆分，
    # 否则 JSON 行被拆成半行条目，写回破坏文件——containment-breach-hd
    # 的 Language/*.subs 实证）
    d = _make_tree()
    p = d / "Language" / "sceneStrings.subs"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{\n    "Cough0": {\n        "subtitle": "*COUGH*"\n    }\n}\n',
                 encoding="utf-8")
    parsed = parse_file(p, "subs1")
    assert parsed.format == "json"
    assert any(e.original == "*COUGH*" for e in parsed.entries)
    assert not any("subtitle" == e.original or '"Cough0"' == e.original
                   for e in parsed.entries)


def _make_loop_junction(tmp_path) -> bool:
    """在 game_dir 内创建指向 game_dir 自身的 junction 循环；返回是否成功。"""
    import subprocess
    game = tmp_path / "looped_game"
    game.mkdir()
    (game / "Data").mkdir()
    (game / "Data" / "text.txt").write_text("hello", encoding="utf-8")
    loop = game / "loop"
    # cmd mklink 输出本地化（中文 GBK）→ text=True 的 _readerthread 按
    # UTF-8 解码崩溃（Windows 全量回归 7 处 UnicodeDecodeError 根因）。
    # 不读文本：bytes 捕获 + 仅判 returncode。
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(loop), str(game)],
        capture_output=True)
    return result.returncode == 0 and loop.is_dir()


def test_tree_hashes_terminates_on_junction_loop(tmp_path):
    """游戏目录内 junction 指向祖先（OneDrive/发布残留）→ 全树哈希必须终止
    且不跟随循环（rglob 会无限递归卡死扫描，真实用户卡死样本）。"""
    from hanhua.core.project import _tree_hashes
    if not _make_loop_junction(tmp_path):
        pytest.skip("junction creation not permitted on this host")
    game = tmp_path / "looped_game"

    hashes = _tree_hashes(game)

    assert "Data/text.txt" in hashes
    assert not any("loop" in rel for rel in hashes)
    assert len(hashes) == 1


def test_scanner_walk_terminates_on_junction_loop(tmp_path):
    """文本发现遍历同样剪掉 junction → 不卡死且不误收循环文件。"""
    if not _make_loop_junction(tmp_path):
        pytest.skip("junction creation not permitted on this host")
    game = tmp_path / "looped_game"

    found = discover(game)

    assert [p.name for p in found] == ["text.txt"]


def _serialized_header_v22() -> bytes:
    """Unity SerializedFile v22+ 头：大端字段（metadata/file_size/version/
    data_offset + endian/reserved + 大端重读 v22 三字段），≥48 字节。"""
    return (struct.pack(">III I B 3x I Q Q 4x", 0, 4096, 22, 2048, 0,
                        1024, 4096, 2048) + b"\x00" * 64)


def test_probe_head_kind_accepts_big_endian_serialized_headers():
    """SerializedFile 无魔数：大端头自洽（v22 48 字节布局）即收。
    老版本头（<v22）只用前 16 字节字段。"""
    from hanhua.core.scanner import probe_head_kind

    assert probe_head_kind(_serialized_header_v22()) == "serialized"

    legacy = struct.pack(">IIII", 0, 1024, 17, 512) + b"\x00" * 44
    assert probe_head_kind(legacy) == "serialized"


def test_probe_head_kind_rejects_addressables_catalog_header():
    """Addressables catalog.bin 的 kMagic 0x0DE38942（BinaryStorageBuffer）
    在此大端读法下 version 巨大 → 拒绝，避免误判为 SerializedFile。"""
    from hanhua.core.scanner import probe_head_kind

    catalog = (b"\x42\x89\xe3\x0d" + b"\x02\x00\x00\x00" + b"\x24\x00\x00\x00"
               + b"\x88\x01\x00\x00" + b"\x00" * 32)
    assert probe_head_kind(catalog) != "serialized"

    # 短于 48 字节的任意二进制也不可能是 SerializedFile
    assert probe_head_kind(b"\x00\x01ordinary data") != "serialized"
    assert probe_head_kind(b"") == "unknown"


def test_discover_skips_iff_rgb_bitmap_files(tmp_path):  # noqa: F811
    """IFF/Reflexive .rgb 位图（FORM+RTEXVERS+BODY 容器）不得被内容探测
    误判为文本：灰度纹理像素字节集中在 0x20-0x7E，strict UTF-8 可过，
    旧启发式会提取成条目（honorplusplus/sonic-suggests/thirstiest 实测
    3 游戏被写回编码阻断）。.rgb 已进二进制黑名单，文件级直接跳过。"""
    from hanhua.core.scanner import discover

    game = tmp_path / "iff_game"
    game.mkdir()
    (game / "text.txt").write_text("Hello world", encoding="utf-8")
    body = bytes([0x40 + (i % 0x30) for i in range(2048)])  # 灰度像素 0x40-0x6F
    blob = b"FORM" + len(body).to_bytes(4, "big") + b"RTEXVERS" + b"\x01\x00\x00\x00" \
        + b"BODY" + len(body).to_bytes(4, "big") + body
    rgb = game / "level1" / "06" / "0686889441fd90cbbfee1b4f3c44b5bc.rgb"
    rgb.parent.mkdir(parents=True)
    rgb.write_bytes(blob)

    found = discover(game)

    assert [p.name for p in found] == ["text.txt"]


def test_probe_kind_iff_rgb_without_extension_still_binary(tmp_path):
    """.rgb 已在扩展名层拦截；无扩展名副本（改名场景）按 IFF 头判定 binary，
    不会因高可打印率被误判 text。"""
    from hanhua.core.scanner import probe_file_kind

    body = bytes([0x40 + (i % 0x30) for i in range(2048)])
    blob = b"FORM" + len(body).to_bytes(4, "big") + b"RTEXVERS" + b"\x01\x00\x00\x00" \
        + b"BODY" + len(body).to_bytes(4, "big") + body
    p = tmp_path / "no_ext"
    p.write_bytes(blob)

    assert probe_file_kind(p) == "binary"


def test_discover_skips_engine_builtin_resources(tmp_path):
    """Resources/ 下引擎内置资源（无后缀 SerializedFile 头自洽）是打包必带
    噪音，不参与文本发现与资产发现。"""
    data_dir = tmp_path / "Game_Data" / "Resources"
    data_dir.mkdir(parents=True)
    (data_dir / "unity default resources").write_bytes(_serialized_header_v22())
    (data_dir / "unity_builtin_extra").write_bytes(_serialized_header_v22())

    assert discover(tmp_path) == []
    assert find_asset_files(tmp_path) == []
