"""
Deterministic episode-level category derivation.

Episodes carry no category in the scraped data. Rather than invent a parallel
taxonomy, we reuse the 16 controlled specialty tags already assigned to each
trial as the category vocabulary, so category filters compose with specialty
filters on the site. A category is the dominant specialty across an episode's
trials, nudged by a few unambiguous title keywords. Multi-topic episodes keep a
`secondary_categories` list instead of being force-collapsed to one label.

Pure and testable: no model calls.
"""

from collections import Counter

try:
    from scripts.trial_utils import (
        VALID_SPECIALTY_TAGS,
        normalize_key_text,
        normalize_specialty_tags,
    )
except ImportError:
    from trial_utils import (
        VALID_SPECIALTY_TAGS,
        normalize_key_text,
        normalize_specialty_tags,
    )

# The category vocabulary IS the specialty vocabulary (reused, not duplicated).
CLINICAL_CATEGORIES = set(VALID_SPECIALTY_TAGS)

# Title keyword -> category hints for episodes whose trials are sparse or whose
# theme is obvious from the name. Keys are matched as whole words against the
# normalized title. Deliberately conservative; only unambiguous mappings.
CATEGORY_KEYWORDS = {
    "afib": "cardiology",
    "heart": "cardiology",
    "hypertension": "cardiology",
    "cholesterol": "cardiology",
    "lipid": "cardiology",
    "statin": "cardiology",
    "diabetes": "endocrinology",
    "thyroid": "endocrinology",
    "obesity": "endocrinology",
    "nutrition": "endocrinology",
    "kidney": "nephrology",
    "renal": "nephrology",
    "dialysis": "nephrology",
    "copd": "pulmonology",
    "asthma": "pulmonology",
    "sepsis": "infectious disease",
    "hiv": "infectious disease",
    "antibiotic": "infectious disease",
    "vaccine": "infectious disease",
    "stroke": "neurology",
    "headache": "neurology",
    "migraine": "neurology",
    "seizure": "neurology",
    "anemia": "hematology",
    "anticoagulation": "hematology",
    "cancer": "oncology",
    "screening": "preventive medicine",
    "gout": "rheumatology",
    "arthritis": "rheumatology",
    "lupus": "rheumatology",
    "rash": "dermatology",
    "eczema": "dermatology",
    "psoriasis": "dermatology",
    "depression": "psychiatry",
    "anxiety": "psychiatry",
    "addiction": "psychiatry",
    "dementia": "geriatrics",
    "frailty": "geriatrics",
    "liver": "gastroenterology",
    "hepatitis": "gastroenterology",
    "ibd": "gastroenterology",
    "reflux": "gastroenterology",
}

# Weight of a single title-keyword vote relative to one specialty-tag mention.
_TITLE_VOTE_WEIGHT = 2
# A secondary category must reach this fraction of the winner's score.
_SECONDARY_RATIO = 0.5


def _title_votes(title: str) -> Counter:
    words = set(normalize_key_text(title).split())
    votes: Counter = Counter()
    for keyword, category in CATEGORY_KEYWORDS.items():
        if keyword in words:
            votes[category] += _TITLE_VOTE_WEIGHT
    return votes


def derive_episode_category(episode: dict, episode_trials: list[dict]) -> dict:
    """Return {category, secondary_categories, category_scores} for an episode.

    category is the highest-scoring specialty (or None when there is no signal);
    secondary_categories are other specialties within _SECONDARY_RATIO of the
    winner, so multi-topic episodes surface all their strong themes.
    """
    scores: Counter = Counter()
    for trial in episode_trials:
        for tag in normalize_specialty_tags(trial.get("specialty_tags")):
            scores[tag] += 1
    scores.update(_title_votes(episode.get("title", "")))

    if not scores:
        return {"category": None, "secondary_categories": [], "category_scores": {}}

    ranked = scores.most_common()
    top_category, top_score = ranked[0]
    secondary = sorted(
        category
        for category, score in ranked[1:]
        if score >= top_score * _SECONDARY_RATIO
    )
    return {
        "category": top_category,
        "secondary_categories": secondary,
        "category_scores": dict(scores),
    }
