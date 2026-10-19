"""记忆扩展表；兼容既有 memories/relations 的列与写入格式。"""

SCHEMA: str = """
CREATE TABLE IF NOT EXISTS memory_terms (
 term TEXT NOT NULL, memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 PRIMARY KEY(term,memory_id));
CREATE INDEX IF NOT EXISTS memory_terms_id ON memory_terms(memory_id);
CREATE TABLE IF NOT EXISTS memory_revisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 snapshot TEXT NOT NULL, changed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_sources (
 id TEXT PRIMARY KEY, content TEXT NOT NULL, source TEXT NOT NULL,
 session_id TEXT NOT NULL, role TEXT NOT NULL, principal TEXT NOT NULL,
 trusted_owner INTEGER NOT NULL, digest TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_source_terms (
 term TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES memory_sources(id) ON DELETE CASCADE,
 PRIMARY KEY(term,source_id));
CREATE TABLE IF NOT EXISTS memory_forgotten_sources (id TEXT PRIMARY KEY, forgotten_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_meta (
 memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
 layer TEXT NOT NULL, source_id TEXT REFERENCES memory_sources(id) ON DELETE CASCADE,
 trusted_owner INTEGER NOT NULL DEFAULT 0, quote TEXT NOT NULL DEFAULT '',
 claim_key TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
 version INTEGER NOT NULL DEFAULT 1, dedup_key TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS memory_meta_source ON memory_meta(source_id);
CREATE INDEX IF NOT EXISTS memory_meta_claim ON memory_meta(claim_key,status);
CREATE TABLE IF NOT EXISTS memory_entities (
 id TEXT PRIMARY KEY, canonical TEXT NOT NULL UNIQUE, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_aliases (
 alias TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
 source_id TEXT REFERENCES memory_sources(id) ON DELETE CASCADE,
 memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS memory_aliases_entity ON memory_aliases(entity_id);
CREATE TABLE IF NOT EXISTS memory_mentions (
 memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 entity_id TEXT NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
 PRIMARY KEY(memory_id,entity_id));
CREATE INDEX IF NOT EXISTS memory_mentions_entity ON memory_mentions(entity_id);
CREATE TABLE IF NOT EXISTS memory_edges (
 memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 subject_id TEXT NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
 predicate TEXT NOT NULL, object_id TEXT NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
 source_id TEXT REFERENCES memory_sources(id) ON DELETE CASCADE, quote TEXT NOT NULL,
 PRIMARY KEY(memory_id,subject_id,predicate,object_id));
CREATE INDEX IF NOT EXISTS memory_edges_subject ON memory_edges(subject_id);
CREATE INDEX IF NOT EXISTS memory_edges_object ON memory_edges(object_id);
CREATE TABLE IF NOT EXISTS memory_vectors (
 memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
 model_key TEXT NOT NULL, dimensions INTEGER NOT NULL, norm REAL NOT NULL, vector TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memory_vectors_model ON memory_vectors(model_key);
CREATE TABLE IF NOT EXISTS memory_conflicts (
 id TEXT PRIMARY KEY, left_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 right_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 resolved_at REAL, chosen_id TEXT, UNIQUE(left_id,right_id));
CREATE TABLE IF NOT EXISTS memory_communities (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL,
 entities TEXT NOT NULL, evidence TEXT NOT NULL, source_ids TEXT NOT NULL,
 truncated INTEGER NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_community_members (
 community_id TEXT NOT NULL REFERENCES memory_communities(id) ON DELETE CASCADE,
 memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 PRIMARY KEY(community_id,memory_id));
CREATE INDEX IF NOT EXISTS memory_community_memory ON memory_community_members(memory_id);
CREATE TABLE IF NOT EXISTS memory_community_nodes (
 community_id TEXT NOT NULL REFERENCES memory_communities(id) ON DELETE CASCADE,
 entity_id TEXT NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
 PRIMARY KEY(community_id,entity_id));
CREATE INDEX IF NOT EXISTS memory_community_entity ON memory_community_nodes(entity_id);
CREATE TABLE IF NOT EXISTS memory_jobs (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE REFERENCES memory_sources(id) ON DELETE CASCADE,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
 last_error TEXT NOT NULL DEFAULT '', checkpoint TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS memory_jobs_ready ON memory_jobs(state,next_attempt,created_at);
CREATE TABLE IF NOT EXISTS memory_state (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO memory_state VALUES('revision',0);
"""
