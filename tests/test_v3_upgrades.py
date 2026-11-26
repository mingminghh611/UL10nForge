"""Regression & feature tests for v3 fixes: security, glossary, memory, KV-cache, source_lang, bundles & bulk ops."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ul10n.core.models import Entry, GameContext, GlossaryItem, STATUS_EDITED, STATUS_TRANSLATED
from ul10n.core.quality import (
    GlossaryMatcher,
    apply_quote_style,
    detect_source_language,
    is_safe_for_global_memory,
    protect_inline_markup,
    restore_inline_markup,
    term_in_text,
)
from ul10n.core.text_scanner import _extract_localization_table_texts
from ul10n.engine.llm_client import ChatResult, normalize_models_url
from ul10n.engine.reviewer import _find_glossary_violations
from ul10n.engine.translator import TranslationPipeline, build_translation_messages
from ul10n.overlay.hud import parse_hotkey_spec
from ul10n.server.app import create_app

APP_ROOT = Path(__file__).resolve().parents[1]


def test_word_boundary_glossary_matcher_and_reviewer_zero_false_positives():
    # Short Latin terms must NEVER match inside unrelated words (Art != Start, Eve != Every)
    assert term_in_text("Art", "Start Game") is False
    assert term_in_text("Art", "Enter the Art Gallery") is True
    assert term_in_text("Lantern", "Two Lanterns glowed in the dark") is True
    assert term_in_text("Eve", "Every morning") is False
    assert term_in_text("魔王", "魔王を倒す") is True

    matcher = GlossaryMatcher(
        project_pairs=[("Art", "阿特"), ("The Keeper", "守灯人"), ("Keeper", "看守")],
        global_pairs=[("Keeper", "全局看守")],
    )
    # Longest match first + project overrides global
    assert matcher.match("Start Game") == []
    hits = matcher.match("Meet The Keeper and Art")
    assert hits[0] == ("The Keeper", "守灯人")
    assert ("Art", "阿特") in hits
    assert matcher.exact_project_lookup("Keeper") == "看守"

    # Reviewer must NOT flag "Start Game" for glossary term "Art=阿特"
    e_ok = Entry(1, "Start Game", "开始游戏", status=STATUS_TRANSLATED)
    assert _find_glossary_violations(e_ok, [GlossaryItem(1, "Art", "阿特")]) == []
    e_bad = Entry(2, "Talk to Art", "和画家交谈", status=STATUS_TRANSLATED)
    assert _find_glossary_violations(e_bad, [GlossaryItem(1, "Art", "阿特")]) == [("Art", "阿特")]


def test_markup_peel_sprite_freeze_quote_style_and_lang_detection():
    # Outer wrapping tags peeled & inline <sprite> frozen cleanly
    src = '<b><color=#ff0>Found <sprite name="coin"> 50 gold!</color></b>'
    masked, outer, frozen = protect_inline_markup(src)
    assert masked == "Found ⟦S0⟧ 50 gold!"
    assert len(outer) == 2 and len(frozen) == 1
    restored = restore_inline_markup("发现了 ⟦S0⟧ 50 枚金币！", outer, frozen)
    assert restored == '<b><color=#ff0>发现了 <sprite name="coin"> 50 枚金币！</color></b>'

    # Quote style normalizer preserves rich-text attributes while unifying prose quotes
    raw_tr = '<color="red">守卫说：“别过去”</color>'
    assert apply_quote_style(raw_tr, "corner") == '<color="red">守卫说：「别过去」</color>'
    assert apply_quote_style('<color="red">「别过去」</color>', "curly") == '<color="red">“别过去”</color>'

    # Source language detection
    assert detect_source_language(["魔王を倒すために旅立つ", "セーブしました"]) == "ja"
    assert detect_source_language(["마왕을 물리치기 위해 떠난다", "저장되었습니다"]) == "ko"
    assert detect_source_language(["Добро пожаловать в игру", "Начать новую игру"]) == "ru"
    assert detect_source_language(["Welcome back, traveler.", "Press Space to Start"]) == "en"

    # Safe localization table extraction helpers (Unity StringTable & I2 LanguageSource)
    assert _extract_localization_table_texts(
        "StringTable", {"m_TableData": [{"m_Localized": "Start Adventure"}, {"m_Localized": ""}]}
    ) == ["Start Adventure"]
    assert _extract_localization_table_texts(
        "LanguageSourceAsset",
        {"mSource": {"mTerms": [{"Languages": ["Open Inventory", "打开背包"]}]}},
    ) == ["Open Inventory"]

    # Overlay hotkey spec parser
    assert parse_hotkey_spec("Alt+Shift+L")[0] == "Alt+Shift+L"
    assert parse_hotkey_spec("F8")[0] == "F8"
    assert parse_hotkey_spec("InvalidKeyName") is None

    # Models URL normalizer
    assert normalize_models_url("https://api.deepseek.com") == "https://api.deepseek.com/v1/models"
    assert normalize_models_url("https://api.siliconflow.cn/v1") == "https://api.siliconflow.cn/v1/models"


def test_kv_cache_invariant_system_prompt_and_safe_cross_game_memory(tmp_path: Path):
    ctx = GameContext(game_name="TestRPG", genre="暗黑奇幻", setting="高塔", style="冷峻克制")
    m1 = build_translation_messages(
        "Raise your Lantern.",
        target_lang="zh-CN",
        model_name="deepseek-chat",
        glossary_hits=[("Lantern", "提灯")],
        context=ctx,
        prev_originals=["The hallway is silent."],
    )
    m2 = build_translation_messages(
        "Open the iron gate.",
        target_lang="zh-CN",
        model_name="deepseek-chat",
        glossary_hits=[("iron gate", "铁门")],
        context=ctx,
        prev_originals=["A key lies on the floor."],
    )
    # System prompt MUST be 100% byte-identical across lines for KV-cache / Prompt-cache hits!
    assert m1[0]["role"] == "system" and m2[0]["role"] == "system"
    assert m1[0]["content"] == m2[0]["content"]
    # Dynamic glossary & neighbors live in user message, with target text on final line
    assert "Lantern=提灯" in m1[1]["content"]
    assert m1[1]["content"].splitlines()[-1] == "Raise your Lantern."

    # Safe cross-game memory: short ambiguous single word never pollutes global memory,
    # and project glossary ALWAYS beats cross-game global memory!
    assert is_safe_for_global_memory("Warden", "ai") is False
    assert is_safe_for_global_memory("Welcome back to the iron lighthouse.", "ai") is True

    app = create_app(app_root=APP_ROOT, data_dir=tmp_path / "data")
    svc = app.state.services
    store = svc.project_store("p_mem")
    # Seed global memory with a translation from another game
    svc.global_store.put_memory("Speak to the Warden.", "与典狱长对话。", "zh-CN", source="manual")
    # Current game defines Warden=守望者 in its project glossary!
    store.upsert_glossary("Warden", "守望者", category="角色")

    class SpyClient:
        model = "deepseek-chat"
        calls = 0

        def chat(self, messages, **_kw):
            self.calls += 1
            return ChatResult(content="与守望者对话。")

        def close(self):
            pass

    cli = SpyClient()
    pipeline = TranslationPipeline(svc.global_store, store)
    oc = pipeline.translate_one("Speak to the Warden.", cli, source="xuat_live")
    assert cli.calls == 1  # Global memory MUST NOT hijack when project glossary term matches!
    assert oc.translation == "与守望者对话。"
    svc.shutdown()


def test_v3_bugfixes_security_source_lang_bulk_impact_bundle_and_live_batch(tmp_path: Path):
    game_dir = tmp_path / "JpGame"
    (game_dir / "JpGame_Data" / "Managed").mkdir(parents=True)
    app = create_app(app_root=APP_ROOT, data_dir=tmp_path / "data")
    svc = app.state.services

    with TestClient(app) as client:
        # 1. Index injects per-process session token meta tag
        r_idx = client.get("/")
        assert r_idx.status_code == 200
        assert 'name="ul10n-token"' in r_idx.text

        # 2. Security guard blocks spoofed Host (DNS rebinding) & cross-site Sec-Fetch-Site
        assert client.get("/api/projects", headers={"Host": "evil.example:18765"}).status_code == 403
        assert client.post(
            "/api/projects/open",
            json={"game_dir": str(game_dir)},
            headers={"Sec-Fetch-Site": "cross-site"},
        ).status_code == 403

        # 3. Bug #1 regression: masked reason_role api_key when reason_role was blank no longer raises NameError!
        r_s1 = client.put(
            "/api/settings",
            json={
                "translate_role": {
                    "mode": "api",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "api_key": "sk-test-4321",
                },
                "reason_role": {"mode": "api", "base_url": "", "model": "", "api_key": ""},
            },
        )
        assert r_s1.status_code == 200
        # Now resubmit masked translate key into reason_role (previously crashed with NameError: _mask_secret)
        r_s2 = client.put(
            "/api/settings",
            json={
                "reason_role": {"mode": "api", "base_url": "", "model": "", "api_key": "••••4321"},
                "quote_style": "corner",
                "overlay_hotkey": "F8",
            },
        )
        assert r_s2.status_code == 200
        assert r_s2.json()["settings"]["quote_style"] == "corner"
        assert r_s2.json()["settings"]["overlay_hotkey"] == "F8"
        assert svc.settings().reason_role.api_key == "sk-test-4321"

        # 4. Bug #8: switching local -> api stops running local llama-server to free VRAM
        client.put("/api/settings", json={"translate_role": {"mode": "local"}})
        stopped: list[str] = []
        with patch.object(svc.runtime, "stop_role", side_effect=lambda r: stopped.append(r)):
            r_sw = client.put(
                "/api/settings",
                json={"translate_role": {"mode": "api", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}},
            )
            assert r_sw.status_code == 200
            assert "translate" in stopped

        # 5. Bug #3: Source language selector writes FromLanguage=ja into AutoTranslatorConfig.ini!
        pid = client.post("/api/projects/open", json={"game_dir": str(game_dir)}).json()["project"]["id"]
        client.post(f"/api/projects/{pid}/deploy", json={})
        r_lang = client.patch(f"/api/projects/{pid}/source-lang", json={"source_lang": "ja"})
        assert r_lang.status_code == 200
        assert r_lang.json()["effective_source_lang"] == "ja"
        cfg_ini = (game_dir / "BepInEx" / "config" / "AutoTranslatorConfig.ini").read_text(encoding="utf-8")
        assert "FromLanguage=ja" in cfg_ini

        # 6. Cloud live micro-batching in POST /xuat/translate packs 3+ misses into ONE cloud API call!
        batch_calls = {"n": 0}

        class LiveCloudBatchClient:
            model = "deepseek-chat"
            is_cloud = True
            total_tokens = 0

            def chat(self, messages, **_kw):
                batch_calls["n"] += 1
                return ChatResult(
                    content=json.dumps(
                        {"1": "铁剑", "2": "治疗药水", "3": "古老地图"},
                        ensure_ascii=False,
                    )
                )

            def close(self):
                pass

        with patch.object(svc.runtime, "create_client_for_role", return_value=LiveCloudBatchClient()):
            r_mb = client.post(
                f"/xuat/translate/{pid}",
                json={"texts": ["Iron Longsword", "Healing Elixir", "Ancient Sea Chart"]},
            )
            assert r_mb.status_code == 200
            assert r_mb.json()["translations"] == ["铁剑", "治疗药水", "古老地图"]
            assert batch_calls["n"] == 1  # ONE batched cloud request instead of 3!

        # 7. Glossary impact count & targeted retranslation
        store = svc.project_store(pid)
        e_lan = store.upsert_entry("Hold the Lantern steady.", "拿稳灯笼。", status=STATUS_TRANSLATED)
        r_gl = client.post(
            f"/api/projects/{pid}/glossary",
            json={"term": "Lantern", "translation": "提灯", "category": "物品"},
        )
        assert r_gl.status_code == 200
        assert r_gl.json()["impacted_count"] == 1

        class RetransClient:
            model = "deepseek-chat"
            is_cloud = False
            total_tokens = 0

            def chat(self, messages, **_kw):
                return ChatResult(content="拿稳提灯。")

            def close(self):
                pass

        with patch.object(svc.runtime, "create_client_for_role", return_value=RetransClient()):
            r_imp_re = client.post(
                f"/api/projects/{pid}/glossary/retranslate-impacted",
                json={"term": "Lantern", "translation": "提灯"},
            )
            assert r_imp_re.status_code == 200
            for _ in range(60):
                cur = client.get("/api/tasks/current").json()["task"]
                if cur and cur["status"] != "running":
                    assert cur["status"] == "completed"
                    break
                time.sleep(0.05)
        assert store.get_by_id(e_lan.id).translation == "拿稳提灯。"

        # 8. Bulk entry actions + extra_filter
        e_tag = store.upsert_entry("<b>Alert {0}</b>", "<b>警报 {0}</b>", status=STATUS_TRANSLATED)
        r_tags = client.get(f"/api/projects/{pid}/entries", params={"extra_filter": "tags"}).json()
        assert any(x["id"] == e_tag.id for x in r_tags["entries"])

        r_bulk = client.post(
            f"/api/projects/{pid}/entries/bulk",
            json={"entry_ids": [e_tag.id], "action": "skip"},
        )
        assert r_bulk.status_code == 200 and r_bulk.json()["changed"] == 1
        assert store.get_by_id(e_tag.id).status == "skipped"

        # 9. Portable `.ul10n.json` bundle export & import (respects locked edits)
        r_bun = client.get(f"/api/projects/{pid}/export-bundle")
        assert r_bun.status_code == 200
        bundle = json.loads(r_bun.content.decode("utf-8"))
        assert bundle["format"] == "ul10nforge-bundle"
        assert len(bundle["glossary"]) >= 1
        assert len(bundle["entries"]) >= 3

        # Import into a fresh second project
        game_dir2 = tmp_path / "JpGameCopy"
        (game_dir2 / "JpGameCopy_Data" / "Managed").mkdir(parents=True)
        pid2 = client.post("/api/projects/open", json={"game_dir": str(game_dir2)}).json()["project"]["id"]
        store2 = svc.project_store(pid2)
        store2.upsert_entry("Hold the Lantern steady.", "手修版本。", status=STATUS_EDITED, locked=True)
        r_imp_b = client.post(f"/api/projects/{pid2}/import-bundle", json={"bundle": bundle})
        assert r_imp_b.status_code == 200
        assert r_imp_b.json()["glossary_added"] >= 1
        assert r_imp_b.json()["entries_added"] >= 3
        # Locked manual edit MUST NOT be overwritten by bundle import!
        assert store2.get_by_original("Hold the Lantern steady.").translation == "手修版本。"

        # 10. Presets API
        r_pre = client.get("/api/runtime/presets")
        assert r_pre.status_code == 200
        assert any(p["id"] == "deepseek" for p in r_pre.json()["presets"])
