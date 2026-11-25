# UL10nForge v2 架构设计文档

## 1. 产品定位与核心原则

UL10nForge v2 是一款**零侵入、纯显示层的 Unity 游戏智能汉化工作台**：
- **绝不修改游戏原生文件**：文本拦截、原位替换与字体回退由成熟的 `BepInEx 5/6`（含 `MelonLoader` 备用）+ `XUnity.AutoTranslator (XUAT)` 承担，彻底告别静态修改二进制资产导致的黑屏、按键失灵与音频丢失。
- **高质量 AI 翻译大脑**：结合「游戏语境档案 + 翻译记忆 + 术语防污染 + 确定性质量门 + 四级语义审校」，提供远超普通实时机翻的翻译质量。
- **实时与离线双闭环**：
  - **实时边玩边翻**：内置 XUAT `CustomTranslate` 本地 HTTP 端点，`Hy-MT3-1.8B` 低延迟响应，翻完自动落盘词典并推送前端；
  - **词典工坊与离线精修**：导入/管理游戏词典，按需拉起 `Qwen3.5-4B` 生成游戏语境、批量审校重译，支持人工逐条精修与锁定，一键导出高优先级 `00_UL10nForge.txt` 词典（下次进游戏 0 延迟、无需后台常驻）。
- **极简审美的用户界面**：FastAPI 托管零构建现代 WebUI，隐藏一切底层调试噪音，只向用户清晰呈现游戏状态、翻译进度、原文/译文对照编辑、术语记忆与极简设置。

## 2. 分层架构（Clean Architecture）

```text
ul10n/
├── core/                      # 领域模型、持久化与纯规则（无 Web/进程副作用）
│   ├── models.py              # 核心数据结构（GameProject/Entry/Glossary/Context/Settings）
│   ├── db.py                  # 双 SQLite 仓库（全局库 global.db + 单游戏库 project.db，WAL）
│   ├── text_scanner.py        # 纯静态 Unity UI 组件只读扫描器（零侵入、不扫代码与脚本）
│   ├── xuat_codec.py          # XUAT TextHelper 源码级编解码 + round-trip 闸门
│   ├── quality.py             # 占位符/富文本/按键名保护 + 内置权威 UI 词表 + 确定性质量门
│   ├── exporter.py            # 多格式导出（JSON / 词典 TXT / 双语对照 / Markdown / CSV）
│   ├── game_detector.py       # 纯 Python Unity 探测（Mono/IL2CPP、x64/x86、Unity 版本）
│   └── hook_deployer.py       # BepInEx 5/6 / MelonLoader + XUAT + 字体无损部署、自愈与离线补丁打包
├── engine/                    # 推理运行时与翻译智能体
│   ├── llm_client.py          # 统一 OpenAI 兼容客户端（多网关规范化 / 思考块剥离 / 重试 / Token 统计）
│   ├── runtime_manager.py     # 统一本地运行时管理（CUDA 12.4 → Vulkan → CPU，按需错峰启停）
│   ├── context_builder.py     # 高信息密度抽样（55 条）→ 生成结构化 GameContext 并自动入术语库
│   ├── translator.py          # 单条实时翻译 + 云端省 Token JSON 编号批处理（含单条质量兜底）
│   ├── reviewer.py            # 智能高风险筛选 + 游戏视角批量审校与克制自动修正
│   └── tasks.py               # 后台任务调度器 + 进度/ETA 跟踪 + SSE 事件广播
├── overlay/
│   └── hud.py                 # 游戏内 Alt+L 全局快捷键悬浮改译窗（热同步生效）
├── server/                    # HTTP 接口与 XUAT 实时服务
│   ├── app.py                 # FastAPI 应用（/xuat/translate + /api/* + /api/events SSE + 无缓存静态托管）
│   └── state.py               # 服务编排与防抖实时词典同步
└── web/                       # 零构建高审美现代前端（ES Modules + 深浅双主题 Design System）
    ├── index.html
    ├── styles.css
    └── app.js
```

## 3. 模型与硬件自适应策略

- **翻译角色（`translate`）**：默认 `Hy-MT3-1.8B-Q8_0.gguf`，支持实时低延迟翻译与批量初翻。
- **推理角色（`reason`）**：默认 `Qwen3.5-4B-Q4_K_M.gguf`（`--reasoning off` 快速输出结构化 JSON），仅在执行「语境识别」与「语义审校」时按需拉起，空闲超时自动释放显存。
- **极速直翻模式（关闭推理与审校）**：用户可在设置中一键关闭推理角色，自动释放本地推理显存、隐藏全部审校与语境 UI，并让「一键自动汉化」直通「扫描 → 批量翻译 → 接入游戏」。
- **本地与云端无缝切换**：支持本地离线模型与任意 OpenAI 兼容云端 API；云端模式自动启用 JSON 编号批处理（省约 70%–85% 输入 Token）与单条质量回退。
- **全硬件后端自动降级**：`runtime/llama-cuda`（CUDA 12.4）→ `runtime/llama-vulkan`（通吃 N/A/Intel 显卡）→ CPU 模式，确保所有 Windows 用户解压即用。
