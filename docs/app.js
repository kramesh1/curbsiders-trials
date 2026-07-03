'use strict';

const PAGE_SIZE = 24;
const TOP_TOPIC_COUNT = 12;
const DEBOUNCE_MS = 180;

const ERA_DEFS = [
  { key: '2020s', label: '2020s', min: 2020, max: 2029 },
  { key: '2010s', label: '2010s', min: 2010, max: 2019 },
  { key: '2000s', label: '2000s', min: 2000, max: 2009 },
  { key: '1990s', label: '1990s', min: 1990, max: 1999 },
  { key: 'pre-1990', label: 'Pre-1990', min: null, max: 1989 },
  { key: 'unknown', label: 'Year unknown', min: null, max: null },
];

const PUBMED_OPTIONS = [
  { key: 'all', label: 'All records' },
  { key: 'with', label: 'With PubMed link' },
  { key: 'without', label: 'Without PubMed link' },
];

const SEARCH_STOPWORDS = new Set([
  'a',
  'an',
  'and',
  'are',
  'as',
  'at',
  'by',
  'for',
  'from',
  'how',
  'in',
  'is',
  'of',
  'on',
  'or',
  'the',
  'to',
  'vs',
  'with',
]);

const SEARCH_SYNONYMS = {
  afib: ['atrial fibrillation'],
  bp: ['blood pressure', 'hypertension'],
  ckd: ['chronic kidney disease', 'kidney'],
  doac: ['direct oral anticoagulant', 'anticoagulation'],
  'glp 1': ['glp-1', 'semaglutide'],
  glp1: ['glp-1', 'semaglutide'],
  hf: ['heart failure'],
  htn: ['hypertension', 'blood pressure'],
  mi: ['myocardial infarction'],
  'sglt 2': ['sglt2', 'sodium glucose', 'gliflozin', 'empagliflozin', 'dapagliflozin', 'canagliflozin'],
  sglt2: ['sglt-2', 'sodium glucose', 'gliflozin', 'empagliflozin', 'dapagliflozin', 'canagliflozin'],
  t2dm: ['type 2 diabetes', 'diabetes'],
};

const state = {
  searchQuery: '',
  topicQuery: '',
  selectedSpecialties: new Set(),
  selectedStudyTypes: new Set(),
  selectedEra: 'all',
  pubmedMode: 'all',
  sort: 'episode-desc',
  sortManuallyChosen: false,
  viewMode: 'pearls',
  page: 1,
  pearlQuery: '',
  pearlSpecialties: new Set(),
  pearlCategories: new Set(),
  pearlLinkedOnly: false,
};

let allTrials = [];
let allPearls = [];
let filteredPearls = [];
let pearlSpecialtyCounts = [];
let pearlCategoryCounts = [];
let filteredTrials = [];
let fuse = null;
let searchDocuments = new Map();
let relevanceById = new Map();
let specialtyCounts = [];
let studyTypeCounts = [];
let eraCounts = [];
let topicSuggestions = [];

async function init() {
  try {
    const resp = await fetch('data/trials.json');
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    allTrials = await resp.json();
  } catch (err) {
    const message =
      `<div class="empty-state"><strong>Could not load data</strong><p>${esc(err.message)}</p></div>`;
    // The browser view (and #cards-grid) starts hidden, so surface the error in
    // the always-visible hero and pearls areas too.
    document.getElementById('hero-stats').textContent = 'Could not load dataset.';
    document.getElementById('pearls-view').innerHTML = message;
    document.getElementById('cards-grid').innerHTML = message;
    return;
  }

  await loadPearls();

  computeFacets();
  computePearlFacets();
  buildSearchIndex();
  hydrateStateFromUrl();
  renderHeroStats();
  renderFilterControls();
  renderPearlsView();
  wireControls();
  applyFilters();
  applyPearlFilters();
}

async function loadPearls() {
  try {
    const resp = await fetch('data/pearls.json');
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    allPearls = await resp.json();
  } catch (err) {
    // Pearls are an optional layer; the rest of the site still works without them.
    allPearls = [];
  }
}

function computePearlFacets() {
  pearlSpecialtyCounts = countAndSort(
    allPearls.flatMap(pearl => pearl.specialty_tags || []),
    label => label
  );
  pearlCategoryCounts = countAndSort(
    allPearls.flatMap(pearl => pearl.episode_categories || []),
    label => label
  );
}

function computeFacets() {
  specialtyCounts = countAndSort(
    allTrials.flatMap(trial => trial.specialty_tags || []),
    label => label
  );

  studyTypeCounts = countAndSort(
    allTrials.map(trial => trial.study_type || 'other'),
    label => label
  );

  eraCounts = ERA_DEFS.map(era => ({
    key: era.key,
    label: era.label,
    count: allTrials.filter(trial => yearEraKey(trial.year) === era.key).length,
  }));

  topicSuggestions = countAndSort(
    allTrials.flatMap(trial => trial.context_topics || []),
    label => label
  ).slice(0, TOP_TOPIC_COUNT);
}

function countAndSort(values, normalize) {
  const counts = new Map();
  for (const value of values) {
    const cleaned = cleanText(normalize(value));
    if (!cleaned) {
      continue;
    }
    counts.set(cleaned, (counts.get(cleaned) || 0) + 1);
  }

  return [...counts.entries()]
    .map(([key, count]) => ({ key, label: key, count }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

function buildSearchIndex() {
  searchDocuments = new Map(allTrials.map(trial => [trial.id, buildSearchDocument(trial)]));
  if (typeof Fuse === 'undefined') {
    fuse = null;
    return;
  }
  fuse = new Fuse(allTrials, {
    keys: [
      { name: 'citation_label', weight: 3.2 },
      { name: 'paper_title', weight: 2.2 },
      { name: 'brief_summary', weight: 1.8 },
      { name: 'context_topic', weight: 1.8 },
      { name: 'context_topics', weight: 1.5 },
      { name: 'specialty_tags', weight: 1.1 },
      { name: 'episode_titles', weight: 0.8 },
    ],
    threshold: 0.34,
    includeScore: true,
    ignoreLocation: true,
  });
}

function buildSearchDocument(trial) {
  const fields = {
    title: [
      trial.citation_label,
      trial.paper_title,
    ],
    topic: [
      trial.context_topic,
      ...(trial.context_topics || []),
    ],
    summary: [
      trial.brief_summary,
    ],
    metadata: [
      trial.study_type,
      trial.year,
      ...(trial.specialty_tags || []),
    ],
    episode: [
      ...(trial.episode_titles || []),
      ...(trial.episodes || []).map(episode => episode.episode_title),
    ],
  };

  const normalizedFields = Object.fromEntries(
    Object.entries(fields).map(([key, values]) => [key, normalizeSearchText(values.filter(Boolean).join(' '))])
  );
  return {
    ...normalizedFields,
    all: Object.values(normalizedFields).join(' '),
  };
}

function hydrateStateFromUrl() {
  const params = new URLSearchParams(window.location.search);
  state.searchQuery = params.get('q') || '';
  state.topicQuery = params.get('topic') || '';
  state.selectedEra = params.get('era') || 'all';
  state.pubmedMode = params.get('pubmed') || 'all';
  state.sort = params.get('sort') || (activeSearchQuery() ? 'relevance' : 'episode-desc');
  state.sortManuallyChosen = params.has('sort');
  const viewParam = params.get('view');
  state.viewMode = ['browser', 'pearls'].includes(viewParam) ? viewParam : 'pearls';
  state.page = clampPage(Number.parseInt(params.get('page') || '1', 10));

  const specialties = params.get('specialties');
  if (specialties) {
    state.selectedSpecialties = new Set(
      specialties.split(',').map(cleanText).filter(Boolean)
    );
  }

  const types = params.get('types');
  if (types) {
    state.selectedStudyTypes = new Set(
      types.split(',').map(cleanText).filter(Boolean)
    );
  }
}

function syncUrl() {
  const params = new URLSearchParams();
  const searchQuery = activeSearchQuery();
  const topicQuery = activeTopicQuery();
  if (searchQuery) {
    params.set('q', searchQuery);
  }
  if (topicQuery) {
    params.set('topic', topicQuery);
  }
  if (state.selectedSpecialties.size) {
    params.set('specialties', [...state.selectedSpecialties].sort().join(','));
  }
  if (state.selectedStudyTypes.size) {
    params.set('types', [...state.selectedStudyTypes].sort().join(','));
  }
  if (state.selectedEra !== 'all') {
    params.set('era', state.selectedEra);
  }
  if (state.pubmedMode !== 'all') {
    params.set('pubmed', state.pubmedMode);
  }
  if (state.viewMode !== 'pearls') {
    params.set('view', state.viewMode);
  }
  if (state.sort !== defaultSort()) {
    params.set('sort', state.sort);
  }
  if (state.page > 1) {
    params.set('page', String(state.page));
  }

  const query = params.toString();
  const nextUrl = `${window.location.pathname}${query ? `?${query}` : ''}`;
  window.history.replaceState(null, '', nextUrl);
}

function renderHeroStats() {
  const mentions = allTrials.reduce((sum, trial) => sum + (trial.mention_count || 0), 0);
  const episodes = new Set(
    allTrials.flatMap(trial => (trial.episodes || []).map(episode => episode.episode_url).filter(Boolean))
  ).size;
  const withPubmed = allTrials.filter(trial => Boolean(trial.pubmed_url)).length;
  const recentEra = eraCounts.find(era => era.key === '2020s')?.count || 0;

  document.getElementById('hero-stats').innerHTML = `
    <div class="stat-card">
      <span class="stat-value">${allTrials.length.toLocaleString()}</span>
      <span class="stat-label">canonical records</span>
    </div>
    <div class="stat-card">
      <span class="stat-value">${mentions.toLocaleString()}</span>
      <span class="stat-label">trial mentions</span>
    </div>
    <div class="stat-card">
      <span class="stat-value">${episodes.toLocaleString()}</span>
      <span class="stat-label">episodes covered</span>
    </div>
    <div class="stat-card">
      <span class="stat-value">${withPubmed.toLocaleString()}</span>
      <span class="stat-label">with PubMed links</span>
    </div>
    <div class="stat-card stat-card-accent">
      <span class="stat-value">${recentEra.toLocaleString()}</span>
      <span class="stat-label">published in the 2020s</span>
    </div>
  `;
}

function renderFilterControls() {
  const specialtyContainer = document.getElementById('specialty-filters');
  specialtyContainer.innerHTML = specialtyCounts.map(item => facetButtonHTML({
    group: 'specialty',
    key: item.key,
    label: cap(item.label),
    count: item.count,
    active: state.selectedSpecialties.has(item.key),
    compact: false,
  })).join('');

  const studyContainer = document.getElementById('study-type-filters');
  studyContainer.innerHTML = studyTypeCounts.map(item => facetButtonHTML({
    group: 'type',
    key: item.key,
    label: studyTypeLabel(item.label),
    count: item.count,
    active: state.selectedStudyTypes.has(item.key),
    compact: true,
  })).join('');

  const eraContainer = document.getElementById('era-filters');
  eraContainer.innerHTML = [
    facetButtonHTML({
      group: 'era',
      key: 'all',
      label: 'All years',
      count: allTrials.length,
      active: state.selectedEra === 'all',
      compact: true,
    }),
    ...eraCounts.map(item => facetButtonHTML({
      group: 'era',
      key: item.key,
      label: item.label,
      count: item.count,
      active: state.selectedEra === item.key,
      compact: true,
    })),
  ].join('');

  const pubmedContainer = document.getElementById('pubmed-filters');
  pubmedContainer.innerHTML = PUBMED_OPTIONS.map(option => {
    const count = option.key === 'with'
      ? allTrials.filter(trial => Boolean(trial.pubmed_url)).length
      : option.key === 'without'
        ? allTrials.filter(trial => !trial.pubmed_url).length
        : allTrials.length;
    return facetButtonHTML({
      group: 'pubmed',
      key: option.key,
      label: option.label,
      count,
      active: state.pubmedMode === option.key,
      compact: true,
    });
  }).join('');

  const topicPresetContainer = document.getElementById('topic-presets');
  topicPresetContainer.innerHTML = topicSuggestions.map(item => `
    <button class="topic-chip" type="button" data-topic-chip="${escAttr(item.key)}">
      <span>${esc(item.label)}</span>
      <span class="chip-count">${item.count}</span>
    </button>
  `).join('');

  const topicDatalist = document.getElementById('topic-suggestions');
  topicDatalist.innerHTML = specialtyCounts.slice(0, 10).map(item => item.label)
    .concat(topicSuggestions.map(item => item.label))
    .filter(onlyUnique)
    .map(label => `<option value="${escAttr(label)}"></option>`)
    .join('');

  const searchInput = document.getElementById('search-input');
  const topicInput = document.getElementById('topic-input');
  if (document.activeElement !== searchInput && searchInput.value !== state.searchQuery) {
    searchInput.value = state.searchQuery;
  }
  if (document.activeElement !== topicInput && topicInput.value !== state.topicQuery) {
    topicInput.value = state.topicQuery;
  }
  document.getElementById('sort-select').value = state.sort;
}

function facetButtonHTML({ group, key, label, count, active, compact }) {
  const className = compact ? 'facet-chip' : 'facet-button';
  const activeClass = active ? ' active' : '';
  return `
    <button
      class="${className}${activeClass}"
      type="button"
      data-filter-group="${group}"
      data-filter-key="${escAttr(key)}"
      aria-pressed="${active ? 'true' : 'false'}"
    >
      <span>${esc(label)}</span>
      <span class="facet-count">${count.toLocaleString()}</span>
    </button>
  `;
}

function wireControls() {
  const searchInput = document.getElementById('search-input');
  const topicInput = document.getElementById('topic-input');

  let searchTimer = null;
  let topicTimer = null;

  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.searchQuery = searchInput.value;
      state.page = 1;
      if (!state.sortManuallyChosen) {
        state.sort = activeSearchQuery() ? 'relevance' : 'episode-desc';
        document.getElementById('sort-select').value = state.sort;
      }
      applyFilters();
    }, DEBOUNCE_MS);
  });

  topicInput.addEventListener('input', () => {
    clearTimeout(topicTimer);
    topicTimer = window.setTimeout(() => {
      state.topicQuery = topicInput.value;
      state.page = 1;
      applyFilters();
    }, DEBOUNCE_MS);
  });

  document.getElementById('sort-select').addEventListener('change', event => {
    state.sort = event.target.value;
    state.sortManuallyChosen = true;
    state.page = 1;
    applyFilters();
  });

  document.getElementById('clear-filters-btn').addEventListener('click', resetFilters);
  document.getElementById('copy-link-btn').addEventListener('click', copyCurrentViewLink);

  document.body.addEventListener('click', event => {
    const viewButton = event.target.closest('[data-view-mode]');
    if (viewButton) {
      const requested = viewButton.dataset.viewMode;
      state.viewMode = ['browser', 'pearls'].includes(requested) ? requested : 'pearls';
      renderViewMode();
      syncUrl();
      return;
    }

    const pearlCategoryButton = event.target.closest('[data-pearl-category]');
    if (pearlCategoryButton) {
      toggleSetValue(state.pearlCategories, pearlCategoryButton.dataset.pearlCategory);
      applyPearlFilters();
      return;
    }

    const pearlSpecialtyButton = event.target.closest('[data-pearl-specialty]');
    if (pearlSpecialtyButton) {
      toggleSetValue(state.pearlSpecialties, pearlSpecialtyButton.dataset.pearlSpecialty);
      applyPearlFilters();
      return;
    }

    const pearlLinkedToggle = event.target.closest('[data-pearl-linked-toggle]');
    if (pearlLinkedToggle) {
      state.pearlLinkedOnly = !state.pearlLinkedOnly;
      applyPearlFilters();
      return;
    }

    const pearlEvidenceButton = event.target.closest('[data-pearl-evidence]');
    if (pearlEvidenceButton) {
      showEvidenceForCitation(pearlEvidenceButton.dataset.pearlEvidence);
      return;
    }

    const filterButton = event.target.closest('[data-filter-group]');
    if (filterButton) {
      handleFilterButton(filterButton.dataset.filterGroup, filterButton.dataset.filterKey);
      return;
    }

    const topicChip = event.target.closest('[data-topic-chip]');
    if (topicChip) {
      state.topicQuery = topicChip.dataset.topicChip || '';
      document.getElementById('topic-input').value = state.topicQuery;
      state.page = 1;
      applyFilters();
      return;
    }

    const removeFilterButton = event.target.closest('[data-remove-filter]');
    if (removeFilterButton) {
      removeActiveFilter(removeFilterButton.dataset.removeFilter, removeFilterButton.dataset.value);
    }
  });
}

function handleFilterButton(group, key) {
  if (group === 'specialty') {
    toggleSetValue(state.selectedSpecialties, key);
  } else if (group === 'type') {
    toggleSetValue(state.selectedStudyTypes, key);
  } else if (group === 'era') {
    state.selectedEra = key;
  } else if (group === 'pubmed') {
    state.pubmedMode = key;
  } else {
    return;
  }

  state.page = 1;
  applyFilters();
}

function removeActiveFilter(kind, value) {
  if (kind === 'search') {
    state.searchQuery = '';
    document.getElementById('search-input').value = '';
  } else if (kind === 'topic') {
    state.topicQuery = '';
    document.getElementById('topic-input').value = '';
  } else if (kind === 'specialty') {
    state.selectedSpecialties.delete(value);
  } else if (kind === 'type') {
    state.selectedStudyTypes.delete(value);
  } else if (kind === 'era') {
    state.selectedEra = 'all';
  } else if (kind === 'pubmed') {
    state.pubmedMode = 'all';
  }

  state.page = 1;
  applyFilters();
}

function resetFilters() {
  state.searchQuery = '';
  state.topicQuery = '';
  state.selectedSpecialties.clear();
  state.selectedStudyTypes.clear();
  state.selectedEra = 'all';
  state.pubmedMode = 'all';
  state.sort = 'episode-desc';
  state.sortManuallyChosen = false;
  state.page = 1;

  document.getElementById('search-input').value = '';
  document.getElementById('topic-input').value = '';
  document.getElementById('sort-select').value = state.sort;

  applyFilters();
}

async function copyCurrentViewLink() {
  const button = document.getElementById('copy-link-btn');
  const url = window.location.href;
  try {
    await navigator.clipboard.writeText(url);
    button.textContent = 'Link copied';
  } catch (err) {
    button.textContent = 'Copy failed';
  }
  window.setTimeout(() => {
    button.textContent = 'Copy view link';
  }, 1400);
}

function applyFilters() {
  relevanceById = new Map();
  const searchQuery = activeSearchQuery();
  const topicQuery = activeTopicQuery();

  let results;
  if (searchQuery) {
    const searchResults = hybridSearch(searchQuery);
    results = searchResults.map(result => result.item);
    searchResults.forEach((result, index) => {
      relevanceById.set(result.item.id, index);
    });
  } else {
    results = [...allTrials];
  }

  if (topicQuery) {
    const topicNeedle = normalizeText(topicQuery);
    results = results.filter(trial => topicMatches(trial, topicNeedle));
  }

  if (state.selectedSpecialties.size) {
    results = results.filter(trial =>
      (trial.specialty_tags || []).some(tag => state.selectedSpecialties.has(tag))
    );
  }

  if (state.selectedStudyTypes.size) {
    results = results.filter(trial => state.selectedStudyTypes.has(trial.study_type || 'other'));
  }

  if (state.selectedEra !== 'all') {
    results = results.filter(trial => yearEraKey(trial.year) === state.selectedEra);
  }

  if (state.pubmedMode === 'with') {
    results = results.filter(trial => Boolean(trial.pubmed_url));
  } else if (state.pubmedMode === 'without') {
    results = results.filter(trial => !trial.pubmed_url);
  }

  const sortKey = resolveSortKey();
  filteredTrials = sortResults(results, sortKey);

  const totalPages = Math.max(1, Math.ceil(filteredTrials.length / PAGE_SIZE));
  state.page = Math.min(state.page, totalPages);

  renderFilterControls();
  renderActiveFilters();
  renderResultsSummary(sortKey);
  renderPage();
  renderViewMode();
  syncUrl();
}

function resolveSortKey() {
  if (state.sort === 'relevance' && !activeSearchQuery()) {
    return 'episode-desc';
  }
  if (!state.sort) {
    return defaultSort();
  }
  return state.sort;
}

function defaultSort() {
  return activeSearchQuery() ? 'relevance' : 'episode-desc';
}

function sortResults(trials, sortKey) {
  const sorted = [...trials];
  sorted.sort((a, b) => {
    switch (sortKey) {
      case 'relevance':
        return (relevanceById.get(a.id) ?? 1) - (relevanceById.get(b.id) ?? 1);
      case 'episode-desc':
        return (b.latest_episode_number || 0) - (a.latest_episode_number || 0)
          || compareTitle(a, b);
      case 'episode-asc':
        return (a.latest_episode_number || 0) - (b.latest_episode_number || 0)
          || compareTitle(a, b);
      case 'mentions-desc':
        return (b.mention_count || 0) - (a.mention_count || 0)
          || compareTitle(a, b);
      case 'episodes-desc':
        return (b.episode_count || 0) - (a.episode_count || 0)
          || compareTitle(a, b);
      case 'year-desc':
        return (b.year || 0) - (a.year || 0)
          || compareTitle(a, b);
      case 'year-asc':
        return (a.year || 0) - (b.year || 0)
          || compareTitle(a, b);
      case 'title-asc':
        return compareTitle(a, b);
      default:
        return 0;
    }
  });
  return sorted;
}

function hybridSearch(query) {
  const parsed = parseSearchQuery(query);
  if (!parsed.positive.length && !parsed.phrases.length) {
    return [];
  }

  const scored = new Map();
  for (const trial of allTrials) {
    const doc = searchDocuments.get(trial.id);
    if (!doc || hasExcludedMatch(doc, parsed.excluded)) {
      continue;
    }

    const lexical = lexicalSearchScore(trial, doc, parsed);
    if (lexical.matched) {
      scored.set(trial.id, {
        item: trial,
        score: lexical.score,
        coverage: lexical.coverage,
      });
    }
  }

  if (fuse) {
    const fuseResults = fuse.search(query, { limit: 300 });
    for (const result of fuseResults) {
      const trial = result.item;
      const doc = searchDocuments.get(trial.id);
      if (!doc || hasExcludedMatch(doc, parsed.excluded)) {
        continue;
      }
      const fuzzyScore = 18 + (1 - Math.min(result.score ?? 1, 1)) * 42;
      const existing = scored.get(trial.id);
      if (existing) {
        existing.score += fuzzyScore * 0.35;
      } else if ((result.score ?? 1) <= 0.42) {
        scored.set(trial.id, {
          item: trial,
          score: fuzzyScore,
          coverage: 0,
        });
      }
    }
  }

  return [...scored.values()]
    .sort((a, b) =>
      b.score - a.score
      || b.coverage - a.coverage
      || (b.item.episode_count || 0) - (a.item.episode_count || 0)
      || (b.item.year || 0) - (a.item.year || 0)
      || compareTitle(a.item, b.item)
    );
}

function parseSearchQuery(query) {
  const positive = [];
  const phrases = [];
  const excluded = [];
  const tokenPattern = /(-?)"([^"]+)"|(-?)(\S+)/g;
  let match;
  while ((match = tokenPattern.exec(query)) !== null) {
    const negative = Boolean(match[1] || match[3]);
    const raw = match[2] || match[4] || '';
    const normalized = normalizeSearchText(raw);
    if (!normalized) {
      continue;
    }

    const target = negative ? excluded : match[2] ? phrases : positive;
    if (!match[2] && SEARCH_STOPWORDS.has(normalized)) {
      continue;
    }
    target.push(normalized);
  }

  return {
    positive: uniqueSearchTerms(positive),
    phrases: uniqueSearchTerms(phrases),
    excluded: uniqueSearchTerms(excluded),
  };
}

function lexicalSearchScore(trial, doc, parsed) {
  let score = 0;
  let matchedTerms = 0;
  const requiredTerms = parsed.positive.length + parsed.phrases.length;

  for (const phrase of parsed.phrases) {
    const phraseScore = fieldMatchScore(doc, phrase, true);
    if (!phraseScore) {
      return { matched: false, score: 0, coverage: 0 };
    }
    matchedTerms += 1;
    score += phraseScore + 38;
  }

  for (const term of parsed.positive) {
    const variants = expandSearchTerm(term);
    const termScore = Math.max(...variants.map(variant => fieldMatchScore(doc, variant, false)));
    if (termScore > 0) {
      matchedTerms += 1;
      score += termScore;
    }
  }

  const coverage = requiredTerms ? matchedTerms / requiredTerms : 0;
  const minimumCoverage = requiredTerms <= 2 ? 1 : 0.67;
  if (coverage < minimumCoverage) {
    return { matched: false, score: 0, coverage };
  }

  score += coverage * 22;
  score += Math.min(trial.episode_count || 0, 6) * 2.2;
  score += Math.min(trial.mention_count || 0, 8) * 0.8;
  if (trial.pubmed_url) {
    score += 1.5;
  }
  return { matched: true, score, coverage };
}

function fieldMatchScore(doc, term, phraseRequired) {
  const weights = [
    ['title', phraseRequired ? 72 : 34],
    ['topic', phraseRequired ? 52 : 25],
    ['summary', phraseRequired ? 34 : 15],
    ['metadata', phraseRequired ? 24 : 12],
    ['episode', phraseRequired ? 18 : 8],
  ];

  let score = 0;
  for (const [field, weight] of weights) {
    const value = doc[field] || '';
    if (!containsSearchTerm(value, term)) {
      continue;
    }
    score += weight;
    if (startsNearFieldBeginning(value, term)) {
      score += Math.round(weight * 0.35);
    }
  }
  return score;
}

function hasExcludedMatch(doc, excludedTerms) {
  return excludedTerms.some(term =>
    expandSearchTerm(term).some(variant => containsSearchTerm(doc.all, variant))
  );
}

function expandSearchTerm(term) {
  return uniqueSearchTerms([
    term,
    ...(SEARCH_SYNONYMS[term] || []).map(normalizeSearchText),
  ]);
}

function uniqueSearchTerms(terms) {
  return [...new Set(terms.map(normalizeSearchText).filter(Boolean))];
}

function containsSearchTerm(value, term) {
  if (!value || !term) {
    return false;
  }
  if (term.length <= 3 && !term.includes(' ')) {
    return new RegExp(`\\b${escapeRegExp(term)}\\b`).test(value);
  }
  return value.includes(term);
}

function startsNearFieldBeginning(value, term) {
  const index = value.indexOf(term);
  return index >= 0 && index <= 24;
}

function renderViewMode() {
  const pearlsView = document.getElementById('pearls-view');
  const browserView = document.getElementById('browser-view');
  const modeButtons = document.querySelectorAll('[data-view-mode]');

  pearlsView.hidden = state.viewMode !== 'pearls';
  browserView.hidden = state.viewMode !== 'browser';
  modeButtons.forEach(button => {
    const active = button.dataset.viewMode === state.viewMode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function renderPearlsView() {
  const container = document.getElementById('pearls-view');

  if (!allPearls.length) {
    container.innerHTML = `
      <div class="empty-state">
        <strong>No pearls yet</strong>
        <p>Run <code>python scripts/extract_pearls.py</code> and <code>python scripts/build_site.py</code> to generate the teaching pearls layer.</p>
      </div>
    `;
    renderViewMode();
    return;
  }

  const linkedCount = allPearls.filter(pearl => (pearl.supporting_citations || []).length).length;
  const episodeCount = new Set(
    allPearls.flatMap(pearl => (pearl.episodes || []).map(episode => episode.episode_url).filter(Boolean))
  ).size;

  container.innerHTML = `
    <section class="pearls-summary">
      <div>
        <p class="section-kicker">Teaching layer</p>
        <h3>Quick, quotable teaching pearls — each linked to the evidence behind it.</h3>
        <p>
          Verbatim clinician takeaways pulled from Curbsiders show notes, paired with the trials,
          guidelines, and reviews cited in the same episode.
        </p>
      </div>
      <div class="pearls-metrics">
        <span><strong>${allPearls.length.toLocaleString()}</strong> pearls</span>
        <span><strong>${linkedCount.toLocaleString()}</strong> with linked evidence</span>
        <span><strong>${episodeCount.toLocaleString()}</strong> source episodes</span>
      </div>
    </section>

    <div class="pearls-controls">
      <div class="search-wrap pearl-search-wrap">
        <svg class="search-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <circle cx="8.5" cy="8.5" r="5.5"></circle>
          <path d="M15 15l-3-3"></path>
        </svg>
        <input id="pearl-search" type="search" placeholder="Search pearls: statins, deprescribing, CKD…" autocomplete="off">
      </div>
      <button class="ghost-btn pearl-linked-btn" type="button" data-pearl-linked-toggle aria-pressed="false">
        With evidence only
      </button>
    </div>

    <div id="pearl-category-chips" class="chip-cloud pearl-category-chips" aria-label="Filter pearls by episode category"></div>

    <div id="pearl-specialty-chips" class="chip-cloud pearl-specialty-chips" aria-label="Filter pearls by specialty"></div>

    <div class="pearls-resultbar">
      <span id="pearl-count" class="results-count"></span>
    </div>

    <div id="pearls-grid" class="pearls-grid"></div>
  `;

  const searchInput = document.getElementById('pearl-search');
  searchInput.value = state.pearlQuery;
  let pearlTimer = null;
  searchInput.addEventListener('input', () => {
    clearTimeout(pearlTimer);
    pearlTimer = window.setTimeout(() => {
      state.pearlQuery = searchInput.value;
      applyPearlFilters();
    }, DEBOUNCE_MS);
  });

  renderViewMode();
}

function applyPearlFilters() {
  if (!allPearls.length) {
    return;
  }

  const needle = normalizeText(state.pearlQuery);
  filteredPearls = allPearls.filter(pearl => {
    if (state.pearlLinkedOnly && !(pearl.supporting_citations || []).length) {
      return false;
    }
    if (state.pearlSpecialties.size
      && !(pearl.specialty_tags || []).some(tag => state.pearlSpecialties.has(tag))) {
      return false;
    }
    if (state.pearlCategories.size
      && !(pearl.episode_categories || []).some(category => state.pearlCategories.has(category))) {
      return false;
    }
    if (needle && !pearlMatchesQuery(pearl, needle)) {
      return false;
    }
    return true;
  });

  renderPearlCategoryChips();
  renderPearlSpecialtyChips();
  renderPearlLinkedToggle();

  const countEl = document.getElementById('pearl-count');
  if (countEl) {
    countEl.textContent = `${filteredPearls.length.toLocaleString()} pearl${filteredPearls.length === 1 ? '' : 's'}`;
  }

  const grid = document.getElementById('pearls-grid');
  if (!grid) {
    return;
  }
  grid.innerHTML = filteredPearls.length
    ? filteredPearls.slice(0, 300).map(pearlCardHTML).join('')
    : `<div class="empty-state"><strong>No matching pearls</strong><p>Try a broader term or clear the specialty filter.</p></div>`;
}

function pearlMatchesQuery(pearl, needle) {
  const haystack = normalizeText([
    pearl.pearl,
    ...(pearl.topics || []),
    ...(pearl.clinical_topics || []),
    ...(pearl.segments || []),
    ...(pearl.specialty_tags || []),
    ...(pearl.episode_categories || []),
    ...(pearl.supporting_citations || []).map(citation => citation.citation_label),
    ...(pearl.episodes || []).map(episode => episode.episode_title),
  ].filter(Boolean).join(' '));
  return haystack.includes(needle);
}

function renderPearlSpecialtyChips() {
  const container = document.getElementById('pearl-specialty-chips');
  if (!container) {
    return;
  }
  container.innerHTML = pearlSpecialtyCounts.map(item => {
    const active = state.pearlSpecialties.has(item.key);
    return `
      <button class="topic-chip${active ? ' active' : ''}" type="button" data-pearl-specialty="${escAttr(item.key)}" aria-pressed="${active}">
        <span>${esc(cap(item.label))}</span>
        <span class="chip-count">${item.count}</span>
      </button>
    `;
  }).join('');
}

function renderPearlCategoryChips() {
  const container = document.getElementById('pearl-category-chips');
  if (!container) {
    return;
  }
  container.innerHTML = pearlCategoryCounts.map(item => {
    const active = state.pearlCategories.has(item.key);
    return `
      <button class="topic-chip${active ? ' active' : ''}" type="button" data-pearl-category="${escAttr(item.key)}" aria-pressed="${active}">
        <span>${esc(cap(item.label))}</span>
        <span class="chip-count">${item.count}</span>
      </button>
    `;
  }).join('');
}

function renderPearlLinkedToggle() {
  const button = document.querySelector('[data-pearl-linked-toggle]');
  if (button) {
    button.classList.toggle('active', state.pearlLinkedOnly);
    button.setAttribute('aria-pressed', state.pearlLinkedOnly ? 'true' : 'false');
  }
}

function pearlCardHTML(pearl) {
  // The segment is the sub-episode topic — more specific than the episode.
  const segments = (pearl.segments || []).slice(0, 2)
    .map(segment => `<span class="pearl-segment">${esc(segment)}</span>`).join('');
  const topics = (pearl.topics || []).slice(0, 3)
    .map(topic => `<span class="topic-tag">${esc(topic)}</span>`).join('');
  const specialties = (pearl.specialty_tags || [])
    .map(tag => `<span class="tag">${esc(cap(tag))}</span>`).join('');

  const citations = (pearl.supporting_citations || []).map(citation => {
    const label = citation.citation_label || citation.paper_title || 'Cited source';
    const badge = `<span class="study-badge ${studyBadgeClass(citation.study_type)}">${esc(studyTypeLabel(citation.study_type))}</span>`;
    const detailBits = [
      citation.journal,
      citation.sample_size ? `n=${Number(citation.sample_size).toLocaleString()}` : null,
      citation.nct_id,
    ].filter(Boolean);
    const detail = detailBits.length
      ? `<span class="pearl-cite-detail">${esc(detailBits.join(' · '))}</span>`
      : '';
    const pubmed = citation.pubmed_url
      ? `<a class="pearl-cite-link" href="${escAttr(safeUrl(citation.pubmed_url))}" target="_blank" rel="noopener">source ↗</a>`
      : '';
    return `
      <div class="pearl-citation">
        <button class="pearl-cite-name" type="button" data-pearl-evidence="${escAttr(citation.canonical_key)}" title="Show this record in the evidence browser">
          ${esc(truncate(label, 70))}
        </button>
        ${badge}
        ${detail}
        ${pubmed}
      </div>
    `;
  }).join('');

  const evidenceBlock = citations
    ? `<div class="pearl-evidence"><p class="pearl-evidence-title">Supporting evidence</p>${citations}</div>`
    : `<p class="pearl-noevidence">Teaching point from the show notes — no cited study in this episode.</p>`;

  const episodes = (pearl.episodes || []).slice(0, 2).map(episode => {
    const label = episode.episode_number ? `Ep. #${episode.episode_number}` : 'Episode';
    return `
      <a class="episode-link" href="${escAttr(safeUrl(episode.episode_url))}" target="_blank" rel="noopener">
        <span class="episode-kicker">${esc(label)}</span>
        <span>${esc(truncate(episode.episode_title || 'Curbsiders episode', 72))}</span>
      </a>
    `;
  }).join('');
  const moreEpisodes = (pearl.episode_count || 0) > 2
    ? `<p class="episode-more">+${pearl.episode_count - 2} more episode${pearl.episode_count - 2 === 1 ? '' : 's'}</p>`
    : '';

  return `
    <article class="pearl-card">
      <p class="pearl-text">${esc(pearl.pearl)}</p>
      <div class="pearl-tags">${segments}${topics}${specialties}</div>
      ${evidenceBlock}
      <div class="pearl-footer">
        ${episodes}
        ${moreEpisodes}
      </div>
    </article>
  `;
}

function showEvidenceForCitation(canonicalKey) {
  const trial = allTrials.find(item => item.canonical_key === canonicalKey);
  if (!trial) {
    return;
  }
  state.viewMode = 'browser';
  state.searchQuery = trial.citation_label || trial.paper_title || '';
  state.topicQuery = '';
  state.selectedSpecialties.clear();
  state.selectedStudyTypes.clear();
  state.selectedEra = 'all';
  state.pubmedMode = 'all';
  state.sort = 'relevance';
  state.sortManuallyChosen = true;
  state.page = 1;

  document.getElementById('search-input').value = state.searchQuery;
  document.getElementById('topic-input').value = '';
  document.getElementById('sort-select').value = state.sort;
  applyFilters();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderActiveFilters() {
  const parts = [];
  const searchQuery = activeSearchQuery();
  const topicQuery = activeTopicQuery();
  if (searchQuery) {
    parts.push(activeFilterHTML('search', `Search: ${searchQuery}`));
  }
  if (topicQuery) {
    parts.push(activeFilterHTML('topic', `Topic: ${topicQuery}`));
  }
  for (const specialty of [...state.selectedSpecialties].sort()) {
    parts.push(activeFilterHTML('specialty', cap(specialty), specialty));
  }
  for (const type of [...state.selectedStudyTypes].sort()) {
    parts.push(activeFilterHTML('type', studyTypeLabel(type), type));
  }
  if (state.selectedEra !== 'all') {
    const era = ERA_DEFS.find(item => item.key === state.selectedEra);
    parts.push(activeFilterHTML('era', era?.label || state.selectedEra));
  }
  if (state.pubmedMode !== 'all') {
    const label = PUBMED_OPTIONS.find(option => option.key === state.pubmedMode)?.label || state.pubmedMode;
    parts.push(activeFilterHTML('pubmed', label));
  }

  const container = document.getElementById('active-filters');
  if (!parts.length) {
    container.innerHTML = `
      <div class="active-filter-summary">
        No active filters. Start with a topic, then narrow by evidence type, specialty, or era.
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="active-filter-summary">Active filters</div>
    <div class="active-filter-list">${parts.join('')}</div>
  `;
}

function activeFilterHTML(kind, label, value = '') {
  return `
    <button class="active-filter-chip" type="button" data-remove-filter="${kind}" data-value="${escAttr(value)}">
      <span>${esc(label)}</span>
      <span aria-hidden="true">×</span>
    </button>
  `;
}

function renderResultsSummary(sortKey) {
  const countEl = document.getElementById('results-count');
  const subtitleEl = document.getElementById('results-subtitle');
  const sortSelect = document.getElementById('sort-select');

  countEl.textContent = `${filteredTrials.length.toLocaleString()} result${filteredTrials.length === 1 ? '' : 's'}`;

  const descriptors = [];
  const topicQuery = activeTopicQuery();
  if (topicQuery) {
    descriptors.push(`topic match: "${topicQuery}"`);
  }
  if (state.selectedStudyTypes.size) {
    descriptors.push(`${state.selectedStudyTypes.size} study type filter${state.selectedStudyTypes.size === 1 ? '' : 's'}`);
  }
  if (state.selectedSpecialties.size) {
    descriptors.push(`${state.selectedSpecialties.size} specialty filter${state.selectedSpecialties.size === 1 ? '' : 's'}`);
  }
  if (state.selectedEra !== 'all') {
    descriptors.push(`era: ${ERA_DEFS.find(era => era.key === state.selectedEra)?.label || state.selectedEra}`);
  }
  if (state.pubmedMode !== 'all') {
    descriptors.push(PUBMED_OPTIONS.find(option => option.key === state.pubmedMode)?.label || state.pubmedMode);
  }
  subtitleEl.textContent = descriptors.length
    ? descriptors.join(' • ')
    : 'Browse the full canonical evidence set';

  sortSelect.value = sortKey;
}

function renderPage() {
  const start = (state.page - 1) * PAGE_SIZE;
  const pageItems = filteredTrials.slice(start, start + PAGE_SIZE);
  const grid = document.getElementById('cards-grid');

  if (!filteredTrials.length) {
    grid.innerHTML = `
      <div class="empty-state">
        <strong>No matching trials</strong>
        <p>Try broadening the topic phrase, removing a specialty, or switching the publication era.</p>
      </div>
    `;
  } else {
    grid.innerHTML = pageItems.map(cardHTML).join('');
  }

  renderPagination();
  if (window.scrollY > 200) {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
}

function renderPagination() {
  const totalPages = Math.ceil(filteredTrials.length / PAGE_SIZE);
  const container = document.getElementById('pagination');

  if (totalPages <= 1) {
    container.innerHTML = '';
    return;
  }

  const parts = [];
  parts.push(pageButton('Prev', state.page - 1, state.page === 1));

  for (let page = 1; page <= totalPages; page += 1) {
    const edge = page === 1 || page === totalPages;
    const near = Math.abs(page - state.page) <= 1;
    if (!edge && !near) {
      const last = parts[parts.length - 1] || '';
      if (!last.includes('ellipsis')) {
        parts.push('<button class="page-btn ellipsis" type="button" disabled>…</button>');
      }
      continue;
    }
    parts.push(pageButton(String(page), page, false, page === state.page));
  }

  parts.push(pageButton('Next', state.page + 1, state.page === totalPages));
  container.innerHTML = parts.join('');

  container.querySelectorAll('[data-page]').forEach(button => {
    button.addEventListener('click', () => {
      state.page = clampPage(Number.parseInt(button.dataset.page, 10));
      renderPage();
      syncUrl();
    });
  });
}

function pageButton(label, page, disabled, active = false) {
  const className = `page-btn${active ? ' active' : ''}`;
  return `
    <button
      class="${className}"
      type="button"
      ${disabled ? 'disabled' : ''}
      data-page="${page}"
    >${label}</button>
  `;
}

function cardHTML(trial) {
  const title = trial.citation_label || trial.paper_title || 'Untitled citation';
  const paperTitle = trial.paper_title && trial.paper_title !== title
    ? `<p class="paper-title">${esc(trial.paper_title)}</p>`
    : '';

  const topics = (trial.context_topics || []).slice(0, 3);
  const topicTags = topics.length
    ? `<div class="topic-tags">${topics.map(topic => `<span class="topic-tag">${esc(topic)}</span>`).join('')}</div>`
    : '';

  const specialties = (trial.specialty_tags || []).map(tag => `
    <span class="tag">${esc(cap(tag))}</span>
  `).join('');

  const metaBits = [
    trial.year ? `Published ${trial.year}` : 'Year unknown',
    trial.study_type ? studyTypeLabel(trial.study_type) : 'Other evidence',
    trial.pubmed_url ? 'PubMed linked' : 'No PubMed link',
  ];

  const recentEpisode = trial.latest_episode_number
    ? `Most recent Curbsiders mention: Ep. #${trial.latest_episode_number}`
    : 'Episode number unavailable';

  const episodeLinks = (trial.episodes || []).slice(0, 3).map(episode => {
    const label = episode.episode_number ? `Ep. #${episode.episode_number}` : 'Episode';
    return `
      <a class="episode-link" href="${escAttr(safeUrl(episode.episode_url))}" target="_blank" rel="noopener">
        <span class="episode-kicker">${esc(label)}</span>
        <span>${esc(truncate(episode.episode_title || 'Curbsiders episode', 84))}</span>
      </a>
    `;
  }).join('');

  const moreEpisodes = (trial.episode_count || 0) > 3
    ? `<p class="episode-more">+${trial.episode_count - 3} more episode${trial.episode_count - 3 === 1 ? '' : 's'}</p>`
    : '';

  const pubmedLink = trial.pubmed_url
    ? `<a class="card-link" href="${escAttr(safeUrl(trial.pubmed_url))}" target="_blank" rel="noopener">Open PubMed</a>`
    : '';

  return `
    <article class="card">
      <div class="card-header">
        <div>
          <p class="card-kicker">${esc(recentEpisode)}</p>
          <h3 class="card-title">${esc(title)}</h3>
        </div>
        <span class="study-badge ${studyBadgeClass(trial.study_type)}">${esc(studyTypeLabel(trial.study_type))}</span>
      </div>

      ${paperTitle}
      <p class="card-summary">${esc(trial.brief_summary || 'No summary available.')}</p>
      ${topicTags}

      <div class="meta-row">
        ${metaBits.map(bit => `<span class="meta-pill">${esc(bit)}</span>`).join('')}
      </div>

      <div class="card-tags">${specialties}</div>

      <div class="card-metrics">
        <span>${(trial.mention_count || 0).toLocaleString()} mention${trial.mention_count === 1 ? '' : 's'}</span>
        <span>${(trial.episode_count || 0).toLocaleString()} episode${trial.episode_count === 1 ? '' : 's'}</span>
      </div>

      <div class="card-footer">
        <div class="episode-links">
          ${episodeLinks}
          ${moreEpisodes}
        </div>
        <div class="card-links">
          ${pubmedLink}
        </div>
      </div>
    </article>
  `;
}

function topicMatches(trial, needle) {
  const haystacks = [
    trial.context_topic,
    ...(trial.context_topics || []),
    trial.brief_summary,
  ];
  return haystacks.some(value => normalizeText(value).includes(needle));
}

function yearEraKey(year) {
  if (!year || Number.isNaN(Number(year))) {
    return 'unknown';
  }
  const numericYear = Number(year);
  for (const era of ERA_DEFS) {
    if (era.key === 'unknown') {
      continue;
    }
    const minOk = era.min === null || numericYear >= era.min;
    const maxOk = era.max === null || numericYear <= era.max;
    if (minOk && maxOk) {
      return era.key;
    }
  }
  return 'unknown';
}

function studyBadgeClass(type) {
  const normalized = cleanText(type)?.toLowerCase() || 'other';
  if (normalized === 'rct') {
    return 'rct';
  }
  if (normalized === 'observational') {
    return 'observational';
  }
  if (normalized === 'guideline') {
    return 'guideline';
  }
  if (normalized === 'meta-analysis' || normalized === 'systematic review') {
    return 'review';
  }
  return 'other';
}

function studyTypeLabel(type) {
  if (!type) {
    return 'Other';
  }
  const normalized = cleanText(type)?.toLowerCase() || '';
  if (normalized === 'rct') {
    return 'RCT';
  }
  if (normalized === 'meta-analysis') {
    return 'Meta-analysis';
  }
  if (normalized === 'systematic review') {
    return 'Systematic review';
  }
  if (normalized === 'case series') {
    return 'Case series';
  }
  if (normalized === 'observational') {
    return 'Observational';
  }
  if (normalized === 'guideline') {
    return 'Guideline';
  }
  return cap(type);
}

function toggleSetValue(set, value) {
  if (set.has(value)) {
    set.delete(value);
  } else {
    set.add(value);
  }
}

function compareTitle(a, b) {
  return (a.citation_label || a.paper_title || '').localeCompare(b.citation_label || b.paper_title || '');
}

function activeSearchQuery() {
  return cleanText(state.searchQuery);
}

function activeTopicQuery() {
  return cleanText(state.topicQuery);
}

function cleanText(value) {
  return String(value || '').trim();
}

function normalizeText(value) {
  return cleanText(value).toLowerCase();
}

function normalizeSearchText(value) {
  return cleanText(value)
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function cap(value) {
  const text = cleanText(value);
  if (!text) {
    return text;
  }
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escAttr(value) {
  return esc(value).replace(/'/g, '&#39;');
}

function safeUrl(value) {
  const text = cleanText(value);
  return /^https?:\/\//i.test(text) ? text : '#';
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function truncate(value, maxLength) {
  const text = String(value || '');
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength - 1)}…`;
}

function clampPage(value) {
  if (!Number.isFinite(value) || value < 1) {
    return 1;
  }
  return value;
}

function onlyUnique(value, index, array) {
  return array.indexOf(value) === index;
}

init();
