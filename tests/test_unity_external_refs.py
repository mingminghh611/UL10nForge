"""写回/识别外部引用解析根回归（#19）。

根因：UnityPy `Environment()` 构造时 path 默认 `os.getcwd()`（工具自身
目录），单文件 `env.load` 不更新 path——Mono 游戏 MonoBehaviour 的
m_Script PPtr（FileID=1 → 外部 globalgamemanagers.assets）deref 时
`find_file` 用 CWD 搜索 → 抛 FileNotFoundError（用户实证：写回失败
"File globalgamemanagers.assets not found in 工具目录"）。

修复：所有加载游戏文件并可能 deref 外部引用的 env 创建点设置
`env.path = 游戏文件所在目录`（writer._patch_asset /
_verify_saved_bundle / _asset_bundle_content_crc、extractor、
font_replace 各站点）。

此处用最小 v22 SerializedFile fixture 回归：外部文件同目录可被
find_file 解析（修复前抛 FileNotFoundError），且 PPtr deref 走
find_file 不再失败。
"""
from __future__ import annotations

import struct

import pytest

from UnityPy import Environment

try:
    from UnityPy import load as unitypy_load
except Exception:  # noqa: BLE001  不同 UnityPy 版本导出名不同
    unitypy_load = None


def _sf_bytes(externals: list[str] | None = None) -> bytes:
    """构造最小 v22 SerializedFile（Unity 2021.3 头格式）。

    头布局（check_file_type 校验点）：
      u_int metadata_size | u_int file_size(4) | u_int version(=22，offset 8)
      | u_int data_offset(4) | u8 endianness | u8[3] reserved
      | u_int metadata_size(extended) | u64 file_size | u64 data_offset | u64 unknown
    metadata：unity_version(null 结尾) + target_platform(i32) + enableTypeTree(u8)
      + type_count/object_count/script_count(各 u32) + externals_count(u32)
      + externals[]（temp_empty(null串) + guid(16) + type(i32) + path(null串)，
      v>=6 FileIdentifier 布局）+ ref_type_count(u32) + userInformation(null 串)
    """
    meta = bytearray()
    meta.extend(b"2021.3.1f1\x00")
    meta += struct.pack("<i", 3)
    meta += b"\x01"
    meta += struct.pack("<I", 0)  # type_count
    meta += struct.pack("<I", 0)  # object_count
    meta += struct.pack("<I", 0)  # script_count
    meta += struct.pack("<I", len(externals or []))
    for ext in externals or []:
        meta += b"\x00"                      # temp_empty (read_string_to_null)
        meta += b"\x00" * 16                 # guid
        meta += struct.pack("<i", 0)         # type (kDeprecated)
        meta.extend(ext.encode("utf-8"))     # path
        meta += b"\x00"
    meta += struct.pack("<I", 0)             # ref_type_count
    meta += b"\x00"                          # userInformation
    hdr = (struct.pack("<IIII", len(meta), 0, 22, 0)
           + b"\x00" + b"\x00\x00\x00")
    data_offset = len(hdr) + 28 + len(meta)
    data_offset += (16 - data_offset % 16) % 16
    file_size = data_offset
    hdr = (struct.pack("<IIII", len(meta), file_size & 0xFFFFFFFF, 22,
                       data_offset & 0xFFFFFFFF)
           + b"\x00" + b"\x00\x00\x00"
           + struct.pack("<I", len(meta))
           + struct.pack("<Q", file_size)
           + struct.pack("<Q", data_offset)
           + struct.pack("<Q", 0))
    pad = data_offset - (len(hdr) + len(meta))
    return bytes(hdr + meta + b"\x00" * pad)


def _write_pair(tmp_path) -> tuple:
    """globalgamemanagers.assets（声明 external level0）+ level0 同目录。"""
    gg = tmp_path / "globalgamemanagers.assets"
    gg.write_bytes(_sf_bytes(externals=["level0"]))
    (tmp_path / "level0").write_bytes(_sf_bytes())
    return gg, tmp_path


def test_env_default_path_is_cwd_not_game_dir(tmp_path, monkeypatch):
    """环境前提：Environment() 默认 path=os.getcwd()——这正是错误来源。"""
    gg, _ = _write_pair(tmp_path)
    # 把 CWD 指到与游戏目录不同的位置，复现用户实证
    other = tmp_path / "tool_cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    env = Environment()
    assert env.path == str(other)
    env.load([str(gg)])
    with pytest.raises(Exception) as exc_info:
        env.find_file("level0")
    assert "not found in" in str(exc_info.value)


def test_env_path_set_to_game_dir_resolves_external(tmp_path):
    """修复核心：env.path=游戏目录 → find_file 能解析同目录 externals。"""
    gg, game_dir = _write_pair(tmp_path)
    env = Environment()
    env.path = str(game_dir)
    env.load([str(gg)])
    found = env.find_file("level0")
    assert found is not None
    # 外部文件确实被挂进 environment（deref 的前提）
    assert any(str(game_dir) in k for k in env.files)


def test_find_file_resolves_external_without_error(tmp_path):
    """端到端：加载声明外部引用的文件后 find_file 不再抛 FileNotFoundError。"""
    gg, game_dir = _write_pair(tmp_path)
    env = Environment(path=str(game_dir))
    env.load([str(gg)])
    try:
        env.find_file("level0")
    except FileNotFoundError:  # pragma: no cover
        pytest.fail("env.path=游戏目录后外部引用仍解析失败")
