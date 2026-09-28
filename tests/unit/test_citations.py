from __future__ import annotations

from orchestrator.citations import check_memo

KNOWN = {"src_1a2b3c4d", "doc_9f8e7d6c"}


def test_fully_cited_memo_passes() -> None:
    memo = (
        "# Memo: storage\n\n## Findings\n\n"
        "- Battery installations roughly doubled over the last year in most markets "
        "[src_1a2b3c4d]\n"
        "- Procurement teams prefer four hour systems for peak shifting contracts [doc_9f8e7d6c]\n"
    )
    report = check_memo(memo, KNOWN)
    assert report.ok and report.claims == 2 and report.validity == 1.0


def test_uncited_and_unknown_citations_are_flagged() -> None:
    memo = (
        "- Analysts widely expect this trend to accelerate sharply next decade.\n"
        "- Sodium ion prices fell below lithium iron phosphate in two markets [src_deadbeef]\n"
        "Short line.\n"
    )
    report = check_memo(memo, KNOWN)
    assert {f.kind for f in report.findings} == {"unsupported_claim", "unknown_source"}
    assert report.claims == 2 and report.supported_claims == 0


def test_personal_data_is_a_policy_finding() -> None:
    report = check_memo("Contact jane.doe@example.com or +1 415 555 0100 for details.", KNOWN)
    assert [f.kind for f in report.findings].count("policy") == 2


def test_model_native_citation_styles_are_normalized() -> None:
    from orchestrator.citations import normalize_citations

    text = (
        "A claim 【doc_9f8e7d6c】 and 【src_1a2b3c4d†L3-L5】 "
        "and grouped [src_1a2b3c4d, doc_9f8e7d6c] and (src_1a2b3c4d)."
    )
    assert normalize_citations(text) == (
        "A claim [doc_9f8e7d6c] and [src_1a2b3c4d] "
        "and grouped [src_1a2b3c4d][doc_9f8e7d6c] and [src_1a2b3c4d]."
    )
    # Normalizing syntax never makes an unknown id valid.
    report = check_memo(normalize_citations("- " + "word " * 8 + "【src_deadbeef】"), KNOWN)
    assert [f.kind for f in report.findings] == ["unknown_source"]
