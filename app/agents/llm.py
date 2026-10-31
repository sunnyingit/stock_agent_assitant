"""OpenAI 兼容的对话模型。密钥放在环境变量，不写进代码。"""

from __future__ import annotations

import json
import re

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app import config

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_http_client = httpx.Client(trust_env=False, timeout=60.0)


class ModelError(RuntimeError):
    pass


def get_chat_model() -> ChatOpenAI:
    if not config.LLM_API_KEY:
        raise ModelError("缺少 LLM_API_KEY 或 DEEPSEEK_API_KEY，无法调用分析模型")
    return ChatOpenAI(
        api_key=config.LLM_API_KEY,
        base_url=config.LLM_BASE_URL,
        model=config.LLM_MODEL,
        temperature=0.2,
        timeout=60,
        http_client=_http_client,
        http_socket_options=(),
    )


def message_text(message) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content)


def parse_json(text: str) -> dict:
    cleaned = _JSON_FENCE.sub("", text.strip())
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start:end + 1]
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ModelError("模型没有返回 JSON 对象")
    return data


def _invoke_json(model, system: str, user: str, temperature: float) -> dict:
    kwargs = {"temperature": temperature, "response_format": {"type": "json_object"}}
    llm = model.bind(**kwargs) if hasattr(model, "bind") else model
    message = llm.invoke([
        SystemMessage(content=system),
        HumanMessage(content=user),
    ])
    data = parse_json(message_text(message))
    if not isinstance(data, dict):
        raise ModelError("模型没有返回 JSON 对象")
    return data


def _json_error(exc: Exception) -> bool:
    if isinstance(exc, (json.JSONDecodeError, ModelError)):
        return True
    return "JSON" in type(exc).__name__


def complete_json(model, system: str, user: str, temperature: float = 0.2) -> dict:
    """调用模型并解析 JSON。测试模型实现 complete(system, user) 即可。"""
    if hasattr(model, "complete"):
        data = model.complete(system, user)
        if not isinstance(data, dict):
            raise ModelError("模型没有返回 JSON 对象")
        return data
    last_error = None
    for _ in range(2):
        try:
            return _invoke_json(model, system, user, temperature)
        except Exception as exc:
            last_error = exc
    if _json_error(last_error):
        raise ModelError(f"模型没有返回合法 JSON: {last_error}") from last_error
    raise ModelError(f"无法连接模型服务: {last_error}") from last_error
