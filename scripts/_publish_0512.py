#!/usr/bin/env python3
"""UL10nForge 0.51.2 GitHub Release 发布（2026-09-11）。

0.51.2 是 0.51.1 的稳定性补丁：启动器 bat 中文 Windows 闪退修复 +
翻译中途进程崩溃根治 + 项目切换过渡期 AttributeError 刷屏修复。
0.51.1 release 保留不删，本脚本创建 v0.51.2 新 release 并上传全部分卷。
Models 分卷与 0.51.1 内容一致（模型未变），文件名重打 0.51.2 版本号。

    python scripts/_publish_0512.py                    # 只创建/更新 0.51.2 文案
    python scripts/_publish_0512.py --upload           # 文案 + 上传全部分卷
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
REPO = "mingminghh611/UL10nForge"
TAG = "v0.51.2"
RELEASE_NAME = "UL10nForge 0.51.2 — 启动与长时翻译稳定性修复"

BODY = """# UL10nForge 0.51.2 — 启动与长时翻译稳定性修复

一个**完全离线**的 Unity 游戏汉化工作台：识别 → 翻译 → 审校 → 写回全流程，每一步都有确定性检查与证据留档。

> 本项目由一位编程与游戏汉化的**新手**借助 **AI 辅助**独立开发——欢迎反馈问题（附复现步骤 + 日志）。

## 🔧 0.51.1 → 0.51.2 修复

- **中文 Windows 上双击启动器闪退**：cmd.exe 执行前以 GBK 代码页预读整个 bat——UTF-8 中文注释在 GBK 解读下长度错位 → 行定位漂移、命令碎裂，第二条实质命令就解析失败。启动器重写为**纯 ASCII + CRLF**（中文提示全部移入 GUI），附 `--check` 自检模式与 debug 模式
- **翻译到一半进程崩溃（大游戏更易触发）**：后台任务 Worker 的信号源 QObject 在项目切换时被 Python 垃圾回收 → 工作线程信号发射抛 `RuntimeError: Signal source has been deleted` 三连失败 → 整个进程崩溃。大游戏翻译动辄数小时，中途打开其他游戏/重新扫描的概率高，所以大游戏更容易崩。三层修复：
  - 运行中的任务引用进入「退休列表」保活，等任务自然结束再清理
  - 公共 `Worker.run` 信号发射兜底（信号源已删 = 结果已无人接收，静默放弃，绝不让异常二次抛出）
  - **翻译进行中拒绝重新扫描/打开其他游戏**——防误触取消数小时的翻译进度
- **项目切换过渡期界面刷新 AttributeError 刷屏**：崩溃日志中 728 处 `'Project' object has no attribute 'store'`（非致命但刷屏干扰排查）——生命周期信号回调统一防御性守卫

3397 项全量回归测试通过。模型文件与 0.51.1 完全一致——**Full/Lite 用户无需重新下载模型**，但代码在包内，仍需下载新版整包。

## 📦 下载哪个包？

| 包 | 内容 | 适合谁 |
|---|---|---|
| `UL10nForge-0.51.2-Full.7z.001~004`（4 卷，共约 7.4 GB） | 全部四个模型 + 内置 Python + llama.cpp | **本地离线用户**（默认模式，数据不出本机，解压即用） |
| `UL10nForge-0.51.2-Lite.7z.001~002`（2 卷，共约 2.5 GB） | 不含大模型，只带重排模型（0.6B）+ 运行时 | **在线 API 用户**（翻译/审核/检索走云端接口） |
| `UL10nForge-0.51.2-Models.7z.001~002`（2 卷，共约 4.2 GB） | 三个大模型（翻译 1.8B / 审核 4B / 检索 0.6B） | Lite 用户转本地离线时补齐模型 |

> Full 版已含全部模型，无需再下模型包；Lite + Models 组合等价于 Full。

**安装**：同一目录下载全部分卷 → 用 7-Zip 解压 `.001` → 解压到**纯英文路径** → 双击 `启动UL10nForge.bat`。详见 README「安装与模型下载」章节。

硬件要求：CUDA 显卡 8GB+ 显存推荐（或大内存纯 CPU 模式）。

## 🧪 已知限制（如实告知）

- **识别不全**：拼接/加密/服务器下发/贴图内文字无法识别，未知形态可能漏识别
- **翻译质量有限**：本地 1.8B 小模型，复杂句/文学性表达有限，需人工审校兜底
- **写回可能有 bug**：按键 UI 失灵、游戏卡住等逻辑性问题可能发生——**建议写回前备份原游戏文件**
- 不做实机测试，UI 溢出、字体渲染等运行期问题可能漏检

遇到问题请带复现步骤提 Issue，或加入交流群：**931708916**。

## 📄 文件校验

分卷（SHA256）：

```text
__SHA256__
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


def _create_release(body: str) -> dict:
    data = json.dumps({"tag_name": TAG, "name": RELEASE_NAME,
                       "body": body, "prerelease": False}).encode("utf-8")
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
    if release.get("body") != body:
        patch = Request(
            f"https://api.github.com/repos/{REPO}/releases/{release['id']}",
            data=json.dumps({"body": body}).encode("utf-8"),
            headers=_headers(), method="PATCH")
        with urlopen(patch, timeout=60) as r:
            release = json.loads(r.read().decode("utf-8"))
    return release


def _upload_asset(release_id: int, path: Path, retries: int = 4) -> None:
    url = (f"https://uploads.github.com/repos/{REPO}/releases/"
           f"{release_id}/assets?name={path.name}")
    total = path.stat().st_size
    # 请求体必须是 bytes：传文件对象时 urllib 不计算 Content-Length
    # → GitHub 返回 400 Bad Content-Length（2GB 分卷上传必现）。
    body = path.read_bytes()
    start = time.monotonic()
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        req = Request(url, data=body,
                      headers={**_headers(),
                               "Content-Type": "application/octet-stream"},
                      method="POST")
        try:
            with urlopen(req, timeout=3600) as r:
                result = json.loads(r.read().decode("utf-8"))
            break
        except Exception as exc:  # noqa: BLE001
            # 大分卷直连上传常见瞬断（SSL EOF / 连接重置）——指数退避重试
            last_exc = exc
            wait = min(30 * attempt, 90)
            print(f"[retry {attempt}/{retries}] {path.name}: {exc}"
                  f"——{wait}s 后重试")
            time.sleep(wait)
    else:
        raise RuntimeError(f"上传失败 {path.name}: {last_exc}")
    elapsed = time.monotonic() - start
    print(f"[ok] {path.name} 已上传 "
          f"({total / 1e9:.2f} GB · {total / 1e6 / max(elapsed, 0.1):.1f} MB/s)"
          f" · {result.get('browser_download_url', '')[:90]}")


def _sha256_manifest() -> str:
    lines = (DIST / "SHA256SUMS.txt").read_text(encoding="utf-8")
    return "\n".join(l for l in lines.splitlines() if l.strip())


def main() -> int:
    body = BODY.replace("__SHA256__", _sha256_manifest())
    release = _create_release(body)
    print(f"release: {release.get('html_url')} (id={release.get('id')})")
    if "--upload" not in sys.argv:
        print("[done] release 已就绪；分卷请到网页手动上传"
              "（或加 --upload 自动上传）")
        return 0
    parts = sorted(DIST.glob("UL10nForge-0.51.2-*.7z.*"))
    if not parts:
        print("[FAIL] 分卷不存在：先运行 _package_0512.py")
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
