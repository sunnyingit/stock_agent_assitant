"""带超时和重试的 GET 请求。"""

from __future__ import annotations

import json
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app import config

_local = threading.local()


def _build_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    retry = Retry(
        total=config.HTTP_RETRIES,
        connect=config.HTTP_RETRIES,
        read=config.HTTP_RETRIES,
        backoff_factor=config.HTTP_BACKOFF,
        status_forcelist=config.HTTP_STATUS_FORCELIST,
        allowed_methods=frozenset(["GET", "HEAD"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.eastmoney.com/",
    })
    return session


def get_session() -> requests.Session:
    session = getattr(_local, "session", None)
    if session is None:
        session = _build_session()
        _local.session = session
    return session


def get_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict | list:
    response = get_session().get(url, params=params, headers=headers, timeout=config.HTTP_TIMEOUT)
    response.raise_for_status()
    text = response.content.decode("utf-8-sig").strip()
    if not text or text.startswith("<"):
        raise ValueError(f"上游没有返回 JSON: {url}")
    return json.loads(text)
