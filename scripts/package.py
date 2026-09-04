#!/usr/bin/env python3
"""UL10nForge 发行包打包脚本。

白名单制：只打包运行时必需的目录/文件；排除一切可再生产物
（__pycache__/logs/*.db/运行时缓存）与隐私风险（工作产物、绝对
路径、开发脚本）。打包完成后自动做隐私扫描 + 完整性验证，缺一
项即非零退出，防止「打包成功但跑不起来」的假成功。

用法：
    python scripts/package.py                # 打包（ZIP_DEFLATED）
    python scripts/package.py --stored       # 不压缩（快，适合 GGUF 已压）
    python scripts/package.py --check        # 只验证上次包（解压到临时目录核对）
    python scripts/package.py --out dist/x.zip   # 指定输出路径（默认 dist/UL10nForge-<date>.zip）
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── 白名单：顶层目录/文件（只打这些） ─────────────────────────
INCLUDE_TOP = [
    "hanhua",           # 全部代码
    "models",           # 4 个 GGUF
    "runtime",          # 内置 Python + llama.cpp
    "fonts",            # 字体链（SDF/中文/TMP bundle）
    "resources",        # tools_manifest.json + font_override 插件载荷
    "tools",            # bmfont + Il2CppDumper（exe/config）
    "scripts",          # 仅 headless 生产入口（文件级白名单裁剪）
    "main.py",
    "requirements.txt",
    "启动UL10nForge.bat",
    "README.md",
]

# scripts 目录文件级白名单（2026-08-15）：只带 all_record_runner.py
# （README 宣传的命令行一键闭环入口，仅依赖 hanhua/stdlib）——
# 其余 50+ 开发/排查脚本不入发行包（避免内部调试内容外泄）
SCRIPTS_FILES_ONLY = {"all_record_runner.py"}

# 隐私扫描跳过目录：runtime 是官方 Python 发行物 + 第三方库
# （pip/httpx 等文档示例含 password= 等形态，非用户数据）
PRIVACY_SKIP_PREFIX = ("runtime",)

# ── 全局排除：可再生产物 / 工作产物 / 隐私风险 ───────────────
GLOBAL_EXCLUDE_DIRS = {
    "__pycache__", ".pytest_cache", ".git", "projects", "tests", "eval",
    "scripts", "font_plugin", "logs",
}
GLOBAL_EXCLUDE_FILES = {
    "review_runtime.json", "coord_runtime_embed.json", "*.db",
    "*.pyc", ".DS_Store", "Thumbs.db",
}

# 目录内白名单（子目录裁剪）
DIR_INCLUDE_ONLY = {
    "tools": ["bmfont", "Il2CppDumper"],
    "docs": None,               # None = 全部（all record 在下方按名排除）
    # 字体链（2026-09-04 D1 复发根治收尾）：SDF_Font_Asset 与
    # TMP_Font_AssetBundles 是静态替换/插件 bundle 源；SimplifiedChinese
    # 只带 TTF 运行时字体源。CFF OTF（24MB）仅是 scripts/
    # convert_cff_to_ttf.py 的开发机转换源，运行时不读、不入发行包
    # （FONT_OPTIONS 白名单只认 TTF；部署闸门拒绝 OTTO 载荷）。
    "fonts": ["SDF_Font_Asset", "SimplifiedChinese",
              "TMP_Font_AssetBundles"],
}

# fonts 目录文件级白名单：SimplifiedChinese 下只带 TTF（OTF 是
# CFF→TTF 转换器输入，运行时不需要）
FONTS_KEEP = {
    "NotoSerifCJKsc-Medium.ttf",
    "NotoSerifCJKsc-Medium SDF.asset",
}
DOCS_EXCLUDE_DIRS = {"all record", "fail record"}

# 文件内白名单（Il2CppDumper：只带运行 exe + 配置；ghidra/ida 脚本是
# 人工逆向辅助，非运行时依赖——打包排除减小体积）
TOOL_IL2CPP_KEEP = {
    "Il2CppDumper.exe", "Il2CppDumper-x86.exe", "config.json",
}

# 隐私扫描：出现即失败（打包产物中禁止用户机器路径/密钥形态）
PRIVACY_PATTERNS = [
    re.compile(r"[A-Za-z]:\\Users\\", re.I),        # 用户目录绝对路径
    re.compile(r"/Users/"),                          # Unix 用户目录
    re.compile(r"api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9]{16,}"),  # API Key 形态
    re.compile(r"sk-[A-Za-z0-9]{16,}"),              # OpenAI 风格密钥
    re.compile(r"password\s*[:=]\s*['\"][^'\"]+['\"]", re.I),
    re.compile(r"token\s*[:=]\s*['\"][A-Za-z0-9]{24,}['\"]", re.I),
]


def _walk_include() -> list[Path]:
    """白名单遍历 → 待打包文件（相对 ROOT）。"""
    files: list[Path] = []
    for top in INCLUDE_TOP:
        path = ROOT / top
        if path.is_file():
            files.append(path)
            continue
        if not path.is_dir():
            print(f"[skip] 缺失: {top}")
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            dirpath_p = Path(dirpath)
            rel = dirpath_p.relative_to(ROOT)
            # 全局目录排除
            dirnames[:] = [
                d for d in dirnames
                if d not in GLOBAL_EXCLUDE_DIRS
                and not any(part in GLOBAL_EXCLUDE_DIRS for part in rel.parts)
                and rel.parts and rel.parts[0] != "docs"
                or (rel.parts[0] == "docs"
                    and d not in DOCS_EXCLUDE_DIRS)]
            # 目录内裁剪
            if rel.parts:
                only = DIR_INCLUDE_ONLY.get(rel.parts[0])
                if only is not None and len(rel.parts) == 1:
                    dirnames[:] = [d for d in dirnames if d in only]
            for name in filenames:
                if name in GLOBAL_EXCLUDE_FILES \
                        or any(name.endswith(ext) for ext in
                               (".pyc",)):
                    continue
                # scripts 文件级白名单：只带 headless 生产入口
                if rel.parts and rel.parts[0] == "scripts" \
                        and name not in SCRIPTS_FILES_ONLY:
                    continue
                # fonts 文件级白名单：SimplifiedChinese 只带 TTF 运行时源
                if rel.parts[:2] == ("fonts", "SimplifiedChinese") \
                        and name not in FONTS_KEEP:
                    continue
                # Il2CppDumper 白名单
                if rel.parts[:2] == ("tools", "Il2CppDumper") \
                        and name not in TOOL_IL2CPP_KEEP:
                    continue
                files.append(dirpath_p / name)
    return sorted(files)


def _scan_privacy(files: list[Path]) -> list[str]:
    """打包内容隐私扫描：绝对路径/密钥形态 → 违规清单。"""
    issues: list[str] = []
    for path in files:
        if path.relative_to(ROOT).parts[0] in PRIVACY_SKIP_PREFIX:
            continue  # 第三方发行物：示例文档字符串非用户数据
        if path.suffix.lower() not in {".py", ".json", ".md", ".txt",
                                       ".bat", ".ps1", ".ini", ".xml",
                                       ".csv", ".tsv", ".html", ".yml",
                                       ".yaml", ".toml", ".cfg", ".conf"}:
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            continue  # 大二进制不扫
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in PRIVACY_PATTERNS:
            match = pattern.search(text)
            if match:
                snippet = text[max(0, match.start() - 20):match.end() + 20]
                issues.append(f"{path}: {pattern.pattern[:30]}… {snippet!r}")
                break
    return issues


def _build_zip(files: list[Path], out: Path, stored: bool) -> None:
    compress = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    start = time.monotonic()
    total_bytes = sum(f.stat().st_size for f in files)
    written = 0
    with zipfile.ZipFile(out, "w", compression=compress,
                         allowZip64=True) as zf:
        for i, path in enumerate(files):
            arc = path.relative_to(ROOT).as_posix()
            zf.write(path, arc)
            written += path.stat().st_size
            if i % 200 == 0:
                pct = written / total_bytes * 100 if total_bytes else 100
                print(f"\r  [{pct:5.1f}%] {i + 1} 文件 "
                      f"{written / 1e6:.0f}/{total_bytes / 1e6:.0f} MB",
                      end="", flush=True)
    elapsed = time.monotonic() - start
    size = out.stat().st_size
    print(f"\n[ok] 包：{out.name}  {size / 1e6:.0f} MB "
          f"({len(files)} 文件，压缩率 {size / total_bytes * 100:.0f}%，"
          f"耗时 {elapsed:.0f}s)")


def _verify_archive(out: Path) -> tuple[bool, list[str]]:
    """解压到临时目录，核对运行时关键文件齐全。"""
    required = [
        "main.py",
        "启动UL10nForge.bat",
        "hanhua/__main__.py",
        "hanhua/ui/main_window.py",
        "models/Hy-MT2-1.8B-Q6_K.gguf",
        "models/Qwen3.5-4B-Q4_K_M.gguf",
        "models/Qwen3-Reranker-0.6B.Q8_0.gguf",
        "models/Qwen3-Embedding-0.6B-Q8_0.gguf",
        "runtime/llama/llama-server.exe",
        "runtime/python/python.exe",
        "tools/bmfont/bmfont64.com",
        "tools/Il2CppDumper/Il2CppDumper.exe",
        "resources/tools_manifest.json",
        "resources/font_override/Hanhua.FontFallback.dll",
    ]
    missing: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(out) as zf:
            zf.extractall(tmp)
        tmp_root = Path(tmp)
        for rel in required:
            if not (tmp_root / rel).is_file():
                missing.append(rel)
    if missing:
        print("[FAIL] 关键文件缺失：")
        for m in missing:
            print(f"  - {m}")
        return False, missing
    print(f"[ok] 完整性：{len(required)} 项关键文件全部就位")
    return True, []


def main() -> int:
    parser = argparse.ArgumentParser(description="UL10nForge 打包脚本")
    parser.add_argument("--stored", action="store_true",
                        help="不压缩（GGUF 已压缩，更快）")
    parser.add_argument("--out", type=Path, default=None,
                        help="输出路径（默认 dist/UL10nForge-<日期>.zip）")
    parser.add_argument("--check", action="store_true",
                        help="只验证最新包，不重新打包")
    args = parser.parse_args()

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = args.out or dist / time.strftime("UL10nForge-%Y%m%d.zip")

    if args.check:
        if not out.is_file():
            print(f"[FAIL] 没有找到包：{out}")
            return 1
        ok, _ = _verify_archive(out)
        return 0 if ok else 1

    print(f"[1/4] 白名单遍历（root: {ROOT.name}）…")
    files = _walk_include()
    total = sum(f.stat().st_size for f in files)
    print(f"  待打包 {len(files)} 文件，{total / 1e6:.0f} MB")

    print("[2/4] 隐私扫描…")
    issues = _scan_privacy(files)
    if issues:
        print(f"[FAIL] 发现 {len(issues)} 处隐私风险，禁止打包：")
        for issue in issues[:20]:
            print(f"  - {issue}")
        return 1
    print("  无隐私风险")

    print(f"[3/4] 压缩打包（{'STORE' if args.stored else 'DEFLATED'}）…")
    _build_zip(files, out, args.stored)

    print("[4/4] 完整性验证…")
    ok, _ = _verify_archive(out)
    if not ok:
        return 1
    print(f"\n[done] {out}  可分发。")
    print("提示：解压后双击「启动UL10nForge.bat」即用；"
          "privacy 扫描已排除绝对路径/密钥形态。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
