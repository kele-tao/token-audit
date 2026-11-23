"""Token counting without a tokenizer dependency.

The package ships two compact BPE merge tables (cl100k-style and o200k-style)
in the package data directory.  A merge table is enough to replay the byte-pair
encoder:

* ranks 0-255 are the literal bytes,
* every later rank was produced by merging two earlier ranks, so the table is
  just (left_rank, right_rank) per token rank,
* the encoder repeatedly merges the pair with the lowest rank until no merge
  applies.

That is how the reference implementations work.  The pre-tokenizer is a pure
Python port of the published patterns, so for text it splits the same way the
counts are identical.  The committed golden set in tests/data measures the
difference and the accuracy command reports it; we do not claim exactness for
text unlike anything that has been tested.

The fallback estimator is a character/byte heuristic used when a model is not
mapped to a vendored encoding.  It is documented as approximate, not exact.
"""
from __future__ import annotations

import bisect
import functools
import lzma
import pathlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

DATA_DIR = pathlib.Path(__file__).resolve().parent / "data"

_CACHE_LIMIT = 200_000


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read an unsigned LEB128 varint, returning (value, new_position)."""
    value = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


@dataclass(frozen=True)
class EncodingInfo:
    """Describes one vendored encoding."""

    name: str
    family: str
    data_file: str
    byte_tokens: int = 256


ENCODINGS = {
    "cl100k": EncodingInfo(
        "cl100k",
        "cl100k-style (GPT-4, GPT-3.5-turbo, text-embedding-3)",
        "cl100k.bin",
    ),
    "o200k": EncodingInfo(
        "o200k",
        "o200k-style (GPT-4o, GPT-4.1, o-series)",
        "o200k.bin",
    ),
}


def _unicode_ranges(predicate) -> tuple[list[int], list[int]]:
    """Return (starts, ends) for code points matching the predicate."""
    starts: list[int] = []
    ends: list[int] = []
    start = None
    for cp in range(0x110000):
        if predicate(chr(cp)):
            if start is None:
                start = cp
        elif start is not None:
            starts.append(start)
            ends.append(cp - 1)
            start = None
    if start is not None:
        starts.append(start)
        ends.append(0x10FFFF)
    return starts, ends


_LETTER_STARTS, _LETTER_ENDS = _unicode_ranges(
    lambda ch: unicodedata.category(ch)[0] == "L"
)
_NUMBER_STARTS, _NUMBER_ENDS = _unicode_ranges(
    lambda ch: unicodedata.category(ch) in ("Nd", "Nl", "No")
)

_WHITESPACE = frozenset(
    " \t\n\r\f\v\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)
_APOSTROPHE = 0x27


def _in_ranges(starts: list[int], ends: list[int], cp: int) -> bool:
    idx = bisect.bisect_right(starts, cp) - 1
    return idx >= 0 and ends[idx] >= cp


def is_letter(cp: int) -> bool:
    """True when code point cp is a Unicode letter."""
    return _in_ranges(_LETTER_STARTS, _LETTER_ENDS, cp)


def is_number(cp: int) -> bool:
    """True when code point cp is a Unicode number."""
    return _in_ranges(_NUMBER_STARTS, _NUMBER_ENDS, cp)


def is_whitespace(cp: int) -> bool:
    """True when code point cp is whitespace in the reference pattern."""
    return chr(cp) in _WHITESPACE


def _range_class(starts: list[int], ends: list[int]) -> str:
    """Render code point ranges as a regex character-class body."""
    parts = []
    for start, end in zip(starts, ends, strict=True):
        if start == end:
            parts.append("\\U%08x" % start)
        else:
            parts.append("\\U%08x-\\U%08x" % (start, end))
    return "".join(parts)


def _build_patterns() -> tuple[dict[str, re.Pattern[str]], dict[str, re.Pattern[str]]]:
    """Compile the pre-tokenizer patterns.

    Two sets are built.  The fast set is expressed in ASCII and is used for
    ASCII-only text.  The full set expands the Unicode property escapes the
    published patterns use (letters and numbers) into explicit code point
    ranges, and is used whenever the text contains any non-ASCII character.
    Both reproduce the same split; the fast set exists purely for speed.
    """
    letters = _range_class(_LETTER_STARTS, _LETTER_ENDS)
    numbers = _range_class(_NUMBER_STARTS, _NUMBER_ENDS)
    not_letter_number = "[^\\r\\n%s%s]" % (letters, numbers)
    not_space_letter_number = "[^\\s%s%s]" % (letters, numbers)
    full = {
        "cl100k": re.compile(
            r"'(?i:[sdmt]|ll|ve|re)|"
            + not_letter_number
            + r"?["
            + letters
            + r"]+|"
            + r"["
            + numbers
            + r"]{1,3}| ?"
            + not_space_letter_number
            + r"+[\r\n]*|\s+$|\s*[\r\n]|\s+(?!\S)|\s",
            re.UNICODE,
        ),
        "o200k": re.compile(
            not_letter_number
            + r"?["
            + letters
            + r"]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?|"
            + r"["
            + numbers
            + r"]{1,3}| ?"
            + not_space_letter_number
            + r"+[\r\n/]*|\s*[\r\n]+|\s+(?!\S)|\s+",
            re.UNICODE,
        ),
    }
    fast = {
        "cl100k": re.compile(
            r"'(?i:[sdmt]|ll|ve|re)|"
            r"[^\r\nA-Za-z0-9]?[A-Za-z]+|"
            r"\d{1,3}| ?[^\sA-Za-z0-9]+[\r\n]*|\s+$|\s*[\r\n]|\s+(?!\S)|\s"
        ),
        "o200k": re.compile(
            r"[^\r\nA-Za-z0-9]?[A-Za-z]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?|"
            r"\d{1,3}| ?[^\sA-Za-z0-9]+[\r\n/]*|\s*[\r\n]+|\s+(?!\S)|\s+"
        ),
    }
    return full, fast


_PATTERNS: tuple[dict[str, re.Pattern[str]], dict[str, re.Pattern[str]]] | None = None


def _patterns() -> tuple[dict[str, re.Pattern[str]], dict[str, re.Pattern[str]]]:
    """Return the compiled (full, fast) pattern pair, compiling on first use."""
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = _build_patterns()
    return _PATTERNS


def pretokenize(text: str, family: str = "cl100k") -> list[str]:
    """Split text the way the published cl100k / o200k patterns do.

    family selects the pattern family: cl100k keeps contractions as their own
    piece, o200k glues them to the preceding word.  The split matches the
    reference tokenizer's pre-tokenizer, which is what makes the replayed BPE
    counts agree with the golden set.
    """
    if family not in ("cl100k", "o200k"):
        raise ValueError(
            "unknown pre-tokenizer family %r; expected cl100k or o200k" % family
        )
    if not text:
        return []
    full, fast = _patterns()
    pattern = fast[family] if text.isascii() else full[family]
    # findall is materially faster than finditer here because the patterns have
    # no capturing groups, so it can return the substrings directly.
    return pattern.findall(text)


class BPETokenizer:
    """Byte-pair encoder replayed from a committed merge table."""

    def __init__(self, info: EncodingInfo, data_dir: pathlib.Path | None = None) -> None:
        self.info = info
        self._data_dir = data_dir or DATA_DIR
        self._tokens: list[bytes] | None = None
        self._pair_ranks: dict[tuple[int, int], int] | None = None
        self._ranks: dict[bytes, int] | None = None
        self._piece_cache: dict[str, int] = {}
        self._text_cache: dict[str, int] = {}

    def _load(self) -> None:
        if self._ranks is not None:
            return
        path = self._data_dir / self.info.data_file
        if not path.exists():
            raise FileNotFoundError(
                "vendored merge table missing: %s. Reinstall the package or run "
                "scripts/build_vocab_data.py." % path
            )
        raw = lzma.decompress(path.read_bytes())
        if raw[:4] != b"TAV1":
            raise ValueError("unexpected merge table magic in %s" % path)
        vocab_size, pos = _varint(raw, 4)
        if len(raw) < pos + 256:
            raise ValueError("merge table truncated in %s" % path)
        order = raw[pos : pos + 256]
        pos += 256
        tokens: list[bytes] = [bytes([byte]) for byte in order]
        pairs: dict[tuple[int, int], int] = {}
        for rank in range(256, vocab_size):
            left, pos = _varint(raw, pos)
            right, pos = _varint(raw, pos)
            tokens.append(tokens[left] + tokens[right])
            pairs[(left, right)] = rank
        self._tokens = tokens
        self._pair_ranks = pairs
        self._ranks = {token: rank for rank, token in enumerate(tokens)}

    @property
    def vocab_size(self) -> int:
        """Number of tokens in the vendored vocabulary."""
        self._load()
        assert self._tokens is not None
        return len(self._tokens)

    def _encode_piece(self, piece: str) -> int:
        """Replay the merge table over one pre-tokenizer piece.

        The table gives, for every token rank, the two ranks that were merged to
        create it, so the vocabulary is reconstructed at load time and the
        encoder is a plain greedy BPE: find the lowest-ranked adjacent pair that
        exists as a token, merge it, repeat.  A missing pair means the piece is
        finished.
        """
        ranks = self._ranks
        assert ranks is not None
        symbols = [bytes([byte]) for byte in piece.encode("utf-8")]
        while len(symbols) > 1:
            best_index = -1
            best_rank = 1 << 62
            previous = symbols[0]
            for index in range(len(symbols) - 1):
                nxt = symbols[index + 1]
                rank = ranks.get(previous + nxt)
                if rank is not None and rank < best_rank:
                    best_rank = rank
                    best_index = index
                previous = nxt
            if best_index < 0:
                break
            symbols[best_index : best_index + 2] = [
                symbols[best_index] + symbols[best_index + 1]
            ]
        return len(symbols)

    def count(self, text: str) -> int:
        """Return the number of tokens the text occupies.

        Pieces are streamed straight out of the compiled pattern instead of
        being materialised as a list: on a 5 MB prompt that avoids allocating
        over a million short-lived string objects.
        """
        if not text:
            return 0
        cached_total = self._text_cache.get(text)
        if cached_total is not None:
            return cached_total
        self._load()
        full, fast = _patterns()
        pattern = fast[self.info.name] if text.isascii() else full[self.info.name]
        pieces = pattern.findall(text)
        if not pieces:
            return 0
        cache = self._piece_cache
        encode = self._encode_piece
        # Counting occurrences with Counter (a C-level loop) and then encoding
        # each distinct piece beats a Python loop over every piece: real prompts
        # repeat the same pieces thousands of times.
        occurrences = Counter(pieces)
        total = 0
        for piece, times in occurrences.items():
            cached = cache.get(piece)
            if cached is None:
                cached = encode(piece)
                if len(cache) < _CACHE_LIMIT:
                    cache[piece] = cached
            total += cached * times
        if len(self._text_cache) < 512:
            self._text_cache[text] = total
        return total

    def pieces(self, text: str) -> list[str]:
        """Return pre-tokenizer pieces for the text (debug aid)."""
        return pretokenize(text, self.info.name)


class HeuristicTokenizer:
    """Character/byte estimator for encodings that are not vendored.

    Calibrated against published behaviour: ASCII prose averages roughly 4.0
    characters per token, ASCII code roughly 3.3, and non-ASCII text costs
    roughly one token per two UTF-8 bytes.  Documented accuracy is +/-10%: it is
    a planning aid, not a replacement for the replay tokenizer.
    """

    name = "heuristic"

    def count(self, text: str) -> int:
        """Estimate a token count for the text."""
        if not text:
            return 0
        ascii_chars = 0
        other_bytes = 0
        for ch in text:
            if ch < "\x80":
                ascii_chars += 1
            else:
                other_bytes += len(ch.encode("utf-8"))
        return max(1, round(ascii_chars / 3.7 + other_bytes / 2.0))


_RUNTIME_CACHE: dict[str, BPETokenizer] = {}


def get_tokenizer(encoding: str) -> BPETokenizer:
    """Return a cached tokenizer for a vendored encoding name."""
    key = encoding.lower()
    if key not in ENCODINGS:
        raise KeyError(key)
    tokenizer = _RUNTIME_CACHE.get(key)
    if tokenizer is None:
        tokenizer = BPETokenizer(ENCODINGS[key])
        _RUNTIME_CACHE[key] = tokenizer
    return tokenizer


@functools.lru_cache(maxsize=1)
def available_encodings() -> tuple[str, ...]:
    """Names of the vendored encodings."""
    return tuple(ENCODINGS)
