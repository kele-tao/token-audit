"""Regenerate the vendored BPE merge tables and the golden token-count set.

This script is a development tool.  It requires tiktoken (and therefore a
network connection the first time it runs) to read the published vocabulary
metadata, but the package it produces has zero runtime dependencies.

    python scripts/build_vocab_data.py

Outputs
-------
src/token_audit/data/cl100k.bin   merge table for cl100k-style encodings
src/token_audit/data/o200k.bin    merge table for o200k-style encodings
tests/data/golden_tokens.json     200 (text, expected count) samples

Binary format (see docs/VOCAB_FORMAT.md):

    magic  b"TAV1"
    varint vocab_size
    bytes  the single-byte token for each of the 256 byte ranks
    repeat vocab_size - 256 times:  varint left_rank, varint right_rank
    lzma (preset 9 | EXTREME) stream

Every non-byte token has exactly one split where bytes(t) == bytes(left) + bytes(right)
with both ranks lower than its own, which is what makes a merge replay possible
without storing the token bytes themselves.
"""
from __future__ import annotations

import json
import lzma
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "src" / "token_audit" / "data"
GOLDEN = ROOT / "tests" / "data" / "golden_tokens.json"
ENCODINGS = {"cl100k": "cl100k_base", "o200k": "o200k_base"}


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def merge_pairs(encoding) -> dict:
    """Map (left_rank, right_rank) to the rank of the merged token.

    A token can be decomposed in more than one way (for example " as" splits
    into " a" + "s" and also into " " + "as").  The replay algorithm only needs
    to know, for two adjacent symbols, whether their byte concatenation is a
    token and what rank it has, so every valid decomposition is recorded.  That
    keeps the integer-keyed replay exactly equivalent to a byte-keyed replay.
    """
    ranks = encoding._mergeable_ranks
    tokens = [None] * len(ranks)
    for token, rank in ranks.items():
        tokens[rank] = token
    if any(t is None for t in tokens):
        raise SystemExit("vocabulary has gaps; refusing to build")
    lookup = {token: idx for idx, token in enumerate(tokens)}
    pairs = {}
    for idx in range(256, len(tokens)):
        token = tokens[idx]
        found = False
        for cut in range(1, len(token)):
            left, right = token[:cut], token[cut:]
            if left in lookup and right in lookup and lookup[left] < idx and lookup[right] < idx:
                pairs[(lookup[left], lookup[right])] = idx
                found = True
        if not found:
            raise SystemExit("token %r has no merge pair" % token)
    return pairs


def byte_order(encoding) -> bytes:
    """Return the single-byte token for each rank 0..255, in rank order."""
    ranks = encoding._mergeable_ranks
    tokens = [None] * 256
    for token, rank in ranks.items():
        if rank < 256:
            if len(token) != 1:
                raise SystemExit("rank %d is not a single byte" % rank)
            tokens[rank] = token[0]
    if any(t is None for t in tokens):
        raise SystemExit("byte ranks are not contiguous")
    return bytes(tokens)


def build_table(encoding, destination) -> int:
    """Write the merge table for one encoding and return its size in bytes."""
    pairs = merge_pairs(encoding)
    order = byte_order(encoding)
    vocab_size = len(encoding._mergeable_ranks)
    chosen: dict[int, tuple[int, int]] = {}
    for (left, right), rank in pairs.items():
        chosen.setdefault(rank, (left, right))
    raw = bytearray(b"TAV1")
    raw += varint(vocab_size)
    raw += order
    for rank in range(256, vocab_size):
        left, right = chosen[rank]
        raw += varint(left) + varint(right)
    compressed = lzma.compress(bytes(raw), preset=9 | lzma.PRESET_EXTREME)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(compressed)
    return len(compressed)


CORPUS = [
    "You are a helpful assistant. Answer concisely and cite sources when they are available.",
    "You are a senior software engineer. Explain tradeoffs, then show the smallest correct patch.",
    "System prompt: you must refuse requests that ask for medical or legal advice.",
    "Please summarise the customer support transcript and list every unresolved question.",
    "Rewrite the paragraph below for a non-technical audience. Keep it under 120 words.",
    "Translate the following product description into German. Preserve the friendly tone.",
    "Given the JSON schema, produce three realistic example rows and explain each field.",
    "Compare the two proposals on cost, risk and delivery time, then recommend one.",
    "Write a unit test that covers empty input, unicode input and a 5 MB input.",
    "Find the bug in this function and explain why the existing tests did not catch it.",
    "The deployment failed twice this week because the integration tests time out under load.",
    "Our enterprise renewals grew 12% quarter over quarter, driven by the new pricing tier.",
    "def parse_config(path: str) -> dict:\n    with open(path, encoding='utf-8') as fh:\n        return json.load(fh)\n",
    "class RetryPolicy:\n    def __init__(self, attempts: int = 3, backoff: float = 0.5) -> None:\n        self.attempts = attempts\n",
    "SELECT c.name, SUM(o.total) AS revenue FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name ORDER BY revenue DESC;",
    "function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }",
    '{"type":"function","function":{"name":"search_documents","description":"Search the knowledge base.","parameters":{"type":"object","properties":{"query":{"type":"string"},"top_k":{"type":"integer","minimum":1,"maximum":20}},"required":["query"]}}}',
    "| Model | Input $/M | Output $/M | Context |\n| --- | --- | --- | --- |\n| gpt-4o | 2.50 | 10.00 | 128k |\n| gpt-4o-mini | 0.15 | 0.60 | 128k |",
    "- **Bold item**: explanation of the setting and its default value.\n- Another bullet with inline code.\n",
    "Error: TypeError: 'NoneType' object is not subscriptable at worker.py line 42 during the nightly batch.",
    "Write a haiku about distributed systems. Keep it under 20 words and do not mention clouds.",
    "Hello! I need help with order #12345 which was supposed to arrive last Tuesday. Can you check the status?",
    "It's 3.5 degrees outside; don't panic -- we'll be fine by noon.",
    "The quick brown fox jumps over the lazy dog. TOKENIZATION is fun, isn't it?",
    "hyperparameterization antidisestablishmentarianism pneumonoultramicroscopicsilicovolcanoconiosis",
    "1234567890 42 3.14159 -0.5 1e10 0xFF 0b1010 2026-11-23T15:42:09+08:00",
    "CamelCaseIdentifier snake_case_name SCREAMING_CONSTANT HTTPResponse XMLHttpRequest",
    "The tokenizer must never claim exactness for text it has not seen in the golden set.",
    "We are within three percent of the reference tokenizer on the committed sample set.",
    "Cost per thousand calls is computed from the committed pricing snapshot, not from an API.",
    "Duplicate system prompts across six files cost the same money as keeping one copy.",
    "A base64 screenshot inside the context window is billed like any other input token.",
    "Few-shot examples often contain more commentary than the task description itself.",
    "A JSON schema with long description fields is legitimate; a schema with redundant titles is waste.",
    "Markdown tables in context are expensive: pipes, dashes and padding characters all bill.",
    "Near-duplicate prompt fragments are found with MinHash over word shingles, not with diffs.",
    "The optimiser never silently drops an instruction; it reports what it changed and why.",
    "Budget planning is arithmetic: requests per day times input and output tokens times price.",
    "Caching changes the arithmetic more than any prompt rewrite you will ever ship.",
    "A chat history export repeats the same system prompt on every turn of every conversation.",
    "Deterministic output means the same input yields byte identical HTML, JSON and terminal text.",
    "When the scan finds nothing, it says so and exits with status code zero.",
    "We do not call any model API. Ever. The tool is a calculator, not an agent.",
]
UNICODE_CORPUS = [
    "\u4f60\u597d\uff0c\u8bf7\u5e2e\u6211\u5199\u4e00\u4e2a Python \u811a\u672c\uff0c\u7528\u4e8e\u7edf\u8ba1\u6587\u4ef6\u5939\u4e2d\u7684\u6587\u4ef6\u6570\u91cf\u3002",
    "\u3053\u306e\u30d7\u30ed\u30f3\u30d7\u30c8\u3092\u65e5\u672c\u8a9e\u3067\u8aac\u660e\u3057\u3066\u304f\u3060\u3055\u3044\u3002\u56de\u7b54\u306f\u7c21\u6f54\u306b\u3002",
    "\uc774 \ubb38\uc7a5\uc744 \ud55c\uad6d\uc5b4\ub85c \ubc88\uc5ed\ud558\uace0 \ud575\uc2ec \ub0b4\uc6a9\uc744 \uc694\uc57d\ud574 \uc8fc\uc138\uc694.",
    "\u0645\u0631\u062d\u0628\u0627\u060c \u064a\u0631\u062c\u0649 \u062a\u0644\u062e\u064a\u0635 \u0647\u0630\u0627 \u0627\u0644\u0646\u0635 \u0628\u0627\u0644\u0644\u063a\u0629 \u0627\u0644\u0639\u0631\u0628\u064a\u0629.",
    "\u041f\u0440\u0438\u0432\u0435\u0442, \u043f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430, \u0441\u043e\u043a\u0440\u0430\u0442\u0438 \u044d\u0442\u043e\u0442 \u0442\u0435\u043a\u0441\u0442 \u0434\u043e \u0434\u0432\u0443\u0445 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0439.",
    "Emoji: \U0001F680\U0001F389\U0001F468\u200D\U0001F469\u200D\U0001F467\u200D\U0001F466 \u2713 \u2192 \u2605 \u2764",
]


def sample_texts(rng, count):
    pool = CORPUS + UNICODE_CORPUS
    texts = []
    while len(texts) < count:
        source = pool[len(texts) % len(pool)]
        if len(source) <= 40:
            texts.append(source)
            continue
        width = rng.randint(60, min(420, len(source) + 120))
        if width >= len(source):
            texts.append(source)
            continue
        start = rng.randint(0, len(source) - width)
        texts.append(source[start : start + width])
    return texts


def main() -> int:
    try:
        import tiktoken
    except ImportError:
        sys.stderr.write("tiktoken is required to rebuild the data files\n")
        return 2
    encodings = {short: tiktoken.get_encoding(name) for short, name in ENCODINGS.items()}
    for short, encoding in encodings.items():
        size = build_table(encoding, DATA / (short + ".bin"))
        print("%s: %d merges, %d bytes compressed" % (short, len(encoding._mergeable_ranks) - 256, size))

    rng = random.Random(20260101)
    texts = sample_texts(rng, 200)
    samples = []
    for index, text in enumerate(texts, 1):
        entry = {"id": "g%03d" % index, "text": text}
        for short, encoding in encodings.items():
            entry[short] = len(encoding.encode(text))
        samples.append(entry)
    payload = {
        "generated_with": "tiktoken %s" % tiktoken.__version__,
        "encodings": {"cl100k": "cl100k_base", "o200k": "o200k_base"},
        "seed": 20260101,
        "samples": samples,
    }
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    total = {short: sum(s[short] for s in samples) for short in encodings}
    print("golden: %d samples, totals %s" % (len(samples), total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
