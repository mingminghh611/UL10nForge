from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import struct
import subprocess

import pytest

import hanhua.core.corpus.inventory as inventory_module
from hanhua.core.corpus.inventory import build_inventory


_PE_SIZE = 0x400


def _write_pe(path: Path, *, cli: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = bytearray(_PE_SIZE)
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


def _make_mono_game(game, *, version=b"2021.3.1f1"):
    data = game / f"{game.name}_Data"
    managed = data / "Managed"
    managed.mkdir(parents=True)
    _write_pe(game / f"{game.name}.exe")
    _write_pe(managed / "Assembly-CSharp.dll", cli=True)
    (data / "globalgamemanagers").write_bytes(b"prefix " + version + b" suffix")
    return game


def test_inventory_requires_an_explicit_existing_directory(tmp_path):
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match="语料根目录不存在"):
        build_inventory(missing)

    regular_file = tmp_path / "games.txt"
    regular_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="语料根目录不是目录"):
        build_inventory(regular_file)


def test_inventory_records_one_fingerprinted_game(tmp_path):
    game_dir = _make_mono_game(tmp_path / "Mono Game")

    inventory = build_inventory(tmp_path)

    assert inventory.schema_version == 1
    assert len(inventory.games) == 1
    game = inventory.games[0]
    assert game.game_id == "Mono Game"
    assert game.source_path == game_dir.resolve()
    assert game.executable_path == "Mono Game.exe"
    assert game.data_path == "Mono Game_Data"
    assert game.unity_version == "2021.3.1f1"
    assert game.runtime == "mono"
    assert game.metadata_version is None
    assert game.file_count == 3
    assert game.total_bytes == 2 * _PE_SIZE + len(
        b"prefix 2021.3.1f1 suffix")
    assert "managed_assembly" in game.evidence
    assert "native_mono_literal_extract" in game.capabilities


def test_inventory_keeps_nested_player_paths_relative_to_source_root(tmp_path):
    source = tmp_path / "Nested Game"
    player = source / "Build" / "Windows"
    data = player / "Chosen_Data"
    (data / "Managed").mkdir(parents=True)
    _write_pe(player / "Chosen.exe")
    _write_pe(data / "Managed" / "Assembly-CSharp.dll", cli=True)
    (data / "globalgamemanagers").write_bytes(b"2021.3.1f1")

    game = build_inventory(tmp_path).games[0]

    assert game.source_path == source.resolve()
    assert game.runtime == "mono"
    assert game.executable_path == "Build/Windows/Chosen.exe"
    assert game.data_path == "Build/Windows/Chosen_Data"


def test_inventory_discovers_only_directories_and_sorts_them_stably(
        tmp_path, monkeypatch):
    zulu = _make_mono_game(tmp_path / "Zulu", version=b"2021.3.1f1")
    alpha = _make_mono_game(tmp_path / "alpha", version=b"2019.4.40f1")
    state = tmp_path / "_state.json"
    state.write_text("{}", encoding="utf-8")
    nested = tmp_path / "alpha" / "Nested Game"
    nested.mkdir()
    original_iterdir = type(tmp_path).iterdir

    def reverse_root_order(path):
        if path == tmp_path.resolve():
            return iter((zulu, state, alpha))
        return original_iterdir(path)

    monkeypatch.setattr(type(tmp_path), "iterdir", reverse_root_order)

    inventory = build_inventory(tmp_path)

    assert [game.game_id for game in inventory.games] == ["alpha", "Zulu"]
    assert [game.unity_version for game in inventory.games] == [
        "2019.4.40f1", "2021.3.1f1",
    ]


def test_inventory_rejects_casefold_duplicate_game_ids(tmp_path, monkeypatch):
    upper = _make_mono_game(tmp_path / "one" / "Game")
    lower = _make_mono_game(tmp_path / "two" / "game")
    original_iterdir = type(tmp_path).iterdir

    def duplicate_ids(path):
        if path == tmp_path.resolve():
            return iter((upper, lower))
        return original_iterdir(path)

    monkeypatch.setattr(type(tmp_path), "iterdir", duplicate_ids)

    with pytest.raises(ValueError, match="游戏 ID 忽略大小写后重复"):
        build_inventory(tmp_path)


def test_inventory_serializes_deterministic_state_and_portable_records(tmp_path):
    game_dir = _make_mono_game(tmp_path / "Portable Game")
    inventory = build_inventory(tmp_path)

    state = inventory.to_state_dict()
    portable = inventory.to_portable_dict()

    assert state["schema_version"] == 1
    assert state["games"][0]["source_path"] == str(game_dir.resolve())
    assert portable["schema_version"] == 1
    assert "source_path" not in portable["games"][0]
    assert portable["games"][0]["executable_path"] == "Portable Game.exe"
    assert portable["games"][0]["data_path"] == "Portable Game_Data"
    assert json.dumps(portable, sort_keys=True) == json.dumps(
        inventory.to_portable_dict(), sort_keys=True,
    )


def test_inventory_models_are_immutable(tmp_path):
    inventory = build_inventory(_make_mono_game(tmp_path / "root" / "Game").parent)

    with pytest.raises(FrozenInstanceError):
        inventory.schema_version = 2
    with pytest.raises(FrozenInstanceError):
        inventory.games[0].runtime = "il2cpp"


def test_inventory_does_not_follow_directory_symlinks_for_totals(tmp_path):
    root = tmp_path / "corpus"
    game_dir = _make_mono_game(root / "Game")
    external = tmp_path / "external"
    external.mkdir()
    (external / "large.bin").write_bytes(b"outside" * 100)
    try:
        os.symlink(external, game_dir / "linked", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    inventory = build_inventory(root)

    assert inventory.games[0].file_count == 3
    assert inventory.games[0].total_bytes == 2 * _PE_SIZE + len(
        b"prefix 2021.3.1f1 suffix",
    )


def test_inventory_ignores_direct_directory_symlink_entries(tmp_path, monkeypatch):
    linked_game = _make_mono_game(tmp_path / "Linked Game")
    original_is_symlink = type(tmp_path).is_symlink

    def mark_game_as_symlink(path):
        if path == linked_game:
            return True
        return original_is_symlink(path)

    monkeypatch.setattr(type(tmp_path), "is_symlink", mark_game_as_symlink)

    assert build_inventory(tmp_path).games == ()


def test_inventory_file_totals_never_follow_symlinks(tmp_path, monkeypatch):
    game_dir = _make_mono_game(tmp_path / "Game")
    nested = game_dir / "nested"
    directory_link = game_dir / "directory-link"
    calls = []

    class FakeStat:
        def __init__(self, size):
            self.st_size = size

    class FakeEntry:
        def __init__(self, path, *, kind, size=0):
            self.path = str(path)
            self.name = path.name
            self.kind = kind
            self.size = size

        def is_dir(self, *, follow_symlinks):
            calls.append((self.name, "is_dir", follow_symlinks))
            return self.kind == "directory" or (
                follow_symlinks and self.kind == "directory_symlink")

        def is_file(self, *, follow_symlinks):
            calls.append((self.name, "is_file", follow_symlinks))
            return self.kind == "file" or (
                follow_symlinks and self.kind == "file_symlink")

        def stat(self, *, follow_symlinks):
            calls.append((self.name, "stat", follow_symlinks))
            return FakeStat(self.size)

    tree = {
        game_dir: [
            FakeEntry(game_dir / "plain.bin", kind="file", size=5),
            FakeEntry(nested, kind="directory"),
            FakeEntry(directory_link, kind="directory_symlink"),
            FakeEntry(game_dir / "file-link.bin", kind="file_symlink", size=99),
        ],
        nested: [FakeEntry(nested / "nested.bin", kind="file", size=7)],
        directory_link: [
            FakeEntry(directory_link / "outside.bin", kind="file", size=101),
        ],
    }

    class FakeScandir:
        def __init__(self, entries):
            self.entries = entries

        def __enter__(self):
            return iter(self.entries)

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(
        inventory_module.os,
        "scandir",
        lambda path: FakeScandir(tree[Path(path)]),
    )

    game = build_inventory(tmp_path).games[0]

    assert (game.file_count, game.total_bytes) == (2, 12)
    assert calls
    assert all(follow_symlinks is False for _, _, follow_symlinks in calls)
    assert not any(name == "outside.bin" for name, _, _ in calls)
    assert not any(
        name in {"directory-link", "file-link.bin"} and operation == "stat"
        for name, operation, _ in calls
    )


def test_inventory_ignores_direct_game_junction(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction test")
    root = tmp_path / "corpus"
    root.mkdir()
    target = _make_mono_game(tmp_path / "outside" / "Junction Game")
    junction = root / "Junction Game"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,  # 中文 GBK 输出勿按 UTF-8 解码（_readerthread 崩溃）
        check=False,
    )
    if created.returncode != 0:
        pytest.skip(f"junction unavailable: {created.stderr.strip()}")
    try:
        assert build_inventory(root).games == ()
    finally:
        os.rmdir(junction)


def test_inventory_does_not_cross_internal_game_junction(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction test")
    root = tmp_path / "corpus"
    game_dir = _make_mono_game(root / "Game")
    external = tmp_path / "outside-assets"
    external.mkdir()
    (external / "NGUI.dll").write_bytes(b"outside-junction")
    junction = game_dir / "Game_Data" / "linked-assets"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
        capture_output=True,  # 中文 GBK 输出勿按 UTF-8 解码（_readerthread 崩溃）
        check=False,
    )
    if created.returncode != 0:
        pytest.skip(f"junction unavailable: {created.stderr.strip()}")
    try:
        game = build_inventory(root).games[0]
        assert "ngui" not in game.evidence
        assert (game.file_count, game.total_bytes) == (
            3, 2 * _PE_SIZE + len(b"prefix 2021.3.1f1 suffix"),
        )
    finally:
        os.rmdir(junction)


def test_inventory_file_totals_reject_fake_directory_reparse_points(
        tmp_path, monkeypatch):
    game_dir = _make_mono_game(tmp_path / "Game")
    fingerprint = inventory_module.fingerprint_game(game_dir)
    junction = game_dir / "fake-junction"
    scanned = []
    original_iterdir = type(tmp_path).iterdir

    class FakeStat:
        st_size = 3

    class FakeEntry:
        def __init__(self, path, kind):
            self.path = str(path)
            self.kind = kind

        def is_dir(self, *, follow_symlinks):
            assert follow_symlinks is False
            return self.kind in {"directory", "junction"}

        def is_file(self, *, follow_symlinks):
            assert follow_symlinks is False
            return self.kind == "file"

        def stat(self, *, follow_symlinks):
            assert follow_symlinks is False
            return FakeStat()

    tree = {
        game_dir: [
            FakeEntry(game_dir / "plain.bin", "file"),
            FakeEntry(junction, "junction"),
        ],
        junction: [FakeEntry(junction / "outside.bin", "file")],
    }

    class FakeScandir:
        def __init__(self, path):
            self.path = path
            scanned.append(path)

        def __enter__(self):
            return iter(tree[self.path])

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(
        inventory_module.os, "scandir", lambda path: FakeScandir(Path(path)))
    monkeypatch.setattr(
        inventory_module, "fingerprint_game", lambda path: fingerprint)
    monkeypatch.setattr(
        inventory_module, "_is_reparse_point",
        lambda path: path.name == junction.name,
    )

    def direct_game_only(path):
        if path == tmp_path.resolve():
            return iter((game_dir,))
        return original_iterdir(path)

    monkeypatch.setattr(type(tmp_path), "iterdir", direct_game_only)

    game = build_inventory(tmp_path).games[0]

    assert (game.file_count, game.total_bytes) == (1, 3)
    assert scanned == [game_dir]
