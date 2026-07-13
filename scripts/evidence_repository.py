"""One canonical evidence universe shared by build, screening, and linking."""

from copy import deepcopy

try:
    from scripts.show_note_evidence import (
        annotate_show_note_matches,
        build_show_note_evidence_records,
        merge_show_note_evidence,
    )
    from scripts.trial_utils import build_canonical_trial_records
except ImportError:
    from show_note_evidence import annotate_show_note_matches, build_show_note_evidence_records, merge_show_note_evidence
    from trial_utils import build_canonical_trial_records


def build_evidence_repository(trial_mentions: list[dict], episodes: list[dict]) -> tuple[list[dict], list[dict], dict]:
    canonical = build_canonical_trial_records(trial_mentions)
    show_note_records = build_show_note_evidence_records(episodes)
    annotated = annotate_show_note_matches(show_note_records, canonical)
    canonical, stats = merge_show_note_evidence(canonical, annotated)
    return canonical, annotated, stats


def evidence_by_episode(canonical: list[dict]) -> dict[str, list[dict]]:
    """Flatten canonical records into episode-specific prompt rows."""
    grouped: dict[str, list[dict]] = {}
    for evidence in canonical:
        for episode in evidence.get("episodes", []) or []:
            url = episode.get("episode_url")
            if not url:
                continue
            row = deepcopy(evidence)
            row["episode_url"] = url
            row["episode_number"] = episode.get("episode_number")
            row["episode_title"] = episode.get("episode_title")
            grouped.setdefault(url, []).append(row)
    return grouped
