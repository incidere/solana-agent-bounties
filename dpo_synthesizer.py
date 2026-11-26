"""Abstention DPO Pair Synthesizer.

Synthesizes Direct Preference Optimization (DPO) preference pairs (prompt, chosen, rejected)
for aligning Large Language Models to balance helpful grounded generation with calibrated
abstention. Categorizes queries into four distinct regimes:
1. ANSWERABLE: Passages contain full evidence. Chosen answers with grounded citations; rejected over-abstains or hallucinates.
2. UNANSWERABLE: Passages lack evidence. Chosen explicitly abstains; rejected fabricates false answers.
3. PARTIALLY_ANSWERABLE: Passages contain partial evidence. Chosen answers grounded subset and abstains on the rest; rejected overconfidently guesses missing parts.
4. FALSE_PRESUPPOSITION: Query contains contradictory premises. Chosen refutes the false premise; rejected accepts and hallucinates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from citation_verifier import CitationVerifier, GroundingStatus, Passage, VerificationReport


class QueryRegime(str, Enum):
    ANSWERABLE = "ANSWERABLE"
    UNANSWERABLE = "UNANSWERABLE"
    PARTIALLY_ANSWERABLE = "PARTIALLY_ANSWERABLE"
    FALSE_PRESUPPOSITION = "FALSE_PRESUPPOSITION"


@dataclass
class DPOPair:
    id: str
    prompt: str
    chosen: str
    rejected: str
    regime: QueryRegime
    passages: List[Passage]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "chosen": self.chosen,
            "rejected": self.rejected,
            "regime": self.regime.value,
            "passages": [asdict(p) for p in self.passages],
            "metadata": self.metadata,
        }

    def to_hf_format(self) -> Dict[str, Any]:
        """HuggingFace standard DPO format."""
        return {
            "prompt": self.prompt,
            "chosen": self.chosen,
            "rejected": self.rejected,
        }


# Canonical synthetic factual seed bank across multiple domains
TOPIC_FACT_CORPUS = [
    {
        "topic": "Helium-3 Lunar Abundance",
        "doc_id": "doc_he3",
        "title": "Lunar Regolith Volatiles",
        "passage": "Helium-3 is deposited in lunar regolith by solar winds over billions of years. Samples from Oceanus Procellarum show concentrations reaching 15 parts per billion. The isotope is considered a potential aneutronic fuel for nuclear fusion with deuterium.",
        "facts": [
            ("What deposits Helium-3 in lunar regolith?", "Solar winds deposit Helium-3 in lunar regolith over billions of years."),
            ("What concentration was measured in Oceanus Procellarum?", "Samples from Oceanus Procellarum show Helium-3 concentrations reaching 15 parts per billion."),
            ("Why is Helium-3 considered valuable for energy?", "Helium-3 is considered a potential aneutronic fuel for nuclear fusion with deuterium."),
        ],
        "absent_questions": [
            "What was the exact cost per ton of Helium-3 extraction on Apollo 17?",
            "Which private mining company constructed the first commercial Helium-3 refinery at Shackleton Crater in 1985?",
        ],
        "false_presuppositions": [
            ("Since Helium-3 was first discovered in Martian volcanic vents, how did it reach the Moon?", "The passage does not state that Helium-3 was discovered in Martian volcanic vents; it indicates that Helium-3 is deposited in lunar regolith by solar winds."),
        ]
    },
    {
        "topic": "Asynchronous Stream Backpressure",
        "doc_id": "doc_stream",
        "title": "Reactive Systems Stream Semantics",
        "passage": "Reactive Streams define a non-blocking backpressure specification where consumers request n items via Subscription.request(n). If the producer buffer exceeds high-watermark threshold K, it suspends packet ingestion until consumer demand signals arrive.",
        "facts": [
            ("How do consumers signal demand in Reactive Streams?", "Consumers request items via Subscription.request(n)."),
            ("What triggers producer suspension under backpressure?", "When the producer buffer exceeds high-watermark threshold K, packet ingestion is suspended until demand signals arrive."),
        ],
        "absent_questions": [
            "What is the UDP port number allocated for Reactive Streams backpressure telemetry?",
            "How many gigabytes of memory does Subscription.request consume per million subscribers?",
        ],
        "false_presuppositions": [
            ("Given that Reactive Streams require synchronous blocking semaphores, how does it achieve high throughput?", "Reactive Streams do not use synchronous blocking semaphores; the specification defines a non-blocking backpressure protocol."),
        ]
    },
    {
        "topic": "CRDT Distributed State",
        "doc_id": "doc_crdt",
        "title": "Conflict-Free Replicated Data Types",
        "passage": "State-based CRDTs, also known as Convergent Replicated Data Types (CvRDTs), achieve strong eventual consistency by ensuring all concurrent state replicas form a semilattice with a monotonic join operator. Every update must increase state monotonically under partial order.",
        "facts": [
            ("What mathematical structure guarantees consistency in State-based CRDTs?", "State-based CRDTs achieve consistency because state replicas form a semilattice with a monotonic join operator."),
            ("What alternative name refers to State-based CRDTs?", "State-based CRDTs are also known as Convergent Replicated Data Types (CvRDTs)."),
        ],
        "absent_questions": [
            "Which patent filed by IBM in 1972 holds exclusive rights to CvRDT join semilattices?",
            "What is the maximum latency in microseconds for CvRDT gossiping over satellite transponders?",
        ],
        "false_presuppositions": [
            ("Why do CvRDTs require a centralized Paxos leader to commit join operations?", "CvRDTs do not require a centralized Paxos leader; they achieve strong eventual consistency peer-to-peer via a join semilattice."),
        ]
    },
    {
        "topic": "Ed25519 Curve Signatures",
        "doc_id": "doc_ed25519",
        "title": "Edwards-curve Digital Signature Algorithm",
        "passage": "Ed25519 is an EdDSA signature scheme using SHA-512 and Curve25519 over the prime field 2^255 - 19. Public keys are 32 bytes and signatures are 64 bytes in length, providing collision resilience and resistance to side-channel timing attacks.",
        "facts": [
            ("What hash function is used in Ed25519?", "Ed25519 is an EdDSA signature scheme using SHA-512."),
            ("What are the exact key and signature sizes in Ed25519?", "Ed25519 public keys are 32 bytes and signatures are 64 bytes in length."),
        ],
        "absent_questions": [
            "How many quantum qubits are required to factor the Ed25519 prime field under Shor's algorithm in under 12 seconds?",
            "Who was the French mathematician who proved the 2^255 - 19 prime field in 1789?",
        ],
        "false_presuppositions": [
            ("Since Ed25519 uses 128-byte public keys and MD5 hashing, why is it preferred over RSA?", "Ed25519 does not use 128-byte keys or MD5; it uses 32-byte public keys and SHA-512."),
        ]
    },
    {
        "topic": "B-Tree Node Split Invariants",
        "doc_id": "doc_btree",
        "title": "Storage Engine B-Tree Mechanics",
        "passage": "In a B-Tree of order M, every internal node except the root contains between ceil(M/2) and M child pointers. When an insertion causes a leaf node to exceed M keys, the median key is promoted to the parent node and the remaining keys are split into two sibling nodes.",
        "facts": [
            ("What happens when an insertion exceeds M keys in a B-Tree leaf?", "When an insertion exceeds M keys, the median key is promoted to the parent node and the remaining keys are split into two sibling nodes."),
            ("How many child pointers can an internal node contain in a B-Tree of order M?", "Every internal node except the root contains between ceil(M/2) and M child pointers."),
        ],
        "absent_questions": [
            "What is the average electricity cost in kilowatt-hours to perform 10,000 B-Tree node splits in Frankfurt?",
            "Which Linux kernel syscall directly forces a B-Tree page split in NVMe hardware?",
        ],
        "false_presuppositions": [
            ("Because B-Trees store all data in the root and never split child nodes, how do they handle gigabyte-scale datasets?", "B-Trees do not store all data in the root; leaf nodes store keys and split when exceeding M keys."),
        ]
    }
]

ABSTENTION_TEMPLATES = [
    "Based on the provided passages, there is insufficient evidence to answer this question.",
    "The provided context does not contain information to answer this inquiry.",
    "Based on the supplied reference materials, no information is available regarding this matter.",
    "The provided documents do not state or imply the details necessary to address this question.",
]

COWARDLY_OVER_ABSTENTION_TEMPLATES = [
    "I am sorry, but I do not possess enough information to answer your question.",
    "I cannot assist with this request because the source documents are inconclusive.",
    "As an AI, I am unable to determine the answer from the context.",
    "I cannot answer this question based on the materials provided.",
]


class DPOSynthesizer:
    def __init__(self, seed: Optional[int] = 42, verifier: Optional[CitationVerifier] = None):
        self.rng = random.Random(seed)
        self.verifier = verifier if verifier is not None else CitationVerifier()

    def format_prompt(self, passages: List[Passage], question: str) -> str:
        """Format passages and question into standard RAG prompt."""
        formatted_passages = []
        for p in passages:
            title_part = f" [{p.title}]" if p.title else ""
            formatted_passages.append(f"Passage [{p.id}]{title_part}:\n{p.text}")
        context_str = "\n\n".join(formatted_passages)
        return f"Context:\n{context_str}\n\nQuestion: {question}\nAnswer with grounded passage citations (e.g. [1] or [{passages[0].id}])."

    def synthesize_answerable_pair(self, topic_entry: Dict[str, Any]) -> DPOPair:
        """Generate Answerable query pair: chosen is grounded with citation; rejected is over-abstention."""
        fact_q, fact_a = self.rng.choice(topic_entry["facts"])
        doc_id = topic_entry["doc_id"]
        passage = Passage(id=doc_id, text=topic_entry["passage"], title=topic_entry["title"])

        prompt = self.format_prompt([passage], fact_q)
        chosen = f"{fact_a} [{doc_id}]"

        # Rejected can either be an ungrounded hallucination or a cowardly over-abstention
        if self.rng.random() < 0.5:
            rejected = self.rng.choice(COWARDLY_OVER_ABSTENTION_TEMPLATES)
            rejection_mode = "over_abstention"
        else:
            # Fabricated entity/statistic with ungrounded citation
            fake_entity = f"According to [{doc_id}], this was established in 1912 by Lord Kelvin with a 99.8% certainty index."
            rejected = fake_entity
            rejection_mode = "hallucinated_facts"

        pair_id = f"ans_{hashlib.sha256((fact_q + chosen).encode()).hexdigest()[:12]}"

        # Verify chosen with verifier
        rep = self.verifier.verify(chosen, [passage])

        return DPOPair(
            id=pair_id,
            prompt=prompt,
            chosen=chosen,
            rejected=rejected,
            regime=QueryRegime.ANSWERABLE,
            passages=[passage],
            metadata={
                "rejection_mode": rejection_mode,
                "chosen_grounding_score": rep.grounding_ratio,
                "topic": topic_entry["topic"],
            },
        )

    def synthesize_unanswerable_pair(self, topic_entry: Dict[str, Any]) -> DPOPair:
        """Generate Unanswerable query pair: chosen abstains; rejected hallucinates."""
        absent_q = self.rng.choice(topic_entry["absent_questions"])
        doc_id = topic_entry["doc_id"]
        passage = Passage(id=doc_id, text=topic_entry["passage"], title=topic_entry["title"])

        prompt = self.format_prompt([passage], absent_q)
        chosen = self.rng.choice(ABSTENTION_TEMPLATES)

        # Rejected hallucinates an answer citing the passage or a nonexistent citation
        rejected = f"Based on [{doc_id}], the answer is exactly 42 units as documented in the primary index [99]."

        pair_id = f"unans_{hashlib.sha256((absent_q + chosen).encode()).hexdigest()[:12]}"

        return DPOPair(
            id=pair_id,
            prompt=prompt,
            chosen=chosen,
            rejected=rejected,
            regime=QueryRegime.UNANSWERABLE,
            passages=[passage],
            metadata={
                "rejection_mode": "hallucination_on_missing_info",
                "topic": topic_entry["topic"],
            },
        )

    def synthesize_partially_answerable_pair(self, topic_entry: Dict[str, Any]) -> DPOPair:
        """Generate Partial query: chosen answers known and abstains on unknown; rejected guesses unknown."""
        fact_q, fact_a = self.rng.choice(topic_entry["facts"])
        absent_q = self.rng.choice(topic_entry["absent_questions"])
        doc_id = topic_entry["doc_id"]
        passage = Passage(id=doc_id, text=topic_entry["passage"], title=topic_entry["title"])

        combined_q = f"{fact_q} Also, {absent_q}"
        prompt = self.format_prompt([passage], combined_q)

        chosen = (
            f"According to [{doc_id}], {fact_a} "
            f"However, the provided passage does not contain information regarding {absent_q.lower().rstrip('?') }."
        )

        rejected = (
            f"According to [{doc_id}], {fact_a} "
            f"Furthermore, {absent_q.lower().rstrip('?')} is precisely 8,500 units per annual cycle."
        )

        pair_id = f"part_{hashlib.sha256((combined_q + chosen).encode()).hexdigest()[:12]}"

        return DPOPair(
            id=pair_id,
            prompt=prompt,
            chosen=chosen,
            rejected=rejected,
            regime=QueryRegime.PARTIALLY_ANSWERABLE,
            passages=[passage],
            metadata={
                "rejection_mode": "overconfident_completion",
                "topic": topic_entry["topic"],
            },
        )

    def synthesize_false_presupposition_pair(self, topic_entry: Dict[str, Any]) -> DPOPair:
        """Generate False Presupposition query: chosen corrects premise; rejected accepts premise."""
        false_q, refute_a = self.rng.choice(topic_entry["false_presuppositions"])
        doc_id = topic_entry["doc_id"]
        passage = Passage(id=doc_id, text=topic_entry["passage"], title=topic_entry["title"])

        prompt = self.format_prompt([passage], false_q)
        chosen = f"{refute_a} [{doc_id}]"

        rejected = f"Indeed, because of that initial premise, [{doc_id}] confirms that this architecture functions via standard protocols."

        pair_id = f"fp_{hashlib.sha256((false_q + chosen).encode()).hexdigest()[:12]}"

        return DPOPair(
            id=pair_id,
            prompt=prompt,
            chosen=chosen,
            rejected=rejected,
            regime=QueryRegime.FALSE_PRESUPPOSITION,
            passages=[passage],
            metadata={
                "rejection_mode": "accepted_false_presupposition",
                "topic": topic_entry["topic"],
            },
        )

    def synthesize_dataset(
        self,
        count: int = 40,
        regime_weights: Optional[Dict[QueryRegime, float]] = None,
    ) -> List[DPOPair]:
        """Synthesize a balanced dataset of DPO preference pairs across regimes."""
        if regime_weights is None:
            regime_weights = {
                QueryRegime.ANSWERABLE: 0.40,
                QueryRegime.UNANSWERABLE: 0.30,
                QueryRegime.PARTIALLY_ANSWERABLE: 0.15,
                QueryRegime.FALSE_PRESUPPOSITION: 0.15,
            }

        pairs: List[DPOPair] = []
        regimes = list(regime_weights.keys())
        weights = [regime_weights[r] for r in regimes]

        for i in range(count):
            regime = self.rng.choices(regimes, weights=weights, k=1)[0]
            topic = self.rng.choice(TOPIC_FACT_CORPUS)

            if regime == QueryRegime.ANSWERABLE:
                pair = self.synthesize_answerable_pair(topic)
            elif regime == QueryRegime.UNANSWERABLE:
                pair = self.synthesize_unanswerable_pair(topic)
            elif regime == QueryRegime.PARTIALLY_ANSWERABLE:
                pair = self.synthesize_partially_answerable_pair(topic)
            else:
                pair = self.synthesize_false_presupposition_pair(topic)

            pairs.append(pair)

        return pairs

    def audit_dataset(self, pairs: List[DPOPair]) -> Dict[str, Any]:
        """Audit the quality, balance, and grounding metrics of a DPO dataset."""
        regime_counts: Dict[str, int] = {}
        total = len(pairs)
        chosen_grounded_in_ans = 0
        ans_total = 0
        abstention_detected_in_unans = 0
        unans_total = 0

        for p in pairs:
            r_str = p.regime.value
            regime_counts[r_str] = regime_counts.get(r_str, 0) + 1

            if p.regime == QueryRegime.ANSWERABLE:
                ans_total += 1
                rep = self.verifier.verify(p.chosen, p.passages)
                if rep.grounding_ratio >= 0.65:
                    chosen_grounded_in_ans += 1
            elif p.regime == QueryRegime.UNANSWERABLE:
                unans_total += 1
                if any(phrase in p.chosen.lower() for phrase in ["insufficient", "not contain", "no information", "unable", "not state"]):
                    abstention_detected_in_unans += 1

        return {
            "total_pairs": total,
            "regime_distribution": regime_counts,
            "answerable_chosen_grounding_pass_rate": (
                round(chosen_grounded_in_ans / max(ans_total, 1), 4)
            ),
            "unanswerable_chosen_abstention_rate": (
                round(abstention_detected_in_unans / max(unans_total, 1), 4)
            ),
        }


def main():
    parser = argparse.ArgumentParser(description="Abstention DPO Pair Synthesizer CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    syn_p = subparsers.add_parser("synthesize", help="Synthesize DPO dataset")
    syn_p.add_argument("--count", type=int, default=50, help="Number of DPO pairs to synthesize")
    syn_p.add_argument("--seed", type=int, default=42, help="PRNG random seed for determinism")
    syn_p.add_argument("--output", required=True, help="Output JSONL file")

    aud_p = subparsers.add_parser("audit", help="Audit an existing DPO JSONL file")
    aud_p.add_argument("--input", required=True, help="Input JSONL file")

    args = parser.parse_args()

    if args.command == "synthesize":
        synth = DPOSynthesizer(seed=args.seed)
        dataset = synth.synthesize_dataset(count=args.count)
        with open(args.output, "w", encoding="utf-8") as f:
            for item in dataset:
                f.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
        audit_res = synth.audit_dataset(dataset)
        print(f"Generated {len(dataset)} DPO pairs written to {args.output}")
        print("Audit results:", json.dumps(audit_res, indent=2))

    elif args.command == "audit":
        synth = DPOSynthesizer()
        pairs = []
        with open(args.input, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    d = json.loads(line)
                    passages = [Passage.from_dict(p) for p in d.get("passages", [])]
                    pairs.append(
                        DPOPair(
                            id=d["id"],
                            prompt=d["prompt"],
                            chosen=d["chosen"],
                            rejected=d["rejected"],
                            regime=QueryRegime(d["regime"]),
                            passages=passages,
                            metadata=d.get("metadata", {}),
                        )
                    )
        audit_res = synth.audit_dataset(pairs)
        print("Audit results:", json.dumps(audit_res, indent=2))


if __name__ == "__main__":
    main()
