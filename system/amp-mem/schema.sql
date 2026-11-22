-- Amp Memory Schema — Persistent cross-session memory
-- Modeled after claude-mem's architecture: sessions, observations, summaries

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Sessions: one per Amp thread/conversation
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_url  TEXT,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    ended_at    TEXT,
    summary     TEXT
);

-- Observations: individual knowledge entries captured during sessions
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER REFERENCES sessions(id),
    ts            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    type          TEXT NOT NULL DEFAULT 'observation',
    topic         TEXT NOT NULL,
    summary       TEXT NOT NULL,
    project       TEXT,
    file_path     TEXT,
    tags          TEXT,
    metadata      TEXT,
    confidence    REAL NOT NULL DEFAULT 1.0,
    access_count  INTEGER NOT NULL DEFAULT 0,
    last_accessed TEXT,
    private       INTEGER NOT NULL DEFAULT 0,
    compacted_at  TEXT,
    superseded_by INTEGER REFERENCES observations(id)
);

-- FTS5 full-text search index
CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
    topic,
    summary,
    project,
    tags,
    content='observations',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS index in sync
CREATE TRIGGER IF NOT EXISTS observations_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observations_fts(rowid, topic, summary, project, tags)
    VALUES (new.id, new.topic, new.summary, new.project, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS observations_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, topic, summary, project, tags)
    VALUES ('delete', old.id, old.topic, old.summary, old.project, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS observations_au AFTER UPDATE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, topic, summary, project, tags)
    VALUES ('delete', old.id, old.topic, old.summary, old.project, old.tags);
    INSERT INTO observations_fts(rowid, topic, summary, project, tags)
    VALUES (new.id, new.topic, new.summary, new.project, new.tags);
END;

-- Compacted summaries: periodic AI-compressed archives
CREATE TABLE IF NOT EXISTS summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    summary         TEXT NOT NULL,
    observation_ids TEXT, -- JSON array of observation IDs that were compacted
    ai_summary      TEXT  -- prose rewrite of `summary`, backfilled by the plugin's generateAISummaries
);

-- Injection log: one row per budgeted `amp-mem context` injection
-- at agent.start. row_ids is a JSON array of the observation ids shown, so
-- "was an injected row ever retrieved later" is measurable (plan note §9).
-- Injection never bumps access_count; only search/detail do.
CREATE TABLE IF NOT EXISTS injection_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    session_id  TEXT,
    project     TEXT,
    chars       INTEGER,
    row_ids     TEXT
);

-- Distillations: records of when kb-distill was run
CREATE TABLE IF NOT EXISTS distillations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    observation_count INTEGER NOT NULL,
    notes_produced  INTEGER NOT NULL DEFAULT 0,
    rules_proposed  INTEGER NOT NULL DEFAULT 0,
    summary         TEXT
);

-- Index for common queries
CREATE INDEX IF NOT EXISTS idx_obs_ts ON observations(ts);
CREATE INDEX IF NOT EXISTS idx_obs_type ON observations(type);
CREATE INDEX IF NOT EXISTS idx_obs_project ON observations(project);
CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id);
