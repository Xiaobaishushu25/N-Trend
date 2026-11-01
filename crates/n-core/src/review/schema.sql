-- Immutable review artifacts; mutable pointers publish complete jobs atomically.
CREATE TABLE IF NOT EXISTS review_state(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL DEFAULT 0,calendar_version TEXT NOT NULL DEFAULT '');
INSERT OR IGNORE INTO review_state(id) VALUES (1);
CREATE TABLE IF NOT EXISTS review_dirty(symbol TEXT PRIMARY KEY,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS review_event_origins(event_id INTEGER PRIMARY KEY,origin TEXT NOT NULL);
INSERT OR IGNORE INTO review_event_origins(event_id,origin) SELECT id,'historical_rebuild' FROM pattern_events;
CREATE TABLE IF NOT EXISTS review_jobs(id TEXT PRIMARY KEY,status TEXT NOT NULL,data TEXT NOT NULL,calendar_json TEXT NOT NULL,implementation_version TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_review_jobs_status ON review_jobs(status);
CREATE TABLE IF NOT EXISTS review_job_symbols(job_id TEXT NOT NULL,symbol TEXT NOT NULL,dirty_revision INTEGER NOT NULL,done INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(job_id,symbol));
CREATE TABLE IF NOT EXISTS review_results(
 result_version TEXT PRIMARY KEY,event_id INTEGER NOT NULL,symbol TEXT NOT NULL,family_id INTEGER NOT NULL,
 warning_ts TEXT NOT NULL,entry_ts TEXT,exit_ts TEXT,direction TEXT NOT NULL,level TEXT NOT NULL,grade TEXT NOT NULL,
 state TEXT NOT NULL,exit_reason TEXT,terminal_reason TEXT,old REAL,quality_v1 REAL,morphology_v1 REAL,
 quality_v2 REAL,morphology_v2 REAL,a_score REAL,b_score REAL,score_status TEXT NOT NULL,
 gross_amount REAL,net_amount REAL,gross_r REAL,net_r REAL,mfe_r REAL,mae_r REAL,bars_held INTEGER NOT NULL,
 summary_json TEXT NOT NULL,detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_result_event ON review_results(event_id,result_version);
CREATE INDEX IF NOT EXISTS idx_review_result_symbol_time ON review_results(symbol,warning_ts,event_id);
CREATE INDEX IF NOT EXISTS idx_review_result_warning ON review_results(warning_ts,event_id);
CREATE INDEX IF NOT EXISTS idx_review_result_entry ON review_results(entry_ts,event_id);
CREATE INDEX IF NOT EXISTS idx_review_result_exit ON review_results(exit_ts,event_id);
CREATE INDEX IF NOT EXISTS idx_review_result_score ON review_results(quality_v2,event_id);
CREATE INDEX IF NOT EXISTS idx_review_result_reasons ON review_results(state,exit_reason,terminal_reason);
-- Current projection contains only published heads; historical versions never inflate scans.
CREATE TABLE IF NOT EXISTS review_current(
 event_id INTEGER PRIMARY KEY,result_version TEXT NOT NULL REFERENCES review_results(result_version),
 summary_json TEXT NOT NULL DEFAULT '{}',
 symbol TEXT GENERATED ALWAYS AS (json_extract(summary_json,'$.symbol')) STORED,
 family_id REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.family_id')) STORED,
 warning_ts TEXT GENERATED ALWAYS AS (json_extract(summary_json,'$.warning_ts')) STORED,
 entry_ts TEXT GENERATED ALWAYS AS (json_extract(summary_json,'$.entry_ts')) STORED,
 exit_ts TEXT GENERATED ALWAYS AS (json_extract(summary_json,'$.exit_ts')) STORED,
 direction TEXT GENERATED ALWAYS AS (json_extract(summary_json,'$.direction')) STORED,
 level TEXT GENERATED ALWAYS AS (json_extract(summary_json,'$.level')) STORED,
 grade TEXT GENERATED ALWAYS AS (json_extract(summary_json,'$.grade')) STORED,
 state TEXT GENERATED ALWAYS AS (json_extract(summary_json,'$.state')) STORED,
 exit_reason TEXT GENERATED ALWAYS AS (json_extract(summary_json,'$.exit_reason')) STORED,
 terminal_reason TEXT GENERATED ALWAYS AS (json_extract(summary_json,'$.terminal_reason')) STORED,
 old REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.scores.old')) STORED,
 quality_v1 REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.scores.quality_v1')) STORED,
 morphology_v1 REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.scores.morphology_v1')) STORED,
 quality_v2 REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.scores.quality_v2')) STORED,
 morphology_v2 REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.scores.morphology_v2')) STORED,
 a_score REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.scores.a_score')) STORED,
 b_score REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.scores.b_score')) STORED,
 score_status TEXT GENERATED ALWAYS AS (json_extract(summary_json,'$.score_status')) STORED,
 gross_amount REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.gross_amount')) STORED,
 net_amount REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.net_amount')) STORED,
 gross_r REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.gross_r')) STORED,
 net_r REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.net_r')) STORED,
 mfe_r REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.mfe_r')) STORED,
 mae_r REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.mae_r')) STORED,
 bars_held REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.bars_held')) STORED
);
CREATE TRIGGER IF NOT EXISTS review_current_insert AFTER INSERT ON review_current BEGIN
 UPDATE review_current SET summary_json=(SELECT summary_json FROM review_results WHERE result_version=NEW.result_version) WHERE event_id=NEW.event_id;
END;
CREATE TRIGGER IF NOT EXISTS review_current_update AFTER UPDATE OF result_version ON review_current BEGIN
 UPDATE review_current SET summary_json=(SELECT summary_json FROM review_results WHERE result_version=NEW.result_version) WHERE event_id=NEW.event_id;
END;
CREATE INDEX IF NOT EXISTS idx_review_current_symbol_time ON review_current(symbol,warning_ts,event_id);
CREATE INDEX IF NOT EXISTS idx_review_current_warning ON review_current(warning_ts,event_id);
CREATE INDEX IF NOT EXISTS idx_review_current_entry ON review_current(entry_ts,event_id);
CREATE INDEX IF NOT EXISTS idx_review_current_exit ON review_current(exit_ts,event_id);
CREATE INDEX IF NOT EXISTS idx_review_current_reasons ON review_current(state,exit_reason,terminal_reason);
CREATE INDEX IF NOT EXISTS idx_review_current_old ON review_current(old,event_id);
CREATE INDEX IF NOT EXISTS idx_review_current_quality_v1 ON review_current(quality_v1,event_id);
CREATE INDEX IF NOT EXISTS idx_review_current_morphology_v1 ON review_current(morphology_v1,event_id);
CREATE INDEX IF NOT EXISTS idx_review_current_quality_v2 ON review_current(quality_v2,event_id);
CREATE INDEX IF NOT EXISTS idx_review_current_morphology_v2 ON review_current(morphology_v2,event_id);
CREATE INDEX IF NOT EXISTS idx_review_current_a_score ON review_current(a_score,event_id);
CREATE INDEX IF NOT EXISTS idx_review_current_b_score ON review_current(b_score,event_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_review_current_result ON review_current(result_version);
CREATE TABLE IF NOT EXISTS review_staging(job_id TEXT NOT NULL,event_id INTEGER NOT NULL,result_version TEXT NOT NULL,PRIMARY KEY(job_id,event_id));
CREATE TABLE IF NOT EXISTS review_nodes(result_version TEXT NOT NULL REFERENCES review_results(result_version),sequence INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(result_version,sequence));
CREATE TABLE IF NOT EXISTS review_holding(result_version TEXT NOT NULL REFERENCES review_results(result_version),sequence INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(result_version,sequence));
-- Transactional outbox: signals/candles and dirty work commit or roll back together.
CREATE TRIGGER IF NOT EXISTS review_event_insert AFTER INSERT ON pattern_events BEGIN
 INSERT OR IGNORE INTO review_event_origins(event_id,origin) VALUES(NEW.id,CASE WHEN (SELECT value FROM settings WHERE key='signal_origin')='historical_rebuild' THEN 'historical_rebuild' ELSE 'live_observed' END);
 INSERT INTO review_dirty(symbol,revision) VALUES(NEW.symbol,1) ON CONFLICT(symbol) DO UPDATE SET revision=review_dirty.revision+1;
END;
CREATE TRIGGER IF NOT EXISTS review_event_immutable_update AFTER UPDATE OF symbol,direction,grade,level,s0_ts,s1_ts,s2_ts,s0_price,s1_price,s2_price,warning_ts,entry,stop,target,risk,entry_score ON pattern_events BEGIN
 INSERT INTO review_dirty(symbol,revision) VALUES(NEW.symbol,1) ON CONFLICT(symbol) DO UPDATE SET revision=review_dirty.revision+1;
END;
CREATE TRIGGER IF NOT EXISTS review_event_delete AFTER DELETE ON pattern_events BEGIN
 INSERT INTO review_dirty(symbol,revision) VALUES(OLD.symbol,1) ON CONFLICT(symbol) DO UPDATE SET revision=review_dirty.revision+1;
END;
CREATE TRIGGER IF NOT EXISTS review_raw_insert AFTER INSERT ON klines WHEN NEW.timeframe IN ('5m','15m') AND EXISTS(SELECT 1 FROM pattern_events WHERE symbol=NEW.symbol) BEGIN
 INSERT INTO review_dirty(symbol,revision) VALUES(NEW.symbol,1) ON CONFLICT(symbol) DO UPDATE SET revision=review_dirty.revision+1;
END;
CREATE TRIGGER IF NOT EXISTS review_raw_update AFTER UPDATE ON klines WHEN NEW.timeframe IN ('5m','15m') AND (OLD.open!=NEW.open OR OLD.high!=NEW.high OR OLD.low!=NEW.low OR OLD.close!=NEW.close OR OLD.volume!=NEW.volume OR OLD.hold!=NEW.hold) AND EXISTS(SELECT 1 FROM pattern_events WHERE symbol=NEW.symbol) BEGIN
 INSERT INTO review_dirty(symbol,revision) VALUES(NEW.symbol,1) ON CONFLICT(symbol) DO UPDATE SET revision=review_dirty.revision+1;
END;
CREATE TRIGGER IF NOT EXISTS review_raw_delete AFTER DELETE ON klines WHEN OLD.timeframe IN ('5m','15m') AND EXISTS(SELECT 1 FROM pattern_events WHERE symbol=OLD.symbol) BEGIN
 INSERT INTO review_dirty(symbol,revision) VALUES(OLD.symbol,1) ON CONFLICT(symbol) DO UPDATE SET revision=review_dirty.revision+1;
END;
CREATE TRIGGER IF NOT EXISTS review_rollover_insert AFTER INSERT ON rollovers BEGIN
 INSERT INTO review_dirty(symbol,revision) VALUES(NEW.symbol,1) ON CONFLICT(symbol) DO UPDATE SET revision=review_dirty.revision+1;
END;
CREATE TRIGGER IF NOT EXISTS review_rollover_update AFTER UPDATE ON rollovers BEGIN
 INSERT INTO review_dirty(symbol,revision) VALUES(NEW.symbol,1) ON CONFLICT(symbol) DO UPDATE SET revision=review_dirty.revision+1;
END;
CREATE TRIGGER IF NOT EXISTS review_rollover_delete AFTER DELETE ON rollovers BEGIN
 INSERT INTO review_dirty(symbol,revision) VALUES(OLD.symbol,1) ON CONFLICT(symbol) DO UPDATE SET revision=review_dirty.revision+1;
END;
CREATE TRIGGER IF NOT EXISTS review_symbol_parameters AFTER UPDATE OF tick_size,variety ON symbols BEGIN
 INSERT INTO review_dirty(symbol,revision) VALUES(NEW.code,1) ON CONFLICT(symbol) DO UPDATE SET revision=review_dirty.revision+1;
END;
