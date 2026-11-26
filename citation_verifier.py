"""Grounded Passage Citation Verifier.

Evaluates retrieval-augmented generations (RAG) and model answers against source passages.
Performs claim segmentation, citation reference resolution, verbatim and n-gram lexical
overlap span matching, hallucination detection, and structured grounding reporting.
"""
from __future__ import annotations

import argparse
import json
import re
import string
import sys
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class GroundingStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIALLY_GROUNDED = "PARTIALLY_GROUNDED"
    UNSUPPORTED = "UNSUPPORTED"
    UNRESOLVED_CITATION = "UNRESOLVED_CITATION"
    MISSING_CITATION = "MISSING_CITATION"


DEFAULT_STOPWORDS = frozenset({
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are",
    "aren't", "as", "at", "be", "because", "been", "before", "being", "below", "between", "both",
    "but", "by", "can't", "cannot", "could", "couldn't", "did", "didn't", "do", "does", "doesn't",
    "doing", "don't", "down", "during", "each", "few", "for", "from", "further", "had", "hadn't",
    "has", "hasn't", "have", "haven't", "having", "he", "he'd", "he'll", "he's", "her", "here",
    "here's", "hers", "herself", "him", "himself", "his", "how", "how's", "i", "i'd", "i'll",
    "i'm", "i've", "if", "in", "into", "is", "isn't", "it", "it's", "its", "itself", "let's",
    "me", "more", "most", "mustn't", "my", "myself", "no", "nor", "not", "of", "off", "on", "once",
    "only", "or", "other", "ought", "our", "ours", "ourselves", "out", "over", "own", "same",
    "shan't", "she", "she'd", "she'll", "she's", "should", "shouldn't", "so", "some", "such",
    "than", "that", "that's", "the", "their", "theirs", "them", "themselves", "then", "there",
    "there's", "these", "they", "they'd", "they'll", "they're", "they've", "this", "those",
    "through", "to", "too", "under", "until", "up", "very", "was", "wasn't", "we", "we'd",
    "we'll", "we're", "we've", "were", "weren't", "what", "what's", "when", "when's", "where",
    "where's", "which", "while", "who", "who's", "whom", "why", "why's", "with", "won't",
    "would", "wouldn't", "you", "you'd", "you'll", "you're", "you've", "your", "yours",
    "yourself", "yourselves", "also", "furthermore", "moreover", "therefore", "thus"
})


@dataclass
class Passage:
    id: str
    text: str
    title: Optional[str] = None
    url: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Passage:
        pid = str(data.get("id") or data.get("passage_id") or data.get("doc_id") or "")
        text = str(data.get("text") or data.get("content") or data.get("passage") or "")
        title = data.get("title")
        url = data.get("url")
        return cls(id=pid, text=text, title=str(title) if title else None, url=str(url) if url else None)


@dataclass
class MatchingSpan:
    passage_id: str
    start: int
    end: int
    text: str
    overlap_ratio: float


@dataclass
class ClaimVerification:
    claim_id: int
    claim_text: str
    raw_citations: List[str]
    resolved_passage_ids: List[str]
    unresolved_citations: List[str]
    status: GroundingStatus
    grounding_score: float
    matching_spans: List[MatchingSpan] = field(default_factory=list)
    unsupported_tokens: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim_text": self.claim_text,
            "raw_citations": self.raw_citations,
            "resolved_passage_ids": self.resolved_passage_ids,
            "unresolved_citations": self.unresolved_citations,
            "status": self.status.value,
            "grounding_score": round(self.grounding_score, 4),
            "matching_spans": [asdict(s) for s in self.matching_spans],
            "unsupported_tokens": self.unsupported_tokens,
        }


@dataclass
class VerificationReport:
    answer: str
    claims: List[ClaimVerification]
    is_grounded: bool
    grounding_ratio: float
    citation_precision: float
    citation_recall: float
    hallucination_score: float
    unresolved_citation_count: int
    missing_citation_count: int
    supported_claim_count: int
    total_claim_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "answer": self.answer,
            "is_grounded": self.is_grounded,
            "grounding_ratio": round(self.grounding_ratio, 4),
            "citation_precision": round(self.citation_precision, 4),
            "citation_recall": round(self.citation_recall, 4),
            "hallucination_score": round(self.hallucination_score, 4),
            "unresolved_citation_count": self.unresolved_citation_count,
            "missing_citation_count": self.missing_citation_count,
            "supported_claim_count": self.supported_claim_count,
            "total_claim_count": self.total_claim_count,
            "claims": [c.to_dict() for c in self.claims],
        }


# Regex to extract bracketed citations: [1], [2, 3], [Doc 1], [Passage 2], [p1], [doc_a]
CITATION_PATTERN = re.compile(r"\[([A-Za-z0-9_\-\s,]+)\]")
# Regex for sentence segmentation
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"])")


def tokenize_words(text: str) -> List[str]:
    """Tokenize into lowercase alphanumeric tokens."""
    return [w.lower().strip(string.punctuation) for w in text.split() if w.strip(string.punctuation)]


def extract_content_tokens(text: str, stopwords: Set[str] = DEFAULT_STOPWORDS) -> List[str]:
    """Extract informative non-stopword tokens."""
    return [t for t in tokenize_words(text) if t and t not in stopwords]


def clean_citation_tag(tag: str) -> List[str]:
    """Parse inside of bracket like '1, 2' or 'Doc 1' into individual citation tags."""
    results = []
    for part in tag.split(","):
        p = part.strip()
        if not p:
            continue
        # If spaced like 'Doc 1' or 'Passage 2', normalize to numeric ID
        m = re.match(r"^(?:doc|passage|ref|p)[\s]+([A-Za-z0-9_\-]+)$", p, re.IGNORECASE)
        if m:
            results.append(m.group(1))
        else:
            results.append(p)
    return results


class CitationVerifier:
    def __init__(
        self,
        min_grounding_threshold: float = 0.65,
        partial_grounding_threshold: float = 0.35,
        min_span_words: int = 3,
        stopwords: Optional[Set[str]] = None,
    ):
        self.min_grounding_threshold = min_grounding_threshold
        self.partial_grounding_threshold = partial_grounding_threshold
        self.min_span_words = min_span_words
        self.stopwords = stopwords if stopwords is not None else DEFAULT_STOPWORDS

    def parse_sentences_and_citations(self, text: str) -> List[Tuple[str, List[str], str]]:
        """Split text into sentences, extracting claims, clean text, and attached citations."""
        raw_sentences = [s.strip() for s in SENTENCE_SPLIT_PATTERN.split(text.strip()) if s.strip()]
        if not raw_sentences and text.strip():
            raw_sentences = [text.strip()]

        claims = []
        for raw_sentence in raw_sentences:
            matches = list(CITATION_PATTERN.finditer(raw_sentence))
            cites: List[str] = []
            for m in matches:
                cites.extend(clean_citation_tag(m.group(1)))
            clean_sentence = CITATION_PATTERN.sub("", raw_sentence).strip()
            # Clean up residual spacing/punctuation
            clean_sentence = re.sub(r"\s+", " ", clean_sentence).strip()
            if clean_sentence:
                claims.append((raw_sentence, cites, clean_sentence))
        return claims

    def find_longest_matching_spans(self, claim_text: str, passage_text: str) -> List[MatchingSpan]:
        """Find verbatim or high-overlap text spans between claim and passage."""
        claim_tokens = tokenize_words(claim_text)
        passage_tokens = tokenize_words(passage_text)
        if not claim_tokens or not passage_tokens:
            return []

        spans: List[MatchingSpan] = []
        n = len(claim_tokens)
        passage_str_lower = passage_text.lower()

        matched_claim_indices = set()

        for span_len in range(min(n, 12), self.min_span_words - 1, -1):
            for i in range(n - span_len + 1):
                if any(idx in matched_claim_indices for idx in range(i, i + span_len)):
                    continue
                window = " ".join(claim_tokens[i : i + span_len])
                found_pos = passage_str_lower.find(window)
                if found_pos != -1:
                    for idx in range(i, i + span_len):
                        matched_claim_indices.add(idx)
                    spans.append(
                        MatchingSpan(
                            passage_id="",
                            start=found_pos,
                            end=found_pos + len(window),
                            text=passage_text[found_pos : found_pos + len(window)],
                            overlap_ratio=span_len / n,
                        )
                    )
        return spans

    def compute_grounding_score(
        self, claim_clean: str, passages: List[Passage]
    ) -> Tuple[float, List[MatchingSpan], List[str]]:
        """Calculate grounding score based on content token coverage and verbatim span matches."""
        claim_tokens = extract_content_tokens(claim_clean, self.stopwords)
        if not claim_tokens:
            all_words = tokenize_words(claim_clean)
            if not all_words:
                return 1.0, [], []
            claim_tokens = all_words

        all_matching_spans: List[MatchingSpan] = []
        covered_tokens: Set[str] = set()

        combined_passage_tokens: Set[str] = set()
        for p in passages:
            p_tokens = set(tokenize_words(p.text))
            combined_passage_tokens.update(p_tokens)
            spans = self.find_longest_matching_spans(claim_clean, p.text)
            for s in spans:
                s.passage_id = p.id
                all_matching_spans.append(s)

        unsupported: List[str] = []
        for t in claim_tokens:
            if t in combined_passage_tokens:
                covered_tokens.add(t)
            else:
                unsupported.append(t)

        token_coverage = len(covered_tokens) / max(len(claim_tokens), 1)

        span_bonus = 0.0
        if all_matching_spans:
            max_span_ratio = max(s.overlap_ratio for s in all_matching_spans)
            span_bonus = min(0.2, max_span_ratio * 0.2)

        final_score = min(1.0, token_coverage * 0.85 + span_bonus)
        return final_score, all_matching_spans, unsupported

    def resolve_citation(self, cite: str, passage_objs: List[Passage], passage_map: Dict[str, Passage]) -> Optional[Passage]:
        """Resolve a citation string to a Passage instance."""
        # 1. Exact match
        if cite in passage_map:
            return passage_map[cite]
        # 2. Case-insensitive match
        for pid, p in passage_map.items():
            if pid.lower() == cite.lower():
                return p
        # 3. Numeric index (1-based: [1] -> passage_objs[0])
        if cite.isdigit():
            idx = int(cite) - 1
            if 0 <= idx < len(passage_objs):
                return passage_objs[idx]
        # 4. Check with prefix stripped (e.g. 'doc_1' -> '1', 'p1' -> '1')
        sub = re.sub(r"^(?:doc[_\-]?|passage[_\-]?|p|ref[_\-]?)", "", cite, flags=re.IGNORECASE)
        if sub and sub != cite:
            if sub in passage_map:
                return passage_map[sub]
            if sub.isdigit():
                idx = int(sub) - 1
                if 0 <= idx < len(passage_objs):
                    return passage_objs[idx]
        return None

    def verify(
        self,
        answer: str,
        passages: List[Dict[str, Any] | Passage],
    ) -> VerificationReport:
        """Verify the full answer text against reference passages."""
        passage_objs = [p if isinstance(p, Passage) else Passage.from_dict(p) for p in passages]
        passage_map: Dict[str, Passage] = {p.id: p for p in passage_objs}

        parsed_claims = self.parse_sentences_and_citations(answer)
        verified_claims: List[ClaimVerification] = []

        supported_count = 0
        total_claims = len(parsed_claims)
        unresolved_total = 0
        missing_citation_total = 0

        for claim_id, (raw_sent, raw_cites, clean_claim) in enumerate(parsed_claims, 1):
            resolved_pids: List[str] = []
            unresolved_cites: List[str] = []

            for cite in raw_cites:
                target_p = self.resolve_citation(cite, passage_objs, passage_map)
                if target_p is not None:
                    resolved_pids.append(target_p.id)
                else:
                    unresolved_cites.append(cite)

            unresolved_total += len(unresolved_cites)

            if unresolved_cites:
                status = GroundingStatus.UNRESOLVED_CITATION
                verified_claims.append(
                    ClaimVerification(
                        claim_id=claim_id,
                        claim_text=clean_claim,
                        raw_citations=raw_cites,
                        resolved_passage_ids=resolved_pids,
                        unresolved_citations=unresolved_cites,
                        status=status,
                        grounding_score=0.0,
                    )
                )
                continue

            if not raw_cites:
                missing_citation_total += 1
                score, spans, unsupp = self.compute_grounding_score(clean_claim, passage_objs)
                verified_claims.append(
                    ClaimVerification(
                        claim_id=claim_id,
                        claim_text=clean_claim,
                        raw_citations=[],
                        resolved_passage_ids=[],
                        unresolved_citations=[],
                        status=GroundingStatus.MISSING_CITATION,
                        grounding_score=score,
                        matching_spans=spans,
                        unsupported_tokens=unsupp,
                    )
                )
                continue

            target_passages = [passage_map[pid] for pid in resolved_pids if pid in passage_map]
            score, spans, unsupp = self.compute_grounding_score(clean_claim, target_passages)

            if score >= self.min_grounding_threshold:
                status = GroundingStatus.SUPPORTED
                supported_count += 1
            elif score >= self.partial_grounding_threshold:
                status = GroundingStatus.PARTIALLY_GROUNDED
            else:
                status = GroundingStatus.UNSUPPORTED

            verified_claims.append(
                ClaimVerification(
                    claim_id=claim_id,
                    claim_text=clean_claim,
                    raw_citations=raw_cites,
                    resolved_passage_ids=resolved_pids,
                    unresolved_citations=[],
                    status=status,
                    grounding_score=score,
                    matching_spans=spans,
                    unsupported_tokens=unsupp,
                )
            )

        grounding_ratio = supported_count / max(total_claims, 1)
        cited_claims = [c for c in verified_claims if c.raw_citations]
        citation_precision = (
            sum(1 for c in cited_claims if c.status == GroundingStatus.SUPPORTED) / max(len(cited_claims), 1)
        )
        citation_recall = supported_count / max(total_claims, 1)
        hallucinated_claims = [
            c for c in verified_claims if c.status in (GroundingStatus.UNSUPPORTED, GroundingStatus.UNRESOLVED_CITATION)
        ]
        hallucination_score = len(hallucinated_claims) / max(total_claims, 1)

        is_grounded = (
            grounding_ratio >= 0.70
            and unresolved_total == 0
            and hallucination_score <= 0.15
        )

        return VerificationReport(
            answer=answer,
            claims=verified_claims,
            is_grounded=is_grounded,
            grounding_ratio=grounding_ratio,
            citation_precision=citation_precision,
            citation_recall=citation_recall,
            hallucination_score=hallucination_score,
            unresolved_citation_count=unresolved_total,
            missing_citation_count=missing_citation_total,
            supported_claim_count=supported_count,
            total_claim_count=total_claims,
        )


def main():
    parser = argparse.ArgumentParser(description="Grounded Passage Citation Verifier CLI")
    parser.add_argument("--input", required=True, help="Input JSON file containing {answer, passages}")
    parser.add_argument("--output", help="Optional output JSON file to write report")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    verifier = CitationVerifier()
    answer = data.get("answer") or data.get("response") or ""
    passages = data.get("passages") or []
    report = verifier.verify(answer, passages)
    out_dict = report.to_dict()

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out_dict, f, indent=2, ensure_ascii=False)
        print(f"Report written to {args.output}")
    else:
        print(json.dumps(out_dict, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
