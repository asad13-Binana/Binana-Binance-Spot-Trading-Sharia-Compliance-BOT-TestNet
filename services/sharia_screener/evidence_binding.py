"""Exact extracted-text bindings for owner-reviewed Sharia evidence.

This module is transport integrity, not a Sharia rules engine.  It binds the
owner's reviewed block to one exact occurrence in one exact extraction of one
exact HTTP response.  Runtime callers verify the complete tuple before an
``EvidenceClaim`` can reach the immutable V19.1 rules executor.

The important invariant is that no substring search or sentence-boundary
guess is performed during authorisation.  The offsets selected during
``propose`` are replayed exactly; changed bytes, changed extraction, edited
quotes, ambiguous occurrences, or altered surrounding context all fail
closed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


EXTRACTOR_VERSION = "local-text-v2-blocks"
CONTEXT_NEIGHBOUR_BLOCKS = 1
MAX_REVIEW_BLOCK_CHARS = 8_000
MAX_REVIEW_CONTEXT_CHARS = 24_000
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class EvidenceBindingError(ValueError):
    """An evidence block cannot be bound or no longer matches its binding."""


@dataclass(frozen=True)
class TextBlock:
    start: int
    end: int
    text: str


def extracted_text_sha256(text: str) -> str:
    """Hash the exact UTF-8 extracted text consumed by the rules executor."""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def text_blocks(text: str) -> list[TextBlock]:
    """Return exact non-empty newline-delimited blocks and their offsets."""
    value = str(text)
    blocks: list[TextBlock] = []
    cursor = 0
    for raw in value.splitlines(keepends=True):
        content_end = cursor + len(raw.rstrip("\r\n"))
        left = cursor
        right = content_end
        while left < right and value[left].isspace():
            left += 1
        while right > left and value[right - 1].isspace():
            right -= 1
        if left < right:
            blocks.append(TextBlock(left, right, value[left:right]))
        cursor += len(raw)
    if cursor < len(value):
        # ``splitlines(keepends=True)`` normally includes the final fragment;
        # retain this guard so a future text implementation cannot drop it.
        tail = value[cursor:]
        left = cursor + len(tail) - len(tail.lstrip())
        right = len(value.rstrip())
        if left < right:
            blocks.append(TextBlock(left, right, value[left:right]))
    if not blocks and value.strip():
        left = len(value) - len(value.lstrip())
        right = len(value.rstrip())
        blocks.append(TextBlock(left, right, value[left:right]))
    return blocks


def _context_span(blocks: list[TextBlock], index: int) -> tuple[int, int]:
    left_index = max(0, index - CONTEXT_NEIGHBOUR_BLOCKS)
    right_index = min(len(blocks) - 1, index + CONTEXT_NEIGHBOUR_BLOCKS)
    return blocks[left_index].start, blocks[right_index].end


def bind_reviewed_block(text: str, quote: str, *, quote_start: int | None = None
                        ) -> dict:
    """Bind one complete extracted-text block and its surrounding context.

    ``quote_start`` is mandatory when the same complete block occurs more than
    once.  This prevents a reviewed occurrence from being silently relocated
    to a different, possibly contradictory, occurrence.
    """
    value = str(text)
    selected = str(quote).strip()
    if not selected:
        raise EvidenceBindingError("reviewed quote is empty")
    if len(selected) > MAX_REVIEW_BLOCK_CHARS:
        raise EvidenceBindingError(
            f"reviewed block exceeds {MAX_REVIEW_BLOCK_CHARS} characters")
    blocks = text_blocks(value)
    matches = [(index, block) for index, block in enumerate(blocks)
               if block.text == selected]
    if quote_start is not None:
        if isinstance(quote_start, bool) or not isinstance(quote_start, int):
            raise EvidenceBindingError("quote_start must be an integer offset")
        matches = [(index, block) for index, block in matches
                   if block.start == quote_start]
    if not matches:
        raise EvidenceBindingError(
            "quote must exactly equal one complete extracted-text block")
    if len(matches) != 1:
        starts = [block.start for _index, block in matches]
        raise EvidenceBindingError(
            f"quote occurs in multiple complete blocks at {starts}; select an "
            "explicit quote_start from the review sheet")
    index, block = matches[0]
    context_start, context_end = _context_span(blocks, index)
    context = value[context_start:context_end]
    if len(context) > MAX_REVIEW_CONTEXT_CHARS:
        raise EvidenceBindingError(
            f"review context exceeds {MAX_REVIEW_CONTEXT_CHARS} characters")
    return {
        "extractor_version": EXTRACTOR_VERSION,
        "text_sha256": extracted_text_sha256(value),
        "quote_start": block.start,
        "quote_end": block.end,
        "context_start": context_start,
        "context_end": context_end,
        "context_sha256": hashlib.sha256(
            context.encode("utf-8")).hexdigest(),
        "context": context,
    }


def verify_reviewed_block(text: str, quote: str, binding: object) -> tuple[bool, str]:
    """Verify an exact binding without searching for or relocating the quote."""
    if not isinstance(binding, dict):
        return False, "evidence binding is missing"
    if binding.get("extractor_version") != EXTRACTOR_VERSION:
        return False, "evidence extractor version is missing or unsupported"
    expected_text_hash = str(binding.get("text_sha256", ""))
    if not _DIGEST.fullmatch(expected_text_hash):
        return False, "evidence text SHA-256 is malformed"
    value = str(text)
    if extracted_text_sha256(value) != expected_text_hash:
        return False, "retrieved extracted text differs from owner-reviewed text"

    offsets: dict[str, int] = {}
    for name in ("quote_start", "quote_end", "context_start", "context_end"):
        raw = binding.get(name)
        if isinstance(raw, bool) or not isinstance(raw, int):
            return False, f"{name} must be an integer"
        offsets[name] = raw
    qs, qe = offsets["quote_start"], offsets["quote_end"]
    cs, ce = offsets["context_start"], offsets["context_end"]
    if not (0 <= cs <= qs < qe <= ce <= len(value)):
        return False, "evidence offsets are out of range or not nested"
    selected = str(quote).strip()
    if not selected or len(selected) > MAX_REVIEW_BLOCK_CHARS:
        return False, "reviewed quote is empty or exceeds the block limit"
    if value[qs:qe] != selected:
        return False, "quote bytes at the reviewed offsets do not match"
    blocks = text_blocks(value)
    if not any(block.start == qs and block.end == qe and block.text == selected
               for block in blocks):
        return False, "quote offsets no longer identify one complete text block"
    context = value[cs:ce]
    if len(context) > MAX_REVIEW_CONTEXT_CHARS:
        return False, "owner-reviewed context exceeds the context limit"
    if context != str(binding.get("context", "")):
        return False, "owner-reviewed context text does not match its offsets"
    expected_context_hash = str(binding.get("context_sha256", ""))
    if (not _DIGEST.fullmatch(expected_context_hash) or
            hashlib.sha256(context.encode("utf-8")).hexdigest() !=
            expected_context_hash):
        return False, "owner-reviewed context SHA-256 does not match"
    return True, ""
