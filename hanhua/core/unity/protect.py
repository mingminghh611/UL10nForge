"""写回后非显示对象保护（Rendezvous 2026-08-18 实证驱动的防御层）。

背景：rawstr 二进制扫描无法可靠区分「显示文本字段」与「逻辑键/标识名
字段」（对象名 m_Name、InputModule 轴名、CutsceneScript 键、UnityEvent
绑定字符串、材质/精灵引用等）。字节级写回把逻辑键当文本翻译后，游戏
代码按原名查找断链 → 运行时空指针崩溃（Rendezvous 过场流程实证：
650 个非显示对象还原后崩溃消失）。

本模块提供写回后的保护步骤：按「显示脚本白名单」只保留显示组件的
翻译，其余被写回改动的对象（含 GameObject 名）从原版恢复。

显示脚本判定：脚本类型（typetree 可解析时用类名；typeless 时用
globalgamemanagers 的 MonoScript pathID 白名单——Rendezvous 实证：
2000=TextMeshProUGUI, 795=UnityEngine.UI.Text, 1267=TextMeshPro,
1195=LocalizationText, 391=TMP_InputField）。
"""
from __future__ import annotations

import struct
from typing import Iterable

from UnityPy import Environment  # noqa: PLC0415 延迟导入（UnityPy 体积大）


#: 显示文本脚本的 MonoScript pathID（globalgamemanagers 内）。
#: Rendezvous 实证：这些脚本的字符串字段是「显示文本」可安全翻译；
#: 其余脚本的字符串字段多为逻辑键（按钮/过场/输入轴/交互绑定）。
DEFAULT_DISPLAY_SCRIPT_PIDS: frozenset[int] = frozenset(
    {2000, 795, 1267, 1195, 391})


def restore_non_display_objects(
        writtenback_dir: str,
        original_dir: str,
        levels: Iterable[int] = range(50),
        display_scripts: frozenset[int] = DEFAULT_DISPLAY_SCRIPT_PIDS,
        *,
        file_prefix: str = "level") -> int:
    """写回后的 level 目录中，恢复「非显示脚本对象」到原版。

    显示脚本对象（白名单内）保留翻译；其余被写回改动的对象
    （GameObject 名、逻辑组件、SpriteRenderer/LightmapSettings 等）
    从 original_dir 的同名文件恢复原对象数据。

    返回恢复的对象总数。集成到 runner 发布阶段，写回后自动执行。
    """
    import os

    restored = 0
    for i in levels:
        fn = f"{file_prefix}{i}"
        wp = os.path.join(writtenback_dir, fn)
        op = os.path.join(original_dir, fn)
        if not (os.path.exists(wp) and os.path.exists(op)):
            continue
        env_w = Environment()
        env_w.load_file(wp)
        env_o = Environment()
        env_o.load_file(op)
        w = {o.path_id: o for o in env_w.objects}
        o = {o.path_id: o for o in env_o.objects}
        to_restore: list[int] = []
        for pid, ow in w.items():
            oo = o.get(pid)
            if oo is None or ow.get_raw_data() == oo.get_raw_data():
                continue
            if _is_display_object(ow, display_scripts):
                continue
            to_restore.append(pid)
        if to_restore:
            for pid in to_restore:
                if pid in o:
                    w[pid].set_raw_data(o[pid].get_raw_data())
            af = next(iter(env_w.objects)).assets_file
            # A10 0.46.0：gen 9-21 data_offset 布局保真（与 writer 统一）
            from hanhua.core.unity.serialized_layout import restore_from_path
            data = restore_from_path(af, wp)
            with open(wp, "wb") as f:
                f.write(data)
            restored += len(to_restore)
    return restored


def _is_display_object(obj, display_scripts: frozenset[int]) -> bool:
    """判断对象是否「显示文本组件」（白名单脚本的 MonoBehaviour）。

    非 MonoBehaviour（GameObject/Transform/MeshRenderer/LightmapSettings
    等）一律视为非显示 → 恢复原版（Rendezvous 实证：引用替换误伤它们
    导致关卡加载崩溃）。
    """
    if str(getattr(obj.type, "name", "")) not in ("MonoBehaviour", "ScriptableObject"):
        return False
    data = obj.get_raw_data()
    if data is None or len(data) < 28:
        return False
    fid = struct.unpack_from("<i", data, 16)[0]
    pid = struct.unpack_from("<q", data, 20)[0]
    # m_Script fileID=1 → globalgamemanagers 的 MonoScript
    return fid == 1 and pid in display_scripts
