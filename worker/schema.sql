CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  submitted_at TEXT NOT NULL,
  target_type TEXT NOT NULL,          -- 'pearl' | 'pearl_link'
  pearl_key TEXT NOT NULL,
  pearl_text_snapshot TEXT,
  canonical_key TEXT,                 -- null for pearl-level feedback
  reason_code TEXT NOT NULL,          -- 'inaccurate'|'outdated'|'wrong_citation'|'unclear'|'other'
  comment TEXT,
  episode_url TEXT,
  client_ip_hash TEXT
);

-- Speeds up the per-IP rate-limit lookup in src/index.js.
CREATE INDEX IF NOT EXISTS idx_feedback_ip_time ON feedback(client_ip_hash, submitted_at);
