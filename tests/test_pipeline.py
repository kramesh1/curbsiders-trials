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
from scripts.ingest import plan_ingest
from scripts.pearl_utils import (
    build_canonical_pearls,
    link_pearls_to_trials,
    parse_pearls_from_show_notes,
    trial_canonical_key,
)
from scripts.scrape_episodes import parse_episode
from scripts.trial_utils import (
    build_canonical_trial_records,
    clean_text,
    dedupe_trial_mentions,
    extract_markdown_links,
    normalize_nct_id,
    normalize_sample_size,
    normalize_trial_record,
    recover_missing_urls_from_show_notes,
    split_show_notes_into_chunks,
)
from scripts.segment_utils import (
    assign_segment_to_pearls,
    assign_segment_to_trials,
    locate_citation_in_show_notes,
    parse_body_sections,
    parse_show_segments,
)
from scripts.category_utils import derive_episode_category
from scripts.trial_detail_utils import parse_detail_from_context


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


class PearlParsingTests(unittest.TestCase):
    SHOW_NOTES = """
Show Segments
Intro
Nutrition Pearls
The Mediterranean and DASH diets consistently outperform restrictive fad diets for long-term cardiovascular health.
Coconut oil is very high in saturated fat and is not a healthy option.
The Ketogenic Diet
The ketogenic diet is high in fat and very low in carbohydrates, and was originally developed for epilepsy.
"""

    def test_parses_pearls_and_topic_and_stops_at_next_heading(self):
        pearls = parse_pearls_from_show_notes(self.SHOW_NOTES)
        self.assertEqual(len(pearls), 2)
        self.assertTrue(all(pearl["topic"] == "Nutrition" for pearl in pearls))
        self.assertIn("Mediterranean and DASH", pearls[0]["pearl"])
        # The body section that follows the pearls list must not be captured.
        self.assertFalseIfPresent(pearls, "originally developed for epilepsy")

    def assertFalseIfPresent(self, pearls, needle):
        self.assertFalse(any(needle in pearl["pearl"] for pearl in pearls))

    def test_toc_pearls_entry_yields_nothing(self):
        notes = "Show Segments\nPearls\nIntro\nCase\nOutro\n"
        self.assertEqual(parse_pearls_from_show_notes(notes), [])

    def test_dedupes_repeated_pearl_within_episode(self):
        notes = (
            "Clinical Pearls\n"
            "Start antihypertensives when blood pressure is persistently elevated on repeat measurement.\n"
            "Hypertension\n"
            "Some body text that is long enough to look like a statement but is a section.\n"
            "Bottom Line Pearls\n"
            "Start antihypertensives when blood pressure is persistently elevated on repeat measurement.\n"
        )
        pearls = parse_pearls_from_show_notes(notes)
        texts = [pearl["pearl"] for pearl in pearls]
        self.assertEqual(texts.count(
            "Start antihypertensives when blood pressure is persistently elevated on repeat measurement."
        ), 1)


class PearlLinkingTests(unittest.TestCase):
    def _dash_trial(self):
        return {
            "citation_label": "Appel et al 1997",
            "paper_title": "A clinical trial of the effects of dietary patterns on blood pressure",
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/9099655",
            "context_topic": "DASH diet and blood pressure reduction",
            "year": 1997,
            "study_type": "RCT",
            "specialty_tags": ["cardiology"],
        }

    def test_links_pearl_to_trial_by_term_overlap(self):
        pearls = [{"topic": "Nutrition", "pearl": "The Mediterranean and DASH diets reduce blood pressure."}]
        link_pearls_to_trials(pearls, [self._dash_trial()])
        citations = pearls[0]["supporting_citations"]
        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0]["citation_label"], "Appel et al 1997")
        self.assertEqual(citations[0]["canonical_key"], "pubmed|https://pubmed.ncbi.nlm.nih.gov/9099655")
        self.assertEqual(pearls[0]["specialty_tags"], ["cardiology"])

    def test_links_by_inline_url_even_with_low_overlap(self):
        pearls = [{
            "topic": None,
            "pearl": "Lean mass hyper-responders warrant monitoring "
                     "[Budoff et al. 2024](https://pubmed.ncbi.nlm.nih.gov/39372369).",
        }]
        trial = {
            "citation_label": "Budoff et al. 2024",
            "paper_title": None,
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/39372369",
            "context_topic": "coronary calcium progression",
            "study_type": "observational",
            "specialty_tags": ["cardiology"],
        }
        link_pearls_to_trials(pearls, [trial])
        self.assertEqual(len(pearls[0]["supporting_citations"]), 1)

    def test_no_link_when_nothing_overlaps(self):
        pearls = [{"topic": None, "pearl": "Insoluble fiber promotes bowel regularity and prevents constipation."}]
        link_pearls_to_trials(pearls, [self._dash_trial()])
        self.assertEqual(pearls[0]["supporting_citations"], [])


class CanonicalPearlTests(unittest.TestCase):
    def test_merges_same_pearl_across_episodes(self):
        base = {
            "topic": "Hypertension",
            "pearl": "Confirm hypertension with out-of-office readings before starting treatment.",
            "specialty_tags": ["cardiology"],
            "supporting_citations": [{
                "citation_label": "SPRINT",
                "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/26551272",
                "canonical_key": "pubmed|https://pubmed.ncbi.nlm.nih.gov/26551272",
                "study_type": "RCT",
                "score": 0.4,
            }],
        }
        pearls = [
            {**base, "episode_number": 100, "episode_title": "#100 HTN", "episode_url": "https://example.org/100", "episode_date": None},
            {**base, "episode_number": 150, "episode_title": "#150 HTN redux", "episode_url": "https://example.org/150", "episode_date": None},
        ]
        canonical = build_canonical_pearls(pearls)
        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0]["episode_count"], 2)
        self.assertEqual(canonical[0]["latest_episode_number"], 150)
        self.assertEqual(canonical[0]["citation_count"], 1)
        self.assertEqual(canonical[0]["id"], 0)

    def test_trial_canonical_key_matches_build_site_format(self):
        trial = {"pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/26551272/"}
        self.assertEqual(
            trial_canonical_key(trial),
            "pubmed|https://pubmed.ncbi.nlm.nih.gov/26551272",
        )


SEGMENT_NOTES = "\n".join([
    "Show Segments",
    "Intro",
    "Case",
    "Ketogenic Diets",
    "Mediterranean & DASH Diets",
    "Nutrition Pearls",
    "Coconut oil is very high in saturated fat and is not a healthy option to recommend.",
    "The Ketogenic Diet",
    "The ketogenic diet is high in fat and very low in carbohydrates (NCT01234567), developed for epilepsy.",
    "[Budoff et al. 2024](https://pubmed.ncbi.nlm.nih.gov/39372369/)",
    "Mediterranean and DASH Diets",
    "The DASH diet lowered blood pressure in a trial of n = 459 adults over eight weeks of follow up.",
    "[Appel et al 1997](https://pubmed.ncbi.nlm.nih.gov/9099655/)",
])


class SegmentParsingTests(unittest.TestCase):
    def test_parse_show_segments_drops_scaffolding_and_stops_at_pearls(self):
        segments = parse_show_segments(SEGMENT_NOTES)
        titles = [segment["title"] for segment in segments]
        self.assertEqual(titles, ["Ketogenic Diets", "Mediterranean & DASH Diets"])

    def test_parse_body_sections_maps_fuzzy_heading_variants(self):
        segments = parse_show_segments(SEGMENT_NOTES)
        sections = parse_body_sections(SEGMENT_NOTES, segments)
        mapped = {section["segment_title"] for section in sections}
        self.assertEqual(mapped, {"Ketogenic Diets", "Mediterranean & DASH Diets"})

    def test_assign_segment_to_trials_places_citation_under_section(self):
        segments = parse_show_segments(SEGMENT_NOTES)
        sections = parse_body_sections(SEGMENT_NOTES, segments)
        trials = [
            {"citation_label": "Budoff et al. 2024", "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/39372369"},
            {"citation_label": "Appel et al 1997", "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/9099655"},
        ]
        assign_segment_to_trials(trials, SEGMENT_NOTES, segments, sections)
        self.assertEqual(trials[0]["segment"], "Ketogenic Diets")
        self.assertEqual(trials[1]["segment"], "Mediterranean & DASH Diets")

    def test_locate_citation_falls_back_to_label_without_url(self):
        segments = parse_show_segments(SEGMENT_NOTES)
        index = locate_citation_in_show_notes(
            {"citation_label": "Appel et al 1997", "pubmed_url": None}, SEGMENT_NOTES
        )
        self.assertIsNotNone(index)

    def test_no_segments_when_block_absent(self):
        self.assertEqual(parse_show_segments("Just some prose with no table of contents."), [])

    def test_drops_boilerplate_and_scaffolding_toc_lines(self):
        notes = "\n".join([
            "Show Segments",
            "Intro and pun",
            "Lipid Management Guidelines Overview",
            "This show is based on articles and news featured in The DIGEST #",
            "Emerging Treatments in Oncology",
            "",
            "",
        ])
        titles = [segment["title"] for segment in parse_show_segments(notes)]
        self.assertEqual(titles, ["Lipid Management Guidelines Overview", "Emerging Treatments in Oncology"])


class PearlSegmentTests(unittest.TestCase):
    def _segments(self):
        return parse_show_segments(SEGMENT_NOTES)

    def test_pearl_inherits_segment_from_linked_trial(self):
        trials = [{"canonical_key": "k1", "segment": "Ketogenic Diets", "segment_index": 0}]
        pearls = [{"pearl": "Monitor lipids for keto patients.", "supporting_citations": [{"canonical_key": "k1", "score": 0.5}]}]
        assign_segment_to_pearls(pearls, trials, self._segments())
        self.assertEqual(pearls[0]["segment"], "Ketogenic Diets")
        self.assertEqual(pearls[0]["segment_index"], 0)

    def test_pearl_falls_back_to_title_overlap(self):
        pearls = [{"pearl": "The ketogenic diet can cause elevated LDL cholesterol.", "supporting_citations": []}]
        assign_segment_to_pearls(pearls, [], self._segments())
        self.assertEqual(pearls[0]["segment"], "Ketogenic Diets")

    def test_pearl_segment_null_when_nothing_clears_threshold(self):
        pearls = [
            {"pearl": "Stay well hydrated during exercise.", "supporting_citations": []},
            {"pearl": "Sleep hygiene improves daytime alertness.", "supporting_citations": []},
            {"pearl": "Wear sunscreen to reduce photoaging.", "supporting_citations": []},
        ]
        assign_segment_to_pearls(pearls, [], self._segments())
        self.assertTrue(all(pearl["segment"] is None for pearl in pearls))


class CategoryTests(unittest.TestCase):
    def test_argmax_over_specialty_tags(self):
        trials = [{"specialty_tags": ["cardiology"]}, {"specialty_tags": ["cardiology"]}, {"specialty_tags": ["nephrology"]}]
        result = derive_episode_category({"title": "#1 A Case"}, trials)
        self.assertEqual(result["category"], "cardiology")

    def test_multi_topic_yields_secondary_categories(self):
        trials = (
            [{"specialty_tags": ["cardiology"]}] * 3
            + [{"specialty_tags": ["endocrinology"]}] * 3
        )
        result = derive_episode_category({"title": "#1 Mixed Bag"}, trials)
        self.assertIn(result["category"], {"cardiology", "endocrinology"})
        self.assertTrue(result["secondary_categories"])

    def test_sparse_episode_yields_none(self):
        result = derive_episode_category({"title": "#999 Mystery Guest"}, [])
        self.assertIsNone(result["category"])

    def test_title_keyword_breaks_ties(self):
        result = derive_episode_category({"title": "#530 Nutrition"}, [])
        self.assertEqual(result["category"], "endocrinology")


class TrialDetailTests(unittest.TestCase):
    def test_parses_nct_sample_size_journal(self):
        detail = parse_detail_from_context(
            "The trial enrolled n = 1,234 adults and was published in NEJM.",
            citation_label="Smith et al. 2020",
        )
        self.assertIsNone(detail["nct_id"])
        self.assertEqual(detail["sample_size"], 1234)
        self.assertEqual(detail["journal"], "New England Journal of Medicine")

    def test_nct_from_label_or_context(self):
        detail = parse_detail_from_context("Registered as NCT01234567 in the registry.")
        self.assertEqual(detail["nct_id"], "NCT01234567")

    def test_journal_not_triggered_by_blood_pressure_prose(self):
        detail = parse_detail_from_context("The DASH diet reduced blood pressure and chest discomfort.")
        self.assertIsNone(detail["journal"])


class EnrichmentNormalizationTests(unittest.TestCase):
    def test_normalize_nct_id(self):
        self.assertEqual(normalize_nct_id("nct01234567"), "NCT01234567")
        self.assertEqual(normalize_nct_id("see NCT09876543 here"), "NCT09876543")
        self.assertIsNone(normalize_nct_id("NCT123"))
        self.assertIsNone(normalize_nct_id(None))

    def test_normalize_sample_size(self):
        self.assertEqual(normalize_sample_size(459), 459)
        self.assertEqual(normalize_sample_size("1,234"), 1234)
        self.assertIsNone(normalize_sample_size(0))
        self.assertIsNone(normalize_sample_size(-5))
        self.assertIsNone(normalize_sample_size("none"))

    def test_canonical_trial_carries_enrichment_fields(self):
        mentions = [
            {
                "citation_label": "Appel et al 1997",
                "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/9099655",
                "segment": "DASH Diet", "nct_id": "NCT01234567", "sample_size": 459,
                "journal": "NEJM", "episode_category": "cardiology",
                "episode_url": "https://example.org/1", "episode_number": 1,
            },
            {
                "citation_label": "Appel et al 1997",
                "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/9099655",
                "segment": "Blood Pressure", "episode_category": "preventive medicine",
                "episode_url": "https://example.org/2", "episode_number": 2,
            },
        ]
        canonical = build_canonical_trial_records(mentions)
        self.assertEqual(len(canonical), 1)
        record = canonical[0]
        self.assertEqual(record["nct_id"], "NCT01234567")
        self.assertEqual(record["sample_size"], 459)
        self.assertEqual(record["segments"], ["Blood Pressure", "DASH Diet"])
        self.assertEqual(record["episode_categories"], ["cardiology", "preventive medicine"])

    def test_canonical_pearl_unions_new_fields(self):
        base = {"pearl": "Confirm hypertension before treating.", "specialty_tags": ["cardiology"]}
        pearls = [
            {**base, "segment": "Diagnosis", "clinical_topic": "Diagnosis", "episode_category": "cardiology",
             "secondary_categories": ["preventive medicine"], "episode_number": 100,
             "episode_title": "#100", "episode_url": "https://example.org/100", "episode_date": None},
            {**base, "segment": "Thresholds", "clinical_topic": "Thresholds", "episode_category": "preventive medicine",
             "secondary_categories": [], "episode_number": 150,
             "episode_title": "#150", "episode_url": "https://example.org/150", "episode_date": None},
        ]
        canonical = build_canonical_pearls(pearls)
        self.assertEqual(len(canonical), 1)
        record = canonical[0]
        self.assertEqual(record["segments"], ["Diagnosis", "Thresholds"])
        self.assertEqual(record["clinical_topics"], ["Diagnosis", "Thresholds"])
        self.assertEqual(record["episode_categories"], ["cardiology", "preventive medicine"])


class IngestPlanTests(unittest.TestCase):
    def test_plan_ingest_returns_only_unprocessed_episodes(self):
        episodes = [
            {"url": "https://example.org/1", "title": "#1"},
            {"url": "https://example.org/2", "title": "#2"},
            {"url": "https://example.org/3", "title": "#3"},
        ]
        state = {
            "https://example.org/1": {"status": "completed"},
            "https://example.org/2": {"status": "failed"},
        }
        pending = plan_ingest(episodes, state)
        self.assertEqual([episode["url"] for episode in pending], ["https://example.org/3"])


if __name__ == "__main__":
    unittest.main()
