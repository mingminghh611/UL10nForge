"""v2 写回：重建 Unity 容器，并对固定容量代码字符串池执行安全替换。

Unity StringTable 通过类型树按稳定 Entry ID 写回；无类型树的序列化字符串随
SerializedFile/BundleFile 一起重建并重开校验。.NET #US 与 IL2CPP 字符串池仍受
原始容量限制，超长译文会在字符边界截断并报告。
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from hanhua.core.memory import ProjectStore
from hanhua.core.models import WriteOutcome, WriteRejection
from hanhua.core.paths import resolve_relative_under
from hanhua.core.placeholders import (_IDENTIFIER, is_code_identifier,
                                      is_key_style_identifier)
from hanhua.core.unity import il2cpp, mono_dll


def _asset_file_name(obj) -> str:
    asset_file = getattr(obj, "assets_file", None)
    return str(getattr(asset_file, "name", "") or "")


def _object_identity(obj) -> tuple[str, int]:
    return _asset_file_name(obj), int(obj.path_id)


def _typetree_value_at_path(tree, field_path: list[str | int]):
    current = tree
    for segment in field_path:
        if isinstance(segment, int):
            if not isinstance(current, list) or not 0 <= segment < len(current):
                raise KeyError(field_path)
        elif not isinstance(current, dict) or segment not in current:
            raise KeyError(field_path)
        current = current[segment]
    return current


def _set_typetree_value_at_path(
        tree, field_path: list[str | int], value: str) -> None:
    if not field_path:
        raise KeyError(field_path)
    parent = _typetree_value_at_path(tree, field_path[:-1])
    leaf = field_path[-1]
    if isinstance(leaf, int):
        if not isinstance(parent, list) or not 0 <= leaf < len(parent):
            raise KeyError(field_path)
    elif not isinstance(parent, dict) or leaf not in parent:
        raise KeyError(field_path)
    parent[leaf] = value


def _entries_for_object_identity(
        by_obj: dict[tuple[str, int], list], object_key: tuple[str, int],
        path_id_counts: dict[int, int]):
    """复合身份优先；旧裸 Path ID 仅在整个容器唯一时兼容。"""
    items = by_obj.get(object_key)
    if items is None and path_id_counts.get(object_key[1]) == 1:
        items = by_obj.get(("", object_key[1]))
    return items


# ── 不可变字段集合（写回安全闸门 P0-4） ──
# 这些字段是 key/标识符/引用/地址/脚本绑定，永远不能被文本写回改动。
# 只保护带 m_ 的 Unity 惯例字段：裸字段名（如 "name"）在自定义对象里
# 可能是真实显示文本，误拦截会造成漏写而不是误写。
_IMMUTABLE_FIELD_NAMES = frozenset({
    "m_Name",                          # Object 名
    "m_Key", "m_Id", "m_EntryID",      # StringTable Entry / 各类稳定 ID
    "m_GUID",                          # 资产 GUID
    "m_FileID", "m_PathID",            # PPtr 引用
    "m_Path", "m_Address",             # 资源/文件地址
    "m_ControlPath", "m_Action", "m_ActionMap",   # Input System 绑定
    "m_Script",                        # MonoBehaviour 脚本引用
    "m_ClassName", "m_Namespace",      # 脚本类名
    "m_LocaleIdentifier", "m_LocaleCode",          # Localization locale
    "m_SharedData",                    # StringTable 表级共享引用
})


def _is_immutable_field_name(name: str) -> bool:
    return name in _IMMUTABLE_FIELD_NAMES


def _collect_immutable_values(
        tree: dict,
        exclude: set[str] | None = None) -> list[tuple[list[str | int], object]]:
    """递归收集 typetree 中所有不可变字段的路径 + 当前值。

    返回列表供写回前快照、写回后重开比对；值可以是 str/int/dict/list，
    用相等性比较（不要求可哈希）。
    exclude：跳过这些字段名（如 TextAsset 的 m_Script 是内容字段会被
    有意修改，不是 MonoBehaviour 脚本引用，不能按不可变字段快照）。
    """
    collected: list[tuple[list[str | int], object]] = []
    skip = exclude or set()

    def walk(node, path: list):
        if isinstance(node, dict):
            for key, value in node.items():
                if (isinstance(key, str)
                        and _is_immutable_field_name(key) and key not in skip):
                    collected.append((list(path) + [key], value))
                walk(value, path + [key])
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, path + [index])

    walk(tree, [])
    return collected


def _snapshot_typetree_surroundings(
        tree: dict, exclude_paths: set[tuple]) -> list[tuple[list[str | int], object]]:
    """C2：收集 typetree 全部非目标叶子路径 + 值（写回前快照）。

    save_typetree 完整重建会重写整个对象——非目标字段（数值标量/数组
    长度/类型名/邻字段）只靠 _IMMUTABLE_FIELD_NAMES 覆盖不足：immutable
    清单之外 UnityPy 序列化缺陷静默改动某字段，重开验证（补丁后字节自证）
    发现不了。快照全部非目标叶子，重开后逐值比对——任何非目标字段被
    save_typetree 改动都会在此拒绝（含 float 精度、数组 m_Size、类型名）。
    exclude_paths：本次补丁的目标叶子路径（有意改动，跳过）。
    """
    collected: list[tuple[list[str | int], object]] = []

    def walk(node, path: list):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, path + [key])
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, path + [index])
        elif tuple(path) not in exclude_paths:
            collected.append((list(path), node))

    walk(tree, [])
    return collected


@dataclass
class WriteResult:
    files: int = 0
    entries: int = 0
    truncated: int = 0
    warnings: list[str] = field(default_factory=list)
    attempted: int = 0
    rejected: list[WriteRejection] = field(default_factory=list)
    _attempted_locators: set[str] = field(default_factory=set, repr=False)
    _resolved_locators: set[str] = field(default_factory=set, repr=False)
    truncated_items: list[str] = field(default_factory=list)   # 截断条目摘要（最多 30 条）
    # ── 写回逻辑层审计（logic_audit）──
    logic_audit: list[dict] = field(default_factory=list)      # 写回前敏感形态审计
    raw_expansions: list[dict] = field(default_factory=list)   # rawstr 扩容记录（译文>原文）
    logic_mismatches: list[str] = field(default_factory=list)  # 重开逻辑验证失败（字符串边界）
    # 工具移植任务 2（patchcrc 等价）：UnityFS header 校验字段状态
    # （写回后内容已变、旧 hash/crc 失效——Unity 默认不校验，严格路径
    # 可用 bundle_crc.patch_checksums 按需修复）
    unityfs_checksum: dict | None = None
    logic_reverted: int = 0        # 反向语义审计自动回退条目数（译文→原文，防逻辑键断链）
    logic_reverted_items: list[str] = field(default_factory=list)  # 回退摘要（最多 30 条）
    # C10：完整回退 locator 集合（file_id:key_path，全量不截断）——发布
    # 成功后由调用方持久化到 store（GUI 状态与游戏实际状态同步）
    reverted_locators: set[str] = field(default_factory=set)
    # W3：被回退（保留原文防断链）的原文完整集合——运行时插件翻译表必须
    # 排除这些串（插件把静态回退的原文再翻译成中文 → 按名比较断链）。
    logic_reverted_sources: set[str] = field(default_factory=set)
    # C4：被写回侧拒绝（尝试写但失败）的条目原文——静态层同样保留原文，
    # 插件运行时翻译同样断链（写不进的对象里往往是键清单/类型描述等结构
    # 串）。与 logic_reverted_sources 一起进运行时排除表；拒绝本身仍走
    # rejected 闸门（P0-2 阻断默认发布），这里只补排除语义。
    rejected_sources: set[str] = field(default_factory=set)

    def __post_init__(self):
        if self.entries and not self.attempted:
            self.attempted = self.entries

    @staticmethod
    def _locator(entry: dict) -> str:
        file_id = str(entry.get("file_id", ""))
        key_path = str(entry.get("key_path", ""))
        if file_id or key_path:
            return f"{file_id}:{key_path}"
        meta = json.loads(entry.get("meta") or "{}")
        offset = meta.get("heap_offset", meta.get("file_offset", meta.get("offset", "?")))
        return f"{meta.get('kind', 'unknown')}:{offset}:{entry.get('original', '')}"

    @property
    def written(self) -> int:
        return self.entries

    @property
    def outcome(self) -> WriteOutcome:
        return WriteOutcome(
            self.attempted, self.written, tuple(self.rejected),
            self.truncated, self.logic_reverted)

    def note_attempt(self, entry: dict) -> str:
        locator = self._locator(entry)
        if locator not in self._attempted_locators:
            self._attempted_locators.add(locator)
            self.attempted += 1
        return locator

    def note_written(self, entry: dict) -> None:
        locator = self.note_attempt(entry)
        if locator not in self._resolved_locators:
            self._resolved_locators.add(locator)
            self.entries += 1

    def note_rejected(self, entry: dict, reason: str) -> None:
        locator = self.note_attempt(entry)
        if locator not in self._resolved_locators:
            self._resolved_locators.add(locator)
            self.rejected.append(WriteRejection(locator, reason))
        original = str(entry.get("original") or "")
        if original:
            self.rejected_sources.add(original)

    def is_resolved(self, entry: dict) -> bool:
        return self._locator(entry) in self._resolved_locators

    def note_truncated(self, original: str, translation: str):
        self.truncated += 1
        if len(self.truncated_items) < 30:
            self.truncated_items.append(
                f"「{original[:42]}」→「{translation[:42]}」")

    def note_logic_reverted(self, entry: dict, reason: str) -> None:
        """逻辑键自动回退记录：主动不写译文（保留原文）——与 rejected
        （尝试写但失败）语义不同，不触发对象闸门阻断。

        必须同步标记 resolved：write_back_v2 尾部循环对未 resolve 的
        候选统一 note_rejected——若漏标记，被回退的条目会同时记 rejected
        （「防线越生效、发布越被误阻」），且原文进 rejected 的 object
        闸门统计造成发布误判。
        """
        locator = self.note_attempt(entry)
        if locator not in self._resolved_locators:
            self._resolved_locators.add(locator)
        # 写回 C10：完整回退 locator 集合（区别于摘要 logic_reverted_items
        # 最多 30 条）——发布成功后由调用方持久化到 store（GUI 状态与
        # 游戏实际状态同步，不再显示 translated）
        self.reverted_locators.add(locator)
        self.logic_reverted += 1
        original = str(entry.get("original") or "")
        if original:
            self.logic_reverted_sources.add(original)
        if len(self.logic_reverted_items) < 30:
            self.logic_reverted_items.append(
                f"{reason}:「{original[:36]}」"
                f"→「{str(entry.get('translation', ''))[:36]}」({locator})")


# 截断提示符：TMP 动态字体可能缺「…」字形（A-主题5-2 递归报错实证），
# 可整体替换为 "..." 等 ASCII 兜底（报告提示项）。
TRUNCATION_ELLIPSIS = "…"


def _fit_bytes(translation: str, capacity: int, encoding: str,
               *, pad: bool = True) -> tuple[bytes, bool]:
    """把译文编码为 ≤ capacity 字节（超长按字符截断，末尾补省略号提示）。返回 (字节, 是否截断)。

    pad=True 时填充 NUL 到 capacity（固定容量原位覆盖用）；pad=False 时
    返回实际字节（长度由记录字段/前缀同步更新，如 IL2CPP/metadata 路径）。
    """
    data = translation.encode(encoding)
    if len(data) <= capacity:
        if pad:
            return data + b"\x00" * (capacity - len(data)), False
        return data, False
    if encoding == "utf-16-le":
        # 预算至少留 1 个字符给省略号
        chars = max(1, capacity // 2 - 1)
        # 审计 #8 补严：Python str 按码点计数，astral 平面字符（emoji/
        # 扩展 CJK）1 码点 = 2 个 UTF-16 单元——按码点数预算的 chars 个
        # 字符编码后可能远超 capacity 字节，末尾 [:capacity] 字节切片
        # 会把代理对切成两半（孤立代理，CLR #US 解码器容忍度不确定）。
        # 先收缩到「截断正文 + 省略号」编码后不超容量。
        while (chars > 0
               and len((translation[:chars]
                        + TRUNCATION_ELLIPSIS).encode("utf-16-le"))
               > capacity):
            chars -= 1
        # 截断点本身不得落在代理对中间（字符串内嵌孤立代理，如
        # surrogatepass 解码产物）：高代理后多退一格
        while (chars > 0
               and "\ud800" <= translation[chars - 1:chars] <= "\udbff"):
            chars -= 1
        data = (translation[:chars] + TRUNCATION_ELLIPSIS).encode("utf-16-le")[:capacity]
        if pad:
            return data + b"\x00" * (capacity - len(data)), True
        return data, True
    # UTF-8：预算按字符截断，末尾补省略号（若容量允许）
    budget = capacity
    if budget >= 6:
        budget -= len(TRUNCATION_ELLIPSIS.encode("utf-8"))   # 预留省略号字节
    while len(translation.encode("utf-8")) > budget:
        translation = translation[:-1]
    if budget >= 6:
        translation += TRUNCATION_ELLIPSIS
    data = translation.encode("utf-8")[:capacity]
    if pad:
        return data + b"\x00" * (capacity - len(data)), True
    return data, True


# 写回 C8：占位符防线只认 {数字开头} 是盲区——SmartFormat 命名占位符
# {name}、格式说明 {1:0.00}/{0,-10:N2}、Ren'Py 等号值 {w=1.5}、结束标签
# {/i} 全部不匹配，截断把 {name 砍成 {name 后照写不拦。扩展为任意非空
# 花括号内容；前后 lookahead 排除 string.Format 转义 {{literal}}/{{0}}
# （渲染为字面 {literal}/{0}，不是占位符——{0}} 形态同样拦掉）。
# 机械恢复补末尾对索引占位（string.Format 按索引取参）与命名占位
# （SmartFormat 按名取参）都安全；等号值/结束标签补末尾是「比丢失好」
# 的最优解（位置敏感，但截断已经破坏了原文，恢复缺失标记优先）。
_FORMAT_PLACEHOLDER = re.compile(r"(?<!\{)\{[^{}\r\n]+\}(?!\})")


def _restore_placeholders(original: str, translation: str) -> str:
    """占位符机械恢复：译文缺失原文的 {n} 时补到译文末尾。

    string.Format 按索引取参，占位符位置/顺序变化不崩溃（与
    _placeholders_intact 注释同源），补末尾是最安全的恢复位——译文
    主体不动只追加。模型漏写 {n} 是稳定行为（翻译时丢占位符），
    机械补回后好译文可正常写回，不再被 reject 丢弃。原文无占位符
    或译文已完整时原样返回。
    """
    found = _FORMAT_PLACEHOLDER.findall(original)
    if not found:
        return translation
    missing = [placeholder for placeholder in found
               if placeholder not in translation]
    if not missing:
        return translation
    return translation + "".join(missing)


def _restore_placeholders_capped(original: str, translation: str,
                                 capacity: int, encoding: str) -> bytes | None:
    """占位符机械恢复（容量感知）：缺失的原文占位符补末尾。

    实证（2026-08-14 Minato 24 条拒绝）：_restore_placeholders 补的
    {n} 在 _fit_bytes 二次截断时被削掉（UTF-16 从末尾截）→
    _placeholders_intact 仍失败 → reject。本函数把「补占位符」与
    「容量」合并处理：从译文正文尾部腾空间（省略号提示与冗余正文
    优先让位，占位符完整性 > 正文尾字——string.Format 缺参即崩溃，
    正文少一两字只是截断，游戏仍可运行）。

    返回恢复后的编码字节（≤ capacity）；占位符总长超过容量（物理
    不可能）时返回 None，由上层 reject（不写坏译文）。
    """
    unit = 2 if encoding == "utf-16-le" else 1
    limit = capacity // unit
    found = _FORMAT_PLACEHOLDER.findall(original)
    if not found:
        data = translation.encode(encoding)
        return data if len(data) <= capacity else data[:capacity]
    missing = [placeholder for placeholder in found
               if placeholder not in translation]
    if not missing:
        data = translation.encode(encoding)
        return data if len(data) <= capacity else data[:capacity]
    tail = "".join(missing)
    if len(tail) > limit:
        return None  # 占位符本身超容量：物理不可能
    body = translation
    # 剥掉 _fit_bytes 截断附加的省略号（截断提示 < 占位符完整性）
    if body.endswith(TRUNCATION_ELLIPSIS):
        body = body[:-1]
    # 从正文尾部剥码元腾空间（按编码后实际字节计——UTF-8 汉字 3 字节
    # 不能按字符数算；UTF-16 每码元 2 字节，统一走 encode 长度）
    while len(body.encode(encoding)) + len(tail.encode(encoding)) > capacity \
            and body:
        body = body[:-1]
    if not body:
        return None
    return (body + tail).encode(encoding)


def _placeholders_intact(original: str, translation: str) -> bool:
    """F2：截断/省略号不得破坏原文的 {n} 占位符（string.Format 崩溃防护）。

    只检查「原文含占位符」的情形：截断后译文必须仍包含全部占位符（按
    文本出现；位置/顺序变化不检查——string.Format 按索引取参数,顺序变化
    不崩溃）。原文无占位符时直接通过。质量门在翻译阶段校验过占位符完整,
    这里的防线针对写回内部的截断。
    """
    found = _FORMAT_PLACEHOLDER.findall(original)
    if not found:
        return True
    return all(placeholder in translation for placeholder in found)


def _encode_compressed_uint(value: int) -> bytes:
    """ECMA-335 压缩无符号整数编码（#US/#Blob 长度前缀用）。"""
    if value < 0x80:
        return bytes((value,))
    if value < 0x4000:
        return bytes((0x80 | (value >> 8), value & 0xFF))
    if value < 0x20000000:
        return bytes((0xC0 | (value >> 24), (value >> 16) & 0xFF,
                      (value >> 8) & 0xFF, value & 0xFF))
    raise ValueError(f"ECMA-335 压缩整数过大：{value}")


def _ecma335_user_string_flag(text: str) -> int:
    """Return the terminal #US kind flag for the effective UTF-16 text."""
    return int(any(
        ord(char) >= 0x7F
        or 0x01 <= ord(char) <= 0x08
        or 0x0E <= ord(char) <= 0x1F
        or char in {"'", "-"}
        for char in text
    ))


def _patch_bytes(blob: bytearray, offset: int, capacity: int, payload: bytes):
    if offset < 0 or capacity < 0 or offset + capacity > len(blob):
        raise ValueError(
            f"二进制字符串范围越界：offset={offset}, capacity={capacity}, size={len(blob)}")
    if len(payload) != capacity:
        raise ValueError(f"二进制字符串长度不匹配：payload={len(payload)}, capacity={capacity}")
    blob[offset:offset + capacity] = payload


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录临时文件 + 原子替换，避免中断留下半写文件。

    文件被占用（游戏进程仍在运行 / Windows Defender 扫描窗口 / 杀软
    隔离）时 os.replace 抛 PermissionError（WinError 5）——短暂重试，
    最终失败包装为可操作的中文错误（上次 93 游戏写回审计 7 个失败全
    因此类锁定，修复后错误信息直接指出处理方向）。
    """
    temp_path: Path | None = None
    try:
        fd, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_path, path)
                temp_path = None
                break
            except PermissionError:
                if attempt == 4:
                    raise PermissionError(
                        f"文件被占用无法写回（可能原因：游戏仍在运行、杀毒软件/"
                        f"Windows Defender 正在扫描）：{path}") from None
                import time as _time
                _time.sleep(0.8)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _patch_catalog_crc_bytes(catalog: bytes, replacements: dict[int, int],
                             require_match: bool = False) -> bytes:
    """将 catalog 内唯一的旧 bundle CRC 替换为新 CRC。

    Addressables 把 AssetBundle.LoadFromFile 的 CRC 参数保存在 catalog.bin；只改
    bundle 会触发 Unity 的 ``CRC Mismatch`` 并拒绝加载。旧 CRC 在一个 catalog 中
    必须至多出现一次，避免把无关位置误认为 RequestOptions 的 CRC 字段。
    """
    encoded_replacements = {
        original_crc.to_bytes(4, "little"): translated_crc.to_bytes(4, "little")
        for original_crc, translated_crc in replacements.items()
        if original_crc != translated_crc
    }
    if not encoded_replacements:
        return catalog
    counts = {old: catalog.count(old) for old in encoded_replacements}
    for old, count in counts.items():
        if count == 0 and require_match:
            raise ValueError(f"Addressables catalog 中未找到 CRC {int.from_bytes(old, 'little'):08x}")
        if count > 1:
            raise ValueError(f"Addressables catalog 中 CRC {int.from_bytes(old, 'little'):08x} 出现 {count} 次")
    patched = bytearray(catalog)
    # 在原始 catalog 上做一次扫描，避免 A→B、B→C 替换发生链式二次命中。
    for offset in range(0, len(catalog) - 3):
        old = catalog[offset:offset + 4]
        new = encoded_replacements.get(old)
        if new is not None:
            patched[offset:offset + 4] = new
    return bytes(patched)


def _asset_bundle_content_crc(path: Path) -> int:
    """计算 Unity 用于 AssetBundle.LoadFromFile 的未压缩内容 CRC-32。"""
    from UnityPy import Environment

    env = Environment()
    try:
        env.load([str(path)])
        bundles = {id(item): item for item in env.files.values()
                   if type(item).__name__ == "BundleFile"}
        if len(bundles) != 1:
            raise ValueError(f"预期恰好一个 BundleFile，实际为 {len(bundles)}：{path}")
        crc = 0
        for item in next(iter(bundles.values())).files.values():
            reader = item.reader
            position = reader.Position
            try:
                reader.Position = 0
                crc = zlib.crc32(reader.read_bytes(reader.Length), crc)
            finally:
                reader.Position = position
        return crc & 0xFFFFFFFF
    finally:
        _dispose_environment(env)


def _update_addressables_catalogs(game_dir: Path, out_dir: Path,
                                  asset_files: list[dict]) -> list[Path]:
    """为已变化的 Addressables bundle 刷新输出 catalog.bin 中的 CRC。"""
    catalogs = [catalog for catalog in out_dir.rglob("catalog.bin")
                if _is_addressables_path(catalog.relative_to(out_dir))]
    if not catalogs:
        # 输出无 catalog（源也无 Addressables 管线，仅 aa 目录放普通
        # AssetBundle）→ 无 patch 目标；提前返回避免对每个 bundle 做
        # CRC 解析（UnityPy 对部分 bundle 格式不支持，且本无必要）。
        return []
    replacements: dict[int, int] = {}
    for file_info in asset_files:
        rel_path = Path(file_info["rel_path"])
        if rel_path.suffix.lower() != ".bundle":
            continue
        if not _is_addressables_path(rel_path):
            continue
        source = resolve_relative_under(game_dir, rel_path)
        target = resolve_relative_under(out_dir, rel_path)
        if not source.exists() or not target.exists():
            continue
        source_crc = _asset_bundle_content_crc(source)
        target_crc = _asset_bundle_content_crc(target)
        if source_crc == target_crc:
            continue
        existing = replacements.get(source_crc)
        if existing is not None and existing != target_crc:
            raise ValueError(f"多个 bundle 共用原 CRC {source_crc:08x}，无法安全更新 catalog")
        replacements[source_crc] = target_crc

    if not replacements:
        return []
    catalogs = [catalog for catalog in out_dir.rglob("catalog.bin")
                if _is_addressables_path(catalog.relative_to(out_dir))]
    catalog_data = {catalog: catalog.read_bytes() for catalog in catalogs}
    for original_crc in replacements:
        encoded = original_crc.to_bytes(4, "little")
        matches = sum(data.count(encoded) for data in catalog_data.values())
        if matches == 0:
            raise ValueError(f"所有 Addressables catalog 中均未找到 CRC {original_crc:08x}")
        if matches > 1:
            raise ValueError(f"Addressables catalog 中 CRC {original_crc:08x} 共出现 {matches} 次")
    patched_data = {
        catalog: _patch_catalog_crc_bytes(original, replacements)
        for catalog, original in catalog_data.items()
    }
    updated = [catalog for catalog, original in catalog_data.items()
               if patched_data[catalog] != original]
    written: list[Path] = []
    try:
        for catalog in updated:
            _atomic_write_bytes(catalog, patched_data[catalog])
            written.append(catalog)
    except Exception:
        for catalog in written:
            _atomic_write_bytes(catalog, catalog_data[catalog])
        raise
    return updated


def _is_addressables_path(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    return any(parts[i:i + 2] == ["streamingassets", "aa"]
               for i in range(len(parts) - 1))


def _validate_addressables_catalog_sources(game_dir: Path, out_dir: Path,
                                           asset_files: list[dict]) -> None:
    """在任何 bundle 写入前确认输出 catalog 能覆盖所有源 CRC。"""
    bundles = [Path(info["rel_path"]) for info in asset_files
               if Path(info["rel_path"]).suffix.lower() == ".bundle"
               and _is_addressables_path(Path(info["rel_path"]))]
    if not bundles:
        return
    catalogs = [catalog for catalog in out_dir.rglob("catalog.bin")
                if _is_addressables_path(catalog.relative_to(out_dir))]
    if not catalogs:
        # 部分游戏仅把普通 AssetBundle 放在 aa 目录（无 Addressables 管线），
        # 源本身没有 catalog.bin → 无 catalog 可 patch，跳过校验；
        # 仅当源有 catalog 而副本缺失（复制遗漏）时才报错。
        source_catalogs = [catalog for catalog in game_dir.rglob("catalog.bin")
                           if _is_addressables_path(
                               catalog.relative_to(game_dir))]
        if not source_catalogs:
            return
        raise ValueError("Addressables bundle 存在，但输出目录中没有 Addressables catalog.bin")
    data = [catalog.read_bytes() for catalog in catalogs]
    for rel_path in bundles:
        source_crc = _asset_bundle_content_crc(
            resolve_relative_under(game_dir, rel_path))
        encoded = source_crc.to_bytes(4, "little")
        matches = sum(blob.count(encoded) for blob in data)
        if matches != 1:
            raise ValueError(
                f"源 bundle CRC {source_crc:08x} 在 Addressables catalog 中应唯一命中，实际 {matches} 次")


def _align(value: int, boundary: int = 4) -> int:
    """返回不小于 value 的 boundary 对齐位置。"""
    return value + (-value % boundary)


def _patch_serialized_string(raw: bytearray, data_offset: int, translation: str
                             ) -> tuple[int, int]:
    """替换 Unity 序列化的 UTF-8 string 字段，并保留字段后的内容。

    Unity string 的长度头只记录实际文本字节数；字段末尾的零字节仅是为了让
    下一个字段落在 4 字节边界。替换范围必须以旧字段结束位置为界，不能以新
    长度计算，否则变长译文会覆盖紧随字符串的序列化字段。

    返回 (old_end, new_end)：本补丁的旧/新字段结束位置（含对齐零），供
    C2 差异白名单记录 span——old_end 与 new_end 之差是后续内容的位移量。
    """
    length_offset = data_offset - 4
    if length_offset < 0 or data_offset > len(raw):
        raise ValueError(f"非法字符串偏移：{data_offset}")
    old_length = int.from_bytes(raw[length_offset:data_offset], "little")
    old_end = _align(data_offset + old_length)
    if old_end > len(raw):
        raise ValueError(f"字符串长度越界：offset={data_offset}, length={old_length}")

    payload = translation.encode("utf-8")
    new_end = _align(data_offset + len(payload))
    raw[length_offset:old_end] = (
        len(payload).to_bytes(4, "little")
        + payload
        + b"\x00" * (new_end - data_offset - len(payload))
    )
    return old_end, new_end


def _assert_asset_diff_whitelist(raw: bytes, patched: bytes,
                                 spans: list[tuple[int, int, int]],
                                 desc: str) -> None:
    """资产侧字节差异白名单（C2，镜像 IL2CPP `_assert_diff_whitelist`）。

    补丁前后逐字节 diff 必须全部落在目标串区（长度头 + 内容 + 对齐零），
    其余字节零变动——独立于「重开 vs 补丁后字节」自证的第二道防线：
    自证只证明保存保真，不证明补丁逻辑没越界；白名单直接对原始字节
    验证「只改了该改的」（扫描误判 / 补丁偏移 bug 在此拦截）。

    spans：(length_offset, old_end, new_end)，按 length_offset 升序。
    原位补丁（new_end == old_end 全部）：diff 必须严格落在 span 并集内。
    变长补丁（任一 new_end != old_end）：后续内容整体位移（rawstr 扩容/
    压缩推挤），逐字节 diff 会蔓延——此时验证「非目标区段内容逐字节保留、
    仅允许位置移动」：非目标区段以子串顺序保留检查（与绝对位置无关），
    UnityPy 序列化/补丁逻辑对非目标字段的任何改动（数值/长度头/相邻串）
    都会使子串缺失而拒绝。
    """
    if all(new_end == old_end for _s, old_end, new_end in spans):
        allowed: set[int] = set()
        for s, _old_end, new_end in spans:
            allowed.update(range(s, new_end))
        bad = [i for i in range(len(raw))
               if raw[i] != patched[i] and i not in allowed]
        if bad:
            raise ValueError(
                f"{desc} 写回差异越出目标串区 {len(bad)} 处"
                f"（首例 0x{bad[0]:x}），非目标字节被改动，拒绝")
        return
    # 变长补丁：非目标区段 = 原始坐标上补丁 span 之间的空隙
    segments: list[tuple[int, int]] = []
    prev = 0
    for s, old_end, _new_end in sorted(spans):
        if s > prev:
            segments.append((prev, s))
        prev = max(prev, old_end)
    if prev < len(raw):
        segments.append((prev, len(raw)))
    cursor = 0
    for a, b in segments:
        segment = raw[a:b]
        if not segment:
            continue
        idx = patched.find(segment, cursor)
        if idx == -1:
            raise ValueError(
                f"{desc} 变长补丁后非目标区段 0x{a:x}..0x{b:x} 内容丢失或被"
                "改动（子串顺序保留检查失败），拒绝")
        # 审计 #7：短段（<4 字节）在长 patched 里极易巧合命中非预期
        # 位置（单字节 segment 任何该字节的出现都算匹配）——附加位置
        # 约束：允许的位移量不超过所有变长 span 的净增量总和（内容
        # 只能被补丁推挤，不能凭空跳很远）。非目标区段相对位置在
        # 变长补丁下的最大漂移 = 全部 span 的字节增量之和。
        max_shift = sum(max(0, new_end - old_end)
                        for _s, old_end, new_end in spans)
        if len(segment) < 4:
            expected_pos = a
            if idx - expected_pos > max_shift + 1:
                raise ValueError(
                    f"{desc} 短非目标区段 0x{a:x}..0x{b:x} 匹配位置漂移 "
                    f"0x{idx:x} 超出补丁位移预算（疑似非目标字节被改动），拒绝")
        cursor = idx + len(segment)


def _apply_localization_translations(tree: dict,
                                     translations: list[tuple[int, str]]) -> bool:
    """按稳定 Entry ID 修改 StringTable 类型树，返回是否发生变化。"""
    by_id = {int(entry_id): translation for entry_id, translation in translations}
    changed = False
    for row in tree.get("m_TableData") or []:
        if not isinstance(row, dict) or row.get("m_Id") not in by_id:
            continue
        translation = by_id[row["m_Id"]]
        if row.get("m_Localized") != translation:
            row["m_Localized"] = translation
            changed = True
    return changed


def _dispose_environment(env) -> None:
    """关闭 UnityPy 保留的文件句柄，Windows 下替换文件前必须执行。

    注意：UnityPy 对未知格式文件加载为 StreamFile——对象自身就是 reader
    （无 .reader 属性），必须对对象自身也调用 dispose，否则文件被锁。
    """
    seen: set[int] = set()

    def dispose_file(file_item) -> None:
        if id(file_item) in seen:
            return
        seen.add(id(file_item))
        for child in (getattr(file_item, "files", None) or {}).values():
            dispose_file(child)
        reader = getattr(file_item, "reader", None)
        dispose = getattr(reader, "dispose", None)
        if dispose:
            try:
                dispose()
            except Exception:  # noqa: BLE001
                pass
        own_dispose = getattr(file_item, "dispose", None)
        if own_dispose is not None and own_dispose is not dispose:
            try:
                own_dispose()
            except Exception:  # noqa: BLE001
                pass

    for file_item in env.files.values():
        dispose_file(file_item)
    env.files.clear()


def _verify_saved_bundle(
        path: Path,
        expected_raw_by_path_id: dict[tuple[str, int], bytes] | dict[int, bytes],
        baseline_hashes: dict[tuple[str, int], tuple[int, bytes]] | None = None,
        expected_typetree_values: dict[
            tuple[str, int], list[tuple[list[str | int], str]]] | None = None,
        expected_immutable_values: dict[
            tuple[str, int], list[tuple[list[str | int], object]]] | None = None,
        expected_string_sequences: dict[
            tuple[str, int], tuple[list[str], dict[str, str]]] | None = None,
        expected_surrounding_values: dict[
            tuple[str, int], list[tuple[list[str | int], object]]] | None = None,
        typetree_generator=None,
) -> None:
    """重开临时容器，确认目标对象正确且未目标对象字节不变。

    typetree_generator：Mono typeless bundle 必须与补丁侧同源生成器——
    否则重开 read_typetree 抛「Expected N bytes, only M」→ 全部按异常
    标记 mismatched（hickory 实证 160 对象全拒），补丁字节其实往返一致。

    expected_immutable_values：写回前快照的不可变字段集合（key/ID/引用/
    地址/脚本绑定），重开后必须保持逐值一致——防止定位错误把标识符当
    文本改写。

    expected_string_sequences：逻辑层验证（§写回逻辑层检查）——rawstr
    改动对象的字符串序列快照（内容序列 + 原文→译文映射），重开后按同一
    扫描规则重新扫描，数量与逐项内容必须一致。扩容插入若破坏字符串长度
    头边界，序列必然变化——这是「字节自证」验证发现不了的结构破坏
    （游戏加载该对象失败 → 按钮无响应/卡住）。

    expected_surrounding_values（C2）：save_typetree 完整重建对象的全部
    非目标叶子快照——immutable 清单之外的字段（数值标量/数组长度/类型名
    /邻字段）任何被重建改动都在此拒绝。与 expected_typetree_values 共用
    一次 read_typetree。
    """
    from UnityPy import Environment

    verifier = Environment()
    if typetree_generator is not None:
        verifier.typetree_generator = typetree_generator
    try:
        verifier.load([str(path)])
        actual: dict[tuple[str, int], bytes] = {}
        actual_objects = {}
        for obj in verifier.objects:
            key = _object_identity(obj)
            if key not in actual:
                actual[key] = obj.get_raw_data()
                actual_objects[key] = obj
        missing: list[object] = []
        mismatched: list[object] = []
        for key, expected in expected_raw_by_path_id.items():
            actual_key = key if isinstance(key, tuple) else next(
                (candidate for candidate in actual if candidate[1] == key), None)
            if actual_key is None or actual_key not in actual:
                missing.append(key)
            elif actual[actual_key] != expected:
                mismatched.append(key)
        if baseline_hashes is not None:
            if set(actual) != set(baseline_hashes):
                missing.extend(sorted(set(baseline_hashes) ^ set(actual)))
            expected_hashes = dict(baseline_hashes)
            for key, expected in expected_raw_by_path_id.items():
                if isinstance(key, tuple):
                    expected_hashes[key] = (len(expected), hashlib.sha256(expected).digest())
            for key, (size, digest) in expected_hashes.items():
                raw = actual.get(key)
                if raw is None or len(raw) != size or hashlib.sha256(raw).digest() != digest:
                    if key not in mismatched:
                        mismatched.append(key)
        for key in (set((expected_typetree_values or {}))
                    | set((expected_immutable_values or {}))
                    | set((expected_surrounding_values or {}))):
            obj = actual_objects.get(key)
            if obj is None:
                missing.append(key)
                continue
            try:
                # 每对象至多 read_typetree 一次（大对象避免重复解析），
                # 三类快照（目标字段 / 不可变字段 / C2 非目标叶子）共用
                tree = obj.read_typetree()
                for field_path, expected_value in (expected_typetree_values
                                                   or {}).get(key, ()):
                    if _typetree_value_at_path(tree, field_path) != expected_value:
                        mismatched.append((key, field_path))
                for field_path, expected_value in (expected_immutable_values
                                                   or {}).get(key, ()):
                    if _typetree_value_at_path(tree, field_path) != expected_value:
                        mismatched.append((key, field_path))
                for field_path, expected_value in (expected_surrounding_values
                                                   or {}).get(key, ()):
                    if _typetree_value_at_path(tree, field_path) != expected_value:
                        mismatched.append((key, ("surrounding", field_path)))
            except (KeyError, TypeError, ValueError, AttributeError):
                mismatched.append(key)
        # 逻辑层验证：rawstr 改动对象字符串序列一致性（边界未破坏）
        for key, (expected_sequence, translations) in (
                (expected_string_sequences or {}).items()):
            obj = actual_objects.get(key)
            if obj is None:
                missing.append(key)
                continue
            try:
                actual_raw = obj.get_raw_data()
            except Exception:  # noqa: BLE001
                mismatched.append((key, "raw_read_failed"))
                continue
            from hanhua.core.unity.logic_audit import verify_logic_layer
            ok, problems = verify_logic_layer(
                actual_raw, expected_sequence, translations)
            if not ok:
                mismatched.append((key, "; ".join(problems)))
        if missing or mismatched:
            raise ValueError(
                "Unity bundle 写回验证失败："
                f"缺失对象={missing}，字节不一致对象={mismatched}"
            )
    finally:
        _dispose_environment(verifier)


def copy_game_dir(game_dir: Path, out_dir: Path, progress_cb: Callable | None = None) -> int:
    """把整个游戏目录复制到 out_dir（进度按文件数）。返回文件数。

    总是重建：副本必须与原始目录一致，二进制写回的偏移才有效
    （上次写回会改变副本内对象布局，旧副本不能再次写回）。
    """
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    all_files = [p for p in game_dir.rglob("*") if p.is_file()]
    copied = 0
    total = len(all_files)

    def copy_fn(src, dst, *, follow_symlinks=True):
        nonlocal copied
        shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
        copied += 1
        if progress_cb:
            progress_cb(copied, total)

    shutil.copytree(game_dir, out_dir, dirs_exist_ok=False, copy_function=copy_fn)
    return total


def _entries_by_file(store: ProjectStore, file_ids: set[str]) -> dict[str, list[dict]]:
    """按 file_id 一次性分组取条目（轻量）：v2 写回循环原是每文件
    _entries_for_file() 全库 SELECT * + Python 过滤（旧实现，已并入本
    函数）——大游戏几百个
    bundle 每个都全表扫一遍 O(N×M)。单次全量 + 分组替代。"""
    from hanhua.core.quality import is_write_ready
    grouped: dict[str, list[dict]] = {}
    for e in store.get_entries():
        fid = e["file_id"]
        if fid in file_ids and is_write_ready(
                e.get("status", ""), e.get("translation", ""),
                e.get("meta", "{}")):
            grouped.setdefault(fid, []).append(e)
    return grouped


def _should_write_entry(e: dict) -> bool:
    """Consume extractor disposition; retain narrow guards for legacy rows only."""
    meta = json.loads(e.get("meta") or "{}")
    kind = meta.get("kind")
    disposition = str(meta.get("disposition", ""))
    if disposition:
        return disposition == "translate"
    if kind == "localization":
        return True
    role = str(meta.get("role", ""))
    if role:
        return role in {"display", "ui", "dialogue"}
    if kind in ("us", "il2cpp"):
        return not is_code_identifier(e["original"])
    if meta.get("obj_is_key_list") and _IDENTIFIER.match(e["original"]):
        return False
    # Compatibility for pre-disposition rows only. New scans return above from
    # persisted provenance and never reach these legacy guards.
    if meta.get("obj_has_values") is True:
        return not is_key_style_identifier(e["original"])
    if is_code_identifier(e["original"]):
        return False
    return not is_key_style_identifier(e["original"])


def _write_rejection_reason(e: dict) -> str:
    meta = json.loads(e.get("meta") or "{}")
    disposition = str(meta.get("disposition", ""))
    if disposition and disposition != "translate":
        return f"disposition_{disposition}"
    role = str(meta.get("role", ""))
    if role and role not in {"display", "ui", "dialogue"}:
        return f"role_{role}"
    return "legacy_key_guard"


def _select_write_items(items: list[tuple[dict, dict]], result: WriteResult,
                        kind: str) -> list[tuple[dict, dict]]:
    selected: list[tuple[dict, dict]] = []
    for entry, meta in items:
        if meta.get("kind") != kind or entry["translation"] == entry["original"]:
            continue
        result.note_attempt(entry)
        if _should_write_entry(entry):
            selected.append((entry, meta))
        else:
            result.note_rejected(entry, _write_rejection_reason(entry))
    return selected


def write_back_v2(store: ProjectStore, game_dir: Path, out_dir: Path,
                  progress_cb: Callable | None = None,
                  typetree_generator=None) -> WriteResult:
    """写回全部 v2 类条目（资源/DLL/metadata）到 out_dir 副本。

    typetree_generator：扫描同源生成器（Mono 游戏无内嵌 typetree 的
    typeless bundle，写回侧必须与扫描侧一致地生成脚本 typetree，否则
    read_typetree 失败 → 整组 typetree_unavailable 拒绝；hickory 实证
    261 条全拒，扫描 350 条全部可读却写不回）。
    """
    result = WriteResult()
    files = store.get_files()
    v2_files = [f for f in files if f["format"].startswith("v2_")]
    for file_info in v2_files:
        resolve_relative_under(game_dir, file_info["rel_path"])
        resolve_relative_under(out_dir, file_info["rel_path"])
    entries_by_file = _entries_by_file(
        store, {f["id"] for f in v2_files})
    changed_bundle_candidates = [
        f for f in v2_files
        if f["format"] == "v2_asset"
        and any(
            _should_write_entry(e) and e["translation"] != e["original"]
            for e in entries_by_file.get(f["id"], ())
        )
    ]
    _validate_addressables_catalog_sources(
        game_dir, out_dir, changed_bundle_candidates)
    # ── 写回前逻辑层审计：对全部待写回条目做逻辑敏感形态检查（只报告
    # 不阻断——按钮文本 back/retry 大量命中短词形态，阻断会误伤真实
    # 显示文本；camelCase/snake_case 等代码标识符形态命中 warn 级）──
    from hanhua.core.unity.logic_audit import audit_entries_before_writeback
    for f in v2_files:
        result.logic_audit.extend(audit_entries_before_writeback(
            e for e in entries_by_file.get(f["id"], ())
            if _should_write_entry(e) and e["translation"] != e["original"]))
    for f in v2_files:
        entries = entries_by_file.get(f["id"], ())
        candidates = [e for e in entries if e["translation"] != e["original"]]
        for entry in candidates:
            result.note_attempt(entry)
        if not entries:
            result.warnings.append(f"无译文条目：{f['rel_path']}")
            continue
        dst = resolve_relative_under(out_dir, f["rel_path"])
        if not dst.exists():
            result.warnings.append(f"副本中缺失：{f['rel_path']}")
            for entry in candidates:
                result.note_rejected(entry, "output_file_missing")
            continue
        entries_before = result.entries
        if f["format"] == "v2_asset":
            _patch_asset(dst, entries, result,
                         typetree_generator=typetree_generator)
        elif f["format"] == "v2_mono":
            _patch_dll(dst, entries, result)
        elif f["format"] == "v2_il2cpp":
            _patch_metadata(dst, entries, result)
        for entry in candidates:
            if not result.is_resolved(entry):
                reason = (_write_rejection_reason(entry)
                          if not _should_write_entry(entry)
                          else "locator_not_found_or_unchanged")
                result.note_rejected(entry, reason)
        if result.entries > entries_before:
            result.files += 1
    _update_addressables_catalogs(game_dir, out_dir, v2_files)
    return result


def _patch_asset(path: Path, entries: list[dict], result: WriteResult,
                 typetree_generator=None):
    import gc
    import time as _time
    from UnityPy import Environment
    env = Environment()
    if typetree_generator is not None:
        env.typetree_generator = typetree_generator
    by_obj: dict[tuple[str, int], list[dict]] = {}
    for e in entries:
        meta = json.loads(e["meta"] or "{}")
        by_obj.setdefault((str(meta.get("asset_file", "")), int(meta.get("obj", -1))), []).append((e, meta))

    expected_raw_by_path_id: dict[tuple[str, int], bytes] = {}
    expected_typetree_values: dict[
        tuple[str, int], list[tuple[list[str | int], str]]] = {}
    expected_immutable_values: dict[
        tuple[str, int], list[tuple[list[str | int], object]]] = {}
    # C2：save_typetree 完整重建对象的全部非目标叶子快照
    expected_surrounding_values: dict[
        tuple[str, int], list[tuple[list[str | int], object]]] = {}
    # 逻辑层审计：rawstr 改动对象的字符串序列快照（重开验证用）
    expected_string_sequences: dict[
        tuple[str, int], tuple[list[str], dict[str, str]]] = {}
    baseline_hashes: dict[tuple[str, int], tuple[int, bytes]] = {}
    changed_any = False
    patched_entries: list[dict] = []
    try:
        env.load([str(path)])
        object_identities = {_object_identity(obj) for obj in env.objects}
        path_id_counts: dict[int, int] = {}
        for _, path_id in object_identities:
            path_id_counts[path_id] = path_id_counts.get(path_id, 0) + 1
        for baseline_obj in env.objects:
            baseline_key = _object_identity(baseline_obj)
            if baseline_key in baseline_hashes:
                continue
            try:
                baseline_raw = baseline_obj.get_raw_data()
            except Exception:  # noqa: BLE001
                continue
            baseline_hashes[baseline_key] = (
                len(baseline_raw), hashlib.sha256(baseline_raw).digest())
        seen_objs: set[tuple[str, int]] = set()
        for obj in env.objects:
            object_key = _object_identity(obj)
            items = _entries_for_object_identity(
                by_obj, object_key, path_id_counts)
            if not items:
                continue
            # bundle 中同一对象会因 Environment 顶层别名列出两次；同一对象只允许补丁一次。
            if object_key in seen_objs:
                continue
            seen_objs.add(object_key)
            tname = obj.type.name
            if tname == "TextAsset":
                try:
                    data = obj.read()
                except Exception:  # noqa: BLE001
                    # F4：read 异常——整对象跳过写回（提取侧同保护，
                    # 防御写回时对象状态异常）
                    for entry, _ in items:
                        result.note_rejected(entry, "object_read_failed")
                    continue
                # 老 Unity（4.x/5.x）：TextAsset.m_Script 是 str（extractor
                # 侧同样兼容），统一 encode 还原为 bytes 再处理，避免
                # startswith/decode 在 str 上崩溃（MarioVsLuigi 实证）
                script = data.m_Script
                if isinstance(script, str):
                    script = script.encode("utf-8-sig", errors="surrogateescape")
                text_items = _select_write_items(items, result, "textasset")
                structured_items = [
                    (entry, meta) for entry, meta in items
                    if meta.get("textasset_format")
                    and entry["translation"]
                    and entry["translation"] != entry["original"]
                ]
                patched_script = _patch_textasset(
                    script, text_items, structured_items, result)
                if patched_script != script:
                    original_lines = script.decode(
                        "utf-8-sig", errors="replace").splitlines()
                    changed_lines = {
                        int(meta["line"])
                        for entry, meta in text_items
                        if "line" in meta
                        and 0 <= int(meta["line"]) < len(original_lines)
                        and original_lines[int(meta["line"])] != entry["translation"]
                    }
                    prev_len = len(patched_entries)
                    patched_entries.extend(
                        entry for entry, meta in text_items
                        if int(meta.get("line", -1)) in changed_lines)
                    # 老 Unity m_Script 是 str 字段：UnityPy save 会对 str
                    # encode("utf8", surrogateescape)（与读取对称），必须赋
                    # str 而非 bytes（bytes 会 AttributeError 实测）。
                    # 注意 decode 不能用 utf-8-sig：UnityPy 读 str 时
                    # decode("utf8") 把 BOM 保留为 U+FEFF 字符，-sig 会吞掉
                    # 它导致保存后字节差 BOM（MarioVsLuigi 28 对象实测）
                    data.m_Script = (
                        patched_script.decode("utf-8", errors="surrogateescape")
                        if isinstance(data.m_Script, str) else patched_script)
                    # 不能用 data.save() + get_raw_data() 快照预期字节：
                    # get_raw_data 从原始 reader 读（reset+read_bytes），
                    # set_raw_data 只更新 self.data，两者不同步（MarioVsLuigi
                    # 28 对象实测：save 后 get_raw_data 仍返回旧字节，导致
                    # 验证把新字节误判为不一致）。save_typetree 直接返回
                    # 实际写出的字节，与其他分支一致。
                    try:
                        expected = obj.save_typetree(data)
                    except Exception as exc:  # noqa: BLE001
                        # 类型引用字符串被污染（Unity Localization 的
                        # TypeName Namespace Assembly 描述等，resonance 实测
                        # save_typetree 抛 ValueError）→ 该对象整组拒绝，
                        # 不让一个坏对象中断整个游戏写回。
                        del patched_entries[prev_len:]
                        for entry, _ in text_items:
                            result.note_rejected(entry, f"save_typetree 失败: {exc}")
                        continue
                    expected_raw_by_path_id[object_key] = expected
                    changed_any = True
                # m_Script 是内容字段（有意修改），只快照 m_Name 等
                # Object 级字段；m_Script 若按不可变字段快照，重开比对会
                # 把内容修改误判为脚本绑定被破坏（MarioVsLuigi 28 对象实测）
                try:
                    tree = obj.read_typetree()
                    immutable = _collect_immutable_values(
                        tree, exclude={"m_Script"})
                    if immutable:
                        expected_immutable_values[object_key] = immutable
                    # C2：非目标叶子快照（m_Script 内容字段有意修改，排除）
                    surroundings = _snapshot_typetree_surroundings(
                        tree, {("m_Script",)})
                    if surroundings:
                        expected_surrounding_values[object_key] = surroundings
                except Exception:  # noqa: BLE001 typeless bundle：字节级验证兜底
                    pass
            else:
                selected_localization = _select_write_items(
                    items, result, "localization")
                localization_items = [
                    (int(meta["entry_id"]), e["translation"])
                    for e, meta in selected_localization
                ]
                if localization_items:
                    tree = obj.read_typetree()
                    localized_targets = dict(localization_items)
                    changed_ids = {
                        row.get("m_Id") for row in (tree.get("m_TableData") or [])
                        if isinstance(row, dict)
                        and row.get("m_Id") in localized_targets
                        and row.get("m_Localized") != localized_targets[row.get("m_Id")]
                    }
                    if _apply_localization_translations(tree, localization_items):
                        try:
                            expected = obj.save_typetree(tree)
                        except Exception as exc:  # noqa: BLE001
                            # 同 typetree 分支：类型引用字符串被污染时整组拒绝
                            for entry, _ in selected_localization:
                                result.note_rejected(
                                    entry, f"save_typetree 失败: {exc}")
                            continue
                        expected_raw_by_path_id[object_key] = expected
                        changed_any = True
                        patched_entries.extend(
                            entry for entry, meta in selected_localization
                            if int(meta["entry_id"]) in changed_ids)
                    immutable = _collect_immutable_values(tree)
                    if immutable:
                        expected_immutable_values[object_key] = immutable
                    # C2：非目标叶子快照——m_Localized 是目标字段，按精确
                    # 路径排除（TableData 每行的 m_Localized 叶子）
                    exclude = {("m_TableData", i, "m_Localized")
                               for i, row in enumerate(tree.get("m_TableData") or [])
                               if isinstance(row, dict)}
                    surroundings = _snapshot_typetree_surroundings(tree, exclude)
                    if surroundings:
                        expected_surrounding_values[object_key] = surroundings
                    continue
                typetree_items = _select_write_items(
                    items, result, "typetree")
                raw_items: list = []
                if typetree_items:
                    try:
                        tree = obj.read_typetree()
                    except Exception:  # noqa: BLE001 typeless bundle
                        tree = None
                    if tree is not None:
                        for entry, _ in _select_write_items(
                                items, result, "rawstr"):
                            result.note_rejected(
                                entry, "typed_locator_preferred")
                        changed_paths: list[tuple[list[str | int], str]] = []
                        prev_len = len(patched_entries)
                        # W2 反向语义审计（typetree 分支）：UnityEvent/代码
                        # 对象的键字段经 typetree 写入——m_MethodName 等
                        # UnityEvent 绑定字段不在不可变字段清单（_IMMUTABLE_
                        # FIELD_NAMES 只收 m_Name/m_Key/…惯例字段），rawstr
                        # 路径的 logic_key_evidence 也不覆盖 typetree 分支。
                        # 按「字段路径信号（UnityEvent 绑定字段）+ 值形态
                        # （type_descriptor/键环境代码形态）」判定逻辑键
                        # 身份：确定性回退（保留原文），与 rawstr 同层防线。
                        from hanhua.core.unity.logic_audit import (
                            typetree_logic_key_evidence,
                        )
                        reverted_typetree: set[int] = set()
                        for entry, meta in typetree_items:
                            verdict = typetree_logic_key_evidence(
                                meta, str(entry["original"]))
                            if not verdict:
                                continue
                            if verdict[0] == "revert":
                                entry["status"] = "skipped"
                                reverted_typetree.add(id(entry))
                                result.note_logic_reverted(entry, verdict[1])
                                result.logic_audit.append({
                                    "stage": "semantic_revert",
                                    "locator": str(entry.get("key_path") or ""),
                                    "obj": meta.get("obj"),
                                    "original": entry["original"],
                                    "translation": entry["translation"],
                                    "reason": verdict[1],
                                })
                        for entry, meta in typetree_items:
                            if id(entry) in reverted_typetree:
                                continue
                            field_path = meta.get("field_path")
                            if not isinstance(field_path, list):
                                result.note_rejected(entry, "field_path_missing")
                                continue
                            if (isinstance(field_path[-1], str)
                                    and _is_immutable_field_name(field_path[-1])):
                                # 定位器指向 key/ID/引用/地址/脚本字段：
                                # 这是扫描误判，写前拦截而不是写坏后才发现
                                result.note_rejected(
                                    entry, "immutable_field_protected")
                                continue
                            try:
                                current = _typetree_value_at_path(tree, field_path)
                            except KeyError:
                                result.note_rejected(entry, "field_path_missing")
                                continue
                            if current != entry["original"]:
                                result.note_rejected(
                                    entry, "field_path_value_mismatch")
                                continue
                            _set_typetree_value_at_path(
                                tree, field_path, entry["translation"])
                            changed_paths.append(
                                (list(field_path), entry["translation"]))
                            patched_entries.append(entry)
                        if changed_paths:
                            try:
                                expected = obj.save_typetree(tree)
                            except Exception as exc:  # noqa: BLE001
                                # resonance-of-the-ocean 实测：Localization
                                # SmartFormat 的「TypeName Namespace Assembly」
                                # 类型描述字段被当文本翻译后，save_typetree
                                # 抛 ValueError（Referenced type not found）
                                # → 该对象整组拒绝 + 回滚，不让一个坏对象
                                # 中断整个游戏写回。
                                del patched_entries[prev_len:]
                                for entry, _ in typetree_items:
                                    result.note_rejected(
                                        entry, f"save_typetree 失败: {exc}")
                                continue
                            expected_raw_by_path_id[object_key] = expected
                            expected_typetree_values[object_key] = changed_paths
                            changed_any = True
                        immutable = _collect_immutable_values(tree)
                        if immutable:
                            expected_immutable_values[object_key] = immutable
                        # C2：非目标叶子快照——save_typetree 完整重建重写
                        # 整个对象，immutable 清单之外的字段（数值标量/数组
                        # 长度/类型名/邻字段）被序列化缺陷静默改动时，重开
                        # 验证（补丁后字节自证）发现不了；此处快照全部非
                        # 目标叶子，重开后逐值比对拒绝。
                        exclude = {
                            tuple(fp) for fp, _ in changed_paths}
                        surroundings = _snapshot_typetree_surroundings(
                            tree, exclude)
                        if surroundings:
                            expected_surrounding_values[object_key] = surroundings
                        continue
                    # F4：typetree 不可用（无内嵌 typetree 的 typeless
                    # bundle），禁止 save_typetree 空模板序列化（UnityPy
                    # #195 半损坏产物）——回退 rawstr 字节级原位补丁；
                    # 无 rawstr 条目才整组拒绝。
                    raw_items = _select_write_items(items, result, "rawstr")
                    if not raw_items:
                        for entry, _ in typetree_items:
                            result.note_rejected(entry, "typetree_unavailable")
                        continue
                    for entry, _ in typetree_items:
                        result.note_rejected(entry, "typetree_unavailable")
                else:
                    # 原生类型（Text/VisualTreeAsset 等）只有 typetree
                    # 条目，无 rawstr 时不要触碰 get_raw_data
                    raw_items = _select_write_items(items, result, "rawstr")
                    if not raw_items:
                        continue
                try:
                    raw = bytearray(obj.get_raw_data())
                except Exception:  # noqa: BLE001
                    # F4：read 异常（对象损坏/类型不支持）——整对象跳过
                    # 而不是中断整个文件，且绝不产生半损坏产物。
                    for entry, _ in raw_items:
                        result.note_rejected(entry, "object_read_failed")
                    continue
                # 逻辑层审计：写回前快照对象字符串序列（重开验证时按同一
                # 扫描规则重新扫描，比较数量与内容——rawstr 扩容插入若
                # 破坏字符串长度头边界，序列必然变化）+ 扩容记录
                from hanhua.core.unity.logic_audit import (
                    audit_raw_expansion, audit_repeat_consistency,
                    logic_key_evidence, snapshot_object_strings,
                )
                string_sequence = snapshot_object_strings(bytes(raw))
                obj_strings = string_sequence
                # 写回侧 m_Name 兜底（2026-08-26 任务三补漏）：MonoBehaviour/
                # ScriptableObject 的 m_Name 是对象标识名（Inspector 标签/
                # Find 查找键），翻译必断链（Rendezvous 崩溃根因）。提取器
                # 已按固定布局排除（_mono_object_name_span），但漏判/旧库
                # 残留的定位器可能仍指向 m_Name 跨度——写回侧按同一布局
                # 再判一次，命中跨度的条目宁可漏写（保留原文防断链）也
                # 不译对象名（宁漏勿坏，与 object_name_shared_word 同原则）。
                # 与逻辑键语义回退同记账（resolved + reverted_locators +
                # 排除表），避免尾部兜底误记 rejected。
                from hanhua.core.unity.extractor import _mono_object_name_span
                _mname = _mono_object_name_span(bytes(raw))
                string_translations: dict[str, str] = {}
                for e, meta in raw_items:
                    expansion = audit_raw_expansion(
                        e, meta, e["original"], e["translation"])
                    if expansion:
                        result.raw_expansions.append(expansion)
                # 互斥一致性：同对象同原文多处出现（doog 实证 Splash ×6）——
                # 任一位置是结构跳过（键身份）或各处译文不一致（模型波动），
                # 全组保留原文，防「译文+原文」混排断链（代码按字典查原文）。
                consistency = audit_repeat_consistency(
                    raw_items,
                    # 一致性回退是主动决策：进逻辑回退记账（resolved +
                    # 排除表），否则尾部兜底循环把它误记 rejected（闸门
                    # 误阻），且保留原文不排除时插件运行时再翻译 → 断链
                    on_revert=lambda e, r: result.note_logic_reverted(e, r))
                for rec in consistency:
                    result.logic_audit.append({**rec, "stage": "consistency"})
                # 反向语义审计（知识库案例「UnityEvent 绑定断裂」「显示文本
                # 当逻辑键」转规则）：对每个待翻译条目按「对象角色 + 形态」
                # 判定逻辑键身份——确定性逻辑键（类型描述符/事件绑定方法名/
                # 代码对象中的比较词）自动回退译文（不写补丁，保留原文）。
                # 疑似键（report 级）照常写入并进审计段供复核。
                reverted_locators: set[str] = set()
                for e, meta in raw_items:
                    # 写回侧 m_Name 兜底：rawstr 偏移落在 m_Name 跨度内的
                    # 定位器一律回退（对象名绝不写译）——提取器漏判或旧库
                    # 残留时仍安全。语义回退记账，防尾部兜底误记 rejected。
                    off = meta.get("offset")
                    if (_mname is not None and isinstance(off, int)
                            and _mname[0] <= off <= _mname[1]):
                        e["status"] = "skipped"
                        reverted_locators.add(str(off))
                        result.note_logic_reverted(e, "rawstr_object_mname")
                        result.logic_audit.append({
                            "stage": "semantic_revert",
                            "locator": str(e.get("key_path") or ""),
                            "obj": meta.get("obj"),
                            "original": e["original"],
                            "translation": e["translation"],
                            "reason": "rawstr_object_mname",
                        })
                        continue
                    stripped = str(e["original"]).strip()
                    verdict = logic_key_evidence(stripped, meta, obj_strings)
                    if not verdict:
                        continue
                    if verdict[0] == "revert":
                        e["status"] = "skipped"
                        reverted_locators.add(str(meta.get("offset")))
                        result.note_logic_reverted(e, verdict[1])
                        result.logic_audit.append({
                            "stage": "semantic_revert",
                            "locator": str(e.get("key_path") or ""),
                            "obj": meta.get("obj"),
                            "original": e["original"],
                            "translation": e["translation"],
                            "reason": verdict[1],
                        })
                    else:  # report 级：疑似逻辑键——照常写入，进审计段供复核
                        result.logic_audit.append({
                            "stage": "semantic_report",
                            "locator": str(e.get("key_path") or ""),
                            "obj": meta.get("obj"),
                            "original": e["original"],
                            "translation": e["translation"],
                            "reason": verdict[1],
                        })
                write_items = [
                    (e, meta) for e, meta in raw_items
                    if str(e.get("translation") or "") != str(e.get("original") or "")
                    and str(meta.get("offset")) not in reverted_locators
                ]
                for e, meta in write_items:
                    string_translations[e["original"]] = e["translation"]
                changed = False
                # C2：差异白名单（写回前预演）——补丁链起始原始字节 + 每个
                # 补丁的 (长度头偏移, 旧结束, 新结束)。从后向前处理：较高
                # 偏移处的扩容不会影响尚未处理的较低偏移。
                raw_original = bytes(raw)
                patch_spans: list[tuple[int, int, int]] = []
                for e, meta in sorted(write_items, key=lambda x: -x[1].get("offset", 0)):
                    old_end, new_end = _patch_serialized_string(
                        raw, meta["offset"], e["translation"])
                    patch_spans.append((meta["offset"] - 4, old_end, new_end))
                    changed = True
                    patched_entries.append(e)
                if changed:
                    expected = bytes(raw)
                    _assert_asset_diff_whitelist(
                        raw_original, expected, patch_spans,
                        f"{path}:{object_key}")
                    obj.set_raw_data(expected)
                    expected_raw_by_path_id[object_key] = expected
                    expected_string_sequences[object_key] = (
                        string_sequence, string_translations)
                    changed_any = True
        if not changed_any:
            return

        # 临时目录必须与目标同卷：%TEMP% 在另一磁盘时 saved.replace 跨卷
        # 改名会抛 WinError 17（系统无法将文件移到不同的磁盘驱动器）。
        # ignore_cleanup_errors：异常路径下 env 句柄未及释放时，cleanup
        # 删除 tmp 撞上 Windows 文件锁会抛 PermissionError 覆盖原始异常
        # （MarioVsLuigi data.unity3d 实测），临时残留无害、不得掩盖真因
        with tempfile.TemporaryDirectory(
            prefix=f".{path.name}.", dir=path.parent,
            ignore_cleanup_errors=True,
        ) as tmp:
            saved = Path(tmp) / path.name
            # bundle 必须沿用原压缩标志；普通 .assets 则保存完整 SerializedFile。
            # 不使用 env.save，避免 Environment 顶层别名导致重复序列化。
            # WebFile（UnityWebData 合并场景，Unity 5.x data.unity3d）同样
            # 是合法顶层容器（MarioVsLuigi 实证）。
            containers = {
                id(fitem): fitem for fitem in env.files.values()
                if type(fitem).__name__ in
                ("BundleFile", "SerializedFile", "WebFile")
            }
            if len(containers) == 0:
                raise ValueError("无顶层 Unity 容器")
            # 多容器：Mono 游戏 MonoBehaviour read_typetree 会按引用自动加载
            # 其他 SerializedFile（如 globalgamemanagers.assets 承载 MonoScript）
            # 进 env.files（Rendezvous sharedassets33/45/0 实证）。引用文件
            # 是只读的（_patch_asset 只补丁目标文件内的对象），按目标文件名
            # 匹配保存正确的容器；找不到才拒绝（不静默写错文件）。
            container = None
            if len(containers) == 1:
                container = next(iter(containers.values()))
            else:
                for c in containers.values():
                    if getattr(c, "name", "") == path.name:
                        container = c
                        break
                if container is None:
                    raise ValueError(
                        f"无法定位目标容器（{path.name}），实际 "
                        f"{len(containers)} 个: "
                        f"{[getattr(c, 'name', '?') for c in containers.values()]}")
            if type(container).__name__ == "BundleFile":
                saved.write_bytes(container.save(packer="original"))
            else:
                saved.write_bytes(container.save())
            # 工具移植任务 2（UABEA patchcrc 等价）：UnityFS header 校验
            # 字段（hash/crc）检测——UnityPy 写回保留旧值，内容已变；
            # Unity 加载默认不校验，但严格路径（显式 crc 校验/第三方
            # 工具）会失败。检测并记录（修复接口 patch_checksums 可
            # 按需调用；自动修复需完整解压 uncompressed，成本高且
            # 无实测需求——见 bundle_crc.py 说明）
            try:
                _raw = saved.read_bytes()
                from hanhua.core.unity.bundle_crc import (
                    checksum_header_offset, read_checksums,
                    verify_checksums)
                if checksum_header_offset(_raw) is not None:
                    _h, _c = read_checksums(_raw)
                    result.unityfs_checksum = {
                        "hash": _h.hex() if _h else None,
                        "crc": _c,
                        "needs_refresh": True,  # 内容已变，旧校验值失效
                    }
            except OSError:
                pass
            try:
                _verify_saved_bundle(
                    saved, expected_raw_by_path_id, baseline_hashes,
                    expected_typetree_values, expected_immutable_values,
                    expected_string_sequences, expected_surrounding_values,
                    typetree_generator=typetree_generator)
            except ValueError as exc:
                # 逻辑层验证失败（含字符串序列不一致）——记录审计详情后
                # 重新抛出：整体拒绝写回，副本保持原样，绝不落地坏产物
                result.logic_mismatches.append(str(exc))
                raise
            _dispose_environment(env)
            gc.collect()
            # 兜底：若仍撞上 Defender 扫描锁定窗口，短重试几次
            for attempt in range(5):
                try:
                    saved.replace(path)
                    for entry in patched_entries:
                        result.note_written(entry)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    _time.sleep(0.8)
    finally:
        _dispose_environment(env)


def _patch_textasset(script: bytes, items: list[tuple[dict, dict]],
                     structured_items: list[tuple[dict, dict]],
                     result: WriteResult) -> bytes:
    """TextAsset 重建（m_Script 是可变长 byte[] 字段，不受长度限制）。

    行级条目按行替换（换行保留）；结构化条目（textasset_format）按格式
    （json/xml/yaml/csv）整体重建——BOM 保留。
    """
    # 调用方已统一 str→bytes（老 Unity m_Script），此处纯 bytes 处理
    bom = script.startswith(b"\xef\xbb\xbf")
    try:
        text = script.decode("utf-8-sig")
    except UnicodeDecodeError:
        # F3：写回侧 strict 校验。文件非合法 UTF-8（编码误判/损坏）时，
        # errors="replace" 会把非法字节静默换成 U+FFFD，重编码即破坏原始
        # 字节（调查报告 1.2 行「编码误判」❌）——整文件拒绝写回。
        for e, _meta in items:
            result.note_rejected(e, "TextAsset 非 UTF-8 编码，拒绝写回")
        for e, _meta in structured_items:
            result.note_rejected(e, "TextAsset 非 UTF-8 编码，拒绝写回")
        return script
    if structured_items:
        from hanhua.core.formats import apply_format_text
        from hanhua.core.formats.xml_format import XmlRewriteUnsafeError
        from hanhua.core.models import TextEntry
        by_fmt: dict[str, list[TextEntry]] = {}
        for e, meta in structured_items:
            fmt = meta.get("textasset_format")
            if fmt not in by_fmt:
                by_fmt[fmt] = []
            # ink 流程控制词写回硬保护（Rendezvous 实证 2026-08-17）：
            # 识别漏网的 done/end/out 若带译文写回会破坏对话流程——
            # 控制词条目强制不写（宁漏勿坏；识别侧 _INK_CONTROL_WORDS
            # 已含全集，此处防御旧库/旧识别结果的漏网译文）
            if (fmt == "json" and str(e.get("original", "")).strip()
                    in {"done", "end", "out"}
                    and (meta.get("kind") == "ink"
                         or json.loads(e.get("meta") or "{}").get("kind")
                         == "ink")):
                result.note_rejected(e, "ink_control_word_guard")
                continue
            by_fmt[fmt].append(TextEntry(
                file_id=e["file_id"],
                key_path=meta.get("inner_path") or e["key_path"],
                original=e["original"], translation=e["translation"],
                status=e["status"],
                meta={**json.loads(e.get("meta") or "{}"), "kind": fmt}))
        changed = False
        for fmt, group in by_fmt.items():
            if fmt == "yaml":
                # 行数守恒预检（Rendezvous 实证 2026-08-17）：旧库 yaml
                # 条目（表头/结构行被过滤导致的缺失行集）按行号重建必然
                # 丢行 → 游戏解析越界黑屏。重建行数 != 原文行数 → 整组
                # 拒绝写回（宁漏勿坏），提示重提取（新提取走 csv 链路）。
                from hanhua.core.formats.yaml_format import apply_yaml
                if len(apply_yaml(group).splitlines()) \
                        != len(text.splitlines()):
                    for e, _ in group:
                        result.note_rejected(e, "yaml_line_loss_guard")
                    continue
            try:
                body = apply_format_text(fmt, group, text, {"kind": "textasset"})
            except XmlRewriteUnsafeError:
                # F7：XML 含 CDATA/DOCTYPE，重序列化会丢失结构——
                # 整文件拒绝写回（零损坏），条目全部警示。
                for e, _ in structured_items:
                    result.note_rejected(e, "xml_cdata_doctype_unsafe")
                return script
            if body != text:
                changed = True
                text = body
        if not changed:
            return script
        for e, _ in structured_items:
            result.note_written(e)
        # 写回 C9：行级/结构化混合场景——同一对象既有结构化条目又有
        # 行级条目（老库条目与结构化条目聚合）。结构化重建后行号会
        # 移位（序列化格式变化），行级条目按「原文行内容」在重建文本
        # 中匹配替换（行结构保持时生效）：唯一匹配 → 写；重复/缺失 →
        # 显式 rejected（带原因），不再静默跳过被尾部循环笼统记账成
        # BLOCKED（安全但误伤面不可审计）。
        line_items = [
            (entry, meta) for entry, meta in
            _select_write_items(items, result, "textasset")
            if "line" in meta]
        if line_items:
            original_lines = script.decode(
                "utf-8-sig", errors="replace").splitlines(keepends=True)
            target_lines = text.splitlines(keepends=True)
            by_line: dict[int, dict] = {}
            for entry, meta in line_items:
                # 审计 #6：重复行号后写覆盖前写，被覆盖条目静默丢失——
                # 重复一律拒绝（与 _patch_metadata/_patch_dll 同构）
                lineno = meta.get("line")
                if not isinstance(lineno, int):
                    continue
                if lineno in by_line:
                    result.note_rejected(entry, "textasset_duplicate_line")
                    continue
                by_line[lineno] = entry
            for lineno, entry in sorted(by_line.items()):
                if not (0 <= lineno < len(original_lines)):
                    result.note_rejected(
                        entry, "textasset_line_out_of_range")
                    continue
                orig_content = original_lines[lineno].strip()
                if not orig_content:
                    result.note_rejected(
                        entry, "textasset_structured_line_blank")
                    continue
                matched: int | None = None
                for i, ln in enumerate(target_lines):
                    if ln.strip() == orig_content:
                        if matched is not None:
                            matched = None      # 重复匹配 → 放弃（改错行）
                            break
                        matched = i
                if matched is None:
                    # 结构化重建改变了行布局（重序列化格式不同）→ 无法
                    # 安全定位，保留原文（显式 rejected 供审计/确认）
                    result.note_rejected(
                        entry, "textasset_structured_line_shifted")
                    continue
                eol = target_lines[matched][
                    len(target_lines[matched].rstrip("\r\n")):]
                target_lines[matched] = entry["translation"] + eol
                result.note_written(entry)
            text = "".join(target_lines)
        data = text.encode("utf-8")
        if bom:
            data = b"\xef\xbb\xbf" + data
        return data
    by_line: dict[int, dict] = {}
    for e, meta in _select_write_items(items, result, "textasset"):
        lineno = meta.get("line")
        if not isinstance(lineno, int):
            continue
        # 审计 #6：重复行号拒绝（与结构化路径同构）
        if lineno in by_line:
            result.note_rejected(e, "textasset_duplicate_line")
            continue
        by_line[lineno] = e
    if not by_line:
        return script
    new_lines = []
    for i, line in enumerate(text.splitlines(keepends=True)):
        content = line.rstrip("\r\n")
        eol = line[len(content):]
        e = by_line.get(i)
        if e is None or not e["translation"]:
            new_lines.append(line)
            continue
        new_lines.append(e["translation"] + eol)
    new_text = "".join(new_lines)
    data = new_text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    return data


def _us_record_offset(meta: dict) -> int | None:
    """解析 #US 记录起始（压缩前缀）位置。

    新提取端写 record_offset（记录起始，CLR token 语义）；旧项目库只有
    heap_offset（数据区位置）——回退分支按旧前缀宽推回记录起始，与旧
    库零迁移兼容。
    """
    record_offset = meta.get("record_offset")
    if isinstance(record_offset, int):
        return record_offset
    heap_offset = meta.get("heap_offset")
    utf16_len = meta.get("utf16_len")
    if isinstance(heap_offset, int) and isinstance(utf16_len, int):
        return heap_offset - len(_encode_compressed_uint(utf16_len + 1))
    return None


def _patch_dll(path: Path, entries: list[dict], result: WriteResult):
    blob = bytearray(path.read_bytes())
    expected: list[tuple[int, bytes, int, dict]] = []
    seen_offsets: set[int] = set()
    for e in entries:
        meta = json.loads(e["meta"] or "{}")
        if meta.get("kind") != "us":
            continue
        if e["translation"] == e["original"]:
            continue
        result.note_attempt(e)
        if not _should_write_entry(e):
            result.note_rejected(e, _write_rejection_reason(e))
            continue
        offset = _us_record_offset(meta)
        if offset is None:
            result.note_rejected(e, "#US meta 缺定位字段")
            continue
        # 审计 #2：record_offset 快速路径不检查 utf16_len——旧库部分迁移
        # meta 有 record_offset 却缺 utf16_len 时此处 KeyError 崩整场写回，
        # 与 _us_record_offset 防御风格对齐（get + isinstance）
        capacity = meta.get("utf16_len")
        if not isinstance(capacity, int):
            result.note_rejected(e, "#US meta 缺 utf16_len")
            continue
        payload, truncated = _fit_bytes(
            e["translation"], capacity, "utf-16-le", pad=False)
        if truncated:
            result.note_truncated(e["original"], e["translation"])
        # F2：占位符完整性全量校验——缺失时机械恢复（补末尾，string.Format
        # 按索引取参位置无关），恢复与容量合并处理：从正文尾部腾空间保
        # 占位符（Minato 24 条实证：旧路径补丁被二次截断削掉仍 reject）。
        # 物理放不下（占位符超容量）才拒绝，不写坏译文。
        restored = _restore_placeholders_capped(
            e["original"], payload.decode("utf-16-le", errors="replace"),
            capacity, "utf-16-le")
        if restored is None:
            result.note_rejected(e, "译文缺失 {n} 占位符")
            continue
        payload = restored
        if not _placeholders_intact(
                e["original"], payload.decode("utf-16-le", errors="replace")):
            result.note_rejected(e, "译文缺失 {n} 占位符")
            continue
        # F3：写回前记录预检——按 offset 读压缩前缀，校验其与 meta 声称的
        # 容量一致（防 offset 语义错位/提取后文件变化写坏记录；MyRustySubmarine
        # 实测「#US 记录缺失」即 heap_offset 语义错位所致，预检在写坏前拦截）
        # 审计 #4：同一 record_offset 两条 entry 时后写覆盖前写，前一条
        # 仍 note_written 虚假成功——重复一律拒绝（与 _patch_metadata #3 同构）
        if offset in seen_offsets:
            result.note_rejected(e, f"#US offset {offset} 重复条目")
            continue
        seen_offsets.add(offset)
        old_ln = capacity + 1
        record = mono_dll.read_us_record_at(bytes(blob), offset)
        if record is None:
            result.note_rejected(e, "#US 记录定位失败（offset 越界或前缀非法）")
            continue
        _data_start, old_raw = record
        if len(old_raw) != old_ln:
            result.note_rejected(
                e, f"#US 记录长度不符（meta={old_ln} 实际={len(old_raw)}）")
            continue
        # F1：#US 记录 = 压缩长度前缀（值 = UTF-16 字节数 + 1，含尾部标志字节）
        # + UTF-16LE 数据 + 标志字节。三处联动：新前缀 + 新数据（紧跟新前缀）
        # + flag 移到新数据末尾后。
        new_ln = len(payload) + 1
        new_prefix = _encode_compressed_uint(new_ln)
        data_start = offset + len(new_prefix)
        flag_pos = data_start + len(payload)
        old_record_end = offset + len(_encode_compressed_uint(old_ln)) + old_ln
        if flag_pos >= len(blob) or old_record_end > len(blob):
            raise ValueError(f"DLL #US 记录范围越界：offset={offset}, size={len(blob)}")
        blob[offset:offset + len(new_prefix)] = new_prefix
        blob[data_start:data_start + len(payload)] = payload
        flag = _ecma335_user_string_flag(payload.decode("utf-16-le"))
        blob[flag_pos] = flag
        # F4：残留清零——译文短于原文时新 flag 之后到旧记录末尾残留旧字节，
        # 清零保持文件干净（CLR 单记录定位不受影响，但二次扫描/外部工具
        # 按流式解析时残留区会断开）。
        if flag_pos + 1 < old_record_end:
            blob[flag_pos + 1:old_record_end] = b"\x00" * (
                old_record_end - flag_pos - 1)
        expected.append((offset, payload, flag, e))
    if not expected:
        return
    _atomic_write_bytes(path, bytes(blob))
    reopened = path.read_bytes()
    # 重开验证：按 token 偏移单记录定位读取（与 CLR 语义一致，自包含，
    # 不依赖堆紧凑）——绝不 rstrip 掩盖尾部 NUL，残留区不干扰验证。
    for offset, payload, flag, entry in expected:
        record = mono_dll.read_us_record_at(reopened, offset)
        if record is None:
            raise ValueError(f"DLL 译文重开验证失败：#US 记录缺失 offset={offset}")
        _data_start, raw_bytes = record
        if raw_bytes != payload + bytes((flag,)):
            raise ValueError(f"DLL 译文重开验证失败：offset={offset}, expected={payload!r}")
        result.note_written(entry)


def _patch_metadata(path: Path, entries: list[dict], result: WriteResult):
    raw = path.read_bytes()
    layout = il2cpp.metadata_data_layout(raw)
    if layout is None:
        return
    data_off, data_size, record_mode = layout
    changes: dict[int, bytes] = {}
    expected: list[tuple[int, bytes, dict]] = []
    # F7：写回前记录存在性预检——独立读取器硬读记录区（不做重叠防御/
    # 过滤），验证每条 meta 声称的 (data_index, length) 有真实记录支撑，
    # 防偏移错位写坏（交叉验证兜底池子自洽，这里补「meta ↔ 文件」一致）。
    # 布局非法（None）时跳过预检：patch_metadata_strings 的交叉验证会拒绝。
    pool = il2cpp._independent_pool_records(raw)
    pool_by_index: dict[int, list[int]] = {}
    if pool is not None:
        for data_index, length, _pos in pool:
            pool_by_index.setdefault(data_index, []).append(length)
    for e in entries:
        meta = json.loads(e["meta"] or "{}")
        if meta.get("kind") != "il2cpp":
            continue
        if e["translation"] == e["original"]:
            continue
        result.note_attempt(e)
        if not _should_write_entry(e):
            result.note_rejected(e, _write_rejection_reason(e))
            continue
        # 旧库迁移/部分损坏的 meta 可能缺定位字段——单条坏 meta 必须
        # 优雅拒绝（note_rejected），不能 KeyError 崩掉整场 IL2CPP 写回
        # （审计 2026-08-20：与 _us_record_offset 的防御风格对齐）
        offset = meta.get("file_offset")
        capacity = meta.get("length")
        if not isinstance(offset, int) or not isinstance(capacity, int):
            result.note_rejected(e, "il2cpp meta 缺 file_offset/length")
            continue
        if not (data_off <= offset < data_off + data_size):
            result.note_rejected(e, "data_index 超出数据区范围")
            continue
        data_index = offset - data_off
        # F7 收口（审计 #5）：预检只在「data_index 在池中且长度不符」时
        # 拒绝——data_index 完全不在池中（独立读取器找不到该偏移的任何
        # 记录）也必须拒绝：偏移错位/旧库 meta 过期指向非记录起始，
        # 放行则把 patch_metadata_strings 的交叉验证当唯一防线
        if (data_index not in pool_by_index
                or capacity not in pool_by_index[data_index]):
            # F7：独立读取器确认该偏移有记录，但长度与 meta 声称不符——
            # meta 过期/提取后文件变化/偏移错位，拒绝写回（绝不写错位置）
            result.note_rejected(
                e, f"data_index {data_index} 长度与文件不符")
            continue
        # pad=False：不填 NUL，长度由记录字段/布局同步更新（F1 修复核心）
        payload, truncated = _fit_bytes(
            e["translation"], capacity, "utf-8", pad=False)
        if truncated:
            result.note_truncated(e["original"], e["translation"])
        # F2：占位符完整性全量校验——缺失时机械恢复（补末尾，string.Format
        # 按索引取参位置无关）。恢复与容量合并处理：从正文尾部腾空间保
        # 占位符（UTF-8 同 Minato 场景），物理放不下才拒绝。
        restored = _restore_placeholders_capped(
            e["original"], payload.decode("utf-8", errors="replace"),
            capacity, "utf-8")
        if restored is None:
            result.note_rejected(e, "译文缺失 {n} 占位符")
            continue
        payload = restored
        if not _placeholders_intact(e["original"], payload.decode("utf-8")):
            result.note_rejected(e, "译文缺失 {n} 占位符")
            continue
        # 审计 #3：同一 data_index 两条 entry（重复提取/旧库合并）时
        # changes dict 后写静默覆盖前写，前一条仍 note_written 虚假成功
        # ——重复一律拒绝，宁可漏写不可谎报
        if data_index in changes:
            result.note_rejected(e, f"data_index {data_index} 重复条目")
            continue
        changes[data_index] = payload
        expected.append((data_index, payload, e))
    if not changes:
        return
    try:
        patched = il2cpp.patch_metadata_strings(raw, changes)
    except ValueError as exc:
        raise ValueError(f"IL2CPP metadata 写回失败：{exc}") from exc
    _atomic_write_bytes(path, patched)
    # 重开验证（C3 全池比对）：重新解析池（与提取同一解析器），与原始
    # 全记录逐条比对——不只检查被补丁记录，未补丁记录的内容/长度也必须
    # 逐字节一致（紧凑重建在末条之后的错误——空洞清零越界、游标偏差——
    # 若仍保持合法 UTF-8 与结构，此前只查被改记录会静默通过）。顺序配对：
    # 重建保持记录顺序（v39 dataIndex 链式更新，explicit 不重排）。
    reopened = path.read_bytes()
    all_records = il2cpp.parse_string_literals(raw)
    verify = il2cpp.parse_string_literals(reopened)
    if len(verify) != len(all_records):
        raise ValueError(
            f"IL2CPP 译文重开验证失败：记录数 {len(all_records)} -> {len(verify)}"
            "（紧凑重建增删了记录，拒绝）")
    for order, (old_index, old_len, old_pos) in enumerate(all_records):
        _new_index, length, data_pos = verify[order]
        if old_index in changes:
            payload = changes[old_index]
            if (length != len(payload)
                    or reopened[data_pos:data_pos + length] != payload):
                raise ValueError(
                    f"IL2CPP 译文重开验证失败：data_index={old_index}, "
                    f"length={length}, expected_length={len(payload)}")
        else:
            # 未补丁记录：内容必须与原始字节逐字节一致（顺序配对下
            # 位置无关——新 dataIndex 是链式重建产物，内容不变才是关键）
            if (length != old_len
                    or reopened[data_pos:data_pos + length]
                    != raw[old_pos:old_pos + old_len]):
                raise ValueError(
                    f"IL2CPP 译文重开验证失败：未补丁记录 "
                    f"data_index={old_index} 内容被重建改动")
    # 全部比对通过后记账（顺序无关，但确保所有补丁条目都验证过）
    for data_index, _payload, entry in expected:
        if data_index in changes:
            result.note_written(entry)
