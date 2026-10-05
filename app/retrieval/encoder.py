"""
Text -> vector, behind a small interface so the service and tests can swap models.

The copied upstream `embedder.py` is left untouched for the M0 regression; this
wrapper adds what the service needs: a batch size (bge-m3 hangs MPS with padded
batches of long chunks, NOTES 11) and the model name for run records.
"""

from threading import Lock
from typing import Protocol


class Encoder(Protocol):
    model_name: str

    def encode_passages(self, texts: list[str]) -> list[list[float]]: ...

    def encode_query(self, text: str) -> list[float]: ...


class SentenceTransformerEncoder:
    def __init__(self, model_name: str, batch_size: int = 1) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self._batch_size = batch_size
        self._model = SentenceTransformer(model_name)
        # FastAPI runs sync endpoints in a thread pool. One model, one device:
        # serialise calls instead of trusting the backend to be thread-safe.
        self._lock = Lock()

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            with self._lock:
                vectors.extend(self._model.encode(batch, show_progress_bar=False).tolist())
        return vectors

    def encode_query(self, text: str) -> list[float]:
        with self._lock:
            return self._model.encode(text, show_progress_bar=False).tolist()
