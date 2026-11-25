"""End-to-end tests for FastAPI backend, XUAT real-time endpoint, inline edit, context & reviewer."""
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ul10n.core.models import Entry, GameContext, STATUS_TRANSLATED
from ul10n.engine.context_builder import infer_game_context
from ul10n.engine.llm_client import ChatResult
from ul10n.engine.reviewer import review_and_refine_entry
from ul10n.engine.translator import TranslationPipeline
from ul10n.server.app import create_app

APP_ROOT = Path(__file__).resolve().parents[1]


class FakeLLMClient:
    def __init__(self, responses: list[str] | dict[str, str]):
        self.model = "Hy-MT3-1.8B-Q8_0"
        self._responses = responses
        self._idx = 0

    def chat(self, messages, **_kwargs):
        user_msg = messages[-1]["content"]
        if isinstance(self._responses, dict):
            for k, v in self._responses.items():
                if k in user_msg:
                    return ChatResult(content=v)
            return ChatResult(content="默认中文译文")
        val = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return ChatResult(content=val)

    def close(self):
        pass


def test_full_api_and_xuat_workflow(tmp_path: Path):
    game_dir = tmp_path / "MyUnityGame"
    (game_dir / "MyUnityGame_Data" / "Managed").mkdir(parents=True)
    data_dir = tmp_path / "data"

    app = create_app(app_root=APP_ROOT, data_dir=data_dir)
    fake_client = FakeLLMClient({
        "Welcome back, traveler.": "欢迎回来，旅人。",
        "Deal {0} fire damage": "造成 {0} 点火焰伤害",
    })

    with TestClient(app) as client:
        # 1. Open project
        r = client.post("/api/projects/open", json={"game_dir": str(game_dir)})
        assert r.status_code == 200
        pid = r.json()["project"]["id"]

        # 2. XUAT real-time builtin UI hit (no model needed!)
        r_ui = client.get(f"/xuat/translate/{pid}", params={"text": "Resume", "from": "en", "to": "zh"})
        assert r_ui.status_code == 200
        assert r_ui.text == "继续"

        # 3. XUAT real-time model translation
        with patch.object(app.state.services.runtime, "create_client_for_role", return_value=fake_client):
            r_live = client.get(
                f"/xuat/translate/{pid}",
                params={"text": "Welcome back, traveler.", "from": "en", "to": "zh"},
            )
            assert r_live.status_code == 200
            assert r_live.text == "欢迎回来，旅人。"

            r_post = client.post(
                f"/xuat/translate/{pid}",
                json={"texts": ["Deal {0} fire damage"]},
            )
            assert r_post.status_code == 200
            assert r_post.json()["translations"] == ["造成 {0} 点火焰伤害"]

        # 4. View entries & inline manual edit
        r_list = client.get(f"/api/projects/{pid}/entries")
        assert r_list.status_code == 200
        entries = r_list.json()["entries"]
        assert len(entries) == 3

        target = next(e for e in entries if e["original"] == "Welcome back, traveler.")
        r_edit = client.patch(
            f"/api/projects/{pid}/entries/{target['id']}",
            json={"translation": "旅人，欢迎归来。"},
        )
        assert r_edit.status_code == 200
        assert r_edit.json()["entry"]["status"] == "edited"
        assert r_edit.json()["entry"]["locked"] is True

        # 5. Live XUAT now serves the user's edited translation immediately from cache
        r_cached = client.get(f"/xuat/translate/{pid}", params={"text": "Welcome back, traveler."})
        assert r_cached.text == "旅人，欢迎归来。"

        # 6. Export XUAT curated dictionary
        r_exp = client.post(f"/api/projects/{pid}/export-xuat")
        assert r_exp.status_code == 200
        assert r_exp.json()["exported"] == 3
        dict_path = Path(r_exp.json()["path"])
        assert dict_path.is_file()
        assert "Welcome back, traveler.=旅人，欢迎归来。" in dict_path.read_text(encoding="utf-8")


def test_context_curly_quotes_and_reviewer_refinement(tmp_path: Path):
    app = create_app(app_root=APP_ROOT, data_dir=tmp_path / "data")
    svc = app.state.services
    store = svc.project_store("p1")

    # Curly-quote malformed JSON from 4B model should still parse cleanly
    ctx_client = FakeLLMClient([
        '{"genre": "角色扮演”, "setting": "奇幻大陆", "summary": "勇士讨伐魔王的故事。"}'
    ])
    ctx = infer_game_context(ctx_client, [Entry(1, "Slay the Demon King.")], game_name="HeroQuest")
    assert ctx.genre == "角色扮演"
    assert ctx.setting == "奇幻大陆"

    # Reviewer flags MAJOR -> auto retranslates -> PASS
    e = store.upsert_entry("Do not open the gate.", "打开大门。", status=STATUS_TRANSLATED)
    reason_client = FakeLLMClient([
        '{"level": "CRITICAL", "reason": "漏译否定词 Do not"}',
        '{"level": "PASS", "reason": "正确"}',
    ])
    tr_client = FakeLLMClient(["不要打开大门。"])
    pipeline = TranslationPipeline(svc.global_store, store)
    updated, verdict = review_and_refine_entry(e, reason_client, tr_client, pipeline, GameContext())
    assert verdict.level == "PASS"
    assert updated.translation == "不要打开大门。"
    svc.shutdown()


def test_empty_tasks_return_400_and_task_seq_monotonic(tmp_path: Path):
    import struct
    import time

    from ul10n.core.text_scanner import _looks_like_noise_display, _scan_length_prefixed_utf8

    # Scanner helper checks
    assert _looks_like_noise_display("UI_Button_01") is True
    assert _looks_like_noise_display("New Text") is True
    assert _looks_like_noise_display("PLAY GAME") is False
    text_bytes = "Press Space to Start".encode("utf-8")
    fake_raw = b"\x00" * 28 + struct.pack("<I", len(text_bytes)) + text_bytes
    assert _scan_length_prefixed_utf8(fake_raw) == ["Press Space to Start"]

    # Empty project -> tasks/translate, tasks/context, tasks/review return HTTP 400 cleanly
    game_dir = tmp_path / "EmptyGame"
    (game_dir / "EmptyGame_Data" / "Managed").mkdir(parents=True)
    app = create_app(app_root=APP_ROOT, data_dir=tmp_path / "data")

    with TestClient(app) as client:
        pid = client.post("/api/projects/open", json={"game_dir": str(game_dir)}).json()["project"]["id"]

        r_tr = client.post(f"/api/projects/{pid}/tasks/translate", json={"include_failed": True})
        assert r_tr.status_code == 400
        r_ctx = client.post(f"/api/projects/{pid}/tasks/context")
        assert r_ctx.status_code == 400
        r_rev = client.post(f"/api/projects/{pid}/tasks/review", json={})
        assert r_rev.status_code == 400
        assert client.get("/api/tasks/current").json()["task"] is None

        # Scan endpoint returns cleanly even when no assets exist
        r_scan = client.post(f"/api/projects/{pid}/scan")
        assert r_scan.status_code == 200
        assert r_scan.json()["added"] == 0

        # Add an entry and run batch translate -> verify monotonic seq and clean completion
        client.post(f"/api/projects/{pid}/import-text", json={"content": "Press Space to Start\n"})
        fake_client = FakeLLMClient({"Press Space to Start": "按空格键开始"})
        with patch.object(app.state.services.runtime, "create_client_for_role", return_value=fake_client):
            r_start = client.post(f"/api/projects/{pid}/tasks/translate", json={"include_failed": True})
            assert r_start.status_code == 200
            start_task = r_start.json()["task"]
            assert start_task["seq"] >= 1

            # Wait briefly for background thread to complete
            final_task = None
            for _ in range(50):
                cur = client.get("/api/tasks/current").json()["task"]
                if cur and cur["status"] != "running":
                    final_task = cur
                    break
                time.sleep(0.05)

            assert final_task is not None
            assert final_task["status"] == "completed"
            assert final_task["done"] == 1
            assert final_task["seq"] > start_task["seq"]


def test_expanded_static_scanner_and_review_autofix(tmp_path: Path):
    import struct

    from ul10n.core.text_scanner import (
        _looks_like_noise_display,
        scan_unity_static_texts_detailed,
    )

    # 1. Pure-static UI scanner MUST NOT blindly scrape StreamingAssets / global-metadata.dat / noise
    game_dir = tmp_path / "StoryGame"
    data_dir = game_dir / "StoryGame_Data"
    sa = data_dir / "StreamingAssets"
    sa.mkdir(parents=True)
    (sa / "dialogue.json").write_text(
        '{"id": "dlg_01", "text": "Dynamic json text handled by hook"}',
        encoding="utf-8",
    )
    meta_dir = data_dir / "il2cpp_data" / "Metadata"
    meta_dir.mkdir(parents=True)
    (meta_dir / "global-metadata.dat").write_bytes(b"\xaf\x1b\xb1\xfa" + b"\x00" * 64)

    rep = scan_unity_static_texts_detailed(game_dir)
    assert rep.texts == []
    assert rep.by_source == {"ui": 0}

    # Noise filters block binary surrogates, debug tags, lorem ipsum, gibberish, and default placeholders
    assert _looks_like_noise_display("\udcee\udcdebinary") is True
    assert _looks_like_noise_display("[DEBUG] selectedSlot={0}") is True
    assert _looks_like_noise_display("Lorem ipsum dolor sit amet") is True
    assert _looks_like_noise_display("fdji ijsdijn j jnf oij iuhwr i iu iujn") is True
    assert _looks_like_noise_display("ssssssssssssssss") is True
    assert _looks_like_noise_display("Start Episode") is False
    assert _looks_like_noise_display("Graphics Settings") is False

    # 2. Reviewer auto-fix: Case A (translation model repeats bad text -> direct suggestion applied)
    app = create_app(app_root=APP_ROOT, data_dir=tmp_path / "data")
    svc = app.state.services
    store = svc.project_store("p_autofix")
    pipeline = TranslationPipeline(svc.global_store, store)

    e_bad = store.upsert_entry("Do not touch the fire.", "触摸火焰。", status=STATUS_TRANSLATED)
    reason_client = FakeLLMClient([
        '{"level": "CRITICAL", "reason": "漏译否定词 Do not", "suggestion": "不要触碰火焰。"}'
    ])
    # Simulate translation model stubbornly repeating the old buggy translation
    stubborn_tr_client = FakeLLMClient(["触摸火焰。"])
    updated, verdict = review_and_refine_entry(
        e_bad, reason_client, stubborn_tr_client, pipeline, GameContext()
    )
    assert updated.status == STATUS_TRANSLATED
    assert updated.review_level == "FIXED"
    assert updated.translation == "不要触碰火焰。"
    assert verdict.level == "FIXED"

    # Case B: MINOR issue -> retranslated cleanly by translate model
    e_minor = store.upsert_entry("Press E to interact", "按E交互一下", status=STATUS_TRANSLATED)
    reason_minor = FakeLLMClient([
        '{"level": "MINOR", "reason": "UI提示应更简洁干练", "suggestion": "按 E 互动"}'
    ])
    tr_improved = FakeLLMClient(["按 E 交互"])
    updated2, verdict2 = review_and_refine_entry(
        e_minor, reason_minor, tr_improved, pipeline, GameContext()
    )
    assert updated2.status == STATUS_TRANSLATED
    assert updated2.review_level == "FIXED"
    assert updated2.translation == "按 E 交互"
    assert verdict2.level == "FIXED"
    svc.shutdown()


def test_live_filter_hot_sync_and_batched_review(tmp_path: Path):
    from ul10n.engine.reviewer import review_batch_and_refine

    game_dir = tmp_path / "LiveGame"
    (game_dir / "LiveGame_Data" / "Managed").mkdir(parents=True)
    app = create_app(app_root=APP_ROOT, data_dir=tmp_path / "data")
    svc = app.state.services

    with TestClient(app) as client:
        pid = client.post("/api/projects/open", json={"game_dir": str(game_dir)}).json()["project"]["id"]
        # Add static prescan entry vs live XUAT entry
        client.post(f"/api/projects/{pid}/import-text", json={"content": "Static Menu=静态菜单\n"})
        fake_tr = FakeLLMClient({"Hello adventurer!": "你好，冒险者！"})
        with patch.object(svc.runtime, "create_client_for_role", return_value=fake_tr):
            r_live = client.get(f"/xuat/translate/{pid}", params={"text": "Hello adventurer!"})
            assert r_live.text == "你好，冒险者！"

        # Filter by status=live returns ONLY xuat_live entries
        r_q = client.get(f"/api/projects/{pid}/entries", params={"status": "live"})
        assert r_q.status_code == 200
        body = r_q.json()
        assert body["total"] == 1
        assert body["stats"]["live"] == 1
        live_id = body["entries"][0]["id"]

        # Save manual edit via overlay helper -> keeps live source, locks, and writes 00_UL10nForge.txt
        saved = svc.save_manual_translation(pid, live_id, "欢迎你，冒险家！")
        assert saved is not None
        assert saved.locked is True
        assert saved.source == "xuat_live"
        r_q2 = client.get(f"/api/projects/{pid}/entries", params={"status": "live"})
        assert r_q2.json()["total"] == 1
        assert r_q2.json()["entries"][0]["translation"] == "欢迎你，冒险家！"

        # Batched review: built-in UI fast-passes in 0ms; remaining items reviewed in 1 batch call
        store = svc.project_store(pid)
        e_ui = store.upsert_entry("Settings", "设置", status=STATUS_TRANSLATED)
        e_ok = store.upsert_entry("Good morning.", "早上好。", status=STATUS_TRANSLATED)
        e_bad = store.upsert_entry("Do not enter.", "进入。", status=STATUS_TRANSLATED)

        call_counter = {"n": 0}

        class CountingReasonClient(FakeLLMClient):
            def chat(self, messages, **kwargs):
                call_counter["n"] += 1
                return super().chat(messages, **kwargs)

        reason_cli = CountingReasonClient([
            '[{"idx": 2, "level": "CRITICAL", "reason": "漏译否定词", "suggestion": "不要进入。"}]'
        ])
        tr_cli = FakeLLMClient(["禁止进入。"])
        pipeline = TranslationPipeline(svc.global_store, store)
        results = review_batch_and_refine(
            [e_ui, e_ok, e_bad], reason_cli, tr_cli, pipeline, GameContext(), batch_size=10, mode="full"
        )
        assert call_counter["n"] == 1  # ONE single batch call for all non-builtin entries!
        by_id = {u.id: (u, v) for u, v in results}
        assert by_id[e_ui.id][0].review_level == "PASS"
        assert by_id[e_ok.id][0].review_level == "PASS"
        assert by_id[e_bad.id][0].review_level == "FIXED"
        assert by_id[e_bad.id][0].translation == "禁止进入。"


def test_p0_p1_p2_security_quality_uninstall_exports_regex_and_undo(tmp_path: Path):
    import io
    import json
    import zipfile

    from ul10n.core.game_detector import detect_game
    from ul10n.core.hook_deployer import deploy_hook, uninstall_hook
    from ul10n.core.quality import check_translation_quality, is_translatable
    from ul10n.core.xuat_codec import compile_xuat_regex_entry, render_xuat_dict_text
    from ul10n.engine.context_builder import sync_context_terms_to_glossary
    from ul10n.engine.reviewer import review_batch_and_refine

    # 1. P0 Quality: Japanese/Korean translatability + dropped number + broken closing tag
    assert is_translatable("魔王を倒すために旅立つ") is True
    assert is_translatable("마왕을 물리치기 위해 떠난다") is True
    assert is_translatable("已经是纯中文句子") is False
    assert check_translation_quality("Deal 25 fire damage", "造成火焰伤害").passed is False
    assert check_translation_quality("<b>Warning</b>", "<b>警告<b>").passed is False

    # 2. P0 Safe Uninstall preserves pre-existing player mods
    mod_game = tmp_path / "ModdedGame"
    (mod_game / "ModdedGame_Data" / "Managed").mkdir(parents=True)
    (mod_game / "BepInEx" / "core").mkdir(parents=True)
    (mod_game / "BepInEx" / "plugins").mkdir(parents=True)
    other_mod = mod_game / "BepInEx" / "plugins" / "PlayerFavoriteMod.dll"
    other_mod.write_bytes(b"PLAYER_MOD_DATA")
    info = detect_game(mod_game)
    res = deploy_hook(info, app_root=APP_ROOT, project_id="p_mod", server_port=18765)
    assert res.manifest["preexisting_loader"] is True
    uninstall_hook(mod_game, res.manifest)
    assert other_mod.is_file() and other_mod.read_bytes() == b"PLAYER_MOD_DATA"

    # 3. P2 Dynamic template -> XUAT regex compiler
    rx = compile_xuat_regex_entry("{0} added to inventory", "{0} 已加入物品栏")
    assert rx == r'r:"^(.+?)\ added\ to\ inventory$"=$1 已加入物品栏'

    # 4. Full API checks: CSRF origin block, API key masking, exports, patch zip, undo, skip, replace, glossary enforcement
    game_dir = tmp_path / "ExportGame"
    (game_dir / "ExportGame_Data" / "Managed").mkdir(parents=True)
    app = create_app(app_root=APP_ROOT, data_dir=tmp_path / "data")
    svc = app.state.services

    with TestClient(app) as client:
        # Cross-origin mutating request blocked
        r_evil = client.post(
            "/api/projects/open",
            json={"game_dir": str(game_dir)},
            headers={"Origin": "https://evil.example"},
        )
        assert r_evil.status_code == 403

        # API key masking & preservation
        r_set = client.put(
            "/api/settings",
            json={"translate_role": {"mode": "api", "base_url": "https://api.example/v1", "model": "m1", "api_key": "sk-secret-9876"}},
        )
        assert r_set.status_code == 200
        assert r_set.json()["settings"]["translate_role"]["api_key"] == "••••9876"
        # Submitting masked key preserves the real stored key
        client.put(
            "/api/settings",
            json={"translate_role": {"mode": "api", "base_url": "https://api.example/v1", "model": "m2", "api_key": "••••9876"}},
        )
        assert svc.settings().translate_role.api_key == "sk-secret-9876"

        # Open project & populate entries
        pid = client.post("/api/projects/open", json={"game_dir": str(game_dir)}).json()["project"]["id"]
        store = svc.project_store(pid)
        e1 = store.upsert_entry("Look sensitivity", "视角灵敏度", status=STATUS_TRANSLATED)
        e2 = store.upsert_entry("{0} added to inventory", "{0} 已加入物品栏", status=STATUS_TRANSLATED)
        e3 = store.upsert_entry("Enter the Lighthouse", "进入那座高塔", status=STATUS_TRANSLATED)

        # Context -> Glossary sync & glossary consistency auto-fix in batch review
        ctx = GameContext(game_name="ExportGame", terms=["Lighthouse=灯塔"])
        assert sync_context_terms_to_glossary(ctx, store) == 1
        reason_cli = FakeLLMClient(["[]"])
        tr_cli = FakeLLMClient(["进入灯塔"])
        review_batch_and_refine(
            [e3], reason_cli, tr_cli, TranslationPipeline(svc.global_store, store), ctx
        )
        fixed_e3 = store.get_by_id(e3.id)
        assert fixed_e3.translation == "进入灯塔"
        assert fixed_e3.prev_translation == "进入那座高塔"

        # Undo restores previous translation
        r_undo = client.post(f"/api/projects/{pid}/entries/{e3.id}/restore")
        assert r_undo.status_code == 200
        assert r_undo.json()["entry"]["translation"] == "进入那座高塔"

        # Bulk replace
        r_rep = client.post(f"/api/projects/{pid}/replace", json={"find": "视角", "replace": "镜头"})
        assert r_rep.status_code == 200
        assert r_rep.json()["replaced"] == 1
        assert store.get_by_id(e1.id).translation == "镜头灵敏度"

        # Multi-format text exports (json, txt, bilingual, md, csv)
        for fmt, expected_sub in (
            ("json", "镜头灵敏度"),
            ("txt", "Look sensitivity=镜头灵敏度"),
            ("bilingual", "原文："),
            ("md", "| # | 原文 | 译文 | 状态 |"),
            ("csv", "镜头灵敏度"),
        ):
            r_ex = client.get(f"/api/projects/{pid}/export-text", params={"fmt": fmt, "scope": "done"})
            assert r_ex.status_code == 200, fmt
            text_out = r_ex.content.decode("utf-8-sig")
            assert expected_sub in text_out, (fmt, text_out[:200])
            if fmt == "json":
                parsed = json.loads(text_out)
                assert parsed["count"] == 3

        # Standalone offline patch zip
        r_zip = client.get(f"/api/projects/{pid}/export-patch")
        assert r_zip.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r_zip.content), "r") as zf:
            names = set(zf.namelist())
            assert "BepInEx/config/AutoTranslatorConfig.ini" in names
            assert "BepInEx/Translation/zh/Text/00_UL10nForge.txt" in names
            assert "安装说明.txt" in names
            cfg_txt = zf.read("BepInEx/config/AutoTranslatorConfig.ini").decode("utf-8")
            assert "Endpoint=\n" in cfg_txt
            dict_txt = zf.read("BepInEx/Translation/zh/Text/00_UL10nForge.txt").decode("utf-8")
            assert "Look sensitivity=镜头灵敏度" in dict_txt
            # Default export does NOT emit fragile auto-generated r:"..." regex rules
            assert 'r:"^' not in dict_txt
            # Opt-in regex compilation still works cleanly when requested
            rx_dict, _ex, _sk, rx_cnt = render_xuat_dict_text(store.all_entries(), include_regex=True)
            assert rx_cnt >= 1 and 'r:"^(.+?)' in rx_dict

        # Skip, clear project records (DB + game dict + context), & project removal cleanup
        r_skip = client.post(f"/api/projects/{pid}/entries/{e2.id}/skip")
        assert r_skip.status_code == 200
        assert r_skip.json()["entry"]["status"] == "skipped"

        # Write a game dict file, then clear all project records and confirm both DB & dict are wiped
        client.post(f"/api/projects/{pid}/export-xuat")
        r_clr = client.post(
            f"/api/projects/{pid}/clear",
            json={"clear_game_dict": True, "clear_context": True, "rescan": False},
        )
        assert r_clr.status_code == 200
        clr_body = r_clr.json()
        assert clr_body["cleared"] == 3
        assert clr_body["stats"]["total"] == 0
        assert clr_body["context"]["genre"] == ""
        assert clr_body["cleared_dict_files"] >= 1
        # Reopening the game dir should stay at 0 (old dict does not re-import)
        r_reopen = client.post("/api/projects/open", json={"game_dir": str(game_dir)})
        assert r_reopen.json()["stats"]["total"] == 0

        r_del = client.delete(f"/api/projects/{pid}")
        assert r_del.status_code == 200
        assert not (tmp_path / "data" / "projects" / f"{pid}.db").exists()


def test_v2_2_source_separation_smart_review_il2cpp_safety_and_nocache(tmp_path: Path):
    from ul10n.core.game_detector import GameInfo, select_tmp_bundle_name
    from ul10n.core.hook_deployer import deploy_hook
    from ul10n.engine.reviewer import is_high_risk_for_review, review_batch_and_refine

    # 1. Unity < 2019.1 skips 2019+ TMP bundles to prevent garbled font crashes
    assert select_tmp_bundle_name("5.0.0f4") == ""
    assert select_tmp_bundle_name("2018.4.36f1") == ""
    assert select_tmp_bundle_name("2019.4.8f1") == "notoserif_sdf_u2019"
    assert select_tmp_bundle_name("2021.1.15f1") == "notoserif_sdf_u2019"
    assert select_tmp_bundle_name("2021.3.35f1") == "notoserif_sdf_u2021"

    # 2. IL2CPP hook deploy writes safe BepInEx.cfg + disables fragile stripped UI frameworks
    il2cpp_dir = tmp_path / "Il2CppGame"
    (il2cpp_dir / "Il2CppGame_Data").mkdir(parents=True)
    (il2cpp_dir / "GameAssembly.dll").write_bytes(b"MZ")
    il2cpp_info = GameInfo(
        game_dir=il2cpp_dir,
        name="Il2CppGame",
        executable=il2cpp_dir / "Il2CppGame.exe",
        data_dir=il2cpp_dir / "Il2CppGame_Data",
        runtime_type="il2cpp",
        arch="x86",
        unity_version="2021.1.15f1",
    )
    dep = deploy_hook(il2cpp_info, app_root=APP_ROOT, project_id="p_il2", server_port=18765)
    bep_cfg = (il2cpp_dir / "BepInEx" / "config" / "BepInEx.cfg").read_text(encoding="utf-8")
    assert "UnityLogListening = false" in bep_cfg
    assert "Enabled = false" in bep_cfg
    xuat_cfg = Path(dep.config_path).read_text(encoding="utf-8")
    assert "EnableUGUI=True" in xuat_cfg
    assert "EnableTextMeshPro=True" in xuat_cfg
    assert "EnableTextMesh=False" in xuat_cfg
    assert "EnableUIElements=False" in xuat_cfg
    assert "EnableSafeMode=True" in xuat_cfg
    assert "EnableSilentMode=True" in xuat_cfg

    # 3. Orthogonal source (static vs live) + status filtering & Cache-Control no-store
    game_dir = tmp_path / "SourceSepGame"
    (game_dir / "SourceSepGame_Data" / "Managed").mkdir(parents=True)
    app = create_app(app_root=APP_ROOT, data_dir=tmp_path / "data")
    svc = app.state.services

    with TestClient(app) as client:
        # No-cache headers prevent stale app.js in browser
        r_idx = client.get("/")
        assert "no-store" in r_idx.headers.get("cache-control", "")
        r_js = client.get("/static/app.js")
        assert r_js.status_code == 200
        assert "no-store" in r_js.headers.get("cache-control", "")

        pid = client.post("/api/projects/open", json={"game_dir": str(game_dir)}).json()["project"]["id"]
        store = svc.project_store(pid)
        e_static_tr = store.upsert_entry("Play Game", "开始游戏", status=STATUS_TRANSLATED, source="unity_asset")
        e_static_time = store.upsert_entry("8:00 AM", "上午8:00", status=STATUS_TRANSLATED, source="unity_asset")
        e_live_neg = store.upsert_entry("Do not wake the beast.", "唤醒野兽。", status=STATUS_TRANSLATED, source="xuat_live")
        e_live_long = store.upsert_entry(
            "You have been banished from the citadel until the moon turns crimson.",
            "你已被驱逐出城堡，直到月亮变红。",
            status=STATUS_TRANSLATED,
            source="xuat_live",
        )

        # Orthogonal query checks
        r_all = client.get(f"/api/projects/{pid}/entries", params={"source": "all", "status": "all"}).json()
        assert r_all["total"] == 4
        assert r_all["stats"]["static"] == 2
        assert r_all["stats"]["live"] == 2

        r_static = client.get(f"/api/projects/{pid}/entries", params={"source": "static", "status": "translated"}).json()
        assert r_static["total"] == 2
        assert all(x["source"] != "xuat_live" for x in r_static["entries"])

        r_live = client.get(f"/api/projects/{pid}/entries", params={"source": "live", "status": "translated"}).json()
        assert r_live["total"] == 2
        assert all(x["source"] == "xuat_live" for x in r_live["entries"])

        # Smart high-risk triage: short/time labels skip LLM (0ms), negation & long sentences go to LLM
        assert is_high_risk_for_review(e_static_tr, {}) is False
        assert is_high_risk_for_review(e_static_time, {}) is False
        assert is_high_risk_for_review(e_live_neg, {}) is True
        assert is_high_risk_for_review(e_live_long, {}) is True

        seen_prompts: list[str] = []

        class CapturingReasonClient(FakeLLMClient):
            def chat(self, messages, **kwargs):
                seen_prompts.append(messages[-1]["content"])
                return ChatResult(
                    content='[{"idx": 1, "level": "CRITICAL", "reason": "漏译否定词", "suggestion": "切勿惊动野兽。"}]'
                )

        reason_cli = CapturingReasonClient([])
        tr_cli = FakeLLMClient(["切勿惊动野兽。"])
        ctx = GameContext(
            game_name="SourceSepGame",
            genre="暗黑奇幻 RPG",
            setting="中世纪城堡",
            style="冷峻克制",
            summary="被流放的守夜人重返高塔。",
        )
        res = review_batch_and_refine(
            [e_static_tr, e_static_time, e_live_neg, e_live_long],
            reason_cli,
            tr_cli,
            TranslationPipeline(svc.global_store, store),
            ctx,
            batch_size=10,
            mode="smart",
        )
        assert len(seen_prompts) == 1
        assert "暗黑奇幻 RPG" in seen_prompts[0]
        assert "Play Game" not in seen_prompts[0]
        assert "8:00 AM" not in seen_prompts[0]
        assert "Do not wake the beast." in seen_prompts[0]
        by_id = {u.id: u for u, _v in res}
        assert by_id[e_static_tr.id].review_level == "PASS"
        assert by_id[e_static_time.id].review_level == "PASS"
        assert by_id[e_live_neg.id].review_level == "FIXED"
        assert by_id[e_live_neg.id].translation == "切勿惊动野兽。"
        assert by_id[e_live_long.id].review_level == "PASS"


def test_cloud_api_pipeline_token_batching_and_rereview(tmp_path: Path):
    import json
    import time

    from ul10n.engine.llm_client import (
        extract_json_array,
        extract_json_object,
        normalize_chat_url,
    )
    from ul10n.engine.reviewer import review_batch_and_refine

    # 1. Cloud URL normalization across major OpenAI-compatible providers
    assert normalize_chat_url("https://api.deepseek.com") == "https://api.deepseek.com/v1/chat/completions"
    assert normalize_chat_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1/chat/completions"
    assert (
        normalize_chat_url("https://generativelanguage.googleapis.com/v1beta/openai")
        == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert (
        normalize_chat_url("https://ark.cn-beijing.volces.com/api/v3")
        == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    )
    assert (
        normalize_chat_url("https://open.bigmodel.cn/api/paas/v4")
        == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    )
    assert (
        normalize_chat_url("https://dashscope.aliyuncs.com/compatible-mode/v1")
        == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )

    # 2. Thinking-block stripping before JSON parsing (decoy brackets inside <think> ignored)
    think_obj = '<think>Checking {0} placeholder and [1] index...</think>\n{"1": "开始游戏"}'
    assert extract_json_object(think_obj) == {"1": "开始游戏"}
    think_arr = '<think>Wait [1] looks fine, check [2]</think>\n[{"idx": 2, "level": "MAJOR"}]'
    assert extract_json_array(think_arr) == [{"idx": 2, "level": "MAJOR"}]

    # 3. Cloud batch translation: 0-token local hits + 1 batch call + strict per-item fallback
    game_dir = tmp_path / "CloudGame"
    (game_dir / "CloudGame_Data" / "Managed").mkdir(parents=True)
    app = create_app(app_root=APP_ROOT, data_dir=tmp_path / "data")
    svc = app.state.services

    with TestClient(app) as client:
        # Blank reason_role cleanly inherits translate_role (including real unmasked API key)
        r_set = client.put(
            "/api/settings",
            json={
                "translate_role": {
                    "mode": "api",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "api_key": "sk-cloud-1234",
                },
                "reason_role": {
                    "mode": "api",
                    "base_url": "",
                    "model": "",
                    "api_key": "",
                },
            },
        )
        assert r_set.status_code == 200
        eff_reason = svc.runtime._role_cfg(svc.settings(), "reason")
        assert eff_reason.base_url == "https://api.deepseek.com/v1"
        assert eff_reason.model == "deepseek-chat"
        assert eff_reason.api_key == "sk-cloud-1234"

        pid = client.post("/api/projects/open", json={"game_dir": str(game_dir)}).json()["project"]["id"]
        store = svc.project_store(pid)
        e_builtin = store.upsert_entry("Settings", "", status="pending")
        e_num = store.upsert_entry("999", "", status="pending")
        e_a = store.upsert_entry("Open the rusted gate.", "", status="pending")
        e_b = store.upsert_entry("Deal {0} frost damage.", "", status="pending")
        e_c = store.upsert_entry("A cold wind howls outside.", "", status="pending")

        class CloudBatchFakeClient:
            def __init__(self):
                self.model = "deepseek-chat"
                self.is_cloud = True
                self.calls = 0
                self.total_tokens = 0

            def chat(self, messages, **_kwargs):
                self.calls += 1
                self.total_tokens += 120
                user_txt = messages[-1]["content"]
                # Batch call receives JSON object with keys "1", "2", "3"
                if user_txt.strip().startswith("{"):
                    # Intentionally drop {0} on item "2" to prove per-item fallback rescues it!
                    return ChatResult(
                        content=json.dumps(
                            {
                                "1": "打开生锈的大门。",
                                "2": "造成冰霜伤害。",
                                "3": "外面寒风呼啸。",
                            },
                            ensure_ascii=False,
                        ),
                        prompt_tokens=90,
                        completion_tokens=30,
                    )
                # Fallback single-line retry for item "2"
                if "{0}" in user_txt:
                    return ChatResult(content="造成 {0} 点冰霜伤害。", prompt_tokens=60, completion_tokens=15)
                return ChatResult(content="默认译文")

            def close(self):
                pass

        cloud_cli = CloudBatchFakeClient()
        pipeline = TranslationPipeline(svc.global_store, store)
        outcomes = pipeline.translate_batch_cloud(
            [e_builtin, e_num, e_a, e_b, e_c], cloud_cli
        )
        assert len(outcomes) == 5
        # 1 batch call for 3 lines + 1 targeted fallback call ONLY for the broken placeholder line = 2 calls total
        assert cloud_cli.calls == 2
        assert store.get_by_id(e_builtin.id).translation == "设置"
        assert store.get_by_id(e_a.id).translation == "打开生锈的大门。"
        assert store.get_by_id(e_b.id).translation == "造成 {0} 点冰霜伤害。"
        assert store.get_by_id(e_c.id).translation == "外面寒风呼啸。"

        # 4. Cloud review direct-suggestion fast-path: 0 extra translate_client calls when suggestion is valid
        e_neg = store.upsert_entry("Do not open the chest.", "打开宝箱。", status=STATUS_TRANSLATED)
        tr_calls = {"n": 0}

        class CountingTrClient(FakeLLMClient):
            is_cloud = True
            total_tokens = 0

            def chat(self, messages, **kwargs):
                tr_calls["n"] += 1
                return super().chat(messages, **kwargs)

        class CloudReasonClient(FakeLLMClient):
            is_cloud = True
            total_tokens = 80

        rev_res = review_batch_and_refine(
            [e_neg],
            CloudReasonClient(
                ['[{"idx": 1, "level": "CRITICAL", "reason": "漏译否定词", "suggestion": "不要打开宝箱。"}]']
            ),
            CountingTrClient(["不应该被调用"]),
            pipeline,
            GameContext(game_name="CloudGame"),
            mode="smart",
        )
        assert tr_calls["n"] == 0
        assert rev_res[0][0].translation == "不要打开宝箱。"
        assert rev_res[0][0].review_level == "FIXED"

        # 5. Re-review works cleanly even after all entries already have review_level="PASS"/"FIXED"!
        for e in store.all_entries((STATUS_TRANSLATED,)):
            store.update_entry_by_id(e.id, review_level="PASS")

        with patch.object(
            svc.runtime,
            "create_client_for_role",
            side_effect=lambda role, _cfg: CloudReasonClient(["[]"]) if role == "reason" else CountingTrClient([]),
        ):
            # Smart review automatically re-reviews high-risk entries instead of returning 400!
            r_rerev = client.post(
                f"/api/projects/{pid}/tasks/review",
                json={"limit": 100, "only_unreviewed": True, "mode": "smart"},
            )
            assert r_rerev.status_code == 200
            for _ in range(50):
                cur = client.get("/api/tasks/current").json()["task"]
                if cur and cur["status"] != "running":
                    assert cur["status"] == "completed"
                    break
                time.sleep(0.05)

            # Full review always reviews all translated unlocked entries
            r_full = client.post(
                f"/api/projects/{pid}/tasks/review",
                json={"limit": 100, "only_unreviewed": False, "mode": "full"},
            )
            assert r_full.status_code == 200
            for _ in range(50):
                cur = client.get("/api/tasks/current").json()["task"]
                if cur and cur["status"] != "running":
                    assert cur["status"] == "completed"
                    break
                time.sleep(0.05)


