import httpx

from hanhua.core.models import ApiConfig
from hanhua.core.translator import (create_client, extract_json_array,
                                    normalize_base_url)


def _make_transport(handler):
    """构造可重用的 MockTransport handler→（factory, client）：
    factory 每次返回同一 client；client 不进入 with 块，供多次请求复用。
    """
    def handler_wrapper(request: httpx.Request) -> httpx.Response:
        return handler(request)
    client = httpx.Client(transport=httpx.MockTransport(handler_wrapper))
    return (lambda: client), client


def test_normalize_url():
    assert normalize_base_url("https://api.openai.com/v1", "openai") == "https://api.openai.com/v1/chat/completions"
    assert normalize_base_url("https://x.com/v1/chat/completions", "openai") == "https://x.com/v1/chat/completions"
    assert normalize_base_url("https://api.anthropic.com", "anthropic") == "https://api.anthropic.com/v1/messages"
    assert normalize_base_url("https://y.com/v1/messages", "anthropic") == "https://y.com/v1/messages"


def test_openai_client_chat():
    factory, _client = _make_transport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": '[{"id":"e1","translation":"你好"}]'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }))
    client = create_client(
        ApiConfig(provider="openai", base_url="https://t/v1", api_key="k", model="m"),
        transport_factory=factory)
    content, usage = client.chat("sys", [{"role": "user", "content": "u"}])
    assert content == '[{"id":"e1","translation":"你好"}]'
    assert usage.prompt == 10 and usage.completion == 5


def test_local_client_uses_hymt2_single_user_payload_without_response_format():
    captured = {}

    def factory():
        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(request.read() and __import__("json").loads(request.content))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "按 E 键打开"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            })
        return httpx.Client(transport=httpx.MockTransport(handler))

    client = create_client(ApiConfig(
        mode="local", provider="anthropic", base_url="http://127.0.0.1:8080/v1",
        api_key="local-token", model="Hy-MT2-1.8B-Q6_K",
    ), transport_factory=factory)

    content, _usage = client.chat(
        "保留输入按键。", [{"role": "user", "content": "Press E to open"}])

    assert content == "按 E 键打开"
    assert client.url == "http://127.0.0.1:8080/v1/chat/completions"
    assert captured["messages"] == [{
        "role": "user", "content": "保留输入按键。\n\nPress E to open",
    }]
    assert "response_format" not in captured
    assert captured["temperature"] == 0.7
    assert captured["top_p"] == 0.6
    assert captured["top_k"] == 20
    assert captured["repeat_penalty"] == 1.05


def test_local_native_translation_uses_official_english_template_and_terms():
    captured = {}

    def factory():
        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(__import__("json").loads(request.content))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "Key30\nG – 投掷"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8},
            })
        return httpx.Client(transport=httpx.MockTransport(handler))

    client = create_client(ApiConfig(
        mode="local", base_url="http://127.0.0.1:8080/v1",
        api_key="local-token", model="Hy-MT2-1.8B-Q6_K",
    ), transport_factory=factory)

    content, _usage = client.translate_text(
        "Key30\nG - to throw", "zh-CN", [("throw", "投掷")])

    assert content == "Key30\nG – 投掷"
    assert len(captured["messages"]) == 1
    prompt = captured["messages"][0]["content"]
    assert captured["messages"][0]["role"] == "user"
    assert "Reference the following translations:" in prompt
    assert "throw translates to 投掷" in prompt
    assert "Translate the following text into Simplified Chinese" in prompt
    assert prompt.endswith("Key30\nG - to throw")
    assert "JSON" not in prompt
    assert "system" not in captured


def test_anthropic_client_chat():
    def factory():
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-api-key"] == "k"
            body = {"content": [{"type": "text", "text": '[{"id":"e1","translation":"你好"}]'}],
                    "usage": {"input_tokens": 8, "output_tokens": 4}}
            return httpx.Response(200, json=body)
        return httpx.Client(transport=httpx.MockTransport(handler))
    client = create_client(
        ApiConfig(mode="api", provider="anthropic", base_url="https://t",
                  api_key="k", model="m"),
        transport_factory=factory)
    content, usage = client.chat("sys", [{"role": "user", "content": "u"}])
    assert content.startswith('[{') and usage.prompt == 8 and usage.completion == 4


def test_extract_json_array_tolerates_fence():
    out = extract_json_array('```json\n[{"id":"e1","translation":"你好"}]\n```')
    assert out == [{"id": "e1", "translation": "你好"}]


def test_extract_json_array_object_wrap():
    out = extract_json_array('{"translations": [{"id": "e1", "translation": "你好"}]}')
    assert out == [{"id": "e1", "translation": "你好"}]


def test_extract_json_array_none():
    assert extract_json_array("抱歉，无法翻译。") is None
    assert extract_json_array("") is None


def test_retry_on_429():
    calls = {"n": 0}

    def factory():
        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(429, json={"error": {"message": "rate"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}],
                                             "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        return httpx.Client(transport=httpx.MockTransport(handler))
    client = create_client(
        ApiConfig(provider="openai", base_url="https://t/v1", api_key="k", model="m"),
        transport_factory=factory)
    content, _ = client.chat("s", [{"role": "user", "content": "u"}])
    assert calls["n"] == 3
    assert content == "[]"


def test_retry_gives_up():
    calls = {"n": 0}

    def factory():
        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503, json={})
        return httpx.Client(transport=httpx.MockTransport(handler))
    client = create_client(
        ApiConfig(provider="openai", base_url="https://t/v1", api_key="k", model="m"),
        transport_factory=factory)
    import pytest
    with pytest.raises(RuntimeError):
        client.chat("s", [{"role": "user", "content": "u"}])
    assert calls["n"] == 3


def test_local_client_chunks_overlong_source():
    """超长文本按行分块翻译（deadbeat 实证：3183 字符歌词 = 1099 tokens
    超 llama-server 槽位 1024 tokens 被拒 request_error）。每请求源文本
    ≤700 字符，逐块翻译后 \n 拼接。"""
    requests = []

    def factory():
        def handler(request: httpx.Request) -> httpx.Response:
            body = __import__("json").loads(request.content)
            prompt = body["messages"][0]["content"]
            requests.append(prompt)
            # 模拟：把「源文本最后一段」作为译文返回（长度/结构保留）
            source = prompt.split("additional explanation:", 1)[1].strip()
            return httpx.Response(200, json={
                "choices": [{"message": {"content": source}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })
        return httpx.Client(transport=httpx.MockTransport(handler))

    client = create_client(ApiConfig(
        mode="local", base_url="http://127.0.0.1:8080/v1",
        api_key="local-token", model="Hy-MT2-1.8B-Q6_K",
    ), transport_factory=factory)

    lines = [f"Lyric line {i} with some words" for i in range(60)]
    text = "\n".join(lines)
    assert len(text) > 700
    content, usage = client.translate_text(text, "zh-CN", ())

    assert len(requests) > 1                      # 分块请求
    assert content == text                        # 拼接无损
    for prompt in requests:
        source = prompt.split("additional explanation:", 1)[1].strip()
        assert len(source) <= 700                 # 每块在槽位内
    assert usage.prompt == 10 * len(requests)     # usage 聚合


def test_local_client_chunks_word_wrapped_without_newlines():
    """无换行超长文本按词切块（每块 ≤700 字符）。"""
    requests = []

    def factory():
        def handler(request: httpx.Request) -> httpx.Response:
            body = __import__("json").loads(request.content)
            requests.append(body["messages"][0]["content"])
            source = body["messages"][0]["content"].split(
                "additional explanation:", 1)[1].strip()
            return httpx.Response(200, json={
                "choices": [{"message": {"content": source}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })
        return httpx.Client(transport=httpx.MockTransport(handler))

    client = create_client(ApiConfig(
        mode="local", base_url="http://127.0.0.1:8080/v1",
        api_key="local-token", model="Hy-MT2-1.8B-Q6_K",
    ), transport_factory=factory)

    text = " ".join(f"word{i}" for i in range(400))
    assert len(text) > 700
    content, _ = client.translate_text(text, "zh-CN", ())
    assert content == text
    assert len(requests) > 1
    for prompt in requests:
        source = prompt.split("additional explanation:", 1)[1].strip()
        assert len(source) <= 700


def test_local_client_short_text_single_request():
    """短文本不分块（单请求，行为不变）。"""
    requests = []

    def factory():
        def handler(request: httpx.Request) -> httpx.Response:
            body = __import__("json").loads(request.content)
            requests.append(body["messages"][0]["content"])
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "斩击: 999"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })
        return httpx.Client(transport=httpx.MockTransport(handler))

    client = create_client(ApiConfig(
        mode="local", base_url="http://127.0.0.1:8080/v1",
        api_key="local-token", model="Hy-MT2-1.8B-Q6_K",
    ), transport_factory=factory)

    content, _ = client.translate_text("slash: 999", "zh-CN", ())
    assert content == "斩击: 999"
    assert len(requests) == 1
