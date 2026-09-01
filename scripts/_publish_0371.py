#!/usr/bin/env python3
"""UL10nForge 0.37.1 GitHub Release 发布。

默认只创建 v0.37.1 release（带完整发布文案 + SHA256 校验清单），
分卷由用户在 GitHub 网页手动上传（2026-09-01 用户指令）。

    python scripts/_publish_0371.py              # 创建/更新 release 文案
    python scripts/_publish_0371.py --upload     # 文案 + 自动上传全部分卷
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
REPO = "mingminghh611/UL10nForge"
TAG = "v0.37.1"
RELEASE_NAME = "UL10nForge 0.37.1 — 识别收敛与语境直连"

BODY = """# UL10nForge 0.37.1

一个**完全离线**的 Unity 游戏汉化工作台：识别 → 翻译 → 审校 → 写回全流程，每一步都有确定性检查与证据留档。

> 本项目由一位编程与游戏汉化的**新手**借助 **AI 辅助**独立开发——欢迎反馈问题（附复现步骤 + 日志）。

## 📦 下载哪个包？

| 包 | 内容 | 适合谁 |
|---|---|---|
| `UL10nForge-0.37.1-Full.7z.001~004`（4 卷，共约 7.3 GB） | 全部四个模型 + 内置 Python + llama.cpp | **本地离线用户**（默认模式，数据不出本机，解压即用） |
| `UL10nForge-0.37.1-Lite.7z.001~002`（2 卷，共约 2.4 GB） | 不含大模型，只带重排模型（0.6B）+ 运行时 | **在线 API 用户**（翻译/审核/检索走云端接口） |
| `UL10nForge-0.37.1-Models.7z.001~002`（2 卷，共约 4.2 GB） | 三个大模型（翻译 1.8B / 审核 4B / 检索 0.6B） | Lite 用户转本地离线时补齐模型 |

> Full 版已含全部模型，无需再下模型包；在线 API 模式只有**重排**这一个 0.6B 轻量任务恒走本地，Lite 版已附带。Lite + Models 组合等价于 Full。

**安装**：同一目录下载全部分卷 → 用 7-Zip 解压 `.001` → 解压到**纯英文路径** → 双击 `启动UL10nForge.bat`。详见 README「安装与模型下载」章节。

硬件要求：CUDA 显卡 8GB+ 显存推荐（或大内存纯 CPU 模式）。

## ✨ 本版重点（0.37.0 → 0.37.1）

- **识别收敛**（0.37.0）：JSON 数据区（hex 颜色/数值公式/资源引用）结构化过滤 + Mono DLL 调试/结构串拦截（动画状态名、图层名、持久化键名、输入按键绑定）——不多识别，不翻坏游戏逻辑
- **语境识别直连**（0.37.0）：游戏语境识别真正注入翻译与审校 prompt
- **headless 闭环补齐**（0.37.1）：`all_record_runner.py` 命令行一键闭环与 GUI 同链路注入游戏语境
- **IL2CPP 交互提示**（0.37.1）：`[PICK UP]` 式方括号动作标签补齐证明链，可翻译可写回
- 全链路回归 + 真实游戏锚点矩阵（显示文本必进池 + 结构串必跳过）全部通过

## 🧪 已知限制（如实告知）

- **识别不全**：拼接/加密/服务器下发/贴图内文字无法识别，未知形态可能漏识别
- **翻译质量有限**：本地 1.8B 小模型，复杂句/文学性表达有限，需人工审校兜底
- **写回可能有 bug**：按键 UI 失灵、游戏卡住等逻辑性问题可能发生——**建议写回前备份原游戏文件**
- 不做实机测试，UI 溢出、字体渲染等运行期问题可能漏检

遇到问题请带复现步骤提 Issue，或加入交流群：**931708916**。

## 📄 文件校验

分卷（SHA256）：

```text
UL10nForge-0.37.1-Full.7z.001  2d3bd5d90f66d248f1013590a2d12342d5b175bc5c6d3645c55aaa36a1a4ec50
UL10nForge-0.37.1-Full.7z.002  26c28a5f69beea2c789ded929b669caf686dad1b1d024d21b77b3f42602a8663
UL10nForge-0.37.1-Full.7z.003  526cf7ced4de44b4403b9e8c7c7cfdb299c1a82a1f26e17e51de3c90fa0f57fa
UL10nForge-0.37.1-Full.7z.004  0991df90b7f37b0064736c0d0a6c4d23699699fe875ff3a791c8a73f8bb0e756
UL10nForge-0.37.1-Lite.7z.001  2c813a9a64af9bea133cbf677044642b5c69a0b9d75e62992c62e6428992e30d
UL10nForge-0.37.1-Lite.7z.002  0aeb8a562627d8d25e69ab10cfd708549ca551ea3b431c9af679fe820ff42813
UL10nForge-0.37.1-Models.7z.001  50dd0f29594cd987ea03dea9eb0c50b279b40bf33bb1c24b82da57c65465b9c7
UL10nForge-0.37.1-Models.7z.002  922434628bcf61c0d3fa8b8a09299400313d7767abdcd074a5189066da2aec53
```
"""


def _token() -> str:
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        stdout=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace")
    for line in (proc.stdout or "").splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    raise RuntimeError("无法从 git 凭据获取 token")


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}",
            "User-Agent": "UL10nForge-release",
            "X-GitHub-Api-Version": "2022-11-28"}


def _create_release() -> dict:
    data = json.dumps({"tag_name": TAG, "name": RELEASE_NAME,
                       "body": BODY, "prerelease": True}).encode("utf-8")
    req = Request(f"https://api.github.com/repos/{REPO}/releases",
                  data=data,
                  headers={**_headers(),
                           "Accept": "application/vnd.github+json"},
                  method="POST")
    try:
        with urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        # HTTPError 的 str 不含响应体——必须读 body 才能看到 already_exists
        detail = ""
        if hasattr(exc, "read"):
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
        if "already_exists" not in str(exc) + detail:
            raise
    # 已存在 → 取 id 并同步文案
    req = Request(
        f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}",
        headers=_headers())
    with urlopen(req, timeout=60) as r:
        release = json.loads(r.read().decode("utf-8"))
    if release.get("body") != BODY:
        patch = Request(
            f"https://api.github.com/repos/{REPO}/releases/{release['id']}",
            data=json.dumps({"body": BODY}).encode("utf-8"),
            headers=_headers(), method="PATCH")
        with urlopen(patch, timeout=60) as r:
            release = json.loads(r.read().decode("utf-8"))
    return release


def _upload_asset(release_id: int, path: Path) -> None:
    url = (f"https://uploads.github.com/repos/{REPO}/releases/"
           f"{release_id}/assets?name={path.name}")
    total = path.stat().st_size
    start = time.monotonic()
    req = Request(url, data=path.open("rb"),
                  headers={**_headers(),
                           "Content-Type": "application/octet-stream"},
                  method="POST")
    try:
        with urlopen(req, timeout=3600) as r:
            result = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"上传失败 {path.name}: {exc}") from exc
    elapsed = time.monotonic() - start
    print(f"[ok] {path.name} 已上传 "
          f"({total / 1e9:.2f} GB · {total / 1e6 / max(elapsed, 0.1):.1f} MB/s)"
          f" · {result.get('browser_download_url', '')[:90]}")


def main() -> int:
    release = _create_release()
    print(f"release: {release.get('html_url')} (id={release.get('id')})")
    if "--upload" not in sys.argv:
        print("[done] release 已就绪；分卷请到网页手动上传"
              "（或加 --upload 自动上传）")
        return 0
    parts = sorted(DIST.glob("UL10nForge-0.37.1-*.7z.*"))
    if not parts:
        print("[FAIL] 分卷不存在：先运行 _package_0371.py")
        return 1
    existing = {a["name"] for a in release.get("assets", [])}
    for part in parts:
        if part.name in existing:
            print(f"[skip] {part.name} 已存在")
            continue
        try:
            _upload_asset(release["id"], part)
        except RuntimeError as exc:
            print(f"[FAIL] {exc}")
            return 1
    print("[done] 全部分卷上传完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
