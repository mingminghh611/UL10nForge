"""v2 写回集成测试：write_all 无进度回调、v1/v2 文件分工、长度适配写回。"""
import json
import tempfile
import threading
from pathlib import Path

import pytest

import hanhua.core.writer as text_writer
from hanhua.core.font_support import FontInstallError, FontInstallResult
from hanhua.core.memory import ProjectStore
from hanhua.core.models import FontConfig
from hanhua.core.project import Project
from hanhua.core.unity.writer import (
    WriteResult, _entries_for_object_identity, _fit_bytes, _patch_asset, _patch_dll,
    _patch_metadata,
    _select_write_items,
    _should_write_entry,
    write_back_v2,
)
from tests.test_scanner import _make_tree
from tests.test_tooling_runner import _make_junction
from tests.test_v2_patch_pools import _build, _us_heap


def _translate_first_pending(project: Project) -> None:
    entry = next(row for row in project.store.get_entries()
                 if row["status"] == "pending")
    project.store.set_manual(entry["file_id"], entry["key_path"], "已翻译")


def test_write_all_keeps_latest_backup_and_cleans_older(
        tmp_path, monkeypatch):
    """发布成功后本次备份保留（可一键回滚），仅后台清理更早的备份。"""
    game_dir = _make_tree()
    project = Project.open_game_dir(game_dir, tmp_path / "app")
    project.scan()
    _translate_first_pending(project)
    project.out_dir.mkdir(parents=True)
    (project.out_dir / "old-output.txt").write_text("old", encoding="utf-8")
    # 预置更早一次发布遗留的备份（合法 uuid 格式；必须与 out_dir 同父
    # 目录——out_dir = game_dir.parent / f"{game_dir.name}_汉化"）
    older = project.out_dir.parent / f".{project.out_dir.name}.backup-{'a' * 32}"
    older.mkdir()
    (older / "even-older.txt").write_text("older", encoding="utf-8")

    import hanhua.core.project as project_module
    original_rmtree = project_module.shutil.rmtree
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()
    cleanup_done = threading.Event()

    def controlled_rmtree(path, *args, **kwargs):
        candidate = Path(path)
        if candidate.name.startswith(f".{project.out_dir.name}.backup-"):
            cleanup_entered.set()
            assert cleanup_release.wait(timeout=5)
            try:
                return original_rmtree(path, *args, **kwargs)
            finally:
                cleanup_done.set()
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(project_module.shutil, "rmtree", controlled_rmtree)
    stages = []
    outcome = []
    errors = []

    def write():
        try:
            outcome.append(project.write_all(stage_cb=stages.append))
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    worker = threading.Thread(target=write)
    worker.start()
    assert cleanup_entered.wait(timeout=5)
    # 发布同步等待清理完成（join 60s）——CLI 写回后立即退出，不等会
    # 成片备份残留（0.25.0 实证：taxes 12 + catfiends 5 个 backup）
    cleanup_release.set()
    worker.join(timeout=30)

    assert not worker.is_alive()
    assert errors == []
    assert outcome[0]["verification"]["reopen_verified"] is True
    assert [event.phase for event in stages][-2:] == [
        "cleanup_pending", "cleanup_complete"]
    assert cleanup_done.is_set()
    assert project.out_dir.is_dir()
    # 发布返回时：本次备份已落盘且写入 manifest（回滚凭据），清理完成
    verification = outcome[0]["verification"]
    assert verification["backup"] is not None
    current = project.out_dir.parent / verification["backup"]
    assert current.is_dir()
    assert (current / "old-output.txt").read_text(encoding="utf-8") == "old"
    manifest_path = project.out_dir / ".hanhua-manifest.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["backup"]["path"] \
        == verification["backup"]
    # 清理完成：更早备份已删，本次备份保留供回滚
    names = {p.name for p in
             project.out_dir.parent.glob(f".{project.out_dir.name}.backup-*")}
    assert older.name not in names
    assert names == {verification["backup"]}


def test_write_all_installs_font_in_staging_before_commit(tmp_path, monkeypatch):
    game_dir = _make_tree()
    supplied = FontConfig(enabled=True, filename="test-font.ttf")
    proj = Project.open_game_dir(
        game_dir,
        tmp_path / "app",
        font_config=supplied,
    )
    supplied.enabled = False
    supplied.filename = "changed-after-open.ttf"
    proj.scan()
    runtime_entry = next(row for row in proj.store.get_entries()
                         if row["status"] == "pending")
    runtime_original = runtime_entry["original"]
    runtime_meta = json.loads(runtime_entry["meta"] or "{}")
    runtime_meta["disposition"] = "translate"
    proj.store.upsert_entries([{
        "file_id": runtime_entry["file_id"],
        "key_path": runtime_entry["key_path"],
        "original": runtime_original,
        "meta": runtime_meta,
    }])
    proj.store.set_manual(
        runtime_entry["file_id"], runtime_entry["key_path"], "已翻译")
    seen_staging = None
    progress = []
    font_result = FontInstallResult(
        installed=True,
        filename="test-font.ttf",
        family="Test Font",
    )

    def install(game, staging, config, *, translations, exclude,
                player_root=None, tmp_bundle=None):
        nonlocal seen_staging
        seen_staging = staging
        assert game == proj.game_dir
        assert staging != proj.out_dir
        assert config == proj.font_config
        assert config is not proj.font_config
        assert config is not supplied
        assert config.enabled is True
        assert config.filename == "test-font.ttf"
        assert translations == {runtime_original: "已翻译"}
        # W3：写回回退的逻辑键原文传给插件排除表（本场景无回退 → 空集）
        assert exclude == set()
        assert all(done < total for done, total in progress)
        (staging / "font-installed.marker").write_text("installed", encoding="utf-8")
        return font_result

    def record_progress(done, total):
        if done == total:
            assert (proj.out_dir / "font-installed.marker").is_file()
            assert seen_staging is not None
            assert not seen_staging.exists()
        progress.append((done, total))

    monkeypatch.setattr("hanhua.core.font.pipeline.install_font_override", install)

    result = proj.write_all(progress_cb=record_progress)

    assert seen_staging is not None
    assert not seen_staging.exists()
    assert (proj.out_dir / "font-installed.marker").read_text(encoding="utf-8") == "installed"
    assert result["font"] is font_result
    assert progress[-1][0] == progress[-1][1]
    assert all(done < total for done, total in progress[:-1])
    assert [done for done, _ in progress] == list(range(1, progress[-1][1] + 1))
    assert len({total for _, total in progress}) == 1


def test_write_all_keeps_previous_output_when_font_install_fails(tmp_path, monkeypatch):
    game_dir = _make_tree()
    config = FontConfig(enabled=True, filename="test-font.ttf")
    proj = Project.open_game_dir(game_dir, tmp_path / "app", font_config=config)
    proj.scan()
    _translate_first_pending(proj)
    proj.out_dir.mkdir(parents=True)
    marker = proj.out_dir / "KEEP-ME.txt"
    marker.write_text("old output", encoding="utf-8")
    progress = []

    def fail_install(*args, **kwargs):
        raise FontInstallError("synthetic font install failure")

    monkeypatch.setattr("hanhua.core.font.pipeline.install_font_override", fail_install)

    with pytest.raises(FontInstallError, match="synthetic font install failure"):
        proj.write_all(progress_cb=lambda done, total: progress.append((done, total)))

    assert marker.read_text(encoding="utf-8") == "old output"
    assert not list(proj.out_dir.parent.glob(f".{proj.out_dir.name}.staging-*"))
    assert not list(proj.out_dir.parent.glob(f".{proj.out_dir.name}.backup-*"))
    assert progress
    assert all(done < total for done, total in progress)
    assert [done for done, _ in progress] == list(range(1, progress[-1][0] + 1))
    assert len({total for _, total in progress}) == 1


def test_write_all_rejects_uninstalled_required_font_before_publish(
        tmp_path, monkeypatch):
    game_dir = _make_tree()
    project = Project.open_game_dir(
        game_dir, tmp_path / "app",
        font_config=FontConfig(enabled=True, filename="test-font.ttf"),
    )
    project.scan()
    entry = next(row for row in project.store.get_entries()
                 if row["status"] == "pending")
    project.store.set_manual(entry["file_id"], entry["key_path"], "已翻译")
    project.out_dir.mkdir(parents=True)
    marker = project.out_dir / "KEEP-ME.txt"
    marker.write_text("old output", encoding="utf-8")
    monkeypatch.setattr(
        "hanhua.core.font.pipeline.install_font_override",
        lambda *args, **kwargs: FontInstallResult(False),
    )

    with pytest.raises(RuntimeError, match="字体.*安装失败"):
        project.write_all()

    assert marker.read_text(encoding="utf-8") == "old output"


def test_write_all_keeps_static_output_when_font_provider_is_unsupported(
        tmp_path, monkeypatch):
    game_dir = _make_tree()
    project = Project.open_game_dir(
        game_dir, tmp_path / "app",
        font_config=FontConfig(enabled=True, filename="test-font.ttf"),
    )
    project.scan()
    _translate_first_pending(project)
    unsupported = FontInstallResult(
        installed=False,
        payload_deployed=False,
        runtime_verified=False,
        provider_supported=False,
        unsupported_reason="IL2CPP font provider is not available",
    )
    monkeypatch.setattr(
        "hanhua.core.font.pipeline.install_font_override",
        lambda *args, **kwargs: unsupported,
    )

    result = project.write_all()

    assert project.out_dir.is_dir()
    assert result["font"] is unsupported
    assert result["verification"]["font_level"] == "unsupported"
    assert any("IL2CPP" in warning
               for warning in result["verification"]["warnings"])


def test_project_snapshots_font_config_and_defaults_to_disabled(tmp_path):
    game_dir = _make_tree()
    supplied = FontConfig(enabled=True, filename="test-font.ttf")

    proj = Project(game_dir, tmp_path / "direct-app", font_config=supplied)
    default_proj = Project.open_game_dir(game_dir, tmp_path / "default-app")
    supplied.enabled = False
    supplied.filename = "changed-after-open.ttf"

    assert proj.font_config == FontConfig(enabled=True, filename="test-font.ttf")
    assert proj.font_config is not supplied
    assert default_proj.font_config == FontConfig(enabled=False)


def test_fit_bytes_padding_both_branches():
    out, truncated = _fit_bytes("很短", 10, "utf-8")
    assert len(out) == 10 and not truncated and out.endswith(b"\x00" * 4)
    out, truncated = _fit_bytes("这是一段很长的翻译文本", 6, "utf-8")
    assert truncated and len(out) == 6
    out.decode("utf-8")   # 合法 UTF-8


def test_fit_bytes_pad_false_truncation_returns_actual_bytes():
    """F1：pad=False 截断路径必须返回实际字节（不补 NUL 到 capacity）。

    #US/metadata 写回的记录长度由前缀/字段同步更新，补 NUL 会把省略号
    之后的 NUL 写进游戏显示文本；伪翻译审计（原文 + U+200B 必然超容量）
    依赖本语义才能产生真实截断记录而非 capacity 位填充。
    """
    out, truncated = _fit_bytes("这是一段很长的翻译文本", 6, "utf-8", pad=False)
    assert truncated and len(out) <= 6
    assert not out.endswith(b"\x00")
    out16, truncated16 = _fit_bytes(
        "这是一段很长的翻译文本", 10, "utf-16-le", pad=False)
    assert truncated16 and len(out16) <= 10
    assert not out16.endswith(b"\x00\x00")
    # 非截断路径 pad=False 行为不变（#US 短译文核心场景）
    ok16, truncated_ok = _fit_bytes("打开", 44, "utf-16-le", pad=False)
    assert not truncated_ok and ok16.decode("utf-16-le") == "打开"


def test_write_all_without_progress_cb():
    """回归：progress_cb=None 时 write_all 必须可运行。"""
    d = _make_tree()
    app_dir = Path(tempfile.mkdtemp()) / "app"
    proj = Project.open_game_dir(d, app_dir)
    proj.scan()
    store = proj.store
    for e in store.get_entries():
        if e["status"] == "pending":
            store.set_manual(e["file_id"], e["key_path"], "译：" + e["original"])
            break
    result = proj.write_all()
    assert proj.out_dir.exists()
    assert result["text_files"] >= 1
    verification = result["verification"]
    assert verification["input_protected"] is True
    assert verification["reopen_verified"] is True
    assert verification["changed_files"] >= 1
    assert verification["written_translations"] >= 1
    assert verification["font_level"] == "disabled"
    assert verification["warnings"] == []
    final_report = result["analysis_report"]
    final_route = {step.step_id: step for step in final_report.route}
    assert final_report.unblocked is True
    assert final_report.completable is True
    assert final_route["translation_quality"].status == "succeeded"
    assert final_route["font"].status == "succeeded"
    assert final_route["writeback"].status == "succeeded"
    # 副本里应包含译文（第一条 pending 是 en.json 的 "Hi"）
    import json
    out_json = json.loads((proj.out_dir / "Localization" / "en.json").read_text(encoding="utf-8"))
    assert out_json["a"] == "译：Hi"


def test_write_all_v1_skips_v2_files(monkeypatch):
    """v2 格式文件绝不能走 v1 文本写回路径（txt apply 会因缺 line_no 崩溃）。"""
    d = _make_tree()
    app_dir = Path(tempfile.mkdtemp()) / "app"
    proj = Project.open_game_dir(d, app_dir)
    resource = d / "a" / "assets.bundle"
    resource.parent.mkdir(parents=True)
    resource.write_bytes(b"synthetic binary resource")
    # 受控 fixture 先固定完整源树，再人工注入 v2 locator；不能复用添加资源前的旧 baseline。
    proj.scan()
    proj.store.add_file("v2fake", "a/assets.bundle", "v2_asset", "utf-8", "\n",
                        {"kind": "asset"})
    proj.store.upsert_entries([{"file_id": "v2fake", "key_path": "asset#1/str/0",
                                "original": "Hello world", "status": "pending",
                                "meta": {"kind": "rawstr", "obj": 1, "offset": 0}}])
    proj.store.set_manual("v2fake", "asset#1/str/0", "你好世界")

    real_render = text_writer._render

    def reject_v2_render(src, file_record, entries, target_lang,
                         normalize_fallback_punctuation=False,
                         skipped=None):
        if file_record["format"].startswith("v2_"):
            raise AssertionError("v2 resource reached the v1 renderer")
        return real_render(src, file_record, entries, target_lang,
                           normalize_fallback_punctuation, skipped)

    v2_result = WriteResult(files=1, entries=1)

    def capture_v2(store, game_dir, staging, typetree_generator=None,
                   triage_service=None, triage_app_dir=None):
        entries = [e for e in store.get_entries() if e["file_id"] == "v2fake"]
        assert game_dir == proj.game_dir
        assert entries[0]["translation"] == "你好世界"
        assert (staging / "a" / "assets.bundle").is_file()
        return v2_result

    monkeypatch.setattr(text_writer, "_render", reject_v2_render)
    monkeypatch.setattr("hanhua.core.project.write_back_v2", capture_v2)

    result = proj.write_all()
    assert result["text_files"] >= 1
    assert result["v2"] is v2_result
    assert result["verification"]["writer_outcome"] == {
        "attempted": 1,
        "written": 1,
        "rejected": [],
        "truncated": 0,
        # P3（0.42.1）：文本路径逐条记账与 v2 writer_outcome 并列呈现
        "text_attempted": 0,
        "text_written": 0,
    }
    assert (proj.out_dir / "a" / "assets.bundle").read_bytes() == (
        b"synthetic binary resource"
    )


def test_write_all_keeps_previous_output_when_write_fails(monkeypatch):
    """staging 写回失败时，已有输出不应被中途删除。"""
    d = _make_tree()
    app_dir = Path(tempfile.mkdtemp()) / "app"
    proj = Project.open_game_dir(d, app_dir)
    proj.scan()
    _translate_first_pending(proj)
    proj.out_dir.mkdir(parents=True)
    marker = proj.out_dir / "KEEP-ME.txt"
    marker.write_text("old output", encoding="utf-8")

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic write failure")

    monkeypatch.setattr("hanhua.core.project.write_back_text", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        proj.write_all()
    assert marker.read_text(encoding="utf-8") == "old output"
    assert not list(proj.out_dir.parent.glob(f".{proj.out_dir.name}.staging-*"))


def test_write_all_restores_previous_output_when_commit_fails(monkeypatch):
    """旧输出移入备份后若 staging 提交失败，必须把旧输出恢复回来。"""
    d = _make_tree()
    app_dir = Path(tempfile.mkdtemp()) / "app"
    proj = Project.open_game_dir(d, app_dir)
    proj.scan()
    _translate_first_pending(proj)
    proj.out_dir.mkdir(parents=True)
    marker = proj.out_dir / "KEEP-ME.txt"
    marker.write_text("old output", encoding="utf-8")

    real_replace = Path.replace

    def fail_staging_commit(source, target):
        if ".staging-" in source.name:
            raise PermissionError("synthetic commit lock")
        real_replace(source, target)

    monkeypatch.setattr("hanhua.core.project._replace_directory", fail_staging_commit)
    with pytest.raises(PermissionError, match="synthetic"):
        proj.write_all()
    assert marker.read_text(encoding="utf-8") == "old output"
    assert not list(proj.out_dir.parent.glob(f".{proj.out_dir.name}.backup-*"))


def test_patch_dll_utf16():
    """#US 堆写回：译文按 UTF-16 字节适配，保持堆结构。"""
    blob = bytearray(b"\x00" + bytes([0x08]) + "Hello".encode("utf-16-le") + b"\x01")
    # 直接调用内部字节逻辑
    from hanhua.core.unity.writer import _patch_bytes
    payload, truncated = _fit_bytes("你好", 10, "utf-16-le")
    assert not truncated and len(payload) == 10
    _patch_bytes(blob, 2, 10, payload)
    assert bytes(blob)[2:2 + 4].decode("utf-16-le") == "你好"
    assert bytes(blob)[-1] == 0x01       # 终结标记保留


def test_patch_dll_recomputes_ecma335_user_string_flag(tmp_path):
    """F1：#US 记录 = 压缩前缀 + 数据 + 尾部 flag；中文译文 flag 重算为 1。
    堆从 offset 1 起自洽（与流式遍历一致），目标记录在 offset 4。"""
    path = tmp_path / "Assembly-CSharp.dll"
    path.write_bytes(_us_heap([(b"\x41", 0), ("Open".encode("utf-16-le"), 0)]))
    result = WriteResult()
    entry = {
        "file_id": "f", "key_path": "us#4",
        "original": "Open", "translation": "打开",
        "meta": json.dumps({
            "kind": "us", "record_offset": 4, "utf16_len": 8,
            "disposition": "translate",
        }),
    }

    _patch_dll(path, [entry], result)

    # 译文 4 字节 → 新 flag 位于 offset 9（旧 flag 位 @13 为残留区已清零）
    assert path.read_bytes()[9] == 1
    assert result.written == 1


def test_patch_dll_recomputes_flag_for_legacy_locator_without_flag_offset(
        tmp_path):
    path = tmp_path / "Assembly-CSharp.dll"
    path.write_bytes(_us_heap([(b"\x41", 0), ("Open".encode("utf-16-le"), 0)]))
    result = WriteResult()
    entry = {
        "file_id": "f", "key_path": "us#4",
        "original": "Open", "translation": "打开",
        "meta": json.dumps({
            "kind": "us", "record_offset": 4, "utf16_len": 8,
            "disposition": "translate",
        }),
    }

    _patch_dll(path, [entry], result)

    assert path.read_bytes()[9] == 1
    assert result.written == 1


def test_patch_metadata_utf8():
    """IL2CPP 字符串池写回：UTF-8 字节适配 + 尾部填 0。"""
    blob = bytearray(b"\x00" * 8 + b"Hello player")
    from hanhua.core.unity.writer import _patch_bytes
    # "玩家你好" = 12 字节 UTF-8，恰好等于容量 12 → 不截断
    payload, truncated = _fit_bytes("玩家你好", 12, "utf-8")
    assert not truncated and payload.decode("utf-8") == "玩家你好"
    _patch_bytes(blob, 8, 12, payload)
    assert len(blob) == 20               # 总长度不变
    assert bytes(blob)[8:8 + 12].decode("utf-8") == "玩家你好"
    # 容量不足时按字符截断
    payload2, truncated2 = _fit_bytes("玩家你好啊", 9, "utf-8")
    assert truncated2 and len(payload2) == 9


def test_dll_and_metadata_use_atomic_write(tmp_path, monkeypatch):
    """固定容量二进制池写回必须通过临时文件原子替换。"""
    written = []

    result = WriteResult()

    def reject_truncation(*args):
        pytest.fail("atomic-write fixtures must not be truncated")

    monkeypatch.setattr(result, "note_truncated", reject_truncation)

    def capture(path, data):
        written.append((path, data))
        Path(path).write_bytes(data)

    monkeypatch.setattr("hanhua.core.unity.writer._atomic_write_bytes", capture)
    dll = tmp_path / "Assembly-CSharp.dll"
    # 自洽 #US 堆（重开验证按流式遍历，全 0 堆会提前 break）
    dll.write_bytes(_us_heap([(b"\x41", 0), ("Hello player".encode("utf-16-le"), 0)]))
    _patch_dll(dll, [{
        "original": "Hello player",
        "translation": "你好",
        "meta": '{"kind":"us","record_offset":4,"utf16_len":24}',
    }], result)

    metadata = tmp_path / "global-metadata.dat"
    metadata.write_bytes(_build(31, ["Hello player"]))
    _patch_metadata(metadata, [{
        "original": "Hello player",
        "translation": "你好",
        "meta": '{"kind":"il2cpp","file_offset":512,"length":12}',
    }], result)

    assert [path for path, _ in written] == [dll, metadata]


@pytest.mark.parametrize("kind", ("dll", "metadata"))
def test_fixed_pool_writer_counts_only_decoded_verified_patch(tmp_path, kind):
    path = tmp_path / ("Assembly-CSharp.dll" if kind == "dll" else "global-metadata.dat")
    if kind == "dll":
        # 自洽 #US 堆：填充记录使 offset 4 是记录起点
        path.write_bytes(_us_heap([(b"\x41", 0), (b"\x00" * 8, 0)]))
    else:
        path.write_bytes(_build(31, ["Hello player"]))
    result = WriteResult()
    if kind == "dll":
        entry = {
            "original": "Hello player", "translation": "你好世界很长",
            "meta": '{"kind":"us","record_offset":4,"utf16_len":8}',
        }
        _patch_dll(path, [entry], result)
        # F1：记录 = 前缀(1) + 数据(8) + flag(1)，无 NUL 填充
        effective = path.read_bytes()[5:13].decode("utf-16-le")
    else:
        entry = {
            "original": "Hello player", "translation": "你好世界很长",
            "meta": '{"kind":"il2cpp","file_offset":512,"length":12}',
        }
        _patch_metadata(path, [entry], result)
        # F1：length 字段同步为实际字节数，无 NUL 填充
        effective = path.read_bytes()[0x200:0x200 + 12].decode("utf-8")

    assert result.entries == 1
    assert result.truncated == 1
    assert effective.endswith("…")


def test_writer_uses_persisted_disposition_and_accounts_every_attempt(tmp_path):
    path = tmp_path / "Assembly-CSharp.dll"
    # 自洽 #US 堆：offset 4 与 offset 20 都是记录起点（重开验证按流式遍历）
    path.write_bytes(_us_heap([
        (b"\x41", 0),                                # 填充 1..4
        ("设置".encode("utf-16-le"), 0),             # 记录 @4（将被写）
        (b"\x41", 0),                                # 填充 10..13
        (b"\x41", 0),                                # 填充 13..16
        (b"\x42\x43", 0),                            # 填充 16..20
        (b"\x44", 0),                                # 记录 @20（不写）
    ]))
    result = WriteResult()
    entries = [
        {
            "file_id": "binary", "key_path": "us/1",
            "original": "Settings", "translation": "设置",
            "meta": '{"kind":"us","record_offset":4,"utf16_len":4,'
                    '"obj_has_values":false,"role":"display",'
                    '"disposition":"translate","reason":"single_visible_string"}',
        },
        {
            "file_id": "binary", "key_path": "us/2",
            "original": "Settings", "translation": "设置",
            "meta": '{"kind":"us","record_offset":20,"utf16_len":8,'
                    '"obj_has_values":false,"role":"structural",'
                    '"disposition":"structural","reason":"localization_key_list"}',
        },
    ]

    _patch_dll(path, entries, result)

    blob = path.read_bytes()
    # F1：记录 = 前缀(1) + 数据(4) + flag(1)；中文 → flag 1
    assert blob[5:9].decode("utf-16-le") == "设置"
    assert blob[9] == 1
    # structural 条目未写：offset 20 记录原样（0x02 前缀 + "D" + flag 0）
    assert blob[20:23] == b"\x02\x44\x00"
    assert result.attempted == 2
    assert result.written == 1
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "disposition_structural"
    assert result.attempted == result.written + len(result.rejected)


def test_raw_settings_write_decision_uses_disposition_not_value_guess():
    display = {
        "original": "Settings", "translation": "设置",
        "meta": '{"kind":"rawstr","obj_has_values":false,'
                '"role":"display","disposition":"translate",'
                '"reason":"single_visible_string"}',
    }
    structural = {
        **display,
        "meta": '{"kind":"rawstr","obj_has_values":false,'
                '"role":"structural","disposition":"structural",'
                '"reason":"localization_key_list"}',
    }

    assert _should_write_entry(display) is True
    assert _should_write_entry(structural) is False


def test_localization_explicit_disposition_overrides_legacy_kind_and_accounts():
    translate = {
        "file_id": "table", "key_path": "loc/1",
        "original": "Settings", "translation": "设置",
        "meta": '{"kind":"localization","entry_id":1,'
                '"disposition":"translate","role":"display"}',
    }
    rejected_entries = [
        {
            "file_id": "table", "key_path": f"loc/{index}",
            "original": "Settings", "translation": "设置",
            "meta": '{"kind":"localization","entry_id":%d,'
                    '"disposition":"%s","role":"structural"}'
                    % (index, disposition),
        }
        for index, disposition in enumerate(
            ("structural", "preserve", "code", "key"), start=2)
    ]
    result = WriteResult()
    legacy = {
        "original": "Settings", "translation": "设置",
        "meta": '{"kind":"localization","entry_id":99}',
    }

    selected = _select_write_items([
        (translate, {"kind": "localization", "entry_id": 1}),
        *[
            (entry, {"kind": "localization", "entry_id": index})
            for index, entry in enumerate(rejected_entries, start=2)
        ],
    ], result, "localization")
    for entry, _meta in selected:
        result.note_written(entry)

    assert [entry for entry, _meta in selected] == [translate]
    assert _should_write_entry(translate) is True
    assert all(_should_write_entry(entry) is False for entry in rejected_entries)
    assert _should_write_entry(legacy) is True
    assert result.outcome.attempted == 5
    assert result.outcome.written == 1
    assert [item.reason for item in result.outcome.rejected] == [
        "disposition_structural", "disposition_preserve",
        "disposition_code", "disposition_key",
    ]


@pytest.mark.parametrize("kind", ("dll", "metadata"))
def test_fixed_pool_writer_rejects_atomic_writer_that_does_not_apply(
        tmp_path, monkeypatch, kind):
    path = tmp_path / ("Assembly-CSharp.dll" if kind == "dll" else "global-metadata.dat")
    if kind == "dll":
        path.write_bytes(_us_heap([(b"\x41", 0), ("Hello player".encode("utf-16-le"), 0)]))
    else:
        path.write_bytes(_build(31, ["Hello player"]))
    result = WriteResult()
    monkeypatch.setattr(
        "hanhua.core.unity.writer._atomic_write_bytes",
        lambda *_args, **_kwargs: None,
    )
    if kind == "dll":
        entry = {
            "original": "Hello player", "translation": "你好",
            "meta": '{"kind":"us","record_offset":4,"utf16_len":24}',
        }
        writer = _patch_dll
    else:
        entry = {
            "original": "Hello player", "translation": "你好",
            "meta": '{"kind":"il2cpp","file_offset":512,"length":12}',
        }
        writer = _patch_metadata

    with pytest.raises(ValueError, match="重开验证失败"):
        writer(path, [entry], result)

    assert result.entries == 0


@pytest.mark.parametrize("kind", ("dll", "metadata"))
def test_fixed_pool_writer_does_not_count_unchanged_translation(tmp_path, kind):
    path = tmp_path / ("Assembly-CSharp.dll" if kind == "dll" else "global-metadata.dat")
    path.write_bytes(b"\x00" * 32)
    result = WriteResult()
    if kind == "dll":
        entry = {
            "original": "Hello player", "translation": "Hello player",
            "meta": '{"kind":"us","record_offset":4,"utf16_len":24}',
        }
        _patch_dll(path, [entry], result)
    else:
        entry = {
            "original": "Hello player", "translation": "Hello player",
            "meta": '{"kind":"il2cpp","file_offset":4,"length":12}',
        }
        _patch_metadata(path, [entry], result)

    assert result.entries == 0


def test_asset_writer_counts_entry_only_after_object_reopen_verification(
        tmp_path, monkeypatch):
    import UnityPy
    import hanhua.core.unity.writer as unity_writer

    class FakeObject:
        def __init__(self, raw):
            self.path_id = 7
            self.assets_file = type("AssetFile", (), {"name": "fixture.assets"})()
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
            self.objects = [FakeObject(Path(paths[0]).read_bytes())]
            self.files = {"main": SerializedFile(self)}

    monkeypatch.setattr(UnityPy, "Environment", FakeEnvironment)
    path = tmp_path / "fixture.assets"
    original = b"Settings"
    path.write_bytes(len(original).to_bytes(4, "little") + original + b"\x00" * 3)
    result = WriteResult()
    verify_calls = 0
    real_verify = unity_writer._verify_saved_bundle

    def count_verify(*args, **kwargs):
        nonlocal verify_calls
        real_verify(*args, **kwargs)
        verify_calls += 1

    monkeypatch.setattr(unity_writer, "_verify_saved_bundle", count_verify)
    entry = {
        "file_id": "fixture", "key_path": "asset#fixture.assets#7/str/0",
        "original": "Settings", "translation": "设置",
        "meta": '{"kind":"rawstr","asset_file":"fixture.assets",'
                '"obj":7,"offset":4,"obj_has_values":false,'
                '"role":"display","disposition":"translate",'
                '"reason":"single_visible_string"}',
    }

    _patch_asset(path, [entry], result)

    assert verify_calls == 1
    assert result.entries == 1
    assert result.attempted == result.written == 1
    assert result.rejected == []
    assert int.from_bytes(path.read_bytes()[:4], "little") == len("设置".encode("utf-8"))


def test_legacy_bare_path_id_requires_container_wide_uniqueness():
    legacy = [{"translation": "legacy"}]
    exact = [{"translation": "exact"}]
    by_obj = {("", 7): legacy, ("CAB-a", 7): exact}
    assert _entries_for_object_identity(by_obj, ("CAB-a", 7), {7: 2}) is exact
    assert _entries_for_object_identity({("", 7): legacy}, ("CAB-b", 7), {7: 2}) is None
    assert _entries_for_object_identity({("", 7): legacy}, ("CAB-b", 7), {7: 1}) is legacy


def test_crc_preflight_receives_only_bundles_with_writable_entries(
        tmp_path, monkeypatch):
    files = [
        {"id": "idle", "format": "v2_asset", "rel_path": "StreamingAssets/aa/idle.bundle"},
        {"id": "same", "format": "v2_asset", "rel_path": "StreamingAssets/aa/same.bundle"},
        {"id": "changed", "format": "v2_asset", "rel_path": "StreamingAssets/aa/changed.bundle"},
    ]

    class Store:
        def get_files(self):
            return files

    changed_entry = {
        "file_id": "changed", "original": "HELLO", "translation": "你好",
        "meta": '{"kind":"localization","obj":1,"entry_id":1}',
    }
    same_entry = {
        "file_id": "same", "original": "UNCHANGED", "translation": "UNCHANGED",
        "meta": '{"kind":"localization","obj":2,"entry_id":2}',
    }
    monkeypatch.setattr(
        "hanhua.core.unity.writer._entries_by_file",
        lambda store, file_ids: (
            {fid: ([changed_entry] if fid == "changed"
                   else [same_entry] if fid == "same"
                   else [])
             for fid in file_ids}
        ),
    )
    captured = []
    monkeypatch.setattr(
        "hanhua.core.unity.writer._validate_addressables_catalog_sources",
        lambda game, out, candidates: captured.extend(candidates),
    )
    monkeypatch.setattr(
        "hanhua.core.unity.writer._update_addressables_catalogs",
        lambda *args: [],
    )

    write_back_v2(Store(), tmp_path / "game", tmp_path / "out")
    assert [item["id"] for item in captured] == ["changed"]


def test_unity_writer_rejects_rel_path_escape_without_touching_external_file(
        tmp_path):
    game_dir = tmp_path / "game"
    out_dir = tmp_path / "output"
    game_dir.mkdir()
    out_dir.mkdir()
    outside = tmp_path / "outside.dll"
    original = b"\0" * 32
    outside.write_bytes(original)
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.add_file("evil", "../outside.dll", "v2_mono", "binary", "")
    store.upsert_entries([{
        "file_id": "evil",
        "key_path": "us/1",
        "original": "Hello player",
        "meta": {"kind": "us", "record_offset": 0, "utf16_len": 12},
    }])
    store.set_manual("evil", "us/1", "你好")

    with pytest.raises(ValueError, match="不安全的相对路径"):
        write_back_v2(store, game_dir, out_dir)

    assert outside.read_bytes() == original


def test_unity_writer_rejects_junction_parent_without_touching_external_file(
        tmp_path):
    game_dir = tmp_path / "game"
    source_parent = game_dir / "linked"
    source_parent.mkdir(parents=True)
    (source_parent / "file.dll").write_bytes(b"\0" * 32)
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "file.dll"
    original = b"X" * 32
    external.write_bytes(original)
    _make_junction(out_dir / "linked", outside)
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.add_file("safe", "linked/file.dll", "v2_mono", "binary", "")
    store.upsert_entries([{
        "file_id": "safe",
        "key_path": "us/1",
        "original": "Hello player",
        "meta": {"kind": "us", "record_offset": 0, "utf16_len": 12},
    }])
    store.set_manual("safe", "us/1", "你好")

    with pytest.raises(ValueError, match="reparse|重解析"):
        write_back_v2(store, game_dir, out_dir)

    assert external.read_bytes() == original


def test_typetree_writer_updates_array_paths_and_reopens_exact_values(
        tmp_path, monkeypatch):
    import UnityPy

    class FakeObject:
        path_id = 7
        assets_file = type("AssetFile", (), {"name": "fixture.assets"})()
        type = type("ObjectType", (), {"name": "MonoBehaviour"})()

        def __init__(self, raw): self.raw = raw
        def get_raw_data(self): return self.raw
        def read_typetree(self): return json.loads(self.raw.decode("utf-8"))
        def save_typetree(self, tree):
            self.raw = json.dumps(tree, ensure_ascii=False).encode("utf-8")
            return self.raw

    class SerializedFile:
        reader = None
        def __init__(self, environment): self.environment = environment
        def save(self): return self.environment.objects[0].get_raw_data()

    class FakeEnvironment:
        def __init__(self): self.objects, self.files = [], {}
        def load(self, paths):
            self.objects = [FakeObject(Path(paths[0]).read_bytes())]
            self.files = {"main": SerializedFile(self)}

    monkeypatch.setattr(UnityPy, "Environment", FakeEnvironment)
    path = tmp_path / "fixture.assets"
    path.write_text(json.dumps({"rows": [{"label": "Settings"},
                                          {"label": "Audio"}]}),
                    encoding="utf-8")
    entries = [
        {"file_id": "f", "key_path": f"field/{index}",
         "original": original, "translation": translated,
         "meta": json.dumps({"kind": "typetree", "obj": 7,
                             "asset_file": "fixture.assets",
                             "field_path": ["rows", index, "label"],
                             "role": "display", "disposition": "translate"})}
        for index, (original, translated) in enumerate(
            [("Settings", "设置"), ("Audio", "音频")])]
    result = WriteResult()

    _patch_asset(path, entries, result)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [row["label"] for row in saved["rows"]] == ["设置", "音频"]
    assert result.outcome.attempted == result.outcome.written == 2
    assert result.outcome.rejected == ()


def test_typetree_writer_rejects_missing_path_with_outcome_identity(
        tmp_path, monkeypatch):
    import UnityPy

    class FakeObject:
        path_id = 7
        assets_file = type("AssetFile", (), {"name": "fixture.assets"})()
        type = type("ObjectType", (), {"name": "MonoBehaviour"})()
        def __init__(self, raw): self.raw = raw
        def get_raw_data(self): return self.raw
        def read_typetree(self): return json.loads(self.raw.decode("utf-8"))

    class FakeEnvironment:
        def __init__(self): self.objects, self.files = [], {}
        def load(self, paths): self.objects = [FakeObject(Path(paths[0]).read_bytes())]

    monkeypatch.setattr(UnityPy, "Environment", FakeEnvironment)
    path = tmp_path / "fixture.assets"
    path.write_text('{"title":"Settings"}', encoding="utf-8")
    entry = {"file_id": "f", "key_path": "field/missing",
             "original": "Settings", "translation": "设置",
             "meta": json.dumps({"kind": "typetree", "obj": 7,
                                 "asset_file": "fixture.assets",
                                 "field_path": ["missing", "title"],
                                 "role": "display",
                                 "disposition": "translate"})}
    result = WriteResult()

    _patch_asset(path, [entry], result)

    assert result.attempted == 1 and result.written == 0
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "field_path_missing"
    assert result.outcome.attempted == (
        result.outcome.written + len(result.outcome.rejected))


def test_typetree_writer_rejects_object_when_save_typetree_fails(
        tmp_path, monkeypatch):
    """resonance-of-the-ocean 实证：Localization「TypeName Namespace Assembly」
    类型描述被翻译后 save_typetree 抛 ValueError——该对象整组拒绝并回滚，
    不让一个坏对象中断整个游戏写回（其余对象仍正常）。"""
    import UnityPy

    class FakeObject:
        path_id = 7
        assets_file = type("AssetFile", (), {"name": "fixture.assets"})()
        type = type("ObjectType", (), {"name": "MonoBehaviour"})()
        def __init__(self, raw): self.raw = raw
        def get_raw_data(self): return self.raw
        def read_typetree(self): return json.loads(self.raw.decode("utf-8"))
        def save_typetree(self, tree):
            raise ValueError(
                "Referenced type not found: Parser"
                "​ UnityEngine.Localization.SmartFormat.Core.Parsing")

    class FakeEnvironment:
        def __init__(self): self.objects, self.files = [], {}
        def load(self, paths): self.objects = [FakeObject(Path(paths[0]).read_bytes())]

    monkeypatch.setattr(UnityPy, "Environment", FakeEnvironment)
    path = tmp_path / "fixture.assets"
    path.write_text('{"rows":[{"label":"Settings"},{"label":"Audio"}]}',
                    encoding="utf-8")
    entries = [
        {"file_id": "f", "key_path": f"field/{index}",
         "original": original, "translation": translated,
         "meta": json.dumps({"kind": "typetree", "obj": 7,
                             "asset_file": "fixture.assets",
                             "field_path": ["rows", index, "label"],
                             "role": "display", "disposition": "translate"})}
        for index, (original, translated) in enumerate(
            [("Settings", "设置"), ("Audio", "音频")])]
    result = WriteResult()

    _patch_asset(path, entries, result)

    assert result.written == 0
    assert len(result.rejected) == 2
    assert all("save_typetree" in r.reason for r in result.rejected)
    # 失败对象不得改变文件内容（回滚后不写盘）
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [row["label"] for row in saved["rows"]] == ["Settings", "Audio"]


def test_write_all_blocks_when_translation_does_not_fit_file_encoding(
        tmp_path):
    """文档1 §5.1：译文无法以源文件编码写回时必须阻断发布，
    不能静默把文件改成 UTF-8（游戏按原编码读取会乱码）。"""
    game_dir = _make_tree()
    text = game_dir / "strings.txt"
    # GBK 编码源文件（含中文值，chardet 才能识别为 gbk 而非 ascii）
    text.write_bytes("a=嗨\n".encode("gbk"))
    project = Project.open_game_dir(game_dir, tmp_path / "app")
    project.scan()
    entry = next(row for row in project.store.get_entries()
                 if row["file_id"] == "strings.txt"
                 and row["status"] == "pending")
    # emoji（U+1F600）在 GBK 编码表外——必须阻断，不能静默回退 UTF-8
    project.store.set_manual(entry["file_id"], entry["key_path"], "你好😀")
    project.out_dir.mkdir(parents=True)
    marker = project.out_dir / "KEEP-ME.txt"
    marker.write_text("old output", encoding="utf-8")

    with pytest.raises(RuntimeError, match="编码写回"):
        project.write_all()

    assert marker.read_text(encoding="utf-8") == "old output"
    assert not list(project.out_dir.parent.glob(
        f".{project.out_dir.name}.staging-*"))


def test_write_result_tracks_reverted_sources_complete():
    """W3：逻辑回退的原文集合必须完整记录（插件排除表数据源），
    不随 30 条摘要截断。"""
    from hanhua.core.unity.writer import WriteResult
    result = WriteResult()
    for i in range(50):
        result.note_logic_reverted(
            {"original": f"Key{i}", "translation": f"键{i}"}, "code_line")
    assert result.logic_reverted == 50
    assert len(result.logic_reverted_items) == 30       # 摘要截断
    assert len(result.logic_reverted_sources) == 50     # 完整集合
    assert "Key0" in result.logic_reverted_sources
    assert "Key49" in result.logic_reverted_sources


def test_reverted_entry_not_double_rejected_by_tail_loop():
    """C1 回归：note_logic_reverted 必须标记 resolved——write_back_v2
    尾部兜底循环（未 resolve 候选统一 note_rejected）不会把主动回退的
    条目再记 rejected。此前漏标记：语义回退条目同时进 rejected，
    「防线越生效、发布越被误阻」（object 闸门按 rejected 阻断）。"""
    from hanhua.core.unity.writer import WriteResult
    result = WriteResult()
    entry = {"original": "Key0", "translation": "键0",
             "meta": json.dumps(
                 {"kind": "us", "file_offset": 4, "disposition": "translate"})}
    # 反向语义审计回退：主动不写（保留原文，进 W3 排除表）
    result.note_logic_reverted(entry, "logic_key_in_code_object")
    # 模拟 write_back_v2 尾部兜底循环：未 resolve 的候选统一 rejected
    for e in [entry]:
        if not result.is_resolved(e):
            result.note_rejected(e, "locator_not_found_or_unchanged")
    assert result.outcome.rejected == ()        # 回退条目绝不进 rejected
    assert result.logic_reverted == 1
    assert result.attempted == 1 and result.written == 0
    assert "Key0" in result.logic_reverted_sources


def test_snapshot_typetree_surroundings_excludes_target_paths():
    """C2：非目标叶子快照——save_typetree 完整重建的字段级保护。目标
    叶子（本次补丁的 m_Localized）排除，其余叶子（m_Id/m_Size/m_Name/
    邻字段）全部入快照供重开比对。"""
    from hanhua.core.unity.writer import _snapshot_typetree_surroundings
    tree = {
        "m_Name": "UITable",
        "m_Size": 4,
        "m_TableData": [
            {"m_Id": 1, "m_Localized": "VOLUME"},
            {"m_Id": 2, "m_Localized": "FULLSCREEN"},
        ],
        "m_Complex": {"nested": {"value": 7}},
    }
    exclude = {("m_TableData", 0, "m_Localized"),
               ("m_TableData", 1, "m_Localized")}
    snap = _snapshot_typetree_surroundings(tree, exclude)
    values = {tuple(path): value for path, value in snap}
    assert ("m_TableData", 0, "m_Localized") not in values
    assert ("m_TableData", 1, "m_Localized") not in values
    assert values[("m_TableData", 0, "m_Id")] == 1
    assert values[("m_TableData", 1, "m_Id")] == 2
    assert values[("m_Size",)] == 4
    assert values[("m_Name",)] == "UITable"
    assert values[("m_Complex", "nested", "value")] == 7
    # 空排除 = 全量叶子快照
    full = _snapshot_typetree_surroundings(tree, set())
    assert len(full) == len(snap) + 2


def test_consistency_revert_enters_reverted_sources_and_avoids_rejection():
    """C1 同族：audit_repeat_consistency 全组回退（键身份/译文不一致）
    经 on_revert 回调进逻辑回退记账——保留原文进 W3 排除表（插件运行
    时不得再翻译，防断链），且尾部循环不把它误记 rejected。"""
    from hanhua.core.unity.logic_audit import audit_repeat_consistency
    from hanhua.core.unity.writer import WriteResult
    result = WriteResult()
    items = [
        ({"original": "Splash", "translation": "画面"},
         {"obj": 7, "offset": 100, "role": "display"}),
        ({"original": "Splash", "translation": "Splash"},
         {"obj": 7, "offset": 140, "role": "structural", "reason": "code_line"}),
    ]
    audit_repeat_consistency(
        items, on_revert=lambda e, r: result.note_logic_reverted(e, r))
    # 模拟 write_back_v2 尾部兜底循环
    for e, _ in items:
        if not result.is_resolved(e):
            result.note_rejected(e, "locator_not_found_or_unchanged")
    assert result.outcome.rejected == ()
    assert result.logic_reverted == 1            # 只记被回退的翻译条目
    assert "Splash" in result.logic_reverted_sources
    assert items[0][0]["translation"] == "Splash"  # 译文撤销回原文


def test_note_rejected_tracks_rejected_sources():
    """C4：note_rejected 记录条目原文（插件排除表数据源）——被拒条目
    静态层保留原文，插件运行时翻译同样按名断链（写不进的对象里往往是
    键清单/类型描述等结构串）。"""
    from hanhua.core.unity.writer import WriteResult
    result = WriteResult()
    result.note_rejected(
        {"original": "Settings", "translation": "设置", "meta": "{}"},
        "immutable_field_protected")
    assert result.rejected_sources == {"Settings"}
    assert result.outcome.rejected[0].reason == "immutable_field_protected"
    # 与逻辑回退原文分池统计（排除表最终合并，但语义各归各）
    assert result.logic_reverted_sources == set()


def test_write_all_merges_rejected_sources_into_exclude(monkeypatch):
    """C4 集成：写回侧拒绝的条目原文并入运行时排除表——与逻辑回退原文
    合并传给插件（静态保留原文 + 插件翻译 → 按名断链）。"""
    game_dir = _make_tree()
    proj = Project.open_game_dir(game_dir, Path(tempfile.mkdtemp()) / "app")
    proj.scan()
    runtime_entry = next(row for row in proj.store.get_entries()
                         if row["status"] == "pending")
    runtime_meta = json.loads(runtime_entry["meta"] or "{}")
    runtime_meta["disposition"] = "translate"
    proj.store.upsert_entries([{
        "file_id": runtime_entry["file_id"],
        "key_path": runtime_entry["key_path"],
        "original": runtime_entry["original"],
        "meta": runtime_meta,
    }])
    proj.store.set_manual(
        runtime_entry["file_id"], runtime_entry["key_path"], "已翻译")

    v2_result = WriteResult(files=0, entries=0)
    v2_result.logic_reverted_sources.add("moveForward")   # 语义回退
    v2_result.note_rejected(
        {"original": "Settings", "translation": "设置", "meta": "{}"},
        "immutable_field_protected")                       # 写回侧拒绝

    def capture_v2(store, game_dir, staging, typetree_generator=None,
                   triage_service=None, triage_app_dir=None):
        return v2_result

    seen_exclude = None
    font_result = FontInstallResult(
        installed=True, filename="test-font.ttf")

    def install(game, staging, config, *, translations, exclude,
                player_root=None, tmp_bundle=None):
        nonlocal seen_exclude
        seen_exclude = set(exclude)
        return font_result

    monkeypatch.setattr("hanhua.core.project.write_back_v2", capture_v2)
    monkeypatch.setattr("hanhua.core.font.pipeline.install_font_override", install)

    proj.write_all(allow_partial=True)   # 拒绝条目走 P0-2 闸门：确认后放行

    assert seen_exclude == {"moveForward", "Settings"}


# ── 写回 C8：占位符防线覆盖命名占位符 {name} ──────────────────────

def test_format_placeholder_intact_named_placeholder(tmp_path):
    """C8：{name} 命名占位符截断丢失 → _placeholders_intact 必须拦截
    （原防线只认 {数字开头}，{name 被砍后照写不拦）。"""
    from hanhua.core.unity.writer import _placeholders_intact
    assert _placeholders_intact(
        "Hello {name}, welcome!", "你好 {name}，欢迎") is True
    assert _placeholders_intact("Hello {name}!", "你好，欢迎") is False
    assert _placeholders_intact("Hello {name}!", "你好，欢迎 {name") is False


def test_format_placeholder_intact_format_spec_and_equals(tmp_path):
    """C8：格式说明 {1:0.00}/{0,-10:N2} 与 Ren'Py 等号值 {w=1.5} 都纳入
    防线（原防线只认数字开头 → {1:0.00} 匹配但 {name} 系全漏）。"""
    from hanhua.core.unity.writer import _placeholders_intact
    assert _placeholders_intact(
        "HP {0}/{1:0.00}", "血量 {0}/{1:0.00}") is True
    assert _placeholders_intact("HP {0}/{1:0.00}", "血量 {0}") is False
    assert _placeholders_intact("Wait {w=1.5}", "等待 {w=1.5}") is True
    assert _placeholders_intact("Wait {w=1.5}", "等待") is False
    assert _placeholders_intact("{/i}bold{/b}", "{/i}粗体") is False


def test_restore_placeholders_restores_named_and_format(tmp_path):
    """C8：机械恢复补末尾覆盖命名/格式说明/等号值占位符（按名取参或
    补末尾均不破坏语义——丢失是更坏的结果）。"""
    from hanhua.core.unity.writer import _restore_placeholders
    assert _restore_placeholders(
        "Hello {name}!", "你好，欢迎") == "你好，欢迎{name}"
    assert _restore_placeholders(
        "HP {0}/{1:0.00}", "血量 {0}") == "血量 {0}{1:0.00}"
    assert _restore_placeholders(
        "Wait {w=1.5}", "等待") == "等待{w=1.5}"


def test_format_placeholder_escaped_braces_not_matched(tmp_path):
    """C8：string.Format 转义 {{/}} 不得误匹配（纯转义文本无占位符）。"""
    from hanhua.core.unity.writer import _placeholders_intact, _restore_placeholders
    assert _placeholders_intact("{{literal}}", "{{字面}}") is True
    assert _restore_placeholders("{{literal}}", "{{字面}}") == "{{字面}}"


def test_restore_placeholders_capped_tight_capacity_keeps_placeholder(tmp_path):
    """C8（2026-08-14 Minato 24 条实证）：译文超容量 + 缺占位符——
    旧路径补丁被二次截断削掉仍 reject；容量感知恢复从正文尾部腾空间，
    占位符完整写回（UTF-16 与 UTF-8 两条写回路径同规则）。"""
    from hanhua.core.unity.writer import (
        _placeholders_intact, _restore_placeholders_capped)
    # UTF-16：#US 记录场景——容量 12 码元，译文 12 字（满）且丢 {0}
    original = "HP {0}/{1}"
    translation = "生命值生命值生命值"      # 12 码元 = 容量满，且无占位符
    restored = _restore_placeholders_capped(
        original, translation, capacity=24, encoding="utf-16-le")
    assert restored is not None
    assert len(restored) == 24                      # 不超容量
    text = restored.decode("utf-16-le")
    assert _placeholders_intact(original, text) is True   # 占位符保住
    assert text.startswith("生命值")                # 正文尾部腾空间而非砍头
    assert text.endswith("{0}{1}")
    # UTF-8：metadata 场景——容量 20 字节，译文 6 个汉字（18B）丢 {w=1.5}
    original2 = "Wait {w=1.5}"
    translation2 = "等待等待等待"                   # 18 字节，满 20 预算
    restored2 = _restore_placeholders_capped(
        original2, translation2, capacity=20, encoding="utf-8")
    assert restored2 is not None
    assert len(restored2) <= 20
    assert _placeholders_intact(original2, restored2.decode("utf-8")) is True
    # 物理不可能：占位符总长 > 容量 → None（上层 reject）
    assert _restore_placeholders_capped(
        "A {b}", "甲", capacity=4, encoding="utf-8") is None
    # 容量充足：直接补末尾，正文不动
    assert _restore_placeholders_capped(
        "HP {0}/{1}", "生命值", capacity=64, encoding="utf-8") == \
        "生命值{0}{1}".encode("utf-8")
