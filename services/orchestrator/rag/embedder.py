"""Embedding 接口：加载 bge-large-zh-v1.5 模型，提供批量 embedding 能力。

模型：BAAI/bge-large-zh-v1.5（1024 维，与 DB schema vector(1024) 一致）
依赖：sentence-transformers（pyproject.toml 已有）

国内镜像：设置环境变量 HF_ENDPOINT=https://hf-mirror.com 即可使用镜像下载模型。
"""

from __future__ import annotations

import logging
import os
from typing import Sequence

import numpy as np

logger = logging.getLogger(__name__)

# 模型名称（HuggingFace 模型 ID 或本地路径）
MODEL_NAME = "BAAI/bge-large-zh-v1.5"
EMBEDDING_DIM = 1024

# 国内镜像（如果未设置 HF_ENDPOINT，自动使用镜像）
HF_MIRROR = "https://hf-mirror.com"

# 在导入 sentence_transformers 之前设置镜像（关键！）
if "HF_ENDPOINT" not in os.environ:
    os.environ["HF_ENDPOINT"] = HF_MIRROR
    logger.info("设置 HF_ENDPOINT=%s（国内镜像）", HF_MIRROR)
else:
    logger.info("使用已有 HF_ENDPOINT=%s", os.environ["HF_ENDPOINT"])


class Embedder:
    """封装 sentence-transformers 模型，提供批量 embedding 接口。"""

    def __init__(self, model_name: str = MODEL_NAME, device: str | None = None):
        """初始化 embedding 模型。

        Args:
            model_name: HuggingFace 模型 ID 或本地路径
            device: 'cuda' / 'cpu' / None（自动检测）
        """
        from sentence_transformers import SentenceTransformer

        logger.info("加载 embedding 模型: %s", model_name)
        self._model_name = model_name
        self._model = SentenceTransformer(model_name, device=device)
        self._dim = self._model.get_sentence_embedding_dimension()
        if self._dim != EMBEDDING_DIM:
            raise ValueError(f"模型维度 {self._dim} 与 schema 定义 {EMBEDDING_DIM} 不一致")
        logger.info("模型加载完成，维度=%d", self._dim)

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    def encode(self, texts: Sequence[str], batch_size: int = 64) -> np.ndarray:
        """批量编码文本为向量。

        Args:
            texts: 文本列表
            batch_size: 批处理大小

        Returns:
            np.ndarray, shape (len(texts), dim), dtype float32
        """
        items = list(texts)
        if not items:
            return np.empty((0, self._dim), dtype=np.float32)
        # bge 系列推荐对 query 加前缀，但文档编码不需要
        embeddings = self._model.encode(
            items,
            batch_size=batch_size,
            show_progress_bar=True,
            normalize_embeddings=True,  # 归一化后余弦相似度 = 内积
        )
        return np.array(embeddings, dtype=np.float32)

    def encode_single(self, text: str) -> np.ndarray:
        """编码单条文本。"""
        return self.encode([text])[0]
