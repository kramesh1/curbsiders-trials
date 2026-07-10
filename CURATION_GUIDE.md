# Curbsiders Evidence Curation Guide

This repository is currently strong enough to browse, search, and begin human review. Treat it as an extracted evidence map, not yet as a fully adjudicated clinical reference.

## Teaching Use

Use the site for two teaching workflows:

1. **Prepare a chalk talk**
   Start in `Teaching pearls` with `Reviewed evidence only` when you want quick practice-changing takeaways. Those links come from the owner-gated `evidence_links` layer, not the noisy term-overlap citation layer.

2. **Trace a Curbsiders citation**
   Open a record, follow the episode backlink or source hyperlink, and compare the record summary against the original show-note context before using it in teaching. Evidence cards now also show reviewed teaching pearls linked back to that evidence when the pearl→evidence link has been signed off.

Use the `Evidence browser` by condition, drug, trial name, or pearl language when you need the broader citation map. Filter by `RCT`, `systematic review`, `meta-analysis`, or `guideline` to separate primary evidence from synthesis and current-practice sources.

## Review Priorities

Review in this order:

1. **Newest episodes**
   Start with episodes `530` through `521` because these are highest-value for current teaching.

2. **High-impact topics**
   Review hypertension, diabetes/cardiorenal protection, anticoagulation, antibiotic duration, ASCVD prevention, obesity/nutrition, and screening.

3. **Records labeled `other`**
   `other` is the noisiest study-type bucket. Many records may be better classified as guideline, review, observational, or background article.

4. **Canonical merges**
   Check records with high `mention_count` or many `episode_titles`. These are useful when correct, but false merges have larger teaching impact.

5. **Missing identifiers**
   Prioritize adding PMID, DOI, or NCT IDs when a record has no outbound link or only a publisher URL.

## Reviewer Checklist

For each reviewed record, confirm:

- The cited paper/trial is actually present in the Curbsiders show notes.
- If the record came only from `data/show_note_evidence.json`, decide whether it should stay as a source-only citation, be merged into an existing canonical record, or be enriched with a better PMID/DOI/title.
- `citation_label` is recognizable to a clinician.
- `paper_title` is not invented if the show notes do not supply it.
- `study_type` matches the cited source.
- `brief_summary` is supported by the show notes and does not overstate clinical impact.
- `specialty_tags` are useful for discovery.
- Episode backlinks point to the right source episode.

For pearl→evidence links specifically, treat `supporting_citations` as heuristic same-episode overlap only. A citation should be used for teaching only after it survives the `evidence_links` adjudication path.

## Show-note evidence layer

`data/show_note_evidence.json` is the deterministic inventory of likely clinical-evidence hyperlinks actually present in show notes. It uses PMID, DOI, PMCID, NCT ID, or normalized URL as the stable key, then `build_site.py` merges those records into the evidence browser. Use it to audit evidence the model extractor missed and to reconcile duplicate records created when a publisher URL and PubMed URL refer to the same paper.

```bash
python scripts/extract_show_note_evidence.py   # write data/show_note_evidence.json and print match/gap counts
python scripts/build_site.py                   # merge show-note evidence into docs/data/trials.json
python scripts/validate_repository.py          # checks dangling pearl links and show-note evidence matches
```

## Adjudicating pearl evidence links

The model-assisted pearl→evidence links (`data/pearl_evidence_links.json`) are reviewed
at **two levels**. First, individual links are curated so a single off-topic study can be
dropped without discarding a pearl's good citations. Second, once you're satisfied with a
pearl's surviving links, you explicitly sign off the **whole record** — `apply` only ever
merges records marked `"approved"` at the record level, so nothing reaches the published
site without that explicit sign-off:

```bash
python scripts/link_pearls_evidence.py report            # coverage + per-link adjudication counts
# 1. Reject links that don't hold up (preview with --dry-run first):
python scripts/link_pearls_evidence.py adjudicate --episode 500 --trial "SPRINT" --reject --note "off-topic"
# 2. Once you've checked the pearl's surviving links against the show notes, sign off the record:
python scripts/link_pearls_evidence.py adjudicate --episode 500 --record --approve --note "checked vs show notes"
python scripts/link_pearls_evidence.py apply             # refresh data/pearls_linked.json
```

Good first review queue: the low-confidence and `background`-support links
(`--confidence low`, `--support background`). The default `apply` step now publishes only
direct, non-low-confidence reviewed links; use `--include-background` or
`--include-low-confidence` only for review/debug artifacts. Use `--reset` to undo a decision (per-link
or, with `--record`, the whole record back to `pending`), or `--from-file feedback.json`
to apply a batch of captured decisions (add `"scope": "record"` to an entry to sign off a
record instead of a link). Rejected links are always dropped by `apply`, and only records
you've explicitly marked `"approved"` are applied at all by default — `apply --include-pending` is
available but bypasses that gate, so avoid it for anything headed to the public site.

As of the latest rebuild, `398` model-drafted records have record-level approval and are
eligible for publication after the stricter `apply` filters. The remaining `447` records
are still `pending`; treat them as unreviewed until they have actually been through the
steps above.

## Local QA Commands

```bash
python scripts/build_site.py
python scripts/extract_show_note_evidence.py   # show-note evidence hyperlink inventory + gap counts
python scripts/validate_repository.py
python scripts/pearl_coverage.py            # episodes with no pearls yet (+ transcript availability)
python -m unittest discover -s tests
python -m py_compile scripts/*.py tests/*.py
```

Run `build_site.py` after any change to `data/trials.json`; otherwise the browser may show stale canonical data.

## Known Limitations

- The extracted summaries are model-generated from show notes and need human review before being treated as authoritative.
- Episode dates are currently missing from all canonical episode backlinks; the live show-note markup did not expose a parseable date, so the scraper left `date` empty. `needs_refresh()` no longer treats a missing date as a reason to re-fetch.
- `study_type = "other"` remains overused and should be tightened during review.
- Curbsiders show notes vary in citation detail, so not every record can be fully enriched without external lookup.
