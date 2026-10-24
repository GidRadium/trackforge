"""Optional ONNX cross-encoder reranker.

The reranker orders existing claims; it never generates text.  ONNX/tokenizers
are imported lazily so the base install remains lexical-only.  Scores are raw
model outputs (sigmoid/softmax) reported as evidence, not probabilities.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from trackforge.application.ports.models import ModelManager
from trackforge.application.ports.ranking import (
    ClaimRanker,
    ScoredCandidate,
    validate_limit,
)
from trackforge.domain.model.claims import Claim


class CrossEncoderRanker:
    """Rerank claims with a local BERT-style cross-encoder ONNX model."""

    id = "cross-encoder/onnx-v1"

    def __init__(
        self,
        manager: ModelManager | None = None,
        model_id: str | None = None,
        *,
        tokenizer_path: str | Path | None = None,
        max_length: int = 256,
        batch_size: int = 8,
        model_name: str | None = None,
    ) -> None:
        self._manager = manager
        self._model_id = model_id
        self._tokenizer_path = Path(tokenizer_path) if tokenizer_path else None
        self._max_length = max(8, int(max_length))
        self._batch_size = max(1, int(batch_size))
        self._model_name = model_name or model_id or ""
        self._session: Any | None = None
        self._tokenizer: Any | None = None
        self._np: Any | None = None
        self._load_error: str | None = None
        self.last_warnings: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        if self._model_id is None or self._manager is None:
            return False
        try:
            info = self._manager.resolve(self._model_id)
        except Exception:
            return False
        return info.available

    def rank(
        self,
        query: str,
        candidates: Sequence[Claim],
        limit: int = 10,
    ) -> list[ScoredCandidate]:
        limit = validate_limit(limit)
        if not candidates:
            return []
        if not self.available:
            self.last_warnings = (
                "cross-encoder skipped: model path/runtime is unavailable; lexical ranker used",
            )
            return []
        try:
            session, tokenizer, np = self._load()
            texts = [_claim_text(claim) for claim in candidates]
            scores = self._score(
                session,
                tokenizer,
                np,
                query,
                texts,
            )
        except Exception as exc:  # optional adapter must degrade, not crash resolve
            self._load_error = f"{type(exc).__name__}: {exc}"
            self.last_warnings = (
                f"cross-encoder skipped: {self._load_error}; lexical ranker used",
            )
            return []
        scored = [
            ScoredCandidate(
                claim=claim,
                score=round(min(1.0, max(0.0, score)), 6),
                ranker=self.id,
                evidence={
                    "model": self._model_name,
                    "score_kind": "model_output",
                    "calibrated": False,
                },
            )
            for claim, score in zip(candidates, scores, strict=True)
        ]
        scored.sort(
            key=lambda item: (item.score, float(item.claim.confidence), str(item.claim.id)),
            reverse=True,
        )
        self.last_warnings = ()
        return scored[:limit]

    def _load(self) -> tuple[Any, Any, Any]:
        if self._session is not None and self._tokenizer is not None and self._np is not None:
            return self._session, self._tokenizer, self._np
        import numpy as np  # type: ignore[import-not-found,import-untyped,unused-ignore]
        import onnxruntime as ort  # type: ignore[import-not-found,import-untyped,unused-ignore]
        from tokenizers import Tokenizer  # type: ignore[import-not-found,import-untyped,unused-ignore]

        if self._manager is None or self._model_id is None:
            raise RuntimeError("cross-encoder manager/model is not configured")
        model_path = self._manager.load_path(self._model_id)
        tokenizer_path = self._tokenizer_path or model_path.parent / "tokenizer.json"
        if not tokenizer_path.exists():
            raise FileNotFoundError(f"tokenizer not found: {tokenizer_path}")
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
        tokenizer.enable_truncation(max_length=self._max_length)
        tokenizer.enable_padding()
        session = ort.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
        )
        self._session, self._tokenizer, self._np = session, tokenizer, np
        return session, tokenizer, np

    def _score(
        self,
        session: Any,
        tokenizer: Any,
        np: Any,
        query: str,
        texts: Sequence[str],
    ) -> list[float]:
        result: list[float] = []
        input_names = {item.name for item in session.get_inputs()}
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            encodings = tokenizer.encode_batch([(query, text) for text in batch])
            feeds: dict[str, Any] = {}
            ids = np.asarray([encoding.ids for encoding in encodings], dtype=np.int64)
            attention = np.asarray(
                [encoding.attention_mask for encoding in encodings],
                dtype=np.int64,
            )
            if "input_ids" in input_names:
                feeds["input_ids"] = ids
            if "attention_mask" in input_names:
                feeds["attention_mask"] = attention
            if "token_type_ids" in input_names:
                feeds["token_type_ids"] = np.asarray(
                    [encoding.type_ids for encoding in encodings],
                    dtype=np.int64,
                )
            outputs = session.run(None, feeds)
            logits = outputs[0]
            for row in logits:
                if len(row) == 1:
                    result.append(_sigmoid(float(row[0])))
                else:
                    result.append(_softmax_positive(row))
        return result


class HybridRanker:
    """Lexical baseline with optional cross-encoder reranking of the top N."""

    id = "hybrid/lexical+cross-encoder-v1"

    def __init__(
        self,
        lexical: ClaimRanker,
        cross_encoder: CrossEncoderRanker | None = None,
        *,
        top_n: int = 20,
        lexical_weight: float = 0.4,
    ) -> None:
        self._lexical = lexical
        self._cross_encoder = cross_encoder
        self._top_n = max(1, int(top_n))
        self._lexical_weight = min(1.0, max(0.0, lexical_weight))
        self.last_warnings: tuple[str, ...] = ()

    def rank(
        self,
        query: str,
        candidates: Sequence[Claim],
        limit: int = 10,
    ) -> list[ScoredCandidate]:
        lexical = self._lexical.rank(query, candidates, limit=max(limit, self._top_n))
        if self._cross_encoder is None:
            self.last_warnings = ("hybrid ranker: cross-encoder is not configured",)
            return lexical[:limit]
        top = lexical[: self._top_n]
        reranked = self._cross_encoder.rank(
            query,
            [item.claim for item in top],
            limit=len(top),
        )
        if not reranked:
            self.last_warnings = self._cross_encoder.last_warnings
            return lexical[:limit]
        by_id = {str(item.claim.id): item for item in lexical}
        combined = [
            ScoredCandidate(
                claim=item.claim,
                score=round(
                    self._lexical_weight * by_id.get(item.claim_id, item).score
                    + (1.0 - self._lexical_weight) * item.score,
                    6,
                ),
                ranker=self.id,
                evidence={
                    "lexical_score": by_id.get(item.claim_id, item).score,
                    "cross_encoder_score": item.score,
                    "cross_encoder_model": item.evidence.get("model"),
                    "score_kind": "weighted_hybrid",
                    "calibrated": False,
                },
            )
            for item in reranked
        ]
        combined.sort(
            key=lambda item: (item.score, float(item.claim.confidence), str(item.claim.id)),
            reverse=True,
        )
        self.last_warnings = ()
        return combined[:limit]


def _claim_text(claim: Claim) -> str:
    value = claim.value
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(item for item in value if isinstance(item, str))
    return str(value)


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


def _softmax_positive(row: Any) -> float:
    values = [float(item) for item in row]
    maximum = max(values)
    exps = [math.exp(item - maximum) for item in values]
    total = sum(exps)
    if total == 0:
        return 0.0
    return exps[1] / total if len(exps) > 1 else exps[0] / total


__all__ = ["CrossEncoderRanker", "HybridRanker"]
