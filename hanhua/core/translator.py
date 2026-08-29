from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from hanhua.core.engine_strings import CORE_MENU_SOURCE_TERMS
from hanhua.core.models import ApiConfig
from hanhua.core.quality import _CJK

class _RetryableStatusError(RuntimeError):
    """瞬时状态码（429/500/503/504）→ 按指数退避重试。"""


class _FatalStatusError(RuntimeError):
    """服务端明确拒绝（4xx）或坏状态（502）→ 立即失败交给上层恢复。"""


MAX_RETRIES = 3
# 可重试的瞬时状态码。502 不在其中：本地 llama-server 返回 502 说明服务
# 已进入坏状态（CUDA OOM / 请求处理崩溃），重试只会排队叠加、雪崩更重 →
# 快速失败交给上层恢复循环（重启服务后重试）。
RETRY_STATUS = {429, 500, 503, 504}
BUILTIN_UI_REFERENCES = (
    ("Settings", "设置"),
    ("Quit", "退出"),
    ("Resolution", "分辨率"),
    ("SFX", "音效"),
    ("Volume", "音量"),
    ("Resume", "继续"),
    # 2026-08-14 用户实证：play 被译「播放」且多次报告——此前不在
    # 内置引用表，模型自由发挥最常见义「播放」；按钮/菜单语境下
    # Play 指「开始游戏」。入表后 prompt 注入 + Q1 语义门 + 主循环
    # 确定性替换三重生效（审核系统提示术语段同源：Start=开始）
    ("Play", "开始"),
    ("Controls", "控制"),
    # 高频回显词（真实语料：cell-machine/final-shot 'back'、faerie-afterlight
    # 'hello'、deepest-sword 'press any key'、hybrid-presence 'Default' 模型
    # 回显原文）→ 参考译文引导模型输出中文
    ("Back", "返回"),
    ("Hello", "你好"),
    ("Press any key", "按任意键"),
    ("Default", "默认"),
    # 独立游戏平台名 itch.io（backrooms 实证：'available at itch page'
    # 模型把 itch 当普通词直译「痒页面」；保留型引用引导模型保留平台名
    # → 'itch 页面'。上下文均为 "on/at itch (page/store/…)" 平台语境，
    # 普通词「痒」在游戏文本中几乎不出现，保留引用误伤风险可忽略）
    ("itch", "itch"),
    # Unity Input System 标准操作提示（containment 实证：'Interact hold'
    # 批量首译回显 + 词级补译跳过 TitleCase + 专名重译注入 (Interact,
    # Interact) 后模型把整条当术语回显）→ 短语级参考译文直接引导；
    # 单独 "Interact" 提示词由动作词排除（见 _retry_with_proper_name_
    # reference 的 _ACTION_VERB_ZH 过滤）避免专名引用陷阱
    ("Interact hold", "交互（长按）"),
    ("Interact", "交互"),
)
BUILTIN_UI_SOURCE_TERMS = CORE_MENU_SOURCE_TERMS


def merge_translation_references(glossary=()) -> tuple[tuple[str, str], ...]:
    """Combine built-in UI references with user terms; user terms win."""
    user_pairs: list[tuple[str, str]] = []
    for item in glossary:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            source, target = item[0], item[1]
        elif isinstance(item, dict):
            source, target = item.get("term"), item.get("translation")
        else:
            source = getattr(item, "term", None)
            target = getattr(item, "translation", None)
        if (isinstance(source, str) and source.strip()
                and isinstance(target, str) and target.strip()):
            user_pairs.append((source, target))
    user_sources = {source.casefold() for source, _ in user_pairs}
    return tuple(
        pair for pair in BUILTIN_UI_REFERENCES
        if pair[0].casefold() not in user_sources
    ) + tuple(user_pairs)


@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0


def normalize_base_url(base_url: str, provider: str) -> str:
    url = base_url.strip().rstrip("/")
    if provider == "anthropic":
        if url.endswith("/messages"):
            return url
        return url + ("/messages" if url.endswith("/v1") else "/v1/messages")
    if url.endswith("/chat/completions"):
        return url
    return url + ("/chat/completions" if url.endswith("/v1") else "/v1/chat/completions")


class ServiceUnavailableError(RuntimeError):
    """翻译服务不可达（连接失败/连接超时/读取超时）——服务已死或未启动。

    F42（8morelives 实证 2026-08-16）：本地 llama-server 长任务中偶发被
    静默终止，客户端默认 120s 超时 × 3 重试 = 每批死等数分钟（774 条后
    龟速爬行永不完成）。连接类错误快速失败（不重试），由批量层触发
    服务重启回调后继续。
    """


class BaseClient:
    def __init__(self, config: ApiConfig, transport_factory: Callable | None = None):
        self.config = config
        provider = "openai" if config.mode == "local" else config.provider
        self.url = normalize_base_url(config.base_url, provider)
        # 连接复用（2026-08-29 实证：本地逐条翻译模式下 _post 每请求
        # 新建 httpx.Client → 每次 TCP 握手 + TLS（本地也走一次 loopback
        # 连接建立），单条 ~1.9s 总开销中模型推理只占 ~100ms，连接建立
        # 占可观比例。默认工厂改为实例级持久连接（keep-alive），多线程
        # 共享一个 httpx.Client 是线程安全的。注入 transport_factory 的
        # 调用方（测试/mock）保持原「每次新开」语义不变。
        self._owned_client: httpx.Client | None = None
        if transport_factory is None:
            self._owned_client = httpx.Client(timeout=config.timeout)
            self._factory = lambda: self._owned_client
        else:
            self._factory = transport_factory

    def close(self) -> None:
        """释放持久连接（长任务结束后调用；未持有则无操作）。"""
        client = self._owned_client
        self._owned_client = None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 关闭失败不阻断
                pass

    def _post(self, url: str, headers: dict, payload: dict) -> tuple[httpx.Response, Usage]:
        last_err: Exception | None = None
        # 持久连接（2026-08-29 修复）：httpx.Client 的 __exit__ 会把实例置为
        # CLOSED，因此「with client:」第二次进入即抛 "Cannot reopen a client
        # instance"——实测 Give Me Strength 122 条 request_error 全因此。
        # 持久路径直接 .post()（连接池内部复用，线程安全）；注入 factory 的
        # 调用方（测试/mock 每请求新 client）保留原 with 语义。
        owned = self._owned_client
        for attempt in range(MAX_RETRIES):
            try:
                if owned is not None:
                    resp = owned.post(url, headers=headers, json=payload)
                else:
                    with self._factory() as client:
                        resp = client.post(url, headers=headers, json=payload)
                # 响应体必须在连接归还前读完；非流式请求 content 已缓冲，
                # 后续 resp.json() 从缓存重解析，安全
                body = resp.json()
                if resp.status_code in RETRY_STATUS:
                    raise _RetryableStatusError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    # 4xx / 502：明确拒绝或服务坏状态 → 立即失败，不重试
                    raise _FatalStatusError(
                        f"HTTP {resp.status_code}: {resp.text[:300]}")
                return resp, self._parse_usage(body)
            except _FatalStatusError:
                raise
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # F42：服务不可达（进程死亡/未启动）→ 快速失败，
                # 重试无意义（服务不会自己回来），交批量层重启。
                # 持久连接遇服务重启会拿到陈旧连接的 ConnectError/
                # RemoteProtocolError → 清空连接池后重试一次
                self._reset_owned_client()
                raise ServiceUnavailableError(
                    f"翻译服务不可达：{type(e).__name__}") from e
            except httpx.ReadTimeout as e:
                # 服务进程活着但无响应（可能崩溃中/卡死）→ 同样快速
                # 失败触发重启（等待只会让每批耗时 120s×3）
                raise ServiceUnavailableError(
                    f"翻译服务无响应：{type(e).__name__}") from e
            except Exception as e:  # noqa: BLE001 瞬时错误（含 _RetryableStatusError）
                last_err = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(1.5 * (2 ** attempt))
        raise RuntimeError(f"请求失败（重试{MAX_RETRIES}次）：{last_err}")

    def _reset_owned_client(self) -> None:
        """服务重启后清空持久连接池（陈旧 keep-alive 连接会持续失败）。"""
        client = self._owned_client
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            self._owned_client = httpx.Client(timeout=self.config.timeout)
            self._factory = lambda: self._owned_client

    def _parse_usage(self, data: dict) -> Usage:
        raise NotImplementedError

    def chat(self, system: str, messages: list[dict]) -> tuple[str, Usage]:
        raise NotImplementedError


class OpenAIClient(BaseClient):
    def chat(self, system: str, messages: list[dict]) -> tuple[str, Usage]:
        payload = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        # 结构化输出：部分兼容端点不支持 response_format，失败时降级重试
        try:
            payload["response_format"] = {"type": "json_object"}
            resp, usage = self._post(self.url, headers, payload)
        except RuntimeError as e:
            if "response_format" not in str(e) and not any(
                    s in str(e) for s in ("400", "validation", "invalid_request")):
                raise
            del payload["response_format"]
            resp, usage = self._post(self.url, headers, payload)
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        return content, usage

    def _parse_usage(self, data: dict) -> Usage:
        u = data.get("usage", {})
        return Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


class LocalOpenAIClient(OpenAIClient):
    """llama-server adapter following Hy-MT2's no-system-prompt contract."""

    accepts_plain_single = True

    _probed_ctx: int | None = None   # 实际可用上下文（探测一次缓存）

    def probe_context_size(self) -> int | None:
        """查询服务端实际可用上下文。

        2026-08-14 用户实证：request (2889 tokens) exceeds context (2048)
        ——配置 --ctx-size 6144 但实际 2048。llama-server 在 KV 显存不足
        时启动自动降级 ctx（--parallel 3 × 6144 放不下 → 每槽 6144/3=
        2048），客户端按配置组装 prompt 必超限。组装前探测实际值，按
        实际预算（见 BatchTranslator）。失败返回 None（调用方回退配置
        值）；探测一次缓存，避免每批请求都查。
        """
        if self._probed_ctx is not None:
            # -1 = 失败缓存：继续返回 None（契约：失败一律 None，
            # 调用方回退配置值；只返回正数实际 ctx）
            return self._probed_ctx if self._probed_ctx > 0 else None
        try:
            base = self.url.rsplit("/chat/completions", 1)[0].rstrip("/")
            owned = self._owned_client
            if owned is not None:
                resp = owned.get(base + "/props", timeout=10, headers={
                    "Authorization": f"Bearer {self.config.api_key}"})
            else:
                with self._factory() as client:
                    resp = client.get(base + "/props", timeout=10, headers={
                        "Authorization": f"Bearer {self.config.api_key}"})
            if resp.status_code != 200:
                raise RuntimeError(f"props status {resp.status_code}")
            ctx = (resp.json().get("default_generation_settings")
                   or {}).get("n_ctx")
            if isinstance(ctx, int) and ctx > 0:
                self._probed_ctx = ctx
                return ctx
        except Exception:  # noqa: BLE001 探测失败不阻断（回退配置值）
            pass
        self._probed_ctx = -1   # 失败标记，不再重试
        return None

    _TARGET_LANGUAGE_NAMES = {
        "zh-cn": "Simplified Chinese",
        "zh-hans": "Simplified Chinese",
        "zh-tw": "Traditional Chinese",
        "zh-hant": "Traditional Chinese",
        "en": "English",
        "ja": "Japanese",
        "ko": "Korean",
        "fr": "French",
        "de": "German",
        "es": "Spanish",
        "ru": "Russian",
    }

    # 单用户消息源文本长度上限（字符）：llama-server 槽位 1024 tokens，
    # 英文约 3 字符/token——3183 字符歌词 = 1099 tokens 超限被拒
    # （deadbeat 实证：request_error）。700 字符 ≈ 230 tokens，留足
    # prompt 引导与术语引用空间
    _MAX_PROMPT_SOURCE_CHARS = 700

    def translate_text(
            self, source_text: str, target_lang: str,
            glossary=()) -> tuple[str, Usage]:
        """Translate one segment with Hy-MT2's official single-user prompt."""
        if len(source_text) > self._MAX_PROMPT_SOURCE_CHARS:
            return self._translate_chunked(
                source_text, target_lang, glossary)
        return self._translate_single(source_text, target_lang, glossary)

    def _translate_single(
            self, source_text: str, target_lang: str,
            glossary=()) -> tuple[str, Usage]:
        language_name = self._TARGET_LANGUAGE_NAMES.get(
            str(target_lang).strip().casefold(), str(target_lang).strip())
        lines: list[str] = []
        terms = [
            (str(source), str(target))
            for source, target in glossary
            if str(source).strip() and str(target).strip()
            and str(source).casefold() in source_text.casefold()
        ]
        if terms:
            lines.append("Reference the following translations:")
            lines.extend(
                f"{source} translates to {target}"
                for source, target in terms
            )
            lines.append("")
        lines.extend([
            f"Translate the following text into {language_name}. "
            "Note that you should only output the translated result without "
            "any additional explanation:",
            "",
            source_text,
        ])
        return self.chat("", [{"role": "user", "content": "\n".join(lines)}])

    def _translate_chunked(
            self, source_text: str, target_lang: str,
            glossary=()) -> tuple[str, Usage]:
        """超长文本按行分块翻译后拼接（deadbeat 歌词 3183 字符实证：
        单条请求 1099 tokens 超槽位被拒；逐块请求每块在槽位内）。

        分块边界优先换行（歌词/长文天然分行）；无换行按词切。块间以
        \n 拼接保持行结构近似。逐块串行（llama-server 槽位共享）。"""
        chunks, joiner = self._chunk_source(source_text)
        parts: list[str] = []
        total = Usage(0, 0)
        for chunk in chunks:
            out, usage = self._translate_single(
                chunk, target_lang, glossary)
            parts.append(out)
            total = Usage(
                total.prompt + usage.prompt,
                total.completion + usage.completion)
        return joiner.join(parts), total

    def translate_lyrics(
            self, source_text: str, target_lang: str,
            glossary=()) -> tuple[str, Usage]:
        """歌词/韵律行专用翻译：中文引导 + 输出限长 + 高重复惩罚。

        1.8B 模型对纯英文歌词句稳定续写英文而非翻译（deadbeat
        'Tonight, the moon has rose...' 2677 字符歌词实证：常规 prompt
        输出英文续写被质量门拒绝）——中文引导显式声明「歌词翻译」触发
        翻译意愿；repeat_penalty 1.35 抑制循环续写；max_tokens 按源句
        长缩放，在续写垃圾出现前截断译文。逐句调用（multiline repair
        已拆句）。

        超长歌词（> _MAX_PROMPT_SOURCE_CHARS）分块翻译：1.8B 对超长
        歌词的单次输出上限约 700 字符（deadbeat 'Modern-day killers'
        3183 字符歌词实证：max_tokens 放大后模型 ~430 tokens 主动 EOS，
        输出 700 字符摘要式译文——开头+结尾、中间 2/3 丢失）→ 分块后
        每块 ≤700 字符，模型对每块输出完整译文，拼接恢复全歌。"""
        if len(source_text) > self._MAX_PROMPT_SOURCE_CHARS:
            chunks, joiner = self._chunk_source(source_text)
            parts: list[str] = []
            total = Usage(0, 0)
            for chunk in chunks:
                out, usage = self._translate_lyrics_single(
                    chunk, target_lang, glossary)
                parts.append(str(out).strip())
                total = Usage(
                    total.prompt + usage.prompt,
                    total.completion + usage.completion)
            return joiner.join(parts), total
        return self._translate_lyrics_single(
            source_text, target_lang, glossary)

    def _translate_lyrics_single(
            self, source_text: str, target_lang: str,
            glossary=()) -> tuple[str, Usage]:
        """单块歌词翻译（translate_lyrics 内部实现；分块路径逐块复用）。"""
        language_name = self._TARGET_LANGUAGE_NAMES.get(
            str(target_lang).strip().casefold(), str(target_lang).strip())
        lines: list[str] = []
        terms = [
            (str(source), str(target))
            for source, target in glossary
            if str(source).strip() and str(target).strip()
            and str(source).casefold() in source_text.casefold()
        ]
        if terms:
            lines.append("Reference the following translations:")
            lines.extend(
                f"{source} translates to {target}"
                for source, target in terms
            )
            lines.append("")
        lines.extend([
            f"这是一段歌词，翻译成{language_name}。只输出翻译后的歌词文本，",
            "不要解释，不要续写原文，不要输出任何英文或原文。",
            "",
            source_text,
        ])
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": "\n".join(lines)}],
            "temperature": 0.7,
            "top_p": 0.6,
            "top_k": 20,
            "repeat_penalty": 1.35,
            # 歌词多为中英日混写，译文中文 1 字符 ≈ 1.2 token——旧缩放
            # len//3+32 按英语假设（3 字符/token）给预算，3183 字符歌词
            # 只给 1093 tokens → 中文翻译 ~1200 字符后预算耗尽，模型
            # 续写原文英文回显被判 target_script_mismatch（deadbeat
            # 'Modern-day killers' 歌词 3 条实证）。按 1 字符 ≈ 1 token
            # 缩放 + 余量，配合 llama-server ctx 6144（prompt ~1100 +
            # 完整译文 ~3100 tokens 装得下）。
            "max_tokens": min(self.config.max_tokens,
                              len(source_text) + 128),
        }
        response, usage = self._post(
            self.url,
            {"Authorization": f"Bearer {self.config.api_key}"}, payload,
        )
        content = response.json()["choices"][0]["message"]["content"]
        return content, usage

    @classmethod
    def _chunk_source(cls, text: str) -> tuple[list[str], str]:
        """按行切 ≤_MAX_PROMPT_SOURCE_CHARS 块（无换行按词切）。

        返回 (块列表, 拼接分隔符)——分隔符与切分单位一致（\n 或空格），
        块译文按同分隔符拼接保持原文结构无损。"""
        limit = cls._MAX_PROMPT_SOURCE_CHARS
        if "\n" in text:
            chunks: list[str] = []
            cur = ""
            for line in text.split("\n"):
                if cur and len(cur) + 1 + len(line) > limit:
                    chunks.append(cur)
                    cur = line
                else:
                    cur = f"{cur}\n{line}" if cur else line
            if cur:
                chunks.append(cur)
            return chunks, "\n"
        chunks = []
        cur = ""
        for word in text.split(" "):
            if cur and len(cur) + 1 + len(word) > limit:
                chunks.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}" if cur else word
        if cur:
            chunks.append(cur)
        return chunks, " "

    def chat(self, system: str, messages: list[dict]) -> tuple[str, Usage]:
        merged = "\n\n".join(
            part for part in [system.strip()] + [
                str(message.get("content", "")).strip() for message in messages
            ] if part
        )
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": merged}],
            "temperature": 0.7,
            "top_p": 0.6,
            "top_k": 20,
            "repeat_penalty": 1.05,
            "max_tokens": self.config.max_tokens,
        }
        response, usage = self._post(
            self.url,
            {"Authorization": f"Bearer {self.config.api_key}"}, payload,
        )
        content = response.json()["choices"][0]["message"]["content"]
        return content, usage


class AnthropicClient(BaseClient):
    def chat(self, system: str, messages: list[dict]) -> tuple[str, Usage]:
        payload = {
            "model": self.config.model,
            "system": system,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        resp, usage = self._post(self.url, {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
        }, payload)
        content = "".join(b.get("text", "") for b in resp.json().get("content", [])
                          if b.get("type") == "text")
        return content, usage

    def _parse_usage(self, data: dict) -> Usage:
        u = data.get("usage", {})
        return Usage(u.get("input_tokens", 0), u.get("output_tokens", 0))


def strip_prompt_echo(text: str, system: str, source: str) -> str:
    """清洗模型输出中的提示词/原文回显（2026-08-14/15 用户实证：难翻译
    内容——乱码/纯符号/格式串——小模型把整个 prompt（提示词+原文）
    当作输出回显，工具页译文栏直接出现提示词全文）。

    三层清洗（空白归一后比较，剥除量以 system/source 长度为上限）：
    ① 提示词前缀（输出以 system 开头 → 模型整体回显了提示词）；
    ② 提示词整行回显（输出中逐行复述提示词的长行 → 删行）；
    ③ 原文前缀（输出以原文开头 → 模型回显原文后跟译文）。
    正常译文与提示词/原文无公共前缀/整行重合，零影响；「模型只回显
    原文未翻译」时剥完剩空串（比把提示词塞给用户安全）。
    """

    def _common_prefix_len(a: str, b: str) -> int:
        n = 0
        for ca, cb in zip(a, b):
            if ca != cb:
                break
            n += 1
        return n

    t = str(text or "").strip()
    sys_text = str(system or "").strip()
    if sys_text and len(sys_text) > 20:
        n = _common_prefix_len(t, sys_text)
        if n >= 20:                     # 至少 20 字符公共前缀，防误伤
            t = t[min(n, len(sys_text)):].strip()
        # 整行回显（模型逐行复述提示词再跟译文/解释）——增强：模型常
        # 加变形（引号/System:/编号/```包裹/合并行）——行包含匹配
        sys_lines = {line.strip() for line in sys_text.splitlines()
                     if len(line.strip()) >= 15}
        if sys_lines:
            kept = [line for line in t.splitlines()
                    if not any(_is_prompt_line(line.strip(), s)
                               for s in sys_lines)]
            if len(kept) != len(t.splitlines()):
                t = "\n".join(kept).strip()
        # 大段剥离：输出中提示词关键行命中 ≥3 行 → 模型整体回显了
        # 提示词（尾部可能跟了译文）——删到最后一个提示词行之后
        if sys_lines and len(t) > 0:
            lines = t.splitlines()
            last_hit = -1
            for i, line in enumerate(lines):
                if any(_is_prompt_line(line.strip(), s) for s in sys_lines):
                    last_hit = i
            if last_hit >= 2:
                t = "\n".join(lines[last_hit + 1:]).strip()
    src_text = str(source or "").strip()
    if src_text:
        # 行级归一（2026-08-16 增强）：模型把原文带标签/编号/引号回显
        # （'原文：hello world'、'2. hello world'、'"hello world"'）——
        # 归一后与原文比对，命中的行从输出剔除（保留其他译文行）
        import re as _re
        _tag_re = _re.compile(r"^(?:原文|翻译|译文|Source|Text|content)\s*[:：]\s*",
                              _re.I)
        _num_re = _re.compile(r"^[0-9]+[.、)）]\s*")
        _quote = "\"'「『【（("
        lines = t.splitlines()
        kept = []
        changed = False
        for line in lines:
            s = line.strip()
            s = _num_re.sub("", s)
            s = _tag_re.sub("", s)
            if len(s) >= 2 and s[0] in _quote and s[-1] in _quote:
                s = s[1:-1].strip()
            if s == src_text or (s and src_text.startswith(s)
                                 and len(s) >= len(src_text) * 0.6):
                changed = True
                continue
            kept.append(line)
        if changed:
            # 模型用了编号/标签格式输出 → 保留行也剥编号前缀
            # （'3. 你好，世界' → '你好，世界'）
            kept = [_num_re.sub("", line) for line in kept]
            t = "\n".join(kept).strip()
        n = _common_prefix_len(t, src_text)
        if n >= 3 and n >= len(src_text) * 0.6:
            remainder = t[min(n, len(src_text)):].strip()
            # 仅当剥后剩余为「纯英文/空」（整段原文回显）才剥离；剩余
            # 含中文（如 'Out of the Loop工作室' —— 模型保留品牌名
            # Out of the Loop + 直译 studio→工作室，是正确译文）→ 保留
            # 整句。2026-08-26 实证：旧逻辑对 'Out of the Loop工作室'
            # 剥前缀剥成 '工作室'，误报「未产出译文」；且绝不剥原句单词
            # 前缀（'SCP-173已经…' 的 'SCP' 是编号标识，剥掉 → '-173已经…'
            # 断义）。剥除要求「整段原文回显」（剩余不含中文），否则保留
            # 完整输出——确保 'Doctor Strange is the main character' 的
            # 前缀 'Doctor' 不回显（裸 translate_text 的 'Doctor Strange
            # is the main character.' 整段无中文回显 → 剥成 '.' 无义）——
            # 但仍以无中文为界，含中文输出（如译文 'Doctor Strange is 主角'
            # 前缀含中文）完整保留。
            if not _CJK.search(remainder):
                t = remainder
        # 引号包裹回显：模型把原文用引号包着返回（"原文"/'原文'/
        # 「原文」）——剥引号后再处理
        if len(t) >= 2 and t[0] in "\"'「『" and t[-1] in "\"'」』":
            inner = t[1:-1].strip()
            if inner == src_text:
                t = ""
            elif inner.startswith(src_text):
                t = inner[len(src_text):].strip()
    # ④ 终末回显护栏（2026-08-20 用户实证：{}【】等不可翻译输入，
    # 小模型把整段提示词当输出回显；模型常改行/合并/穿插原文，使
    # ① 前缀剥除与 ② 逐行匹配漏判——提示词被拆成 <15 字符短句时 ②
    # 不触发，前缀剥除后第二句提示词残留）。去空白归一后若剩余仍是
    # 提示词的连续片段（≥10 字符），判纯回显 → 返回空串（比塞提示词
    # 给用户安全；正常译文不会是翻译指令的长片段，零误伤）。
    if sys_text and len(sys_text) > 20:
        import re as _re_g
        _squeeze = lambda s: _re_g.sub(r"\s+", "", s or "")
        _t_n, _s_n = _squeeze(t), _squeeze(sys_text)
        if _s_n and len(_t_n) >= 10 and _t_n in _s_n:
            return ""
    return t


def translate_source_directive(
        client: BaseClient, source_text: str, target_lang: str = "zh-CN",
        glossary=()) -> tuple[str, Usage]:
    """单条直译：Hy-MT2 官方 prompt 或中文显式指令，供人工审核 AI 翻译等
    交互场景复用。中文显式指令（「请将以下文本翻译为简体中文，直接输出
    译文」）对 1.8B 小模型是翻译意图最强信号（2026-08-26 实测：英文 prompt
    下 'Out of the Loop studio' 稳定回显原文，中文指令下正确输出
    'Out of the Loop 工作室'）；返回原文未翻译结果由调用方清洗。
    """
    if callable(getattr(client, "translate_text", None)):
        return client.translate_text(source_text, target_lang, glossary)
    language_name = client._TARGET_LANGUAGE_NAMES.get(
        str(target_lang).strip().casefold(), str(target_lang).strip()) \
        if hasattr(client, "_TARGET_LANGUAGE_NAMES") else str(target_lang).strip()
    lines: list[str] = []
    terms = [
        (str(source), str(target))
        for source, target in glossary
        if str(source).strip() and str(target).strip()
        and str(source).casefold() in source_text.casefold()
    ]
    if terms:
        lines.append("参考以下译法：")
        lines.extend(
            f"{source} 翻译为 {target}"
            for source, target in terms
        )
        lines.append("")
    lines.extend([
        f"请将以下文本翻译为{language_name}，直接输出译文，不要输出任何其他内容：",
        "",
        source_text,
    ])
    return client.chat("", [{"role": "user", "content": "\n".join(lines)}])


# 交互式单条直译专用：动作词身份表（知识库词表，跨游戏通用）——
# TitleCase 动作词（Interact/Press/Use…）不是专名，注入 (词,词) 保留
# 引用会让小模型把整条短语当术语回显（containment 实证）；裸翻译模型
# 反而能直译（'互动保持'）。与 batch_translator._retry_with_proper_
# name_reference 同源判定（避免重复 import batch_translator 引入重量级
# 依赖，这里按需导入）。
_ACTION_VERB_ZH = None
_DISPLAY_WORDS_CASEFOLD = None

# 英语常见功能词/代词/句首虚词：TitleCase 形态下仍非专名（'This is…'、
# 'The…'、'And'），与 batch_translator 的 UI 词表互补——这些词在真实
# 专名提取中极少是名称（人名/地名/品牌名几乎不落入此表）。
_STOP_WORDS_CASEFOLD = frozenset(
    w.casefold() for w in
    "the a an and or but nor for yet so this that these those i you he she it "
    "we they me him her us them my your his her its our their mine yours ours "
    "to of in on at by with from into onto upon of per via as than then now "
    "was were is are be been being am do does did will would shall should can "
    "could may might must has have had not no off out up down over under again "
    "further once here there when where why how all any both each few more most "
    "other some such only own same very just too also quite rather".split())


def _load_translate_helpers():
    """按需加载专名提取用词表（延迟 import 规避循环依赖）。"""
    global _ACTION_VERB_ZH, _DISPLAY_WORDS_CASEFOLD
    if _ACTION_VERB_ZH is None:
        from hanhua.core.knowledge import _ACTION_VERB_ZH as _AV
        from hanhua.core.placeholders import DISPLAY_WORDS
        _ACTION_VERB_ZH = _AV
        _DISPLAY_WORDS_CASEFOLD = {w.casefold() for w in DISPLAY_WORDS}


def proper_words_of(source: str) -> list[str]:
    """提取原文中可注入保留引用的专名（TitleCase 且非 UI 词）。

    与批量翻译 _retry_with_proper_name_reference 同源启发式。相邻
    TitleCase 词合并为一个专名（'Doctor Strange'、'Out of the Loop'
    的品牌名整段保留）；'This is a simple sentence'（无专名）→ 空，
    不触发保留引用（真漏翻仍失败）。
    """
    _load_translate_helpers()
    tokens = str(source or "").split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        w = tokens[i].strip(".,;:!?\"'()[]{}")
        if (w and w[0].isupper() and w[1:].islower()
                and len(w) >= 3
                and w.casefold() not in _DISPLAY_WORDS_CASEFOLD
                and w.casefold() not in _ACTION_VERB_ZH
                and w.casefold() not in _STOP_WORDS_CASEFOLD
                and w.casefold() not in BUILTIN_UI_SOURCE_TERMS):
            merged = w
            j = i + 1
            while j < len(tokens):
                nxt = tokens[j].strip(".,;:!?\"'()[]{}")
                if (nxt and nxt[0].isupper() and nxt[1:].islower()
                        and nxt.casefold() not in _DISPLAY_WORDS_CASEFOLD
                        and nxt.casefold() not in _ACTION_VERB_ZH
                        and nxt.casefold() not in _STOP_WORDS_CASEFOLD):
                    merged += " " + nxt
                    j += 1
                else:
                    break
            out.append(merged)
            i = j
        else:
            i += 1
    return out


def _is_only_punct(text: str) -> bool:
    """是否纯标点/符号残渣（AI 翻译剥原文回显后只剩 '.' 等无义标点）。

    与审校页 _is_only_punctuation 同源（统一收口到 translator）。
    """
    import unicodedata
    if not text:
        return False
    for ch in text:
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N"):
            return False
    return True


def _direct_zh_directive(client, source: str,
                         target_lang: str) -> str:
    """中文「逐词补译」指令：整条原文作一个整体译名强制翻译（禁止回显）。

    1.8B 模型把整段原文当整体回显时（'Out of the Loop studio' 等短
    专名/品牌名），中文显式「整体译名」指令是翻译意图最强信号，实测
    稳定产出 'Out of the Loop 工作室'。返回 strip 后的原文剥离结果。
    """
    try:
        # 2026-08-26 用户实证「adsadsadasda 等实在无法翻译的文本」：中文
        # 「整体译名」指令下 1.8B 仍回显整段原文（模型无法区分不可译文本
        # 与名称）。明确禁止回显 + 若无法翻译就输出原文（保证绝无空输出，
        # 交 translate_interactive 的 ④ 原文兜底；剥回显后为空也走兜底）。
        direct = ("请将以下名称翻译为简体中文，直接输出译名，不得回显"
                  "原文，不要添加任何解释。若确实无法翻译，直接输出原"
                  "文本身：\n\n" + source)
        out, _usage = client.chat("", [{"role": "user",
                                        "content": direct}])
    except Exception:  # noqa: BLE001 二次尝试失败交 ④ 原文兜底
        return ""
    return strip_prompt_echo(out, "", source)


def translate_interactive(client, source_text: str,
                          target_lang: str = "zh-CN",
                          glossary=()) -> str:
    """交互式单条直译：本地/API 通用多级降级链，返回最终译文（绝不空）。

    供「翻译」工具页与审校页「AI 翻译」共用的稳健翻译入口（2026-08-26
    修复「模型未产出译文」误报——1.8B 在英文 prompt 下回显简单原文被
    strip_prompt_echo 剥空后误报未产出）：

    ① 中文显式指令 + 术语引用（翻译意图最强信号，实测正确产出）；
    ② 剥离指令前缀回显的干净译文为空 → 中文「逐词补译」指令（把整条
       原文当整体译名，根治品牌名被剥成空串的误杀）；
    ③ 仍空 → 专名「保留引用」重译（'Doctor Strange 是主角。'）；
    ④ 全空 → 返回原文兜底（2026-08-26 用户要求「绝不能空输出」：模型
       确无法产出时直接输出原文本，保证交互 UI 必有可见输出）。

    各级输出经 strip_prompt_echo 清洗；纯标点残渣（剥原文回显后剩 '.'）
    不算译文，继续降级。调用方负责本地模式 ensure_running 与 create_client。
    """
    from hanhua.core.quality import _CJK as _cjk_re
    out, _usage = translate_source_directive(
        client, source_text, target_lang, glossary)
    clean = strip_prompt_echo(out, "", source_text)
    # ① 有中文即视为有效译文（含 'Out of the Loop 工作室' 这类品牌名
    # 保留 + 直译组合，正是期望译文）。无中文（纯英文/纯标点）视为回显
    # 或不可译 → 继续降级（与批量翻译 untranslated_text 口径一致：
    # 纯英文输出不视为已翻译）。
    if clean.strip() and _cjk_re.search(clean):
        return clean.strip()
    # ② 中文「逐词补译」指令（整条原文作整体译名，禁止回显）。纯标点
    # 残渣不算译文 → 继续 ③（2026-08-26 修复：此前的 `return ""` 短路
    # 让 ③ 永不触发，与「继续降级」注释矛盾）。
    clean2 = _direct_zh_directive(client, source_text, target_lang)
    if not _is_only_punct(clean2) and clean2.strip():
        return clean2.strip()
    # ③ 专名「保留引用」重译：'Markiplier was here' 注入 (Markiplier,
    # Markiplier) → 模型保留专名只译其余部分 → 'Markiplier 曾来过这里'
    proper_words = proper_words_of(source_text)
    if proper_words:
        try:
            ref_out, _u3 = translate_source_directive(
                client, source_text, target_lang,
                tuple((w, w) for w in proper_words))
            clean3 = strip_prompt_echo(ref_out, "", source_text)
            if not _is_only_punct(clean3) and clean3.strip():
                return clean3.strip()
        except Exception:  # noqa: BLE001 引用重试失败交 ④ 原文兜底
            pass
    # ④ 全空 → 返回原文（用户要求「绝不能空输出」）。
    return str(source_text or "").strip()


def _is_prompt_line(line: str, sys_line: str) -> bool:
    """输出行是否回显了提示词行（含模型常见变形）。

    匹配形态：完全相等 / 提示词+「：」（原有）；提示词嵌入行（引号
    包裹、System:/指令: 前缀、编号前缀、```包裹、模型合并两行提示词）；
    行是提示词行的一部分（模型拆行回显）。要求 sys_line ≥15 字符
    （短行不判——防正常译文短语误伤）。
    """
    if len(sys_line) < 15 or not line:
        return False
    if line == sys_line or line.startswith(sys_line + "："):
        return True
    if sys_line in line:
        return True
    if line in sys_line and len(line) >= 15:
        return True
    stripped = line.strip("\"'`「『【")
    if stripped == sys_line or sys_line in stripped:
        return True
    return False


def create_client(config: ApiConfig, transport_factory: Callable | None = None) -> BaseClient:
    if config.mode == "local":
        return LocalOpenAIClient(config, transport_factory)
    if config.provider == "anthropic":
        return AnthropicClient(config, transport_factory)
    return OpenAIClient(config, transport_factory)


def extract_json_array(text: str) -> list[dict] | None:
    """宽容 JSON 提取：去代码块围栏 → 平衡括号解析 → 支持 {"translations": [...]} 包装与单对象。"""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip()).strip()
    for opener, closer in (("[", "]"), ("{", "}")):
        start = t.find(opener)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(t)):
            if t[i] == opener:
                depth += 1
            elif t[i] == closer:
                depth -= 1
                if depth == 0:
                    chunk = t[start:i + 1]
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, list):
                        return data
                    if isinstance(data, dict) and isinstance(data.get("translations"), list):
                        return data["translations"]
                    if isinstance(data, dict) and "id" in data and "translation" in data:
                        return [data]                       # 单条对象 {"id":..., "translation":...}
    return None


_ID_PAT = re.compile(r'"id"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"')
_TRANSLATION_PAT = re.compile(
    r'"translation"\s*:\s*("(?:[^"\\]|\\.)*")', re.S)


def extract_json_array_fallback(text: str) -> list[dict] | None:
    """行级兜底：模型输出不是合法 JSON 时，逐条提取 "id" 与 "translation" 字段。
    适用于译文含未转义引号/换行导致整体解析失败的情况。"""
    ids = _ID_PAT.findall(text)
    trs = _TRANSLATION_PAT.findall(text)
    if not ids or len(ids) != len(trs):
        return None
    out: list[dict] = []
    for i, _ in enumerate(ids):
        try:
            out.append({"id": json.loads(f'"{ids[i]}"'), "translation": json.loads(trs[i])})
        except json.JSONDecodeError:
            return None
    return out
