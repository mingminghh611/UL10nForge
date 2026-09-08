"""A11（midnight-maid-night 黑屏）：场景名语料采集回归测试。

harvest_scene_name_corpus 的教训：函数体里误用裸 `re.IGNORECASE`（本模块
`import re as _re`）→ NameError 被外层「静默回退」except 吞掉 → 真实游戏
语料恒为空集，Layer2 语料门整体哑失效（903 名 + 6 标签全部采不到）。
跳过是哑信号的又一实证。

回归防线：本组用假 UnityPy Environment **真实执行函数内部路径**（含
scene_like 过滤分支），任何内部异常被吞成空集时断言即失败。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


class _FakeEnv:
    """假 UnityPy Environment：记录 load 调用，返回预设对象流。"""

    def __init__(self, objects):
        self._objects = objects
        self.loaded_paths = None
        self.path = None

    def load(self, paths):
        self.loaded_paths = list(paths)

    @property
    def objects(self):
        return iter(self._objects)


def _game_object(name):
    return SimpleNamespace(
        type=SimpleNamespace(name="GameObject"),
        read=lambda: SimpleNamespace(m_Name=name))


def _tag_manager(tags):
    return SimpleNamespace(
        type=SimpleNamespace(name="TagManager"),
        read_typetree=lambda: {"tags": tags})


def _harvest(monkeypatch, tmp_path, asset_names, objects, with_gg=True):
    from hanhua.core.unity import extractor as ux

    data_dir = tmp_path / "Data"
    data_dir.mkdir(exist_ok=True)
    files = []
    for n in asset_names:
        f = data_dir / n
        f.write_bytes(b"\x00" * 16)
        files.append(f)
    gg = data_dir / "globalgamemanagers"
    if with_gg:
        gg.write_bytes(b"\x00" * 16)
    else:
        gg = None

    env = _FakeEnv(objects)
    import UnityPy
    monkeypatch.setattr(UnityPy, "Environment", lambda: env)
    corpus = ux.harvest_scene_name_corpus(files, gg)
    return corpus, env, data_dir


class TestHarvestSceneNameCorpus:
    def test_harvests_names_and_tags(self, monkeypatch, tmp_path):
        # 真实执行内部路径（含 scene_like 过滤分支）：旧 bug（裸 re 引用
        # NameError 被吞）在此直接表现为空集断言失败
        objects = [
            _game_object("Ruth"),
            _game_object("Hallway Trigger"),
            SimpleNamespace(type=SimpleNamespace(name="Transform"),
                            read=lambda: None),  # 非 GameObject 跳过
            _tag_manager(["Ruth", "Naomi", "Crouch"]),
        ]
        corpus, env, _ = _harvest(
            monkeypatch, tmp_path,
            ["level0", "level1", "resources.assets", "sharedassets0.assets"],
            objects)
        assert corpus == frozenset(
            {"Ruth", "Hallway Trigger", "Naomi", "Crouch"})

    def test_scene_filter_loads_only_scene_like_plus_gg(
            self, monkeypatch, tmp_path):
        # E5/E6 内存纪律：只 load level*/unity/sharedassets + gg，
        # resources.assets 不进 load 列表
        objects = [_game_object("SceneObj")]
        corpus, env, data_dir = _harvest(
            monkeypatch, tmp_path,
            ["level0", "resources.assets", "sharedassets0.assets"],
            objects)
        assert corpus == frozenset({"SceneObj"})
        loaded = [Path(p).name for p in env.loaded_paths]
        assert loaded == ["level0", "globalgamemanagers"]
        assert env.path == str(data_dir)

    def test_no_scene_files_falls_back_to_all(self, monkeypatch, tmp_path):
        # 无场景形态文件时退回全量（小游戏资源本就不多）
        objects = [_game_object("FallbackObj")]
        corpus, env, _ = _harvest(
            monkeypatch, tmp_path,
            ["resources.assets", "sharedassets0.assets"], objects)
        assert corpus == frozenset({"FallbackObj"})
        loaded = sorted(Path(p).name for p in env.loaded_paths)
        assert loaded == ["globalgamemanagers", "resources.assets",
                          "sharedassets0.assets"]

    def test_bad_object_silently_skipped(self, monkeypatch, tmp_path):
        # 单对象读失败 → 跳过该对象，其余照常采集（采集是兜底防线，
        # 不因个别坏对象整包放弃）
        def boom():
            raise RuntimeError("bad object")

        objects = [
            SimpleNamespace(type=SimpleNamespace(name="GameObject"),
                            read=boom),
            _game_object("GoodObj"),
        ]
        corpus, _, _ = _harvest(
            monkeypatch, tmp_path, ["level0"], objects)
        assert corpus == frozenset({"GoodObj"})

    def test_over_limit_returns_empty(self, monkeypatch, tmp_path):
        # 超上限（形态误判信号）→ 空集：语料门宁可失明不可被噪声稀释
        objects = [_game_object(f"n{i}") for i in range(10_001)]
        corpus, _, _ = _harvest(
            monkeypatch, tmp_path, ["level0"], objects)
        assert corpus == frozenset()

    def test_load_failure_returns_empty(self, monkeypatch, tmp_path):
        # env.load 抛错 → 空集（退回 IL 模式证明单层防线，不阻断扫描）
        class _BoomEnv(_FakeEnv):
            def load(self, paths):
                raise RuntimeError("corrupt")

        from hanhua.core.unity import extractor as ux
        data_dir = tmp_path / "Data"
        data_dir.mkdir(exist_ok=True)
        files = [data_dir / "level0"]
        files[0].write_bytes(b"\x00" * 16)
        gg = data_dir / "globalgamemanagers"
        gg.write_bytes(b"\x00" * 16)
        import UnityPy
        monkeypatch.setattr(UnityPy, "Environment", _BoomEnv)
        assert ux.harvest_scene_name_corpus(files, gg) == frozenset()
