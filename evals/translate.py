"""
zh -> en query translation that leaves code identifiers alone.

Results are cached as JSON next to the dataset, so every translation can be read
by a person and the experiment re-runs without the translation model.
"""

import json
from pathlib import Path

MT_MODEL = "Helsinki-NLP/opus-mt-zh-en"


def is_cjk(char: str) -> bool:
    return "一" <= char <= "鿿"


def split_cjk_segments(text: str) -> list[tuple[bool, str]]:
    """
    Split a query into runs of Chinese and non-Chinese text.

    "chunk_python 函数" -> [(False, "chunk_python "), (True, "函数")]
    Chinese punctuation that sits between Chinese characters stays in the Chinese run.
    """
    segments: list[tuple[bool, str]] = []
    current = ""
    current_is_cjk = False
    for char in text:
        char_is_cjk = is_cjk(char)
        if not current:
            current = char
            current_is_cjk = char_is_cjk
        elif char_is_cjk == current_is_cjk:
            current += char
        else:
            segments.append((current_is_cjk, current))
            current = char
            current_is_cjk = char_is_cjk
    if current:
        segments.append((current_is_cjk, current))
    return segments


def translate_keep_identifiers(queries: list[dict], cache_path: Path) -> dict[str, str]:
    """
    Translate only the Chinese runs of each query; copy everything else verbatim.

    Whole-query MT rewrote identifiers ("chunk_python" -> "cunk_python",
    "collection" -> "Collaction"), which broke exact-identifier lookups that the
    untranslated query had already answered.
    """
    if cache_path.exists():
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("model") == MT_MODEL and len(cached["queries"]) == len(queries):
            return cached["queries"]

    from transformers import MarianMTModel, MarianTokenizer

    print(f"分段翻译 {len(queries)} 条查询 ({MT_MODEL}) ...")
    tokenizer = MarianTokenizer.from_pretrained(MT_MODEL)
    model = MarianMTModel.from_pretrained(MT_MODEL)

    translated = {}
    for item in queries:
        parts = []
        for segment_is_cjk, segment in split_cjk_segments(item["query"]):
            if not segment_is_cjk:
                parts.append(segment.strip())
                continue
            batch = tokenizer([segment], return_tensors="pt")
            output = model.generate(**batch, max_new_tokens=128, num_beams=4)
            parts.append(tokenizer.decode(output[0], skip_special_tokens=True).strip())

        words = []
        for part in parts:
            if part:
                words.append(part)
        translated[item["id"]] = " ".join(words)

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "note": "zh -> en machine translation of the Chinese runs only; ASCII runs copied verbatim. Generated, not hand-edited.",
                "model": MT_MODEL,
                "queries": translated,
            },
            f,
            ensure_ascii=False,
            indent=1,
        )
    return translated
