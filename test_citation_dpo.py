"""Unit and Integration Tests for Grounded Passage Citation Verifier & Abstention DPO Synthesizer.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from citation_verifier import (
    CitationVerifier,
    ClaimVerification,
    GroundingStatus,
    Passage,
    VerificationReport,
    clean_citation_tag,
    extract_content_tokens,
    tokenize_words,
)
from dpo_synthesizer import (
    DPOPair,
    DPOSynthesizer,
    QueryRegime,
    TOPIC_FACT_CORPUS,
)


class TestCitationVerifier(unittest.TestCase):
    def setUp(self):
        self.verifier = CitationVerifier(min_grounding_threshold=0.65, partial_grounding_threshold=0.35)
        self.passages = [
            Passage(
                id="doc_1",
                title="Quantum Teleportation",
                text="Quantum teleportation transfers quantum information between separated qubits using shared entanglement and classical communication. In 1993, Bennett et al. published the theoretical protocol. Real-world experiments achieved transmission across 144 kilometers in free space.",
            ),
            Passage(
                id="doc_2",
                title="Optical Fiber Attenuation",
                text="Telecom optical fibers exhibit minimum transmission loss near 1550 nanometers wavelength, typically 0.2 dB per kilometer. Erbium-doped fiber amplifiers (EDFAs) operate in this C-band window to regenerate optical signals without electrical conversion.",
            ),
        ]

    def test_citation_parsing_and_tag_cleaning(self):
        self.assertEqual(clean_citation_tag("1"), ["1"])
        self.assertEqual(clean_citation_tag("doc_1, doc_2"), ["doc_1", "doc_2"])
        self.assertEqual(clean_citation_tag("Doc 1"), ["1"])
        self.assertEqual(clean_citation_tag("Passage 4"), ["4"])
        self.assertEqual(clean_citation_tag("ref-9"), ["ref-9"])

        text = "Quantum teleportation was published by Bennett in 1993 [doc_1]. EDFAs amplify signals in the 1550 nm window [doc_2]."
        claims = self.verifier.parse_sentences_and_citations(text)
        self.assertEqual(len(claims), 2)
        self.assertEqual(claims[0][1], ["doc_1"])
        self.assertEqual(claims[1][1], ["doc_2"])

    def test_fully_grounded_answer(self):
        answer = "Bennett published the theoretical protocol for quantum teleportation in 1993 [doc_1]. Telecom fibers exhibit minimum loss near 1550 nanometers [doc_2]."
        report = self.verifier.verify(answer, self.passages)
        self.assertTrue(report.is_grounded)
        self.assertEqual(report.total_claim_count, 2)
        self.assertEqual(report.supported_claim_count, 2)
        self.assertAlmostEqual(report.grounding_ratio, 1.0, places=2)
        self.assertAlmostEqual(report.citation_precision, 1.0, places=2)
        self.assertEqual(report.hallucination_score, 0.0)
        self.assertEqual(report.unresolved_citation_count, 0)

    def test_unsupported_hallucinated_claim(self):
        answer = "Quantum teleportation was secretly patented by Nikola Tesla in Colorado Springs [doc_1]."
        report = self.verifier.verify(answer, self.passages)
        self.assertFalse(report.is_grounded)
        self.assertEqual(report.total_claim_count, 1)
        claim = report.claims[0]
        self.assertEqual(claim.status, GroundingStatus.UNSUPPORTED)
        self.assertIn("tesla", [t.lower() for t in claim.unsupported_tokens])
        self.assertGreater(report.hallucination_score, 0.5)

    def test_unresolved_citation_marker(self):
        answer = "Optical amplifiers operate near 1550 nanometers [doc_nonexistent_99]."
        report = self.verifier.verify(answer, self.passages)
        self.assertFalse(report.is_grounded)
        self.assertEqual(report.unresolved_citation_count, 1)
        self.assertEqual(report.claims[0].status, GroundingStatus.UNRESOLVED_CITATION)
        self.assertEqual(report.claims[0].unresolved_citations, ["doc_nonexistent_99"])

    def test_missing_citation(self):
        answer = "Quantum teleportation transfers quantum information between separated qubits."
        report = self.verifier.verify(answer, self.passages)
        self.assertEqual(report.missing_citation_count, 1)
        self.assertEqual(report.claims[0].status, GroundingStatus.MISSING_CITATION)

    def test_numeric_loose_citation_indexing(self):
        # 1-based index mapping: [1] maps to self.passages[0]
        answer = "Experiments achieved transmission across 144 kilometers in free space [1]."
        report = self.verifier.verify(answer, self.passages)
        self.assertEqual(report.claims[0].resolved_passage_ids, ["doc_1"])
        self.assertEqual(report.claims[0].status, GroundingStatus.SUPPORTED)

    def test_matching_spans_detection(self):
        answer = "Real-world experiments achieved transmission across 144 kilometers in free space [doc_1]."
        report = self.verifier.verify(answer, self.passages)
        claim = report.claims[0]
        self.assertEqual(claim.status, GroundingStatus.SUPPORTED)
        self.assertGreater(len(claim.matching_spans), 0)
        span = claim.matching_spans[0]
        self.assertEqual(span.passage_id, "doc_1")
        self.assertIn("144 kilometers", span.text)

    def test_empty_and_trivial_inputs(self):
        report = self.verifier.verify("", self.passages)
        self.assertEqual(report.total_claim_count, 0)
        self.assertEqual(report.grounding_ratio, 0.0)

        report2 = self.verifier.verify("   ", [])
        self.assertEqual(report2.total_claim_count, 0)


class TestDPOSynthesizer(unittest.TestCase):
    def setUp(self):
        self.synth = DPOSynthesizer(seed=1337)
        self.corpus_entry = TOPIC_FACT_CORPUS[0]  # Helium-3 Lunar Abundance

    def test_synthesize_answerable_pair(self):
        pair = self.synth.synthesize_answerable_pair(self.corpus_entry)
        self.assertEqual(pair.regime, QueryRegime.ANSWERABLE)
        self.assertIn("doc_he3", pair.prompt)
        self.assertIn("doc_he3", pair.chosen)
        # Verify chosen is grounded
        rep = self.synth.verifier.verify(pair.chosen, pair.passages)
        self.assertEqual(rep.claims[0].status, GroundingStatus.SUPPORTED)
        # Check that rejected is either refusal or hallucination
        self.assertNotEqual(pair.chosen, pair.rejected)

    def test_synthesize_unanswerable_pair(self):
        pair = self.synth.synthesize_unanswerable_pair(self.corpus_entry)
        self.assertEqual(pair.regime, QueryRegime.UNANSWERABLE)
        # Chosen should contain an explicit refusal/abstention
        self.assertTrue(
            any(k in pair.chosen.lower() for k in ["insufficient", "not contain", "no information", "not state"])
        )
        # Rejected should contain a fabricated answer
        self.assertIn("[doc_he3]", pair.rejected)
        # Audit with verifier: rejected fails grounding on absent topic
        rep = self.synth.verifier.verify(pair.rejected, pair.passages)
        self.assertFalse(rep.is_grounded)

    def test_synthesize_partially_answerable_pair(self):
        pair = self.synth.synthesize_partially_answerable_pair(self.corpus_entry)
        self.assertEqual(pair.regime, QueryRegime.PARTIALLY_ANSWERABLE)
        # Chosen acknowledges grounded fact AND abstains on missing fact
        self.assertIn("[doc_he3]", pair.chosen)
        self.assertIn("does not contain information", pair.chosen.lower())
        # Rejected overconfidently states fake facts
        self.assertIn("8,500 units", pair.rejected)

    def test_synthesize_false_presupposition_pair(self):
        pair = self.synth.synthesize_false_presupposition_pair(self.corpus_entry)
        self.assertEqual(pair.regime, QueryRegime.FALSE_PRESUPPOSITION)
        # Chosen refutes the false premise
        self.assertTrue(
            any(k in pair.chosen.lower() for k in ["does not state", "not supported", "solar winds", "refute"])
        )
        # Rejected gullibly accepts the premise
        self.assertIn("Indeed", pair.rejected)

    def test_dataset_generation_and_balance(self):
        dataset = self.synth.synthesize_dataset(count=40)
        self.assertEqual(len(dataset), 40)
        regimes = {p.regime for p in dataset}
        # All 4 regimes should be represented
        self.assertEqual(len(regimes), 4)

        audit = self.synth.audit_dataset(dataset)
        self.assertEqual(audit["total_pairs"], 40)
        self.assertGreaterEqual(audit["answerable_chosen_grounding_pass_rate"], 0.90)
        self.assertGreaterEqual(audit["unanswerable_chosen_abstention_rate"], 0.90)

    def test_deterministic_seeded_synthesis(self):
        synth_a = DPOSynthesizer(seed=9999)
        synth_b = DPOSynthesizer(seed=9999)

        data_a = synth_a.synthesize_dataset(count=20)
        data_b = synth_b.synthesize_dataset(count=20)

        for pa, pb in zip(data_a, data_b):
            self.assertEqual(pa.id, pb.id)
            self.assertEqual(pa.prompt, pb.prompt)
            self.assertEqual(pa.chosen, pb.chosen)
            self.assertEqual(pa.rejected, pb.rejected)
            self.assertEqual(pa.regime, pb.regime)

    def test_huggingface_format_export(self):
        pair = self.synth.synthesize_answerable_pair(self.corpus_entry)
        hf = pair.to_hf_format()
        self.assertEqual(set(hf.keys()), {"prompt", "chosen", "rejected"})
        self.assertIsInstance(hf["prompt"], str)
        self.assertIsInstance(hf["chosen"], str)
        self.assertIsInstance(hf["rejected"], str)

    def test_high_throughput_generation(self):
        start = time.perf_counter()
        dataset = self.synth.synthesize_dataset(count=500)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(dataset), 500)
        throughput = len(dataset) / max(elapsed, 0.001)
        print(f"\n[BENCHMARK] Synthesized {len(dataset)} DPO pairs in {elapsed:.4f}s ({throughput:,.0f} pairs/sec)")
        self.assertGreater(throughput, 500.0)


class TestCLIIntegration(unittest.TestCase):
    def setUp(self):
        self.root_dir = Path(__file__).resolve().parent

    def test_citation_verifier_cli(self):
        sample_input = {
            "answer": "Helium-3 is deposited in lunar regolith by solar winds [doc_he3].",
            "passages": [
                {
                    "id": "doc_he3",
                    "text": "Helium-3 is deposited in lunar regolith by solar winds over billions of years.",
                }
            ],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as inf:
            json.dump(sample_input, inf)
            in_path = inf.name

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as outf:
            out_path = outf.name

        try:
            cmd = [sys.executable, "-B", str(self.root_dir / "citation_verifier.py"), "--input", in_path, "--output", out_path]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
            self.assertIn("Report written", proc.stdout)

            with open(out_path, "r", encoding="utf-8") as f:
                rep = json.load(f)
            self.assertTrue(rep["is_grounded"])
            self.assertEqual(rep["supported_claim_count"], 1)
        finally:
            if os.path.exists(in_path):
                os.unlink(in_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_dpo_synthesizer_cli(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as outf:
            out_path = outf.name

        try:
            # 1. Synthesize command
            cmd_syn = [
                sys.executable, "-B", str(self.root_dir / "dpo_synthesizer.py"),
                "synthesize", "--count", "25", "--seed", "777", "--output", out_path
            ]
            proc_syn = subprocess.run(cmd_syn, capture_output=True, text=True, check=True)
            self.assertIn("Generated 25 DPO pairs", proc_syn.stdout)

            # Check line count
            with open(out_path, "r", encoding="utf-8") as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 25)

            # 2. Audit command
            cmd_aud = [
                sys.executable, "-B", str(self.root_dir / "dpo_synthesizer.py"),
                "audit", "--input", out_path
            ]
            proc_aud = subprocess.run(cmd_aud, capture_output=True, text=True, check=True)
            self.assertIn("Audit results:", proc_aud.stdout)
            self.assertIn("total_pairs", proc_aud.stdout)
        finally:
            if os.path.exists(out_path):
                os.unlink(out_path)


if __name__ == "__main__":
    unittest.main()
