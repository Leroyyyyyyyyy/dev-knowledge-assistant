"""Embedding models compared in the retrieval experiments, with the prefixes each expects."""

# key -> model id and the prefixes the model was trained with.
# e5 models expect "query: " / "passage: "; the others take raw text.
EMBEDDERS = {
    "minilm-l6": {
        "model": "all-MiniLM-L6-v2",
        "query_prefix": "",
        "passage_prefix": "",
        "batch_size": 128,
    },
    "multi-minilm-l12": {
        "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "query_prefix": "",
        "passage_prefix": "",
        "batch_size": 128,
    },
    "me5-base": {
        "model": "intfloat/multilingual-e5-base",
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
        "batch_size": 32,
    },
    "bge-m3": {
        "model": "BAAI/bge-m3",
        "query_prefix": "",
        "passage_prefix": "",
        "batch_size": 8,
    },
}
