from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

import scripts.audit_game_corpus as cli_module
from hanhua.core.corpus.models import CorpusAudit, CorpusAuditGame
from scripts.audit_game_corpus import main
from tests.test_scanner import _write_pe


def _make_mono_game(game: Path) -> Path:
    data = game / f"{game.name}_Data"
    managed = data / "Managed"
    managed.mkdir(parents=True)
    _write_pe(game / f"{game.name}.exe")
    _write_pe(managed / "Assembly-CSharp.dll", cli=True)
    (data / "globalgamemanagers").write_bytes(b"prefix 2021.3.1f1 suffix")
    return game


def test_audit_lock_is_nonblocking_and_reusable_after_release(tmp_path):
    games_root = tmp_path / "games"
    _make_mono_game(games_root / "Game")
    state = tmp_path / "run" / "state.json"
    lock_path = Path(f"{state}.lock")

    with cli_module._audit_lock(state, games_root):
        assert lock_path.is_file()
        with pytest.raises(ValueError, match="already running|lock"):
            with cli_module._audit_lock(state, games_root):
                raise AssertionError("second acquisition unexpectedly succeeded")

    with cli_module._audit_lock(state, games_root):
        assert lock_path.is_file()
    assert lock_path.is_file()


def test_full_cli_lock_contention_preserves_state_and_report_then_reenters(
        tmp_path, monkeypatch, capsys):
    games_root = tmp_path / "games"
    source = _make_mono_game(games_root / "Game").resolve()
    state = tmp_path / "run" / "state.json"
    report = tmp_path / "run" / "report.json"
    state.parent.mkdir(parents=True)
    original_state = b"existing audit state"
    original_report = b"existing portable report"
    state.write_bytes(original_state)
    report.write_bytes(original_report)
    calls = []

    def fake_audit(inventory, app_dir, state_path, force):
        calls.append(Path(state_path))
        Path(state_path).write_bytes(b"new audit state")
        return CorpusAudit(games_root.resolve(), (
            CorpusAuditGame(
                "Game", source, status="passed", input_fingerprint="a" * 64,
                source_manifest={}, status_counts={}, role_counts={},
                confidence_counts={}, reason_counts={}, disposition_counts={}),
        ))

    monkeypatch.setattr(cli_module, "audit_inventory", fake_audit)
    arguments = [
        "--games-root", str(games_root),
        "--app-dir", str(tmp_path / "app"),
        "--state", str(state),
        "--report", str(report),
    ]

    with cli_module._audit_lock(state, games_root):
        assert main(arguments) != 0
        assert state.read_bytes() == original_state
        assert report.read_bytes() == original_report
        assert calls == []
    assert "already running" in capsys.readouterr().err

    assert main(arguments) == 0
    assert calls == [state]
    assert state.read_bytes() == b"new audit state"
    assert json.loads(report.read_text(encoding="utf-8"))[
        "report_type"] == "audit"
    assert Path(f"{state}.lock").is_file()


def test_full_cli_cross_process_lock_releases_after_abnormal_exit(
        tmp_path, monkeypatch):
    games_root = tmp_path / "games"
    source = _make_mono_game(games_root / "Game").resolve()
    state = tmp_path / "run" / "state.json"
    report = tmp_path / "run" / "report.json"
    state.parent.mkdir(parents=True)
    original_state = b"cross-process audit state"
    original_report = b"cross-process portable report"
    state.write_bytes(original_state)
    report.write_bytes(original_report)
    child_script = "\n".join((
        "from pathlib import Path",
        "import sys",
        "from scripts.audit_game_corpus import _audit_lock",
        "with _audit_lock(Path(sys.argv[1]), Path(sys.argv[2])):",
        "    print('READY', flush=True)",
        "    sys.stdin.buffer.read(1)",
    ))
    process = subprocess.Popen(
        [sys.executable, "-c", child_script, str(state), str(games_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready: queue.Queue[str] = queue.Queue()
    assert process.stdout is not None
    reader = threading.Thread(
        target=lambda: ready.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        assert ready.get(timeout=10).strip() == "READY"
        arguments = [
            "--games-root", str(games_root),
            "--app-dir", str(tmp_path / "app"),
            "--state", str(state),
            "--report", str(report),
        ]
        started = time.monotonic()
        assert main(arguments) != 0
        assert time.monotonic() - started < 5
        assert state.read_bytes() == original_state
        assert report.read_bytes() == original_report
    finally:
        process.kill()
        process.communicate(timeout=10)

    def fake_audit(inventory, app_dir, state_path, force):
        Path(state_path).write_bytes(b"reentered audit state")
        return CorpusAudit(games_root.resolve(), (
            CorpusAuditGame(
                "Game", source, status="passed", input_fingerprint="a" * 64,
                source_manifest={}, status_counts={}, role_counts={},
                confidence_counts={}, reason_counts={}, disposition_counts={}),
        ))

    monkeypatch.setattr(cli_module, "audit_inventory", fake_audit)

    assert main(arguments) == 0
    assert state.read_bytes() == b"reentered audit state"
    assert json.loads(report.read_text(encoding="utf-8"))[
        "report_type"] == "audit"


def test_audit_lock_rejects_predictable_symlink_target(tmp_path):
    games_root = tmp_path / "games"
    _make_mono_game(games_root / "Game")
    state = tmp_path / "run" / "state.json"
    state.parent.mkdir(parents=True)
    external = tmp_path / "external-lock-target"
    original = b"do not touch"
    external.write_bytes(original)
    lock_path = Path(f"{state}.lock")
    try:
        lock_path.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlink unavailable: {exc}")

    with pytest.raises(ValueError, match="reparse|lock"):
        with cli_module._audit_lock(state, games_root):
            raise AssertionError("symlink lock unexpectedly acquired")

    assert external.read_bytes() == original


def test_audit_lock_rejects_reported_reparse_before_creation(
        tmp_path, monkeypatch):
    games_root = tmp_path / "games"
    _make_mono_game(games_root / "Game")
    state = tmp_path / "run" / "state.json"
    lock_path = Path(f"{state}.lock").absolute()
    original_check = cli_module._is_reparse_point
    monkeypatch.setattr(
        cli_module, "_is_reparse_point",
        lambda path: Path(path).absolute() == lock_path
        or original_check(Path(path)),
    )

    with pytest.raises(ValueError, match="reparse"):
        with cli_module._audit_lock(state, games_root):
            raise AssertionError("reparse lock unexpectedly acquired")

    assert not lock_path.exists()


def test_inventory_only_reserves_state_and_writes_portable_report(
        tmp_path, capsys):
    games_root = tmp_path / "games"
    _make_mono_game(games_root / "Beta")
    _make_mono_game(games_root / "Alpha")
    state = tmp_path / "run" / "state.json"
    report = tmp_path / "run" / "report.json"

    exit_code = main([
        "--games-root", str(games_root),
        "--app-dir", str(tmp_path / "app"),
        "--state", str(state),
        "--report", str(report),
        "--inventory-only",
    ])

    assert exit_code == 0
    assert not state.exists()
    assert report.is_file()
    report_payload = json.loads(report.read_text(encoding="utf-8"))
    assert report_payload["schema_version"] == 1
    assert report_payload["report_type"] == "inventory"
    assert "source_path" not in report.read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert "Alpha: inventoried runtime=mono" in output
    assert "Beta: inventoried runtime=mono" in output
    assert "inventoried=2" in output


def test_inventory_only_then_full_audit_reuses_paths_without_state_conflict(
        tmp_path, monkeypatch):
    games_root = tmp_path / "games"
    source = _make_mono_game(games_root / "Game").resolve()
    state = tmp_path / "run" / "state.json"
    report = tmp_path / "run" / "report.json"
    common = [
        "--games-root", str(games_root),
        "--app-dir", str(tmp_path / "app"),
        "--state", str(state),
        "--report", str(report),
    ]

    assert main([*common, "--inventory-only"]) == 0
    assert not state.exists()
    assert json.loads(report.read_text(encoding="utf-8"))[
        "report_type"] == "inventory"

    def fake_audit(inventory, app_dir, state_path, force):
        assert not Path(state_path).exists()
        Path(state_path).write_bytes(b"audit-state")
        return CorpusAudit(games_root.resolve(), (
            CorpusAuditGame(
                "Game", source, status="passed", input_fingerprint="a" * 64,
                source_manifest={}, status_counts={}, role_counts={},
                confidence_counts={}, reason_counts={}, disposition_counts={}),
        ))

    monkeypatch.setattr(cli_module, "audit_inventory", fake_audit)

    assert main(common) == 0
    assert state.read_bytes() == b"audit-state"
    assert json.loads(report.read_text(encoding="utf-8"))[
        "report_type"] == "audit"


def test_inventory_only_preserves_existing_audit_state_bytes(tmp_path):
    games_root = tmp_path / "games"
    _make_mono_game(games_root / "Game")
    state = tmp_path / "run" / "state.json"
    state.parent.mkdir(parents=True)
    original = b'{"schema_version":1,"audit":"resume"}\n'
    state.write_bytes(original)

    assert main([
        "--games-root", str(games_root),
        "--app-dir", str(tmp_path / "app"),
        "--state", str(state),
        "--report", str(tmp_path / "run" / "report.json"),
        "--inventory-only",
    ]) == 0

    assert state.read_bytes() == original


def test_full_audit_writes_portable_report_and_returns_nonzero_for_failures(
        tmp_path, monkeypatch, capsys):
    games_root = tmp_path / "games"
    alpha = _make_mono_game(games_root / "Alpha").resolve()
    beta = _make_mono_game(games_root / "Beta").resolve()
    state = tmp_path / "run" / "state.json"
    report = tmp_path / "run" / "report.json"

    def fake_audit(inventory, app_dir, state_path, force):
        assert Path(app_dir) == tmp_path / "app"
        assert force is True
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(state_path).write_text("{}", encoding="utf-8")
        return CorpusAudit(games_root.resolve(), (
            CorpusAuditGame(
                "Alpha", alpha, status="passed", input_fingerprint="a" * 64,
                source_manifest={}, status_counts={}, role_counts={},
                confidence_counts={}, reason_counts={}, disposition_counts={}),
            CorpusAuditGame(
                "Beta", beta, status="failed", failure_category="scan_exception",
                diagnostic={
                    "type": "RuntimeError", "status": None,
                    "message": f"could not open {tmp_path / 'app' / 'project.db'}",
                }),
        ))

    monkeypatch.setattr(cli_module, "audit_inventory", fake_audit)

    exit_code = main([
        "--games-root", str(games_root),
        "--app-dir", str(tmp_path / "app"),
        "--state", str(state),
        "--report", str(report),
        "--force",
    ])

    assert exit_code == 1
    assert state.is_file() and report.is_file()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["report_type"] == "audit"
    assert [game["status"] for game in payload["games"]] == [
        "passed", "failed"]
    serialized = report.read_text(encoding="utf-8")
    assert "source_path" not in serialized
    assert str(tmp_path) not in payload["games"][1]["diagnostic"]["message"]
    assert "Beta: failed" in capsys.readouterr().out


@pytest.mark.parametrize(("absolute_path", "forbidden", "at_start"), [
    (r"\\private-host\secret-share\users\alice\project.db",
     ("private-host", "secret-share", "alice", "project.db"), False),
    (r"\\?\C:\Users\Alice\secret-project.db",
     ("Users", "Alice", "secret-project.db"), False),
    (r"\\?\UNC\private-host\secret-share\audit\state.json",
     ("private-host", "secret-share", "audit", "state.json"), False),
    ("//private-host/secret-share/users/alice/project.db",
     ("private-host", "secret-share", "alice", "project.db"), False),
    ("/home/alice/private/project.db",
     ("home", "alice", "private", "project.db"), False),
    ("//start-host/start-share/private.db",
     ("start-host", "start-share", "private.db"), True),
    ("/home/start-user/private.db",
     ("home", "start-user", "private.db"), True),
])
def test_full_report_redacts_absolute_paths_at_supported_boundaries(
        tmp_path, monkeypatch, absolute_path, forbidden, at_start):
    games_root = tmp_path / "games"
    source = _make_mono_game(games_root / "Game").resolve()
    state = tmp_path / "run" / "state.json"
    report = tmp_path / "run" / "report.json"
    normal_backslashes = r"token=foo\bar"

    def fake_audit(inventory, app_dir, state_path, force):
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(state_path).write_text("{}", encoding="utf-8")
        return CorpusAudit(games_root.resolve(), (
            CorpusAuditGame(
                "Game", source, status="failed",
                failure_category="scan_exception",
                diagnostic={
                    "type": "RuntimeError", "status": None,
                    "message": (
                        absolute_path
                        if at_start else
                        f'failed path="{absolute_path}"; {normal_backslashes}'),
                }),
        ))

    monkeypatch.setattr(cli_module, "audit_inventory", fake_audit)

    assert main([
        "--games-root", str(games_root),
        "--app-dir", str(tmp_path / "app"),
        "--state", str(state),
        "--report", str(report),
    ]) == 1

    message = json.loads(report.read_text(encoding="utf-8"))[
        "games"][0]["diagnostic"]["message"]
    assert all(fragment not in message for fragment in forbidden)
    if not at_start:
        assert normal_backslashes in message


@pytest.mark.parametrize("output_name", ["state", "report"])
def test_cli_rejects_outputs_inside_game_trees(tmp_path, output_name, capsys):
    games_root = tmp_path / "games"
    game = _make_mono_game(games_root / "Game")
    paths = {
        "state": tmp_path / "state.json",
        "report": tmp_path / "report.json",
    }
    paths[output_name] = game / f"{output_name}.json"

    exit_code = main([
        "--games-root", str(games_root),
        "--app-dir", str(tmp_path / "app"),
        "--state", str(paths["state"]),
        "--report", str(paths["report"]),
        "--inventory-only",
    ])

    assert exit_code != 0
    assert not paths[output_name].exists()
    assert "inside the corpus root" in capsys.readouterr().err


def test_cli_rejects_report_reparse_path(tmp_path, monkeypatch, capsys):
    games_root = tmp_path / "games"
    _make_mono_game(games_root / "Game")
    report_parent = tmp_path / "report-junction"
    report = report_parent / "report.json"
    monkeypatch.setattr(
        cli_module, "_is_reparse_point",
        lambda path: Path(path) == report_parent.absolute(),
    )

    exit_code = main([
        "--games-root", str(games_root),
        "--app-dir", str(tmp_path / "app"),
        "--state", str(tmp_path / "state.json"),
        "--report", str(report),
        "--inventory-only",
    ])

    assert exit_code != 0
    assert not report.exists()
    assert "reparse point" in capsys.readouterr().err


def test_cli_rejects_app_dir_inside_game_before_writing(tmp_path, capsys):
    games_root = tmp_path / "games"
    game = _make_mono_game(games_root / "Game")
    app_dir = game / "audit-app"
    state = tmp_path / "state.json"
    report = tmp_path / "report.json"

    exit_code = main([
        "--games-root", str(games_root),
        "--app-dir", str(app_dir),
        "--state", str(state),
        "--report", str(report),
        "--inventory-only",
    ])

    assert exit_code != 0
    assert not app_dir.exists()
    assert not state.exists()
    assert not report.exists()
    assert "inside the corpus root" in capsys.readouterr().err


def test_cli_rejects_app_dir_junction_before_writing(tmp_path, capsys):
    games_root = tmp_path / "games"
    _make_mono_game(games_root / "Game")
    before = cli_module.build_inventory(games_root).to_state_dict()
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "marker.txt"
    marker.write_text("unchanged", encoding="utf-8")
    junction = tmp_path / "app-junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
        capture_output=True, check=False)  # 中文 GBK 输出勿按 UTF-8 解码
    if created.returncode != 0:
        pytest.skip("junction unavailable")
    state = tmp_path / "state.json"
    report = tmp_path / "report.json"

    exit_code = main([
        "--games-root", str(games_root),
        "--app-dir", str(junction / "app"),
        "--state", str(state),
        "--report", str(report),
        "--inventory-only",
    ])

    assert exit_code != 0
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert list(external.iterdir()) == [marker]
    assert not state.exists()
    assert not report.exists()
    assert cli_module.build_inventory(games_root).to_state_dict() == before
    assert "reparse point" in capsys.readouterr().err


@pytest.mark.parametrize("unsafe_argument", ["app-dir", "state", "report"])
def test_cli_rejects_direct_corpus_children_without_changing_inventory(
        tmp_path, unsafe_argument, capsys):
    games_root = tmp_path / "games"
    _make_mono_game(games_root / "Game")
    before = cli_module.build_inventory(games_root).to_state_dict()
    unsafe = games_root / (
        "audit-app" if unsafe_argument == "app-dir" else f"{unsafe_argument}.json")
    arguments = {
        "app-dir": tmp_path / "app",
        "state": tmp_path / "state.json",
        "report": tmp_path / "report.json",
    }
    arguments[unsafe_argument] = unsafe

    exit_code = main([
        "--games-root", str(games_root),
        "--app-dir", str(arguments["app-dir"]),
        "--state", str(arguments["state"]),
        "--report", str(arguments["report"]),
        "--inventory-only",
    ])

    assert exit_code != 0
    assert not unsafe.exists()
    assert not (tmp_path / "app").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "report.json").exists()
    assert cli_module.build_inventory(games_root).to_state_dict() == before
    assert "corpus root" in capsys.readouterr().err


@pytest.mark.parametrize("unsafe_argument", ["app-dir", "state", "report"])
def test_cli_rejects_corpus_root_itself_without_changing_inventory(
        tmp_path, unsafe_argument, capsys):
    games_root = tmp_path / "games"
    _make_mono_game(games_root / "Game")
    before = cli_module.build_inventory(games_root).to_state_dict()
    arguments = {
        "app-dir": tmp_path / "app",
        "state": tmp_path / "state.json",
        "report": tmp_path / "report.json",
    }
    arguments[unsafe_argument] = games_root

    exit_code = main([
        "--games-root", str(games_root),
        "--app-dir", str(arguments["app-dir"]),
        "--state", str(arguments["state"]),
        "--report", str(arguments["report"]),
        "--inventory-only",
    ])

    assert exit_code != 0
    assert not (tmp_path / "app").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "report.json").exists()
    assert cli_module.build_inventory(games_root).to_state_dict() == before
    assert "corpus root" in capsys.readouterr().err
