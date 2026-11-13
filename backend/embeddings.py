"""Embedding API adapter with shape validation and bounded retries."""
from __future__ import annotations

import math
import time
from typing import Callable

import httpx


class EmbeddingError(RuntimeError):
    pass


def embed(texts: list[str], settings: dict, key: str, query: bool = False, consume: Callable[[], None] | None = None, checkpoint: Callable[[], None] | None = None) -> list[list[float]]:
    config = settings.get("embedding", settings)
    base_url = config["base_url"].rstrip("/")
    if not base_url or not config["model"] or not key:
        raise EmbeddingError("请先填写 API 地址、模型名称和密钥")
    endpoint = base_url if base_url.endswith("/embeddings") else base_url + "/embeddings"
    prefix = config.get("query_prefix" if query else "document_prefix", "")
    payload = {**config.get("extra_body", {}), "model": config["model"], "input": [prefix + text for text in texts]}
    dimension = int(config.get("dimensions", 0))
    if dimension:
        payload["dimensions"] = dimension
    with httpx.Client(timeout=float(config.get("timeout", 60)), follow_redirects=False, trust_env=False) as client:
        for attempt in range(5):
            if checkpoint:
                checkpoint()
            if consume:
                consume()
            try:
                response = client.post(endpoint, headers={"Authorization": f"Bearer {key}"}, json=payload)
            except httpx.TransportError as exc:
                if attempt == 4:
                    raise EmbeddingError("连接嵌入 API 失败，请检查网络和接口地址") from exc
                _wait(2 ** attempt, checkpoint)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 4:
                    raise EmbeddingError(f"嵌入 API 暂时不可用，HTTP {response.status_code}")
                try:
                    delay = min(30, max(1, float(response.headers.get("Retry-After", 2 ** attempt))))
                except ValueError:
                    delay = 2 ** attempt
                _wait(delay, checkpoint)
                continue
            if not response.is_success:
                raise EmbeddingError(f"嵌入 API 请求失败，HTTP {response.status_code}，请检查模型、权限和参数")
            try:
                data = response.json()["data"]
                if len(data) != len(texts):
                    raise ValueError("返回条数不匹配")
                ordered = sorted(data, key=lambda item: item["index"])
                if [item["index"] for item in ordered] != list(range(len(texts))):
                    raise ValueError("返回序号不完整")
                vectors = [[float(value) for value in item["embedding"]] for item in ordered]
                actual = len(vectors[0]) if vectors else 0
                if not 1 <= actual <= 32768 or (dimension and actual != dimension):
                    raise ValueError("向量维度与设置不匹配")
                for vector in vectors:
                    if len(vector) != actual or not all(math.isfinite(value) for value in vector) or not any(vector):
                        raise ValueError("向量内容无效")
                return vectors
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                raise EmbeddingError(f"API 返回格式错误或向量无效：{exc}") from exc
    raise EmbeddingError("嵌入请求失败")


def _wait(seconds: float, checkpoint: Callable[[], None] | None) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if checkpoint:
            checkpoint()
        time.sleep(min(0.2, max(0, end - time.monotonic())))
