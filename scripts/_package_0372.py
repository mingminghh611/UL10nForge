#!/usr/bin/env python3
"""UL10nForge 0.37.2 三通道发行打包（2026-09-02）。

复用 package.py 的白名单清单与隐私扫描，产出三个 7z 包（GitHub 单文件
< 2GB，超限自动分卷 .7z.001…）。Models 通道复用 0.37.1 已产出的分卷
（三通道口径：Full=全部 / Lite=排除 3 大模型 / Models=只含 3 大模型；
模型文件 0.37.1→0.37.2 无变化，0.37.2 代码更新不影响模型包）。

    python scripts/_package_0372.py              # 打包 + 验证 + SHA256
    python scripts/_package_0372.py --verify     # 只验证已有分卷完整性
    python scripts/_package_0372.py --reuse-models   # Models 复用 0.37.1 分卷（重命名）
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
VERSION = "0.37.2"
OLD_VERSION = "0.37.1"
SEVEN_ZIP = Path(r"C:\Program Files\7-Zip\7z.exe")
# GitHub Release 单文件上限 2GB（必须严格小于 2147483648）
VOLUME_BYTES = 2 * 1024 * 1024 * 1024 - 1024 * 1024

# 三通道（2026-09-01 用户口径，F4）：
#   Full   = 全部内容（含四个模型）
#   Lite   = 不含大模型，只留重排（在线 API 用户；重排恒本地）
#   Models = 三个大模型打包（翻译+审核+检索，Lite 用户补齐用；重排已在 Lite 里）
CHANNELS = {
    "Full": None,  # None = 不过滤
    "Lite": {"Hy-MT2-1.8B-Q6_K.gguf",
             "Qwen3.5-4B-Q4_K_M.gguf",
             "Qwen3-Embedding-0.6B-Q8_0.gguf"},
    # 注意：Models 用正确文件名 Qwen3-Embedding-0.6B-Q8_0.gguf
    # （0.37.1 脚本里的 .Q8_0 是 typo——当时磁盘文件名已是 -Q8_0，
    # __ONLY__ 集合里的错误名匹配不到任何文件，实际打包的是运行时）。
    "Models": {"__ONLY__", "Hy-MT2-1.8B-Q6_K.gguf",
               "Qwen3.5-4B-Q4_K_M.gguf",
               "Qwen3-Embedding-0.6B-Q8_0.gguf"},
}


def _collect(exclude: set[str] | None) -> list[Path]:
    sys.path.insert(0, str(ROOT / "scripts"))
    from package import _scan_privacy, _walk_include
    files = _walk_include()
    if exclude is None:
        selected = files
    elif "__ONLY__" in exclude:
        selected = [f for f in files if f.name in exclude]
    else:
        selected = [f for f in files if f.name not in exclude]
    issues = _scan_privacy(selected)
    if issues:
        print(f"[FAIL] 隐私风险 {len(issues)} 处：")
        for issue in issues[:20]:
            print(f"  - {issue}")
        sys.exit(1)
    return selected


def _build(channel: str, exclude: set[str] | None) -> Path:
    files = _collect(exclude)
    total = sum(f.stat().st_size for f in files)
    out = DIST / f"UL10nForge-{VERSION}-{channel}.7z"
    print(f"[{channel}] {len(files)} 文件 {total / 1e9:.2f} GB")
    volumes = 1 + total // VOLUME_BYTES
    if volumes > 1:
        print(f"  超过 2GB，分 {volumes} 卷")
    listfile = DIST / f"_list_{channel}.txt"
    listfile.write_text(
        "\n".join(f.relative_to(ROOT).as_posix() for f in files),
        encoding="utf-8")
    cmd = [str(SEVEN_ZIP), "a", "-t7z", f"-v{VOLUME_BYTES}b",
           "-mx0", "-y", str(out), f"@{listfile}"]
    start = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print((proc.stdout or "")[-400:].encode(
            "gbk", errors="replace").decode("gbk"))
        raise SystemExit(f"[FAIL] {channel} 7z 退出码 {proc.returncode}")
    print(f"  打包完成，耗时 {time.monotonic() - start:.0f}s")
    return out


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify(channel: str) -> bool:
    parts = sorted(DIST.glob(f"UL10nForge-{VERSION}-{channel}.7z*"))
    if not parts:
        print(f"[FAIL] {channel} 没有产物")
        return False
    print(f"[验证] {channel}：{len(parts)} 个分卷")
    for p in parts:
        print(f"  {p.name}  {p.stat().st_size / 1e9:.2f} GB")
    test = subprocess.run(
        [str(SEVEN_ZIP), "t", str(parts[0])],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    if test.returncode != 0 or "Everything is Ok" not in (test.stdout or ""):
        print(f"[FAIL] {channel} 完整性验证失败："
              f"{(test.stdout or '')[-300:]}")
        ok = False
    else:
        print(f"  [ok] 完整性验证通过（Everything is Ok）")
        ok = True
    return ok


def _reuse_models() -> bool:
    """复用 0.37.1 Models 分卷（模型文件 0.37.1→0.37.2 无变化）：
    复制旧卷为 0.37.2 名并核对分卷数/校验。返回是否成功。
    """
    old = sorted(DIST.glob(f"UL10nForge-{OLD_VERSION}-Models.7z*"))
    if not old:
        print(f"[reuse] 没有 0.37.1 Models 分卷可复用，走重新打包")
        return False
    for p in old:
        new = DIST / p.name.replace(OLD_VERSION, VERSION)
        if new.exists():
            print(f"[reuse] {new.name} 已存在，跳过")
            continue
        print(f"[reuse] {p.name} → {new.name}")
        shutil.copy2(p, new)
    return _verify("Models")


def main() -> int:
    DIST.mkdir(exist_ok=True)
    if "--verify" in sys.argv:
        return 0 if all(_verify(c) for c in CHANNELS) else 1
    if "--reuse-models" in sys.argv and _reuse_models():
        channels = [c for c in CHANNELS if c != "Models"]
    else:
        channels = list(CHANNELS)

    print("[隐私扫描 + 清单] …")
    manifest: list[str] = []
    for channel in channels:
        out = _build(channel, CHANNELS[channel])
        parts = sorted(DIST.glob(f"{out.stem}.7z*"))
        for p in parts:
            manifest.append(f"{p.name}  {_sha256(p)}")
    if "Models" in CHANNELS and "Models" not in channels:
        # reuse 分支：Models 卷名已就位，补其校验
        for p in sorted(DIST.glob(f"UL10nForge-{VERSION}-Models.7z*")):
            manifest.append(f"{p.name}  {_sha256(p)}")
    (DIST / "SHA256SUMS.txt").write_text(
        "\n".join(manifest) + "\n", encoding="utf-8")
    print("\n[SHA256]")
    print("\n".join(manifest))
    print("\n[done] 全部通道打包完成")
    return 0 if all(_verify(c) for c in CHANNELS) else 1


if __name__ == "__main__":
    sys.exit(main())
