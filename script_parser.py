import re
from typing import Any


HEADER_RE = re.compile(
    r"^(?:#{1,6}\s*)?(?:bài\s*\d+\b|học\s+nhanh\s+từ\s+vựng\b|từ\s+vựng\b)",
    re.IGNORECASE,
)
SEPARATOR_RE = re.compile(r"^(?:-{3,}|={3,}|\*{3,})$")
NUMBERING_RE = re.compile(r"^\s*(?:[-•*]+|\d+[.)-])\s*")
VI_PREFIX_RE = re.compile(r"^(?:vi|vn|tiếng\s*việt)\s*:\s*", re.IGNORECASE)
EN_PREFIX_RE = re.compile(r"^(?:en|eng|tiếng\s*anh)\s*:\s*", re.IGNORECASE)


def _clean_text(value: str) -> str:
    value = value.replace("_", " ")
    value = re.sub(r"\s+", " ", value).strip(" \t|–—-")
    return value


def _sentence_case(value: str) -> str:
    value = _clean_text(value)
    if not value:
        return value
    if value.isupper() or value.islower():
        value = value.lower()
    return value[:1].upper() + value[1:]


def _parse_pair(line: str) -> tuple[str, str] | None:
    line = NUMBERING_RE.sub("", line.strip())
    if not line:
        return None

    # Preferred input: Vietnamese | English | optional duplicate/pronunciation.
    parts = [_clean_text(part) for part in line.split("|")]
    parts = [part for part in parts if part]
    if len(parts) >= 2:
        vi = VI_PREFIX_RE.sub("", parts[0]).strip()
        en = EN_PREFIX_RE.sub("", parts[1]).strip()
        if vi and en:
            return _sentence_case(vi), _sentence_case(en)

    # Also accept: VI: ... EN: ...
    match = re.match(
        r"^(?:vi|vn|tiếng\s*việt)\s*:\s*(.+?)\s+(?:en|eng|tiếng\s*anh)\s*:\s*(.+)$",
        line,
        re.IGNORECASE,
    )
    if match:
        return _sentence_case(match.group(1)), _sentence_case(match.group(2))

    # Tabs are common when content is copied from a sheet.
    tab_parts = [_clean_text(part) for part in line.split("\t") if _clean_text(part)]
    if len(tab_parts) >= 2:
        return _sentence_case(tab_parts[0]), _sentence_case(tab_parts[1])

    return None


def parse_scripts(raw: str) -> dict[str, Any]:
    raw = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    ignored: list[str] = []
    blank_run = 0

    def finish_block() -> None:
        nonlocal current
        if current:
            blocks.append(current)
            current = []

    for original in raw.split("\n"):
        line = original.strip()
        if not line:
            blank_run += 1
            # Two empty lines are treated as an intentional script break.
            if blank_run >= 2:
                finish_block()
            continue

        blank_run = 0
        if SEPARATOR_RE.match(line):
            finish_block()
            continue
        if HEADER_RE.match(line):
            finish_block()
            continue

        pair = _parse_pair(line)
        if pair:
            current.append({"vi": pair[0], "en": pair[1]})
        elif line and not line.startswith("#"):
            ignored.append(line[:140])

    finish_block()

    normalized_blocks: list[dict[str, Any]] = []
    for index, items in enumerate(blocks, start=1):
        normalized_blocks.append(
            {
                "index": index,
                "name": f"Bài {index:02d}",
                "items": items,
            }
        )

    normalized_parts = []
    for block in normalized_blocks:
        rows = ["# Học nhanh từ vựng"]
        rows.extend(f"{item['vi']} | {item['en']}" for item in block["items"])
        normalized_parts.append("\n".join(rows))

    return {
        "scripts": normalized_blocks,
        "script_count": len(normalized_blocks),
        "word_count": sum(len(block["items"]) for block in normalized_blocks),
        "normalized": "\n\n\n".join(normalized_parts),
        "ignored": ignored,
    }

