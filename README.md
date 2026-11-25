# UL10nForge · Unity 游戏汉化工具

一款面向 Windows 的 **Unity 游戏中文汉化工具**，支持本地离线模型与在线 API，**绝不修改游戏原文件**。

---

## 它是怎么工作的？（钩子原理）

本工具底层基于 Unity Mod 社区成熟稳定的开源钩子方案：

- **加载器**：`BepInEx 5 / 6`（自动识别 Mono / IL2CPP / 32位 / 64位，IL2CPP 64位可选 `MelonLoader`）
- **文本拦截与替换**：`XUnity.AutoTranslator (XUAT)`

运行游戏时，钩子会在**画面显示层**拦截游戏里的英文文本，自动替换成中文并写入本地词典：
- **安全无损**：不解包、不改动游戏任何原始文件与存档；卸载时按安装记录精准清理，不误删你原本装的其他 Mod。
- **边玩边沉淀**：开玩前先扫出静态菜单并译好；游玩中出现的剧情对话由钩子实时抓取翻译，翻过一次永久保存，下次打开直接秒出中文。

---

## 主要功能

- **低延迟实时翻译**：游玩中实时抓取屏幕文本，并行加速翻译，结合游戏背景、术语表和前文对话尽量一次翻顺。
- **本地离线 & 在线 API 任选**：
  - **本地离线**：自带本地模型引擎（支持 N 卡 CUDA / 通用 Vulkan / CPU），不用联网，完全免费；
  - **在线 API**：支持 DeepSeek、OpenAI、Gemini、豆包、智谱、阿里百炼、硅基流动等任意 OpenAI 兼容接口，批量打包省 Token。
- **可选「直翻模式」**：设置中可一键**关闭推理与审校**，跳过背景识别与审校步骤，直接扫描、翻译并接入游戏，更省显存也更快。
- **游戏内随时改译 (`Alt+L`)**：玩到哪句觉得译得不顺眼，游戏中按 **`Alt+L`** 弹出悬浮窗就能直接改，保存后立刻热更新到游戏画面。
- **三套中文字体实时预览**：内置**思源宋体、霞鹜文楷、缝合像素**三套中文字体，设置里点选就能看到真实游戏样式预览，字号和排版自动适配，无需折腾。
- **一键打包分享补丁**：支持导出 TXT / JSON / CSV / Markdown，也可直接打包**独立离线汉化补丁 `.zip`**，发给朋友解压即玩，无需装本工具。

---

## 下载与安装

### 方式一：网盘下载整合包（推荐，解压即用）

> **网盘下载链接**
> - 夸克网盘：`[在此填入你的网盘链接]`（提取码：`[待填写]`）
> - 百度网盘：`[在此填入你的网盘链接]`（提取码：`[待填写]`）

### 方式二：GitHub Releases 分卷下载

前往本仓库右侧 [Releases](https://github.com/mingminghh611/UL10nForge/releases) 页面下载发布包。因为单文件大小限制，完整包拆分为几个压缩包，按需下载并**全部解压到同一个文件夹**即可：

| 压缩包 | 说明 | 是否必下 |
| :--- | :--- | :--- |
| `UL10nForge-v1.0.0-Core.zip` | 主程序 + 内置 Python + BepInEx/XUAT 钩子包 + 三套中文字体 | **必下**（仅用在线 API 下这一个就够） |
| `UL10nForge-v1.0.0-LocalEngine.zip` | 本地离线模型运行库（CUDA + Vulkan） | 用本地离线翻译时下载 |
| `UL10nForge-v1.0.0-Model-Translate.zip` | 本地主翻译模型（Hy-MT3 1.8B） | 用本地离线翻译时下载 |
| `UL10nForge-v1.0.0-Model-Reason-Part*.zip` | 本地背景识别与审校模型（Qwen3.5 4B，拆分分卷） | 可选（开「直翻模式」可不下） |

> 解压到同一个文件夹后，直接双击根目录的 **`启动UL10nForge.bat`**，会自动在浏览器打开工作台（`http://127.0.0.1:18765`）。

### 方式三：从源码运行（适合开发者 / 纯在线 API）

需要 Windows 10/11 和 Python 3.12+：

```bash
git clone https://github.com/mingminghh611/UL10nForge.git
cd UL10nForge
pip install -r requirements.txt
python main.py
```

---

## 使用步骤

1. **打开游戏**：点击「打开游戏」，选择 Unity 游戏所在的文件夹。
2. **一键自动汉化**：点击「一键自动汉化」，工具会自动扫描静态菜单、翻译并部署 `BepInEx + XUnity.AutoTranslator` 钩子和中文字体。
3. **启动游玩**：点击「启动游戏」直接开玩。遇到动态剧情文本会自动实时翻译；想微调某句译文，在游戏中按 **`Alt+L`** 呼出悬浮窗修改保存即可生效。

---

## 常见问题

<details>
<summary><b>1. 会破坏游戏文件或导致坏档吗？怎么卸载？</b></summary>

不会。工具只往游戏目录放入独立的钩子插件和词典，不修改游戏原文件。在工作台点「更多 ▾ → 卸载汉化插件」即可彻底清理干净。
</details>

<details>
<summary><b>2. 为什么有些游戏扫出来的文本很少，甚至为 0？</b></summary>

为了绝对安全，扫描只读取静态界面文本，不碰代码和脚本。很多游戏的剧情文本是在运行时动态加载的，直接点「启动游戏」玩到对应画面，钩子就会自动实时翻译并保存。
</details>

<details>
<summary><b>3. 游戏中按 <code>Alt+L</code> 没反应或被游戏盖住？</b></summary>

在工作台「更多 ▾」勾选「无边框启动游戏」（或在游戏内切为无边框窗口模式），避免独占全屏遮挡系统悬浮窗。
</details>

<details>
<summary><b>4. 全新 Windows 电脑打开本地引擎报错缺 DLL？</b></summary>

安装微软官方的 **Visual C++ 2015–2022 Redistributable (x64)** 即可。如果只用在线 API，不需要本地引擎。
</details>

---

## 致谢与许可

本工具依赖以下优秀的开源项目与免费开源字体，在此致谢：

- 钩子与加载器：[BepInEx](https://github.com/BepInEx/BepInEx)（[协议](BepInEx-LICENSE.txt)） · [XUnity.AutoTranslator](https://github.com/bbepis/XUnity.AutoTranslator)（[协议](XUnity.AutoTranslator-LICENSE.txt)） · [MelonLoader](https://github.com/LavaGang/MelonLoader)
- 运行库与工具：[llama.cpp](https://github.com/ggml-org/llama.cpp) · [UnityPy](https://github.com/K0lb3/UnityPy) · [FastAPI](https://github.com/fastapi/fastapi)
- 中文字体：[思源宋体 Noto Serif CJK](https://github.com/notofonts/noto-cjk) · [霞鹜文楷 LXGW WenKai](https://github.com/lxgw/LxgwWenKai) · [缝合像素字体 Fusion Pixel](https://github.com/TakWolf/fusion-pixel-font)

> 仅用于个人游戏本地化学习与体验，请支持正版游戏。
