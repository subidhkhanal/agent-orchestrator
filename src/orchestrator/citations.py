"""Deterministic citation and policy checks on a markdown memo.

Used by the reviewer's `policy_check` tool, by the reviewer node as a code-level backstop on
its verdict, and later by the eval harness to measure citation validity.

Convention: a claim cites sources inline with markers like ``[src_1a2b3c4d]`` (web) or
``[doc_1a2b3c4d]`` (RAG documents). A claim is any non-heading line with at least
MIN_CLAIM_WORDS words, except lines under an "Open questions" heading: the writer is told to
list what the sources do not cover there, so those lines have nothing to cite. Policy checks
(emails, phone numbers) still apply to every line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CITATION_RE = re.compile(r"\[((?:src|doc)_[0-9a-f]{6,})\]")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{8,}\d)(?!\w)")
MIN_CLAIM_WORDS = 8
HEADING_RE = re.compile(r"^\s*#+\s*(.*)$")
UNCITED_SECTION_RE = re.compile(r"^open questions\b", re.IGNORECASE)


_ID = r"(?:src|doc)_[0-9a-f]{6,}"
# Model-native citation styles seen in practice, e.g. gpt-oss writes 【doc_94e09c6b】 or
# 【src_1a2b3c4d†L3-L5】, and many models group ids as [src_a, src_b].
_LENTICULAR = re.compile(rf"【\s*({_ID})[^】]*】")
_GROUPED = re.compile(rf"\[\s*({_ID}(?:\s*[,;]\s*{_ID})+)\s*\]")
_PAREN = re.compile(rf"\(\s*({_ID})\s*\)")


def normalize_citations(text: str) -> str:
    """Rewrite known citation variants to the canonical [id] form.

    This runs in code when an artifact is written, so the strict checker below can stay
    strict. Only the *syntax* is normalized: an id that is not in state is still flagged.
    """
    text = _LENTICULAR.sub(lambda m: f"[{m.group(1)}]", text)
    text = _GROUPED.sub(
        lambda m: "".join(f"[{i.strip()}]" for i in re.split(r"[,;]", m.group(1))), text
    )
    return _PAREN.sub(lambda m: f"[{m.group(1)}]", text)


@dataclass(frozen=True)
class Finding:
    kind: Literal["unsupported_claim", "unknown_source", "policy"]
    line: int
    text: str
    detail: str


@dataclass(frozen=True)
class CitationReport:
    claims: int
    supported_claims: int
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def validity(self) -> float:
        """Share of claims that cite at least one source that exists in state."""
        return 1.0 if self.claims == 0 else self.supported_claims / self.claims


def _is_claim(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    words = CITATION_RE.sub("", stripped).lstrip("-*0123456789. ").split()
    return len(words) >= MIN_CLAIM_WORDS


def check_memo(content: str, known_source_ids: set[str]) -> CitationReport:
    findings: list[Finding] = []
    claims = supported = 0
    in_open_questions = False
    for number, line in enumerate(content.splitlines(), start=1):
        heading = HEADING_RE.match(line)
        if heading:
            in_open_questions = bool(UNCITED_SECTION_RE.match(heading.group(1).strip()))
        for match in EMAIL_RE.finditer(line):
            findings.append(
                Finding("policy", number, line, f"contains an email address {match.group()!r}")
            )
        if PHONE_RE.search(CITATION_RE.sub("", line)):
            findings.append(
                Finding("policy", number, line, "contains what looks like a phone number")
            )
        if in_open_questions or not _is_claim(line):
            continue
        claims += 1
        cited = CITATION_RE.findall(line)
        unknown = [c for c in cited if c not in known_source_ids]
        for c in unknown:
            findings.append(
                Finding("unknown_source", number, line, f"cites {c}, which is not in state")
            )
        if not cited:
            findings.append(Finding("unsupported_claim", number, line, "claim has no citation"))
        elif len(unknown) < len(cited):
            supported += 1
    return CitationReport(claims=claims, supported_claims=supported, findings=tuple(findings))
