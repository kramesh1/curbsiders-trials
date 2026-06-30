import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.extract_trials import (
    build_progress_line,
    merge_episode_result,
    parse_json_response,
    pending_episodes,
)
from scripts.extract_trials_batch import (
    build_episode_results_from_batch,
    collect_excluded_episode_urls,
    load_batch_episode_urls,
    select_episodes,
    write_batch_input,
)
from scripts.scrape_episodes import parse_episode
from scripts.trial_utils import (
    build_canonical_trial_records,
    clean_text,
    dedupe_trial_mentions,
    extract_markdown_links,
    normalize_trial_record,
    recover_missing_urls_from_show_notes,
    split_show_notes_into_chunks,
)


class ParseJsonResponseTests(unittest.TestCase):
    def test_accepts_code_fenced_array(self):
        payload = """```json
        [{"citation_label":"ASPREE"}]
        ```"""
        result = parse_json_response(payload)
        self.assertEqual(result[0]["citation_label"], "ASPREE")

    def test_accepts_wrapped_object(self):
        payload = '{"trials":[{"citation_label":"ASCEND"}]}'
        result = parse_json_response(payload)
        self.assertEqual(result[0]["citation_label"], "ASCEND")


class ChunkingTests(unittest.TestCase):
    def test_splits_long_show_notes_without_losing_text(self):
        notes = "\n".join(f"line {i} " + ("x" * 80) for i in range(120))
        chunks = split_show_notes_into_chunks(notes, max_chars=800, overlap_lines=2)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 800 for chunk in chunks))
        self.assertIn("line 0", chunks[0])
        self.assertIn("line 119", chunks[-1])

    def test_extract_markdown_links_handles_urls_with_parentheses(self):
        text = (
            "[Effects of aspirin on risks of vascular events and cancer according to "
            "bodyweight and dose: analysis of individual patient data from randomised trials]"
            "(https://www.thelancet.com/journals/lancet/article/PIIS0140-6736(18)31133-4/fulltext)"
        )
        links = extract_markdown_links(text)
        self.assertEqual(len(links), 1)
        self.assertEqual(
            links[0]["url"],
            "https://www.thelancet.com/journals/lancet/article/PIIS0140-6736(18)31133-4/fulltext",
        )

    def test_recovers_missing_url_and_dedupes_generic_topic_card(self):
        show_notes = """
        Rothwell, Peter M et al.
        [Effects of aspirin on risks of vascular events and cancer according to bodyweight and dose: analysis of individual patient data from randomised trials](https://www.thelancet.com/journals/lancet/article/PIIS0140-6736(18)31133-4/fulltext)
        Bottom line? Low dose aspirin is not effective in patients weighing more than 70 kg.
        """
        trials = [
            {
                "citation_label": "Rothwell, Peter M et al.",
                "paper_title": "Effects of aspirin on risks of vascular events and cancer according to bodyweight and dose: analysis of individual patient data from randomised trials",
                "pubmed_url": None,
                "year": 2018,
                "brief_summary": "Summary.",
                "context_topic": "Aspirin dosing for prevention of vascular events according to body weight",
                "study_type": "RCT",
                "specialty_tags": ["cardiology"],
                "episode_number": 111,
                "episode_title": "Hotcakes",
                "episode_url": "https://example.org/111",
            },
            {
                "citation_label": "low dose aspirin",
                "paper_title": None,
                "pubmed_url": None,
                "year": None,
                "brief_summary": "Topic summary.",
                "context_topic": "Low-dose aspirin for primary prevention and bodyweight effects",
                "study_type": "other",
                "specialty_tags": ["cardiology", "preventive medicine"],
                "episode_number": 111,
                "episode_title": "Hotcakes",
                "episode_url": "https://example.org/111",
            },
        ]
        enriched = recover_missing_urls_from_show_notes(trials, show_notes)
        deduped = dedupe_trial_mentions(enriched)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(
            deduped[0]["pubmed_url"],
            "https://www.thelancet.com/journals/lancet/article/PIIS0140-6736(18)31133-4/fulltext",
        )
        self.assertEqual(deduped[0]["study_type"], "RCT")


class NullSentinelTests(unittest.TestCase):
    def test_clean_text_coalesces_null_sentinels(self):
        for value in ["null", "NULL", "None", "n/a", "N/A", "nil", "undefined", "  null  "]:
            self.assertIsNone(clean_text(value), value)
        self.assertEqual(clean_text("ASPREE"), "ASPREE")

    def test_normalize_drops_sentinel_pubmed_url_and_title(self):
        record = normalize_trial_record({
            "citation_label": "ASPREE",
            "paper_title": "null",
            "pubmed_url": "null",
            "context_topic": "n/a",
            "study_type": "RCT",
            "specialty_tags": ["geriatrics"],
            "episode_url": "https://example.org/100",
        })
        self.assertIsNotNone(record)
        self.assertIsNone(record["pubmed_url"])
        self.assertIsNone(record["paper_title"])
        self.assertIsNone(record["context_topic"])

    def test_canonical_record_has_no_sentinel_link(self):
        canonical = build_canonical_trial_records([
            {
                "citation_label": "Qian ET, Casey JD, et al. Cefepime vs piperacillin",
                "paper_title": None,
                "pubmed_url": "null",
                "year": 2023,
                "brief_summary": "Summary.",
                "context_topic": "Empiric antibiotics",
                "study_type": "RCT",
                "specialty_tags": ["infectious disease"],
                "episode_number": 400,
                "episode_title": "Antibiotics episode",
                "episode_url": "https://example.org/400",
            },
        ])
        self.assertEqual(len(canonical), 1)
        self.assertIsNone(canonical[0]["pubmed_url"])


class CanonicalizationTests(unittest.TestCase):
    def test_dedupes_mentions_within_episode(self):
        trials = [
            {
                "citation_label": "ASPREE",
                "paper_title": "Effect of Aspirin on Disability-free Survival",
                "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/30221597/",
                "year": 2018,
                "brief_summary": "Short summary.",
                "context_topic": "Aspirin in primary prevention",
                "study_type": "rct",
                "specialty_tags": ["geriatrics"],
                "episode_number": 100,
                "episode_title": "Aspirin episode",
                "episode_url": "https://example.org/100",
            },
            {
                "citation_label": "ASPREE trial",
                "paper_title": "Effect of Aspirin on Disability-free Survival",
                "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/30221597",
                "year": "2018",
                "brief_summary": "A longer summary for the same trial.",
                "context_topic": "Primary prevention aspirin",
                "study_type": "RCT",
                "specialty_tags": ["geriatrics", "preventive medicine"],
                "episode_number": 100,
                "episode_title": "Aspirin episode",
                "episode_url": "https://example.org/100",
            },
        ]
        deduped = dedupe_trial_mentions(trials)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["study_type"], "RCT")
        self.assertIn("preventive medicine", deduped[0]["specialty_tags"])

    def test_groups_same_trial_across_episodes(self):
        trials = [
            {
                "citation_label": "ASPREE",
                "paper_title": "Effect of Aspirin on Disability-free Survival",
                "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/30221597/",
                "year": 2018,
                "brief_summary": "Summary.",
                "context_topic": "Aspirin in older adults",
                "study_type": "RCT",
                "specialty_tags": ["geriatrics"],
                "episode_number": 100,
                "episode_title": "Aspirin episode",
                "episode_url": "https://example.org/100",
            },
            {
                "citation_label": "ASPREE",
                "paper_title": "Effect of Aspirin on Disability-free Survival",
                "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/30221597",
                "year": 2018,
                "brief_summary": "Summary.",
                "context_topic": "Primary prevention",
                "study_type": "RCT",
                "specialty_tags": ["preventive medicine"],
                "episode_number": 150,
                "episode_title": "Another aspirin episode",
                "episode_url": "https://example.org/150",
            },
        ]
        canonical = build_canonical_trial_records(trials)
        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0]["episode_count"], 2)
        self.assertEqual(canonical[0]["latest_episode_number"], 150)
        self.assertCountEqual(
            canonical[0]["specialty_tags"],
            ["geriatrics", "preventive medicine"],
        )


class ScrapeEpisodeTests(unittest.TestCase):
    def test_parses_episode_date(self):
        html = """
        <html>
          <head>
            <meta property="article:published_time" content="2025-06-01T05:00:00+00:00" />
          </head>
          <body>
            <h1>#530 Example Episode</h1>
            <div class="entry-content"><p>Notes here</p></div>
          </body>
        </html>
        """
        title, notes, date = parse_episode(html)
        self.assertEqual(title, "#530 Example Episode")
        self.assertEqual(date, "2025-06-01")
        self.assertIn("Notes here", notes)


class ParallelPipelineTests(unittest.TestCase):
    def test_build_progress_line_contains_key_counts(self):
        line = build_progress_line(5, 20, failed=1, trial_mentions=42)
        self.assertIn("5/20", line)
        self.assertIn("failed 1", line)
        self.assertIn("mentions 42", line)

    def test_pending_episodes_skips_completed_and_failed_by_default(self):
        episodes = [
            {"url": "https://example.org/1", "title": "#1"},
            {"url": "https://example.org/2", "title": "#2"},
            {"url": "https://example.org/3", "title": "#3"},
        ]
        state = {
            "https://example.org/1": {"status": "completed"},
            "https://example.org/2": {"status": "failed"},
        }
        todo = pending_episodes(episodes, state, retry_failures=False, limit=None)
        self.assertEqual([episode["url"] for episode in todo], ["https://example.org/3"])

    def test_pending_episodes_can_retry_failures(self):
        episodes = [
            {"url": "https://example.org/1", "title": "#1"},
            {"url": "https://example.org/2", "title": "#2"},
        ]
        state = {"https://example.org/1": {"status": "failed"}}
        todo = pending_episodes(episodes, state, retry_failures=True, limit=None)
        self.assertEqual(
            [episode["url"] for episode in todo],
            ["https://example.org/1", "https://example.org/2"],
        )

    def test_merge_episode_result_updates_state_and_trials(self):
        by_episode = {"https://example.org/1": [{"episode_url": "https://example.org/1"}]}
        state = {}
        delta = merge_episode_result(
            by_episode,
            state,
            {
                "episode_url": "https://example.org/1",
                "episode_number": 1,
                "episode_title": "Episode 1",
                "status": "completed",
                "processed_at": "2026-06-28T00:00:00+00:00",
                "started_at": "2026-06-28T00:00:00+00:00",
                "chunk_count": 2,
                "raw_mentions": 3,
                "deduped_mentions": 2,
                "error": None,
                "trials": [
                    {"episode_url": "https://example.org/1"},
                    {"episode_url": "https://example.org/1"},
                ],
            },
        )
        self.assertEqual(delta, 1)
        self.assertEqual(len(by_episode["https://example.org/1"]), 2)
        self.assertEqual(state["https://example.org/1"]["status"], "completed")


class BatchPipelineTests(unittest.TestCase):
    def test_load_batch_episode_urls_from_manifest(self):
        with TemporaryDirectory() as tmpdir:
            batch_dir = Path(tmpdir)
            manifest = {
                "requests": {
                    "req-00000": {"episode_url": "https://example.org/530"},
                    "req-00001": {"episode_url": "https://example.org/529"},
                    "req-00002": {"episode_url": "https://example.org/530"},
                }
            }
            (batch_dir / "manifest.json").write_text(json.dumps(manifest))
            urls = load_batch_episode_urls(batch_dir)
            self.assertEqual(urls, {"https://example.org/530", "https://example.org/529"})

    def test_select_episodes_can_include_completed(self):
        episodes = [
            {"url": "https://example.org/10", "title": "#10"},
            {"url": "https://example.org/9", "title": "#9"},
        ]
        state = {"https://example.org/10": {"status": "completed"}}
        selected = select_episodes(
            episodes,
            state,
            limit=1,
            include_completed=True,
            retry_failures=False,
            excluded_episode_urls=set(),
        )
        self.assertEqual(selected[0]["url"], "https://example.org/10")

    def test_select_episodes_respects_excluded_episode_urls(self):
        episodes = [
            {"url": "https://example.org/10", "title": "#10"},
            {"url": "https://example.org/9", "title": "#9"},
        ]
        selected = select_episodes(
            episodes,
            {},
            limit=None,
            include_completed=True,
            retry_failures=False,
            excluded_episode_urls={"https://example.org/10"},
        )
        self.assertEqual([episode["url"] for episode in selected], ["https://example.org/9"])

    def test_write_batch_input_creates_manifest_and_requests(self):
        episode = {
            "url": "https://example.org/530",
            "title": "#530 Example",
            "date": "2025-06-01",
            "show_notes": "\n".join(["line " + ("x" * 100)] * 80),
            "episode_number": 530,
        }
        with TemporaryDirectory() as tmpdir:
            batch_dir = Path(tmpdir)
            requests_path, manifest = write_batch_input(batch_dir, [episode], "gpt-5.5")
            self.assertTrue(requests_path.exists())
            self.assertGreater(manifest["request_count"], 0)
            self.assertEqual(manifest["episode_count"], 1)

    def test_build_episode_results_from_batch_groups_by_episode(self):
        with TemporaryDirectory() as tmpdir:
            batch_dir = Path(tmpdir)
            manifest = {
                "requests": {
                    "req-00000": {
                        "episode_url": "https://example.org/530",
                        "episode_number": 530,
                        "episode_title": "#530 Example",
                        "episode_date": "2025-06-01",
                        "chunk_index": 1,
                        "total_chunks": 1,
                    }
                }
            }
            (batch_dir / "manifest.json").write_text(json.dumps(manifest))
            output_payload = json.dumps(
                {
                    "custom_id": "req-00000",
                    "response": {
                        "body": {
                            "choices": [
                                {
                                    "message": {
                                        "content": json.dumps(
                                            {
                                                "trials": [
                                                    {
                                                        "citation_label": "ASPREE",
                                                        "paper_title": "Effect of Aspirin on Disability-free Survival",
                                                        "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/30221597/",
                                                        "year": 2018,
                                                        "brief_summary": "Summary.",
                                                        "context_topic": "Aspirin",
                                                        "study_type": "RCT",
                                                        "specialty_tags": ["geriatrics"],
                                                    }
                                                ]
                                            }
                                        )
                                    }
                                }
                            ]
                        }
                    },
                }
            )
            episodes = build_episode_results_from_batch(batch_dir, output_payload, None)
            self.assertIn("https://example.org/530", episodes)
            self.assertEqual(episodes["https://example.org/530"]["received_chunks"], 1)
            self.assertEqual(len(episodes["https://example.org/530"]["raw_trials"]), 1)


if __name__ == "__main__":
    unittest.main()
