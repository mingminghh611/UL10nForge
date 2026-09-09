#!/usr/bin/env python3
"""UL10nForge 0.51.0 GitHub Release 发布。

默认只创建 v0.51.0 release（带完整发布文案 + SHA256 校验清单）。
Models 分卷与 0.37.1 内容一致（模型未变），文件名重打 0.51.0 版本号。

    python scripts/_publish_0510.py              # 创建/更新 release 文案
    python scripts/_publish_0510.py --upload     # 文案 + 自动上传全部分卷
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
TAG = "v0.51.0"
RELEASE_NAME = "UL10nForge 0.51.0 — 工具第二个公开版本：全面升级"

BODY = """# UL10nForge 0.51.0 — 第二个公开版本

一个**完全离线**的 Unity 游戏汉化工作台：识别 → 翻译 → 审校 → 写回全流程，每一步都有确定性检查与证据留档。

> 本项目由一位编程与游戏汉化的**新手**借助 **AI 辅助**独立开发——欢迎反馈问题（附复现步骤 + 日志）。

距离首个公开测试版 **0.30.0**（2026-08-15）一个多月，期间迭代 20+ 个内部版本、修复大量真实游戏实测问题、回归测试增长到 **3433 项**。这一版的主线可以概括成三句话：**写回更安全、识别更全、跑得更快更稳**——外加一个全新的在线 API 模式。

## ✨ 相对 0.30.0 的整体提升

### 1️⃣ 写回安全：从「能写回」到「写不坏」
写坏一个游戏的方式比想象中多——这一版把它们逐个根治并建立了多层防线：
- **按键失灵/游戏卡死**：对象名保护、事件绑定保护（UnityEvent / 输入轴 / FMOD 路径）、逻辑绑定字符串识别拦截——逻辑标识绝不翻译
- **场景写坏黑屏**：三个独立根因全部闭环（字体资产体积暴涨 / 资源布局重排错位 / 逻辑比较键被译导致运行时恒 false）
- **IL2CPP 变长写回**：译文比原文长不再只能截断——原位扩容 → 全局紧凑重建 → 诚实拒绝三层链路，装不下宁可整条跳过（宁漏勿坏）
- **写回 AI 升级**：AI 参与写回分诊（只有跳过权、没有多写权）+ 二进制证据卡 + 预演（Dry Run）
- **审计自动修复闭环**：写回审计发现 FAIL 自动修复（只改库内译文处置，绝不碰磁盘字节）

### 2️⃣ 识别能力：更全、更透明
- **AI 辅助召回**：确定性规则拿不准的文本交给模型结合现场证据判定——0.30.0 时代大量「看起来像代码」的按钮文本 / 短对话被误跳过，现在能被捞回翻译池
- **引擎字符串吸收层**：il2cpp 引擎日志 / 异常串等引擎残留被识别并排除，不再混进翻译池浪费算力
- **跳过率告警**：被跳过的文本从「哑信号」变为可见可排查——识别漏了你能看见
- **未知形态明示阻断**：检测不到的文本形态诚实报告，绝不假报成功

### 3️⃣ 翻译与审校：更懂这个游戏
- **游戏语境自动识别**：扫描后自动抽样 → 模型识别游戏档案（类型 / 题材 / 角色 / 术语 / 风格）→ 注入每一次翻译与审校——模型知道自己在翻一个花札游戏，而不是对着孤立字符串猜
- **审核误阻断治理**：数字等值（200.000=20 万）、平行语言列、占位符等大量「假罪证」误拦根治；梗文 / 低质量原文豁免但真坏译文照拦
- **四级审核 + 反馈重译**：PASS / MINOR / MAJOR / CRITICAL 分级，MAJOR 以上带理由自动重译（≤2 轮收敛）
- **术语沉淀防污染**：沉淀的术语带语境保护，杜绝「Deck 在所有游戏里被强制成牌组」式跨游戏污染
- **XUAT 词典导出**（新能力）：已有 XUnity.AutoTranslator 环境的游戏可零写坏接入——工具只导出词典，替换交给成熟运行时完成

### 4️⃣ 性能与内存：大游戏不再劝退
- **扫描提速**：多个 O(N²) 性能根因根治（逐条数据库查询 / 循环内重编正则 / 全量重扫），大目录扫描时间与峰值内存显著下降
- **内存暴涨根治**：字体阶段节点树预筛（毫秒级替代病态解析）、写回验证指纹流式处理——全程不再出现内存锯齿
- **翻译提速**：GPU 自动分担（显存放不下自动部分层卸载给 CPU）+ HTTP 持久连接复用

### 5️⃣ 全新：在线 API 模式
不想下载 7GB 模型？现在可以只下 2.5GB 轻量版，翻译 / 审核 / 检索三张云端 API 卡各自配置端点（可指向不同服务），全链路已接通；只有重排这一个 0.6B 轻量任务恒走本地。

### 6️⃣ 体验与可追溯
- 设置页 / 术语库页 / 翻译页 / 审校页全面重做（信息分级、进度 3-3-3-1 统一、来源文件筛选）
- **全量运行日志**：每个环节的每个事件单一出口落盘（`~/.hanhua/run-logs/`），出问题可完整回溯
- 命令行一键闭环 `python scripts/all_record_runner.py "<游戏目录>"`，与 GUI 同链路同质量

> 0.37.2 之后的逐版本详细变更见仓库 README「版本」章节。

## 📦 下载哪个包？

| 包 | 内容 | 适合谁 |
|---|---|---|
| `UL10nForge-0.51.0-Full.7z.001~004`（4 卷，共约 7.4 GB） | 全部四个模型 + 内置 Python + llama.cpp | **本地离线用户**（默认模式，数据不出本机，解压即用） |
| `UL10nForge-0.51.0-Lite.7z.001~002`（2 卷，共约 2.5 GB） | 不含大模型，只带重排模型（0.6B）+ 运行时 | **在线 API 用户**（翻译/审核/检索走云端接口） |
| `UL10nForge-0.51.0-Models.7z.001~002`（2 卷，共约 4.2 GB） | 三个大模型（翻译 1.8B / 审核 4B / 检索 0.6B） | Lite 用户转本地离线时补齐模型 |

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
UL10nForge-0.51.0-Full.7z.001  046a62e925e5ca80ed3306011672745b80abbfb15eb94ed973c476695f20566f
UL10nForge-0.51.0-Full.7z.002  cf9c2b4bd189327cb513cf05100b1addacef64f50a31c776442b1efe215b3e36
UL10nForge-0.51.0-Full.7z.003  6a0bd12023515fcbd827f1d05813dadc4ddda7aa7ec1169062cd1bcd14acc997
UL10nForge-0.51.0-Full.7z.004  11202e61bde873f41a6db4b15f14323e656ecab27fdb62dabcb0f9baa39f93f3
UL10nForge-0.51.0-Lite.7z.001  607b5cc223fb31fa080c10c3c3b321ae122e5e6903bd2b756ff301fdda2d1731
UL10nForge-0.51.0-Lite.7z.002  c5d6ce671b80dd887b2649b111031686048f469c1c5e150cad18e8184a01b971
UL10nForge-0.51.0-Models.7z.001  50dd0f29594cd987ea03dea9eb0c50b279b40bf33bb1c24b82da57c65465b9c7
UL10nForge-0.51.0-Models.7z.002  922434628bcf61c0d3fa8b8a09299400313d7767abdcd074a5189066da2aec53
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
    # 请求体必须是 bytes：传文件对象时 urllib 不计算 Content-Length
    # → GitHub 返回 400 Bad Content-Length（2GB 分卷上传必现）。
    body = path.read_bytes()
    req = Request(url, data=body,
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
    parts = sorted(DIST.glob("UL10nForge-0.51.0-*.7z.*"))
    if not parts:
        print("[FAIL] 分卷不存在：先运行 _package_0510.py")
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
