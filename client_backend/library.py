from __future__ import annotations

from contextlib import contextmanager, closing
import json
import re
import sqlite3
import threading
from pathlib import Path

from .biblio import authors_text, normalize, year_of
from .advanced_search import compile_rule
from .common import AppError, bounded_int, doi, dumps, norm, now, require, uid


SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items(
 id TEXT PRIMARY KEY,data TEXT NOT NULL,title TEXT NOT NULL,title_norm TEXT NOT NULL,
 authors TEXT NOT NULL,year TEXT NOT NULL,doi TEXT NOT NULL,container TEXT NOT NULL,
 reading_state TEXT NOT NULL,starred INTEGER NOT NULL DEFAULT 0,citation_key TEXT NOT NULL UNIQUE,
 revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
CREATE INDEX IF NOT EXISTS item_doi ON items(doi);
CREATE INDEX IF NOT EXISTS item_title ON items(title_norm,year);
CREATE INDEX IF NOT EXISTS item_updated ON items(updated_at,id);
CREATE INDEX IF NOT EXISTS item_deleted ON items(deleted_at,reading_state);
CREATE TABLE IF NOT EXISTS collections(id TEXT PRIMARY KEY,name TEXT NOT NULL,parent_id TEXT REFERENCES collections(id),created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_items(collection_id TEXT REFERENCES collections(id) ON DELETE CASCADE,item_id TEXT REFERENCES items(id),PRIMARY KEY(collection_id,item_id));
CREATE TABLE IF NOT EXISTS tags(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,color TEXT NOT NULL DEFAULT '#5577aa');
CREATE TABLE IF NOT EXISTS item_tags(item_id TEXT REFERENCES items(id),tag_id TEXT REFERENCES tags(id),PRIMARY KEY(item_id,tag_id));
CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,path TEXT NOT NULL,mode TEXT NOT NULL,mime TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS object_hash ON objects(sha256);
CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY,item_id TEXT NOT NULL REFERENCES items(id),object_id TEXT NOT NULL REFERENCES objects(id),name TEXT NOT NULL,version TEXT NOT NULL,text_status TEXT NOT NULL DEFAULT 'pending',page_count INTEGER,text_json TEXT,position_json TEXT NOT NULL DEFAULT '{}',role TEXT NOT NULL DEFAULT 'main',created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS attachment_item ON attachments(item_id);
CREATE TABLE IF NOT EXISTS annotations(id TEXT PRIMARY KEY,attachment_id TEXT NOT NULL REFERENCES attachments(id),version TEXT NOT NULL,data TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS relation_discoveries(
 session_id TEXT NOT NULL REFERENCES relation_sessions(id) ON DELETE CASCADE,
 anchor_item_id TEXT NOT NULL REFERENCES items(id),
 work_id TEXT NOT NULL,direction TEXT NOT NULL,
 work_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
 imported_item_id TEXT REFERENCES items(id),retrieved_at TEXT NOT NULL,
 PRIMARY KEY(session_id,anchor_item_id,work_id,direction));
CREATE INDEX IF NOT EXISTS relation_discovery_session ON relation_discoveries(session_id,status,direction);
CREATE TABLE IF NOT EXISTS relation_discovery_decisions(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL,anchor_item_id TEXT NOT NULL,
 work_id TEXT NOT NULL,direction TEXT NOT NULL,status TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS relation_discovery_decision_lookup ON relation_discovery_decisions(session_id,anchor_item_id,work_id,direction,created_at);
CREATE TABLE IF NOT EXISTS relation_discovery_pages(
 session_id TEXT NOT NULL,anchor_item_id TEXT NOT NULL,direction TEXT NOT NULL,
 next_offset INTEGER NOT NULL DEFAULT 0,has_more INTEGER NOT NULL DEFAULT 1,
 updated_at TEXT NOT NULL,PRIMARY KEY(session_id,anchor_item_id,direction));
CREATE TABLE IF NOT EXISTS manual_relations(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES relation_sessions(id) ON DELETE CASCADE,
 left_item_id TEXT NOT NULL REFERENCES items(id),right_item_id TEXT NOT NULL REFERENCES items(id),
 label TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(session_id,left_item_id,right_item_id,label));
CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY,item_id TEXT REFERENCES items(id),title TEXT NOT NULL,content TEXT NOT NULL,tags TEXT NOT NULL DEFAULT '[]',revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS note_history(note_id TEXT NOT NULL,revision INTEGER NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,tags TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(note_id,revision));
CREATE TABLE IF NOT EXISTS provenance(id TEXT PRIMARY KEY,item_id TEXT NOT NULL,source TEXT NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS merge_events(id TEXT PRIMARY KEY,data TEXT NOT NULL,undone INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS import_batches(id TEXT PRIMARY KEY,data TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS import_entries(batch_id TEXT NOT NULL,entry_key TEXT NOT NULL,item_id TEXT NOT NULL,PRIMARY KEY(batch_id,entry_key));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,progress REAL NOT NULL DEFAULT 0,message TEXT NOT NULL DEFAULT '',result TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reading_sessions(
 id TEXT PRIMARY KEY,item_id TEXT NOT NULL UNIQUE REFERENCES items(id),attachment_id TEXT REFERENCES attachments(id),
 status TEXT NOT NULL DEFAULT 'planned',goal TEXT NOT NULL DEFAULT '',target_minutes INTEGER NOT NULL DEFAULT 30,
 seconds_read INTEGER NOT NULL DEFAULT 0,last_page INTEGER NOT NULL DEFAULT 1,page_count INTEGER,
 revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,started_at TEXT,completed_at TEXT);
CREATE INDEX IF NOT EXISTS reading_session_attachment ON reading_sessions(attachment_id);
CREATE TABLE IF NOT EXISTS reading_cards(
 id TEXT PRIMARY KEY,item_id TEXT NOT NULL UNIQUE REFERENCES items(id),data TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS terms(
 id TEXT PRIMARY KEY,item_id TEXT NOT NULL REFERENCES items(id),attachment_id TEXT REFERENCES attachments(id),term TEXT NOT NULL,
 translation TEXT NOT NULL DEFAULT '',explanation TEXT NOT NULL DEFAULT '',source_json TEXT NOT NULL DEFAULT '{}',tags TEXT NOT NULL DEFAULT '[]',
 starred INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(item_id,term));
CREATE INDEX IF NOT EXISTS term_item ON terms(item_id,updated_at);
CREATE TABLE IF NOT EXISTS assistant_runs(
 id TEXT PRIMARY KEY,item_id TEXT NOT NULL REFERENCES items(id),attachment_id TEXT REFERENCES attachments(id),job_id TEXT UNIQUE,
 task TEXT NOT NULL,input_json TEXT NOT NULL,result TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS assistant_run_item ON assistant_runs(item_id,created_at);
CREATE TABLE IF NOT EXISTS translation_pages(
 attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
 page INTEGER NOT NULL,source_hash TEXT NOT NULL,translations_json TEXT NOT NULL,
 updated_at TEXT NOT NULL,PRIMARY KEY(attachment_id,page,source_hash));
CREATE TABLE IF NOT EXISTS ai_search_sessions(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,brief TEXT NOT NULL,mode TEXT NOT NULL,plan_json TEXT NOT NULL,sources_json TEXT NOT NULL,
 state TEXT NOT NULL,result_count INTEGER NOT NULL DEFAULT 0,planning_warning TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ai_search_session_updated ON ai_search_sessions(updated_at);
CREATE TABLE IF NOT EXISTS ai_search_source_runs(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES ai_search_sessions(id) ON DELETE CASCADE,source TEXT NOT NULL,state TEXT NOT NULL,
 progress REAL NOT NULL DEFAULT 0,query TEXT NOT NULL DEFAULT '',received INTEGER NOT NULL DEFAULT 0,accepted INTEGER NOT NULL DEFAULT 0,message TEXT NOT NULL DEFAULT '',
 error TEXT,started_at TEXT,completed_at TEXT,UNIQUE(session_id,source));
CREATE TABLE IF NOT EXISTS ai_search_works(
 id TEXT PRIMARY KEY,canonical_key TEXT NOT NULL UNIQUE,title TEXT NOT NULL,title_norm TEXT NOT NULL,authors_json TEXT NOT NULL,year INTEGER,venue TEXT NOT NULL,
 abstract TEXT NOT NULL,doi TEXT NOT NULL DEFAULT '',pmid TEXT NOT NULL DEFAULT '',arxiv_id TEXT NOT NULL DEFAULT '',url TEXT NOT NULL DEFAULT '',citation_count INTEGER NOT NULL DEFAULT 0,
 oa_url TEXT NOT NULL DEFAULT '',type TEXT NOT NULL DEFAULT '',meta_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ai_search_work_title ON ai_search_works(title_norm,year);
CREATE INDEX IF NOT EXISTS ai_search_work_doi ON ai_search_works(doi);
CREATE TABLE IF NOT EXISTS ai_search_work_sources(
 work_id TEXT NOT NULL REFERENCES ai_search_works(id) ON DELETE CASCADE,source TEXT NOT NULL,source_id TEXT NOT NULL,retrieved_at TEXT NOT NULL,
 raw_json TEXT NOT NULL,PRIMARY KEY(work_id,source,source_id));
CREATE TABLE IF NOT EXISTS ai_search_candidates(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES ai_search_sessions(id) ON DELETE CASCADE,work_id TEXT NOT NULL REFERENCES ai_search_works(id),
 score REAL NOT NULL,tier TEXT NOT NULL,explanation TEXT NOT NULL,evidence_json TEXT NOT NULL,imported_item_id TEXT REFERENCES items(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(session_id,work_id));
CREATE INDEX IF NOT EXISTS ai_search_candidate_session ON ai_search_candidates(session_id,tier,score DESC);
CREATE TABLE IF NOT EXISTS search_runs(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES ai_search_sessions(id),plan_json TEXT NOT NULL,state TEXT NOT NULL,
 results_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,completed_at TEXT);
CREATE INDEX IF NOT EXISTS search_runs_session ON search_runs(session_id,created_at);
CREATE TABLE IF NOT EXISTS search_query_runs(
 id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES search_runs(id),source TEXT NOT NULL,query TEXT NOT NULL,
 state TEXT NOT NULL,received INTEGER NOT NULL DEFAULT 0,truncated INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',elapsed_ms INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS search_source_cursors(
 run_id TEXT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
 query_run_id TEXT NOT NULL REFERENCES search_query_runs(id) ON DELETE CASCADE,
 source TEXT NOT NULL,query TEXT NOT NULL,next_offset INTEGER NOT NULL DEFAULT 0,
 page_size INTEGER NOT NULL,has_more INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,
 PRIMARY KEY(run_id,query_run_id));
CREATE INDEX IF NOT EXISTS search_cursors_active ON search_source_cursors(run_id,has_more,source);
CREATE TABLE IF NOT EXISTS search_evaluations(
 run_id TEXT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
 candidate_id TEXT NOT NULL,grade INTEGER NOT NULL CHECK(grade BETWEEN 0 AND 3),
 hard_match TEXT NOT NULL CHECK(hard_match IN ('pass','fail','unknown')),
 note TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,
 PRIMARY KEY(run_id,candidate_id));
CREATE TABLE IF NOT EXISTS search_citation_edges(
 run_id TEXT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
 anchor_id TEXT NOT NULL,related_id TEXT NOT NULL,direction TEXT NOT NULL CHECK(direction IN ('citing','references')),
 source TEXT NOT NULL DEFAULT 'openalex',created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,anchor_id,related_id,direction));
CREATE TABLE IF NOT EXISTS search_reranks(
 run_id TEXT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
 candidate_id TEXT NOT NULL,score REAL NOT NULL CHECK(score BETWEEN 0 AND 1),
 quote TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,candidate_id));
CREATE TABLE IF NOT EXISTS search_candidate_details(
 candidate_id TEXT PRIMARY KEY REFERENCES ai_search_candidates(id) ON DELETE CASCADE,run_id TEXT NOT NULL REFERENCES search_runs(id),data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_decisions(
 session_id TEXT NOT NULL,work_id TEXT NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(session_id,work_id));
CREATE TABLE IF NOT EXISTS article_profiles(
 id TEXT PRIMARY KEY,item_id TEXT NOT NULL UNIQUE REFERENCES items(id) ON DELETE CASCADE,
 fingerprint TEXT NOT NULL,profile_json TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'ready',warning TEXT NOT NULL DEFAULT '',
 version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS article_profile_state ON article_profiles(state,updated_at);
CREATE TABLE IF NOT EXISTS article_profile_evidence(
 id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES article_profiles(id) ON DELETE CASCADE,item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 field TEXT NOT NULL,source_type TEXT NOT NULL,attachment_id TEXT REFERENCES attachments(id) ON DELETE SET NULL,page INTEGER,quote TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS article_profile_evidence_item ON article_profile_evidence(item_id,field);
CREATE TABLE IF NOT EXISTS relation_sessions(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,purpose TEXT NOT NULL,mode TEXT NOT NULL,remote_ai INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'planned',
 job_id TEXT UNIQUE,settings_json TEXT NOT NULL DEFAULT '{}',synthesis_json TEXT NOT NULL DEFAULT '{}',warning TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS relation_session_updated ON relation_sessions(updated_at);
CREATE TABLE IF NOT EXISTS relation_session_items(
 session_id TEXT NOT NULL REFERENCES relation_sessions(id) ON DELETE CASCADE,item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 profile_version INTEGER,ordinal INTEGER NOT NULL,PRIMARY KEY(session_id,item_id));
CREATE TABLE IF NOT EXISTS article_relations(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES relation_sessions(id) ON DELETE CASCADE,left_item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 right_item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,relation_type TEXT NOT NULL,confidence REAL NOT NULL,rationale TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'candidate',
 engine TEXT NOT NULL DEFAULT 'local',user_note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS article_relation_session ON article_relations(session_id,status,confidence DESC);
CREATE TABLE IF NOT EXISTS relation_evidence(
 id TEXT PRIMARY KEY,relation_id TEXT NOT NULL REFERENCES article_relations(id) ON DELETE CASCADE,item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 attachment_id TEXT REFERENCES attachments(id) ON DELETE SET NULL,page INTEGER,profile_evidence_id TEXT REFERENCES article_profile_evidence(id) ON DELETE SET NULL,
 quote TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS relation_evidence_relation ON relation_evidence(relation_id,item_id);
CREATE TABLE IF NOT EXISTS relation_runs(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES relation_sessions(id) ON DELETE CASCADE,
 status TEXT NOT NULL,summary_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,completed_at TEXT);
CREATE INDEX IF NOT EXISTS relation_runs_session ON relation_runs(session_id,created_at DESC);
CREATE TABLE IF NOT EXISTS relation_reviews(
 relation_id TEXT PRIMARY KEY REFERENCES article_relations(id) ON DELETE CASCADE,
 evidence_hash TEXT NOT NULL,review_state TEXT NOT NULL DEFAULT 'current',updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS relation_history(
 id TEXT PRIMARY KEY,relation_id TEXT NOT NULL REFERENCES article_relations(id) ON DELETE CASCADE,
 run_id TEXT NOT NULL REFERENCES relation_runs(id) ON DELETE CASCADE,
 snapshot_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS relation_history_relation ON relation_history(relation_id,created_at DESC);
CREATE TABLE IF NOT EXISTS fulltext_sources(
 id TEXT PRIMARY KEY,item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 candidate_id TEXT REFERENCES ai_search_candidates(id) ON DELETE SET NULL,
 url TEXT NOT NULL,origin TEXT NOT NULL DEFAULT 'manual',resolved_url TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL DEFAULT 'unknown',
 state TEXT NOT NULL DEFAULT 'candidate',attachment_id TEXT REFERENCES attachments(id) ON DELETE SET NULL,
 error TEXT NOT NULL DEFAULT '',checked_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(item_id,url));
CREATE INDEX IF NOT EXISTS fulltext_sources_item ON fulltext_sources(item_id,state,updated_at);
CREATE TABLE IF NOT EXISTS document_chunks(
 id TEXT PRIMARY KEY,item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 attachment_id TEXT REFERENCES attachments(id) ON DELETE CASCADE,source_type TEXT NOT NULL,
 source_id TEXT NOT NULL DEFAULT '',source_version TEXT NOT NULL DEFAULT '',page INTEGER,
 start_offset INTEGER NOT NULL DEFAULT 0,text TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS document_chunks_item ON document_chunks(item_id,source_type,attachment_id,page);
CREATE VIRTUAL TABLE IF NOT EXISTS document_chunk_fts USING fts5(chunk_id UNINDEXED,text,tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS chunk_fts_insert AFTER INSERT ON document_chunks BEGIN
 INSERT INTO document_chunk_fts(chunk_id,text) VALUES(new.id,new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunk_fts_delete AFTER DELETE ON document_chunks BEGIN
 DELETE FROM document_chunk_fts WHERE chunk_id=old.id;
END;
CREATE TABLE IF NOT EXISTS research_conversations(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,item_ids_json TEXT NOT NULL,collection_id TEXT REFERENCES collections(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_messages(
 id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES research_conversations(id) ON DELETE CASCADE,
 role TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'completed',result_json TEXT NOT NULL DEFAULT '{}',
 job_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS research_messages_conversation ON research_messages(conversation_id,created_at);
CREATE TABLE IF NOT EXISTS research_projects(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',archived INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_project_links(
 project_id TEXT NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,entity_type TEXT NOT NULL,
 entity_id TEXT NOT NULL,ordinal INTEGER NOT NULL DEFAULT 0,note TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(project_id,entity_type,entity_id));
CREATE INDEX IF NOT EXISTS research_project_entity ON research_project_links(entity_type,entity_id);
CREATE TABLE IF NOT EXISTS smart_collections(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,rule_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS relation_synthesis_notes(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES relation_sessions(id) ON DELETE CASCADE,note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL,UNIQUE(session_id,note_id));
CREATE TABLE IF NOT EXISTS writing_document_sessions(
 id TEXT PRIMARY KEY,host TEXT NOT NULL,document_fingerprint TEXT NOT NULL,document_name TEXT NOT NULL DEFAULT '',
 style_id TEXT NOT NULL,locale TEXT NOT NULL DEFAULT 'zh-CN',state_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS writing_document_session_updated ON writing_document_sessions(updated_at DESC);
CREATE TABLE IF NOT EXISTS writing_citation_clusters(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES writing_document_sessions(id) ON DELETE CASCADE,
 anchor_key TEXT NOT NULL,ordinal INTEGER NOT NULL,citation_json TEXT NOT NULL,rendered_text TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(session_id,anchor_key));
CREATE INDEX IF NOT EXISTS writing_citation_cluster_session ON writing_citation_clusters(session_id,ordinal);
CREATE TABLE IF NOT EXISTS writing_citation_items(
 cluster_id TEXT NOT NULL REFERENCES writing_citation_clusters(id) ON DELETE CASCADE,item_id TEXT NOT NULL REFERENCES items(id),
 sort_order INTEGER NOT NULL,locator TEXT NOT NULL DEFAULT '',label TEXT NOT NULL DEFAULT 'page',prefix TEXT NOT NULL DEFAULT '',suffix TEXT NOT NULL DEFAULT '',
 suppress_author INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(cluster_id,item_id));
CREATE TABLE IF NOT EXISTS writing_bibliographies(
 session_id TEXT PRIMARY KEY REFERENCES writing_document_sessions(id) ON DELETE CASCADE,anchor_key TEXT NOT NULL DEFAULT '',
 style_id TEXT NOT NULL,rendered_hash TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS writing_events(
 id TEXT PRIMARY KEY,session_id TEXT REFERENCES writing_document_sessions(id) ON DELETE SET NULL,event_type TEXT NOT NULL,
 detail_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS writing_event_session ON writing_events(session_id,created_at DESC);
CREATE TABLE IF NOT EXISTS search_comparisons(
 id TEXT PRIMARY KEY,query TEXT NOT NULL,title TEXT NOT NULL,source TEXT NOT NULL,source_url TEXT NOT NULL DEFAULT '',
 external_json TEXT NOT NULL,local_json TEXT NOT NULL,fields_json TEXT NOT NULL,conclusion TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS search_comparison_updated ON search_comparisons(updated_at DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5(item_id UNINDEXED,content,tokenize='unicode61');
PRAGMA user_version=2;
'''


class Library:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.storage = self.root / 'storage'
        self.storage.mkdir(exist_ok=True)
        self.path = self.root / 'library.sqlite3'
        self.writer = threading.RLock()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            require(version <= 2, '数据库版本比应用更新，请更新应用')
            if version == 1:
                backup_dir = self.root / 'migration-backups'
                backup_dir.mkdir(exist_ok=True)
                backup_path = backup_dir / 'library-before-v2.sqlite3'
                if not backup_path.exists():
                    with closing(sqlite3.connect(backup_path)) as target:
                        db.backup(target)
            db.executescript(SCHEMA)
            if 'role' not in {row[1] for row in db.execute('PRAGMA table_info(attachments)')}:
                db.execute("ALTER TABLE attachments ADD COLUMN role TEXT NOT NULL DEFAULT 'main'")
            db.commit()

    @contextmanager
    def db(self, write=False):
        if write:
            self.writer.acquire()
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
            if write:
                self.writer.release()

    def _row_item(self, row):
        if row is None:
            raise AppError('NOT_FOUND', '文献不存在')
        item = json.loads(row['data'])
        item.update(id=row['id'], revision=row['revision'], createdAt=row['created_at'], updatedAt=row['updated_at'],
                    deletedAt=row['deleted_at'], citationKey=row['citation_key'], starred=bool(row['starred']),
                    readingState=row['reading_state'], authorText=row['authors'], year=row['year'])
        return item

    def _get(self, db, item_id):
        return self._row_item(db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone())

    def get(self, item_id):
        with self.db() as db:
            item = self._get(db, item_id)
            item['attachments'] = [dict(row) for row in db.execute('''SELECT a.id,a.name,a.version,a.role,a.text_status AS textStatus,a.page_count AS pageCount,
              a.position_json AS position,o.bytes,o.mime,o.mode FROM attachments a JOIN objects o ON o.id=a.object_id WHERE a.item_id=? ORDER BY a.created_at''', (item_id,))]
            for attachment in item['attachments']:
                attachment['position'] = json.loads(attachment['position'])
            item['collections'] = [row[0] for row in db.execute('SELECT collection_id FROM collection_items WHERE item_id=?', (item_id,))]
            item['noteCount'] = db.execute('SELECT count(*) FROM notes WHERE item_id=?', (item_id,)).fetchone()[0]
            item['provenance'] = [dict(row) for row in db.execute('SELECT source,created_at AS createdAt FROM provenance WHERE item_id=? ORDER BY created_at DESC LIMIT 20', (item_id,))]
            return item

    def _key(self, db, value, exclude=None):
        key = re.sub(r'[^\w:.-]', '', str(value.get('citationKey') or ''), flags=re.UNICODE)
        if not key:
            first = (value.get('author') or [{}])[0]
            base = first.get('family') or first.get('literal') or 'reference'
            words = re.findall(r'\w+', norm(value['title']))
            key = re.sub(r'[^\w]', '', norm(base)) + (year_of(value) or 'nd') + (words[0] if words else '')
        key = key[:160] or 'reference'
        original, suffix = key, 1
        while db.execute('SELECT 1 FROM items WHERE citation_key=? AND id<>?', (key, exclude or '')).fetchone():
            key, suffix = original + str(suffix), suffix + 1
        return key

    def _tags(self, db, item_id, tags):
        db.execute('DELETE FROM item_tags WHERE item_id=?', (item_id,))
        for tag in tags:
            row = db.execute('SELECT id FROM tags WHERE name=?', (tag,)).fetchone()
            tag_id = row[0] if row else uid()
            db.execute('INSERT OR IGNORE INTO tags(id,name) VALUES(?,?)', (tag_id, tag))
            db.execute('INSERT OR IGNORE INTO item_tags VALUES(?,?)', (item_id, tag_id))

    def _index(self, db, item_id):
        item = self._get(db, item_id)
        notes = '\n'.join(row[0] + '\n' + row[1] for row in db.execute('SELECT title,content FROM notes WHERE item_id=?', (item_id,)))
        pages = '\n'.join(row[0] or '' for row in db.execute('SELECT text_json FROM attachments WHERE item_id=?', (item_id,)))
        card = db.execute('SELECT data FROM reading_cards WHERE item_id=?', (item_id,)).fetchone()
        card_text = ' '.join(str(value) for value in json.loads(card[0]).values()) if card else ''
        terms = '\n'.join('\n'.join(str(row[key]) for key in ('term', 'translation', 'explanation')) for row in db.execute('SELECT term,translation,explanation FROM terms WHERE item_id=?', (item_id,)))
        content = ' '.join([item['title'], item['authorText'], item.get('abstract', ''), item.get('container-title', ''),
                            str(item.get('DOI', '')), str(item.get('PMID', '')), ' '.join(item.get('tags', [])), notes, pages, card_text, terms])
        db.execute('DELETE FROM item_fts WHERE item_id=?', (item_id,))
        db.execute('INSERT INTO item_fts(item_id,content) VALUES(?,?)', (item_id, norm(content)))
        # Profiles and relationship maps are derived from this local material.
        # Marking them stale keeps old AI interpretations from looking current
        # after an item, card, note, term, or PDF index changes.
        db.execute("UPDATE article_profiles SET state='stale',updated_at=? WHERE item_id=?", (now(), item_id))
        db.execute("""UPDATE relation_sessions SET state='stale',updated_at=?
            WHERE state NOT IN ('running','queued') AND id IN
            (SELECT session_id FROM relation_session_items WHERE item_id=?)""", (now(), item_id))

    def _create(self, db, data, collection_id=None, source='manual'):
        value = normalize(data)
        item_id, timestamp = uid(), now()
        value['citationKey'] = self._key(db, value)
        db.execute('''INSERT INTO items(id,data,title,title_norm,authors,year,doi,container,reading_state,starred,citation_key,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', (item_id, dumps(value), value['title'], norm(value['title']), authors_text(value['author']),
            year_of(value), doi(value.get('DOI')), value.get('container-title', ''), value['readingState'], int(bool(value.get('starred'))), value['citationKey'], timestamp, timestamp))
        self._tags(db, item_id, value['tags'])
        if collection_id:
            require(db.execute('SELECT 1 FROM collections WHERE id=?', (collection_id,)).fetchone(), '集合不存在')
            db.execute('INSERT INTO collection_items VALUES(?,?)', (collection_id, item_id))
        db.execute('INSERT INTO provenance VALUES(?,?,?,?,?)', (uid(), item_id, source, dumps(data), timestamp))
        self._index(db, item_id)
        return item_id

    def create(self, data, collection_id=None, source='manual'):
        with self.db(True) as db:
            item_id = self._create(db, data, collection_id, source)
        return self.get(item_id)

    def _update(self, db, item_id, patch, revision):
        current = self._get(db, item_id)
        if revision != current['revision']:
            raise AppError('REVISION_CONFLICT', '文献已更新，请重新打开后再编辑', {'current': current})
        forbidden = {'id', 'revision', 'createdAt', 'updatedAt', 'deletedAt', 'attachments', 'provenance', 'collections', 'noteCount', 'authorText'}
        require(not (set(patch) & forbidden), '包含不可编辑字段')
        value = normalize({**json.loads(db.execute('SELECT data FROM items WHERE id=?', (item_id,)).fetchone()[0]), **patch})
        value['citationKey'] = str(patch.get('citationKey', current['citationKey'])).strip()
        require(bool(re.fullmatch(r'[\w:.-]{1,160}', value['citationKey'])), '引用键只允许字母、数字、下划线、点、冒号和连字符')
        conflict = db.execute('SELECT id FROM items WHERE citation_key=? AND id<>?', (value['citationKey'], item_id)).fetchone()
        require(not conflict, '引用键已经存在，请使用其他引用键')
        db.execute('''UPDATE items SET data=?,title=?,title_norm=?,authors=?,year=?,doi=?,container=?,reading_state=?,starred=?,
            citation_key=?,revision=revision+1,updated_at=? WHERE id=?''', (dumps(value), value['title'], norm(value['title']), authors_text(value['author']),
            year_of(value), doi(value.get('DOI')), value.get('container-title', ''), value['readingState'], int(bool(value.get('starred'))), value['citationKey'], now(), item_id))
        self._tags(db, item_id, value['tags'])
        self._index(db, item_id)

    def update(self, item_id, patch, revision):
        with self.db(True) as db:
            self._update(db, item_id, patch, revision)
            db.execute('INSERT INTO provenance VALUES(?,?,?,?,?)', (uid(), item_id, 'manual edit', dumps(patch), now()))
        return self.get(item_id)

    def bulk(self, ids, action, value=None):
        require(isinstance(ids, list) and 0 < len(ids) <= 5000, '请选择1–5000条文献')
        with self.db(True) as db:
            for item_id in dict.fromkeys(ids):
                item = self._get(db, item_id)
                if action in ('trash', 'restore'):
                    if action == 'restore':
                        require(item['deletedAt'] is not None, '文献不在回收站')
                        for event in db.execute('SELECT data FROM merge_events WHERE undone=0'):
                            require(item_id not in json.loads(event['data']).get('sourceIds', []),
                                    '该条目已合并；请在“重复条目”中撤销合并，不能单独恢复')
                    db.execute('UPDATE items SET deleted_at=?,revision=revision+1,updated_at=? WHERE id=?', (now() if action == 'trash' else None, now(), item_id))
                elif action == 'collection':
                    require(db.execute('SELECT 1 FROM collections WHERE id=?', (value,)).fetchone(), '集合不存在')
                    db.execute('INSERT OR IGNORE INTO collection_items VALUES(?,?)', (value, item_id))
                    db.execute('UPDATE items SET revision=revision+1,updated_at=? WHERE id=?', (now(), item_id))
                elif action == 'removeCollection':
                    db.execute('DELETE FROM collection_items WHERE collection_id=? AND item_id=?', (value, item_id))
                    db.execute('UPDATE items SET revision=revision+1,updated_at=? WHERE id=?', (now(), item_id))
                elif action in ('readingState', 'starred', 'tags'):
                    self._update(db, item_id, {action: value}, item['revision'])
                elif action == 'addTags':
                    require(isinstance(value, list), '标签应为列表')
                    self._update(db, item_id, {'tags': list(dict.fromkeys(item.get('tags', []) + value))}, item['revision'])
                else:
                    raise AppError('INVALID_ARGUMENT', '不支持的批量操作')
        return {'updated': len(set(ids))}

    def delete_permanently(self, ids=None):
        """Purge only trashed records; unlink owned files after a committed database deletion."""
        if ids is not None:
            require(isinstance(ids, list) and 0 < len(ids) <= 5000, '请选择1–5000条回收站文献')
            ids = list(dict.fromkeys(ids))
        owned_paths = []
        with self.db(True) as db:
            if ids is None:
                ids = [row[0] for row in db.execute('SELECT id FROM items WHERE deleted_at IS NOT NULL')]
            if not ids:
                return {'deleted': 0, 'removedFiles': 0, 'fileWarnings': []}
            placeholders = ','.join('?' for _ in ids)
            rows = db.execute(f'SELECT id FROM items WHERE id IN ({placeholders}) AND deleted_at IS NOT NULL', ids).fetchall()
            require(len(rows) == len(ids), '只能永久删除回收站中的文献')
            attachment_ids = [row[0] for row in db.execute(f'SELECT id FROM attachments WHERE item_id IN ({placeholders})', ids)]
            object_ids = [row[0] for row in db.execute(f'SELECT DISTINCT object_id FROM attachments WHERE item_id IN ({placeholders})', ids)]
            note_ids = [row[0] for row in db.execute(f'SELECT id FROM notes WHERE item_id IN ({placeholders})', ids)]
            cluster_ids = [row[0] for row in db.execute(f'SELECT DISTINCT cluster_id FROM writing_citation_items WHERE item_id IN ({placeholders})', ids)]
            for table in ('collection_items', 'item_tags', 'provenance', 'import_entries', 'terms',
                          'reading_cards', 'reading_sessions', 'assistant_runs', 'fulltext_sources',
                          'document_chunks', 'relation_evidence', 'article_profile_evidence',
                          'relation_session_items', 'research_project_links'):
                column = 'entity_id' if table == 'research_project_links' else 'item_id'
                extra = " AND entity_type='item'" if table == 'research_project_links' else ''
                db.execute(f'DELETE FROM {table} WHERE {column} IN ({placeholders}){extra}', ids)
            db.execute(f'DELETE FROM item_fts WHERE item_id IN ({placeholders})', ids)
            db.execute(f'DELETE FROM relation_discoveries WHERE anchor_item_id IN ({placeholders})', ids)
            db.execute(f'UPDATE relation_discoveries SET imported_item_id=NULL WHERE imported_item_id IN ({placeholders})', ids)
            db.execute(f'UPDATE ai_search_candidates SET imported_item_id=NULL WHERE imported_item_id IN ({placeholders})', ids)
            db.execute(f'DELETE FROM relation_discovery_decisions WHERE anchor_item_id IN ({placeholders})', ids)
            db.execute(f'DELETE FROM relation_discovery_pages WHERE anchor_item_id IN ({placeholders})', ids)
            for table in ('manual_relations', 'article_relations'):
                db.execute(f'DELETE FROM {table} WHERE left_item_id IN ({placeholders}) OR right_item_id IN ({placeholders})', ids * 2)
            db.execute(f'DELETE FROM writing_citation_items WHERE item_id IN ({placeholders})', ids)
            for cluster_id in cluster_ids:
                cluster = db.execute('SELECT session_id FROM writing_citation_clusters WHERE id=?', (cluster_id,)).fetchone()
                if cluster:
                    if db.execute('SELECT 1 FROM writing_citation_items WHERE cluster_id=?', (cluster_id,)).fetchone():
                        db.execute("UPDATE writing_citation_clusters SET rendered_text='' WHERE id=?", (cluster_id,))
                    else:
                        db.execute('DELETE FROM writing_citation_clusters WHERE id=?', (cluster_id,))
                    db.execute("UPDATE writing_bibliographies SET rendered_hash='' WHERE session_id=?", (cluster['session_id'],))
            if note_ids:
                note_marks = ','.join('?' for _ in note_ids)
                db.execute(f'DELETE FROM relation_synthesis_notes WHERE note_id IN ({note_marks})', note_ids)
                db.execute(f'DELETE FROM note_history WHERE note_id IN ({note_marks})', note_ids)
            db.execute(f'DELETE FROM notes WHERE item_id IN ({placeholders})', ids)
            if attachment_ids:
                marks = ','.join('?' for _ in attachment_ids)
                for table in ('terms', 'reading_sessions', 'assistant_runs'):
                    db.execute(f'UPDATE {table} SET attachment_id=NULL WHERE attachment_id IN ({marks})', attachment_ids)
                db.execute(f'DELETE FROM annotations WHERE attachment_id IN ({marks})', attachment_ids)
            db.execute(f'DELETE FROM attachments WHERE item_id IN ({placeholders})', ids)
            db.execute(f'DELETE FROM article_profiles WHERE item_id IN ({placeholders})', ids)
            for row in db.execute('SELECT id,item_ids_json FROM research_conversations').fetchall():
                original = json.loads(row['item_ids_json'])
                updated = [item_id for item_id in original if item_id not in ids]
                if updated != original:
                    db.execute('UPDATE research_conversations SET item_ids_json=?,updated_at=? WHERE id=?',
                               (dumps(updated), now(), row['id']))
            for event in db.execute('SELECT id,data FROM merge_events').fetchall():
                snapshot = json.loads(event['data'])
                if snapshot.get('targetId') in ids or set(snapshot.get('sourceIds', [])) & set(ids):
                    db.execute('DELETE FROM merge_events WHERE id=?', (event['id'],))
            db.execute(f'DELETE FROM items WHERE id IN ({placeholders})', ids)
            for object_id in object_ids:
                if db.execute('SELECT 1 FROM attachments WHERE object_id=?', (object_id,)).fetchone():
                    continue
                obj = db.execute('SELECT path,mode FROM objects WHERE id=?', (object_id,)).fetchone()
                if obj:
                    db.execute('DELETE FROM objects WHERE id=?', (object_id,))
                    path = Path(obj['path']).resolve()
                    if obj['mode'] == 'managed' and path.is_relative_to(self.storage.resolve()) and path != self.storage.resolve():
                        owned_paths.append(path)
            require(db.execute('PRAGMA foreign_key_check').fetchone() is None, '删除后数据库关联校验失败')
        warnings, removed = [], 0
        for path in set(owned_paths):
            with self.db() as db:
                still_used = db.execute('SELECT 1 FROM objects WHERE path=?', (str(path),)).fetchone()
            if still_used:
                continue
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                warnings.append({'path': str(path), 'error': str(exc)[:200]})
        return {'deleted': len(ids), 'removedFiles': removed, 'fileWarnings': warnings}

    def query(self, params):
        limit = bounded_int(params.get('limit'), 100, 1, 500)
        offset = bounded_int(params.get('offset'), 0, 0, 10000000)
        args, where = [], ['i.deleted_at IS ' + ('NOT NULL' if params.get('view') == 'trash' else 'NULL')]
        q = norm(params.get('q'))[:1000]
        field = params.get('field', 'all')
        if q:
            if field in ('title', 'authors', 'doi', 'container'):
                where.append(f'lower(i.{field}) LIKE ? ESCAPE \'\\\'')
                args.append('%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
            elif re.search(r'[\u3400-\u9fff]', q):
                where.append('i.id IN (SELECT item_id FROM item_fts WHERE content LIKE ? ESCAPE \'\\\')')
                args.append('%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
            else:
                terms = re.findall(r'\w+', q)
                if terms:
                    where.append('i.id IN (SELECT item_id FROM item_fts WHERE item_fts MATCH ?)')
                    args.append(' AND '.join('"' + t.replace('"', '""') + '"*' for t in terms))
        if params.get('collectionId'):
            where.append('i.id IN (SELECT item_id FROM collection_items WHERE collection_id=?)')
            args.append(params['collectionId'])
        if params.get('collectionIds'):
            collection_ids = params['collectionIds']
            require(isinstance(collection_ids, list) and 1 <= len(collection_ids) <= 50 and
                    all(isinstance(value, str) and len(value) <= 100 for value in collection_ids), '集合范围不正确')
            where.append('i.id IN (SELECT item_id FROM collection_items WHERE collection_id IN (' +
                         ','.join('?' for _ in collection_ids) + '))')
            args.extend(collection_ids)
        if params.get('advanced'):
            advanced_sql, advanced_args = compile_rule(params['advanced'])
            where.append(advanced_sql)
            args.extend(advanced_args)
        if params.get('tag'):
            where.append('i.id IN (SELECT item_id FROM item_tags JOIN tags ON tags.id=item_tags.tag_id WHERE name=?)')
            args.append(params['tag'])
        if params.get('readingState') or params.get('view') == 'unread':
            where.append('reading_state=?')
            args.append(params.get('readingState') or 'unread')
        if params.get('view') == 'starred':
            where.append('starred=1')
        if params.get('year'):
            where.append('year=?')
            args.append(str(params['year']))
        if params.get('hasAttachment') in ('yes', 'no'):
            where.append(('NOT ' if params['hasAttachment'] == 'no' else '') + 'EXISTS (SELECT 1 FROM attachments a WHERE a.item_id=i.id)')
        if params.get('ids') is not None:
            ids = params['ids']
            require(isinstance(ids, list) and len(ids) <= 10000, '条目列表过长')
            where.append('i.id IN (' + ','.join('?' for _ in ids) + ')')
            args.extend(ids)
        columns = {'title': 'title_norm', 'authors': 'authors', 'year': 'year', 'container': 'container', 'created': 'created_at', 'updated': 'updated_at'}
        order = columns.get(params.get('sort'), 'created_at')
        direction = 'ASC' if params.get('direction') == 'asc' else 'DESC'
        clause = ' AND '.join(where)
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM items i WHERE ' + clause, args).fetchone()[0]
            rows = db.execute(f'''SELECT i.*, (SELECT count(*) FROM attachments WHERE item_id=i.id) AS attachment_count,
                (SELECT a.id FROM attachments a JOIN objects o ON o.id=a.object_id WHERE a.item_id=i.id AND o.mime='application/pdf' ORDER BY a.created_at LIMIT 1) AS pdf_id
                FROM items i WHERE {clause} ORDER BY {order} {direction},i.id ASC LIMIT ? OFFSET ?''', [*args, limit, offset]).fetchall()
            items = []
            for row in rows:
                item = self._row_item(row)
                item.update(attachmentCount=row['attachment_count'], pdfId=row['pdf_id'])
                items.append(item)
            return {'items': items, 'total': total, 'offset': offset, 'hasMore': offset + len(items) < total}

    def collections(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('''SELECT c.id,c.name,c.parent_id AS parentId,
               (SELECT count(*) FROM collection_items ci JOIN items i ON i.id=ci.item_id WHERE ci.collection_id=c.id AND i.deleted_at IS NULL) AS count
               FROM collections c ORDER BY c.created_at,c.name''')]

    def collection_edit(self, action, data):
        with self.db(True) as db:
            if action == 'create':
                name = str(data.get('name', '')).strip()
                require(0 < len(name) <= 200, '集合名称长度应为1–200个字符')
                parent = data.get('parentId') or None
                if parent:
                    require(db.execute('SELECT 1 FROM collections WHERE id=?', (parent,)).fetchone(), '父集合不存在')
                collection_id = uid()
                db.execute('INSERT INTO collections VALUES(?,?,?,?)', (collection_id, name, parent, now()))
                return {'id': collection_id}
            collection_id = data.get('id')
            current = db.execute('SELECT * FROM collections WHERE id=?', (collection_id,)).fetchone()
            require(current, '集合不存在')
            if action == 'rename':
                name = str(data.get('name', '')).strip()
                require(0 < len(name) <= 200, '集合名称长度应为1–200个字符')
                db.execute('UPDATE collections SET name=? WHERE id=?', (name, collection_id))
            elif action == 'delete':
                db.execute('UPDATE collections SET parent_id=? WHERE parent_id=?', (current['parent_id'], collection_id))
                db.execute('DELETE FROM collections WHERE id=?', (collection_id,))
            elif action == 'move':
                parent = data.get('parentId') or None
                ancestor, seen = parent, {collection_id}
                while ancestor:
                    require(ancestor not in seen, '集合不能移动到自身或子集合中')
                    seen.add(ancestor)
                    row = db.execute('SELECT parent_id FROM collections WHERE id=?', (ancestor,)).fetchone()
                    require(row, '父集合不存在')
                    ancestor = row[0]
                db.execute('UPDATE collections SET parent_id=? WHERE id=?', (parent, collection_id))
            else:
                raise AppError('INVALID_ARGUMENT', '集合操作不支持')
        return {'ok': True}

    def stats(self):
        with self.db() as db:
            counts = {key: db.execute(query).fetchone()[0] for key, query in {
                'items': 'SELECT count(*) FROM items WHERE deleted_at IS NULL',
                'unread': "SELECT count(*) FROM items WHERE deleted_at IS NULL AND reading_state='unread'",
                'starred': 'SELECT count(*) FROM items WHERE deleted_at IS NULL AND starred=1',
                'trash': 'SELECT count(*) FROM items WHERE deleted_at IS NOT NULL',
                'attachments': 'SELECT count(*) FROM attachments', 'notes': 'SELECT count(*) FROM notes',
                'annotations': 'SELECT count(*) FROM annotations', 'terms': 'SELECT count(*) FROM terms',
                'readingCards': 'SELECT count(*) FROM reading_cards', 'assistantRuns': 'SELECT count(*) FROM assistant_runs',
                'aiSearchSessions': 'SELECT count(*) FROM ai_search_sessions', 'aiSearchCandidates': 'SELECT count(*) FROM ai_search_candidates',
                'relationSessions': 'SELECT count(*) FROM relation_sessions', 'articleRelations': 'SELECT count(*) FROM article_relations',
                'storageBytes': 'SELECT coalesce(sum(bytes),0) FROM objects'}.items()}
            counts['tags'] = [dict(row) for row in db.execute('SELECT t.name,t.color,count(*) AS count FROM tags t JOIN item_tags it ON it.tag_id=t.id JOIN items i ON i.id=it.item_id WHERE i.deleted_at IS NULL GROUP BY t.id ORDER BY t.name')]
            counts['years'] = [row[0] for row in db.execute("SELECT DISTINCT year FROM items WHERE year<>'' AND deleted_at IS NULL ORDER BY year DESC")]
            counts['root'] = str(self.root)
            return counts

    def notes_list(self, item_id=None, query=''):
        where, args = [], []
        if item_id:
            where.append('n.item_id=?')
            args.append(item_id)
        if query:
            where.append("(n.title LIKE ? OR n.content LIKE ? OR n.tags LIKE ?)")
            args.extend(['%' + query + '%'] * 3)
        with self.db() as db:
            return [dict(row) for row in db.execute('''SELECT n.id,n.item_id AS itemId,n.title,n.content,n.tags,n.revision,n.updated_at AS updatedAt,
               i.title AS itemTitle FROM notes n LEFT JOIN items i ON i.id=n.item_id''' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY n.updated_at DESC LIMIT 2000', args)]

    def note_save(self, data):
        content = str(data.get('content') or '')
        require(len(content) <= 4_000_000, '笔记内容过大，请拆分')
        title = str(data.get('title') or '未命名笔记')[:500]
        item_id, note_id = data.get('itemId') or None, data.get('id') or uid()
        tags = data.get('tags', [])
        require(isinstance(tags, list), '标签格式不正确')
        with self.db(True) as db:
            old = db.execute('SELECT * FROM notes WHERE id=?', (note_id,)).fetchone()
            if old:
                if data.get('revision') != old['revision']:
                    raise AppError('REVISION_CONFLICT', '笔记已有更新；当前编辑内容已保留，请重新载入后合并', {'current': dict(old)})
                db.execute('INSERT OR IGNORE INTO note_history VALUES(?,?,?,?,?,?)', (note_id, old['revision'], old['title'], old['content'], old['tags'], now()))
                revision = old['revision'] + 1
                db.execute('UPDATE notes SET title=?,content=?,tags=?,revision=?,updated_at=? WHERE id=?', (title, content, dumps(tags), revision, now(), note_id))
                item_id = old['item_id']
            else:
                if item_id:
                    self._get(db, item_id)
                revision = 1
                db.execute('INSERT INTO notes VALUES(?,?,?,?,?,?,?,?)', (note_id, item_id, title, content, dumps(tags), revision, now(), now()))
            if item_id:
                self._index(db, item_id)
        return {'id': note_id, 'revision': revision, 'itemId': item_id, 'title': title, 'content': content, 'tags': tags}

    def note_history(self, note_id):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM note_history WHERE note_id=? ORDER BY revision DESC', (note_id,))]

    def notes_delete(self, note_id):
        with self.db(True) as db:
            old = db.execute('SELECT * FROM notes WHERE id=?', (note_id,)).fetchone()
            require(old, '笔记不存在')
            db.execute('INSERT OR IGNORE INTO note_history VALUES(?,?,?,?,?,?)', (note_id, old['revision'], old['title'], old['content'], old['tags'], now()))
            db.execute('DELETE FROM notes WHERE id=?', (note_id,))
            if old['item_id']:
                self._index(db, old['item_id'])
        return {'ok': True}

    def _reading_session(self, db, item_id, attachment_id=None):
        self._get(db, item_id)
        row = db.execute('SELECT * FROM reading_sessions WHERE item_id=?', (item_id,)).fetchone()
        if not row:
            session_id, timestamp = uid(), now()
            db.execute('''INSERT INTO reading_sessions(id,item_id,attachment_id,created_at,updated_at)
                VALUES(?,?,?,?,?)''', (session_id, item_id, attachment_id, timestamp, timestamp))
            row = db.execute('SELECT * FROM reading_sessions WHERE id=?', (session_id,)).fetchone()
        elif attachment_id and row['attachment_id'] != attachment_id:
            db.execute('UPDATE reading_sessions SET attachment_id=?,updated_at=? WHERE id=?', (attachment_id, now(), row['id']))
            row = db.execute('SELECT * FROM reading_sessions WHERE id=?', (row['id'],)).fetchone()
        return row

    @staticmethod
    def _reading_session_value(row):
        return {**dict(row), 'itemId': row['item_id'], 'attachmentId': row['attachment_id'], 'targetMinutes': row['target_minutes'],
                'secondsRead': row['seconds_read'], 'lastPage': row['last_page'], 'pageCount': row['page_count'],
                'createdAt': row['created_at'], 'updatedAt': row['updated_at'], 'startedAt': row['started_at'], 'completedAt': row['completed_at']}

    @staticmethod
    def _card_value(row):
        value = json.loads(row['data'])
        return {**value, 'id': row['id'], 'itemId': row['item_id'], 'revision': row['revision'], 'createdAt': row['created_at'], 'updatedAt': row['updated_at']}

    def reading_get(self, item_id, attachment_id=None):
        with self.db(True) as db:
            session = self._reading_session(db, item_id, attachment_id)
            card = db.execute('SELECT * FROM reading_cards WHERE item_id=?', (item_id,)).fetchone()
            if not card:
                timestamp = now()
                db.execute('INSERT INTO reading_cards(id,item_id,data,created_at,updated_at) VALUES(?,?,?,?,?)',
                           (uid(), item_id, dumps({'researchQuestion': '', 'methods': '', 'findings': '', 'limitations': '', 'conclusion': '', 'summary': ''}), timestamp, timestamp))
                card = db.execute('SELECT * FROM reading_cards WHERE item_id=?', (item_id,)).fetchone()
            return {'session': self._reading_session_value(session), 'card': self._card_value(card)}

    def reading_save(self, data):
        item_id = data.get('itemId')
        require(isinstance(item_id, str), '缺少文献标识')
        session_patch, card_patch = data.get('session') or {}, data.get('card') or {}
        require(isinstance(session_patch, dict) and isinstance(card_patch, dict), '阅读数据格式不正确')
        allowed_session = {'status', 'goal', 'targetMinutes', 'revision'}
        allowed_card = {'researchQuestion', 'methods', 'findings', 'limitations', 'conclusion', 'summary', 'revision'}
        require(set(session_patch) <= allowed_session and set(card_patch) <= allowed_card, '阅读字段不支持')
        with self.db(True) as db:
            session = self._reading_session(db, item_id, data.get('attachmentId'))
            if session_patch:
                require(session_patch.get('revision', session['revision']) == session['revision'], '阅读进度已更新，请重新载入')
                status = session_patch.get('status', session['status'])
                require(status in ('planned', 'reading', 'completed'), '阅读状态不正确')
                goal = str(session_patch.get('goal', session['goal'])).strip()[:1000]
                target = bounded_int(session_patch.get('targetMinutes', session['target_minutes']), session['target_minutes'], 1, 100000)
                timestamp = now()
                db.execute('''UPDATE reading_sessions SET status=?,goal=?,target_minutes=?,revision=revision+1,updated_at=?,
                    started_at=coalesce(started_at,?),completed_at=? WHERE id=?''',
                    (status, goal, target, timestamp, timestamp if status in ('reading', 'completed') else None, timestamp if status == 'completed' else None, session['id']))
            card = db.execute('SELECT * FROM reading_cards WHERE item_id=?', (item_id,)).fetchone()
            if card_patch:
                require(card_patch.get('revision', card['revision']) == card['revision'], '阅读卡已更新，请重新载入')
                value = json.loads(card['data'])
                for key in allowed_card - {'revision'}:
                    if key in card_patch:
                        value[key] = str(card_patch[key])[:40000]
                db.execute('UPDATE reading_cards SET data=?,revision=revision+1,updated_at=? WHERE id=?', (dumps(value), now(), card['id']))
                self._index(db, item_id)
            session = db.execute('SELECT * FROM reading_sessions WHERE item_id=?', (item_id,)).fetchone()
            card = db.execute('SELECT * FROM reading_cards WHERE item_id=?', (item_id,)).fetchone()
            return {'session': self._reading_session_value(session), 'card': self._card_value(card)}

    def reading_activity(self, data):
        item_id = data.get('itemId')
        require(isinstance(item_id, str), '缺少文献标识')
        seconds = bounded_int(data.get('elapsedSeconds'), 0, 0, 3600)
        page = bounded_int(data.get('page'), 1, 1, 100000)
        page_count = bounded_int(data.get('pageCount'), 1, 1, 100000)
        with self.db(True) as db:
            session = self._reading_session(db, item_id, data.get('attachmentId'))
            status = 'reading' if session['status'] == 'planned' else session['status']
            timestamp = now()
            db.execute('''UPDATE reading_sessions SET seconds_read=seconds_read+?,last_page=?,page_count=?,status=?,updated_at=?,
                started_at=coalesce(started_at,?) WHERE id=?''', (seconds, page, page_count, status, timestamp, timestamp, session['id']))
            row = db.execute('SELECT * FROM reading_sessions WHERE id=?', (session['id'],)).fetchone()
            return self._reading_session_value(row)

    def terms_list(self, item_id, query=''):
        require(isinstance(item_id, str), '缺少文献标识')
        q = str(query or '').strip()[:300]
        with self.db() as db:
            rows = db.execute('''SELECT id,item_id AS itemId,attachment_id AS attachmentId,term,translation,explanation,source_json AS source,
                tags,starred,revision,created_at AS createdAt,updated_at AS updatedAt FROM terms WHERE item_id=?
                AND (?='' OR term LIKE ? OR translation LIKE ? OR explanation LIKE ?) ORDER BY starred DESC,updated_at DESC LIMIT 500''',
                (item_id, q, '%' + q + '%', '%' + q + '%', '%' + q + '%')).fetchall()
            return [{**dict(row), 'source': json.loads(row['source']), 'tags': json.loads(row['tags']), 'starred': bool(row['starred'])} for row in rows]

    def terms_save(self, data):
        item_id, term_id = data.get('itemId'), data.get('id') or uid()
        term = str(data.get('term') or '').strip()
        require(isinstance(item_id, str) and 0 < len(term) <= 160, '术语长度应为1–160个字符')
        translation, explanation = str(data.get('translation') or '')[:1000], str(data.get('explanation') or '')[:20000]
        source, tags = data.get('source') or {}, data.get('tags') or []
        require(isinstance(source, dict) and isinstance(tags, list) and len(tags) <= 30, '术语格式不正确')
        clean_tags = [str(value).strip()[:80] for value in tags if str(value).strip()]
        with self.db(True) as db:
            self._get(db, item_id)
            old = db.execute('SELECT * FROM terms WHERE id=?', (term_id,)).fetchone()
            if old:
                require(old['item_id'] == item_id and data.get('revision') == old['revision'], '术语已更新，请重新载入')
                revision = old['revision'] + 1
                db.execute('''UPDATE terms SET term=?,translation=?,explanation=?,source_json=?,tags=?,starred=?,revision=?,updated_at=? WHERE id=?''',
                    (term, translation, explanation, dumps(source), dumps(clean_tags), int(bool(data.get('starred'))), revision, now(), term_id))
            else:
                existing = db.execute('SELECT id FROM terms WHERE item_id=? AND term=?', (item_id, term)).fetchone()
                require(not existing, '此文献中已有相同术语')
                revision = 1
                db.execute('''INSERT INTO terms(id,item_id,attachment_id,term,translation,explanation,source_json,tags,starred,revision,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (term_id, item_id, data.get('attachmentId') or None, term, translation, explanation, dumps(source), dumps(clean_tags), int(bool(data.get('starred'))), revision, now(), now()))
            self._index(db, item_id)
            row = db.execute('SELECT * FROM terms WHERE id=?', (term_id,)).fetchone()
            return {**dict(row), 'itemId': row['item_id'], 'attachmentId': row['attachment_id'], 'source': json.loads(row['source_json']), 'tags': json.loads(row['tags']), 'starred': bool(row['starred'])}

    def terms_delete(self, term_id, revision):
        with self.db(True) as db:
            row = db.execute('SELECT * FROM terms WHERE id=?', (term_id,)).fetchone()
            require(row and row['revision'] == revision, '术语已更新或不存在')
            db.execute('DELETE FROM terms WHERE id=?', (term_id,))
            self._index(db, row['item_id'])
        return {'ok': True}

    def duplicates(self):
        with self.db() as db:
            groups = {}
            for row in db.execute('SELECT * FROM items WHERE deleted_at IS NULL ORDER BY created_at'):
                key = ('doi:' + row['doi']) if row['doi'] else 'title:' + row['title_norm'] + ':' + row['year']
                groups.setdefault(key, []).append(self._row_item(row))
            return [{'key': key, 'items': items, 'basis': 'DOI' if key.startswith('doi:') else '标题与年份（需人工核对）'} for key, items in groups.items() if len(items) > 1][:500]

    def merge(self, target_id, source_ids):
        require(isinstance(source_ids, list) and 0 < len(source_ids) <= 50 and target_id not in source_ids, '请选择需要合并的其他条目')
        source_ids = list(dict.fromkeys(source_ids))
        ids = [target_id, *source_ids]
        placeholders = ','.join('?' for _ in ids)
        with self.db(True) as db:
            direct = ('attachments', 'notes', 'provenance', 'assistant_runs', 'document_chunks',
                      'relation_evidence', 'article_profile_evidence', 'import_entries')
            snapshot = {table: [dict(row) for row in db.execute(f'SELECT * FROM {table} WHERE {field} IN ({placeholders})', ids)] for table, field in [
                ('items', 'id'), ('attachments', 'item_id'), ('notes', 'item_id'), ('collection_items', 'item_id'),
                ('item_tags', 'item_id'), ('provenance', 'item_id'), ('assistant_runs', 'item_id'),
                ('document_chunks', 'item_id'), ('relation_evidence', 'item_id'),
                ('article_profile_evidence', 'item_id'), ('import_entries', 'item_id'),
                ('terms', 'item_id'), ('reading_cards', 'item_id'), ('reading_sessions', 'item_id'),
                ('fulltext_sources', 'item_id'), ('relation_session_items', 'item_id'),
                ('writing_citation_items', 'item_id'), ('article_profiles', 'item_id'),
                ('ai_search_candidates', 'imported_item_id'), ('research_project_links', 'entity_id')]}
            profile_ids = [record['id'] for record in snapshot['article_profiles']]
            if profile_ids:
                profile_marks = ','.join('?' for _ in profile_ids)
                known = {record['id'] for record in snapshot['article_profile_evidence']}
                snapshot['article_profile_evidence'].extend(dict(row) for row in db.execute(
                    f'SELECT * FROM article_profile_evidence WHERE profile_id IN ({profile_marks})', profile_ids)
                    if row['id'] not in known)
            for table in ('manual_relations', 'article_relations'):
                snapshot[table] = [dict(row) for row in db.execute(f'''SELECT * FROM {table}
                    WHERE left_item_id IN ({placeholders}) OR right_item_id IN ({placeholders})''', ids * 2)]
            for table in ('relation_discoveries', 'relation_discovery_decisions', 'relation_discovery_pages'):
                snapshot[table] = [dict(row) for row in db.execute(f'SELECT * FROM {table} WHERE anchor_item_id IN ({placeholders})', ids)]
            snapshot['relation_discovery_imports'] = [dict(row) for row in db.execute(
                f'SELECT session_id,anchor_item_id,work_id,direction,imported_item_id FROM relation_discoveries WHERE imported_item_id IN ({placeholders})', ids)]
            snapshot['research_conversations'] = [dict(row) for row in db.execute('SELECT * FROM research_conversations')
                                                  if any(item_id in json.loads(row['item_ids_json']) for item_id in ids)]
            require(len(snapshot['items']) == len(ids), '合并条目不存在')
            require(all(row['deleted_at'] is None for row in snapshot['items']), '回收站条目不能合并')
            target = self._get(db, target_id)
            combined_tags = list(target.get('tags', []))
            affected_sessions = set()
            for source_id in source_ids:
                source = self._get(db, source_id)
                combined_tags.extend(source.get('tags', []))
                for table in direct:
                    db.execute(f'UPDATE {table} SET item_id=? WHERE item_id=?', (target_id, source_id))
                db.execute('UPDATE ai_search_candidates SET imported_item_id=? WHERE imported_item_id=?', (target_id, source_id))
                db.execute('UPDATE relation_discoveries SET imported_item_id=? WHERE imported_item_id=?', (target_id, source_id))
                for row in db.execute('SELECT * FROM terms WHERE item_id=?', (source_id,)).fetchall():
                    term = row['term']
                    if db.execute('SELECT 1 FROM terms WHERE item_id=? AND term=?', (target_id, term)).fetchone():
                        term = (term[:135] + '（合并自 ' + source_id[:8] + '）')[:160]
                    db.execute('UPDATE terms SET item_id=?,term=?,updated_at=? WHERE id=?', (target_id, term, now(), row['id']))
                source_card = db.execute('SELECT * FROM reading_cards WHERE item_id=?', (source_id,)).fetchone()
                target_card = db.execute('SELECT * FROM reading_cards WHERE item_id=?', (target_id,)).fetchone()
                if source_card and target_card:
                    combined = json.loads(target_card['data'])
                    for key, value in json.loads(source_card['data']).items():
                        if not value:
                            continue
                        if not combined.get(key):
                            combined[key] = value
                        elif combined[key] != value:
                            combined[key] += '\n\n[合并来源 ' + source_id[:8] + ']\n' + str(value)
                    db.execute('UPDATE reading_cards SET data=?,revision=revision+1,updated_at=? WHERE id=?',
                               (dumps(combined), now(), target_card['id']))
                    db.execute('DELETE FROM reading_cards WHERE id=?', (source_card['id'],))
                elif source_card:
                    db.execute('UPDATE reading_cards SET item_id=? WHERE id=?', (target_id, source_card['id']))
                source_session = db.execute('SELECT * FROM reading_sessions WHERE item_id=?', (source_id,)).fetchone()
                target_session = db.execute('SELECT * FROM reading_sessions WHERE item_id=?', (target_id,)).fetchone()
                if source_session and target_session:
                    status = max((source_session['status'], target_session['status']), key=lambda value: {'planned': 0, 'reading': 1, 'completed': 2}.get(value, 0))
                    db.execute('''UPDATE reading_sessions SET seconds_read=seconds_read+?,status=?,goal=?,
                        attachment_id=coalesce(attachment_id,?),last_page=CASE WHEN attachment_id IS NULL THEN ? ELSE last_page END,
                        revision=revision+1,updated_at=? WHERE id=?''',
                        (source_session['seconds_read'], status, target_session['goal'] or source_session['goal'],
                         source_session['attachment_id'], source_session['last_page'], now(), target_session['id']))
                    db.execute('DELETE FROM reading_sessions WHERE id=?', (source_session['id'],))
                elif source_session:
                    db.execute('UPDATE reading_sessions SET item_id=? WHERE id=?', (target_id, source_session['id']))
                for row in db.execute('SELECT * FROM fulltext_sources WHERE item_id=?', (source_id,)).fetchall():
                    existing_source = db.execute('SELECT * FROM fulltext_sources WHERE item_id=? AND url=?', (target_id, row['url'])).fetchone()
                    if existing_source:
                        if row['attachment_id'] and not existing_source['attachment_id']:
                            db.execute('''UPDATE fulltext_sources SET attachment_id=?,resolved_url=?,kind=?,state=?,updated_at=? WHERE id=?''',
                                       (row['attachment_id'], row['resolved_url'], row['kind'], row['state'], now(), existing_source['id']))
                        db.execute('DELETE FROM fulltext_sources WHERE id=?', (row['id'],))
                    else:
                        db.execute('UPDATE fulltext_sources SET item_id=? WHERE id=?', (target_id, row['id']))
                for row in db.execute('SELECT * FROM relation_session_items WHERE item_id=?', (source_id,)).fetchall():
                    affected_sessions.add(row['session_id'])
                    if db.execute('SELECT 1 FROM relation_session_items WHERE session_id=? AND item_id=?', (row['session_id'], target_id)).fetchone():
                        db.execute('DELETE FROM relation_session_items WHERE session_id=? AND item_id=?', (row['session_id'], source_id))
                    else:
                        db.execute('UPDATE relation_session_items SET item_id=? WHERE session_id=? AND item_id=?', (target_id, row['session_id'], source_id))
                for row in db.execute('SELECT * FROM writing_citation_items WHERE item_id=?', (source_id,)).fetchall():
                    if db.execute('SELECT 1 FROM writing_citation_items WHERE cluster_id=? AND item_id=?', (row['cluster_id'], target_id)).fetchone():
                        db.execute('DELETE FROM writing_citation_items WHERE cluster_id=? AND item_id=?', (row['cluster_id'], source_id))
                    else:
                        db.execute('UPDATE writing_citation_items SET item_id=? WHERE cluster_id=? AND item_id=?', (target_id, row['cluster_id'], source_id))
                for row in db.execute("SELECT * FROM research_project_links WHERE entity_type='item' AND entity_id=?", (source_id,)).fetchall():
                    if db.execute("SELECT 1 FROM research_project_links WHERE project_id=? AND entity_type='item' AND entity_id=?", (row['project_id'], target_id)).fetchone():
                        db.execute("DELETE FROM research_project_links WHERE project_id=? AND entity_type='item' AND entity_id=?", (row['project_id'], source_id))
                    else:
                        db.execute("UPDATE research_project_links SET entity_id=? WHERE project_id=? AND entity_type='item' AND entity_id=?", (target_id, row['project_id'], source_id))
                for table in ('article_relations', 'manual_relations'):
                    for row in db.execute(f'SELECT * FROM {table} WHERE left_item_id=? OR right_item_id=?', (source_id, source_id)).fetchall():
                        left = target_id if row['left_item_id'] == source_id else row['left_item_id']
                        right = target_id if row['right_item_id'] == source_id else row['right_item_id']
                        collision = db.execute('''SELECT * FROM manual_relations WHERE session_id=? AND left_item_id=? AND right_item_id=? AND label=? AND id<>?''',
                            (row['session_id'], left, right, row['label'], row['id'])).fetchone() if table == 'manual_relations' else None
                        if collision:
                            if row['note'] and row['note'] not in collision['note']:
                                combined_note = (collision['note'] + '\n\n[合并来源 ' + source_id[:8] + ']\n' + row['note']).strip()
                                db.execute('UPDATE manual_relations SET note=?,updated_at=? WHERE id=?', (combined_note, now(), collision['id']))
                            db.execute('DELETE FROM manual_relations WHERE id=?', (row['id'],))
                        else:
                            db.execute(f'UPDATE {table} SET left_item_id=?,right_item_id=?,updated_at=? WHERE id=?', (left, right, now(), row['id']))
                        affected_sessions.add(row['session_id'])
                for table in ('relation_discoveries', 'relation_discovery_pages'):
                    for row in db.execute(f'SELECT * FROM {table} WHERE anchor_item_id=?', (source_id,)).fetchall():
                        identity = ('session_id', 'work_id', 'direction') if table == 'relation_discoveries' else ('session_id', 'direction')
                        where = ' AND '.join(f'{key}=?' for key in identity)
                        if db.execute(f'SELECT 1 FROM {table} WHERE anchor_item_id=? AND {where}', (target_id, *(row[key] for key in identity))).fetchone():
                            db.execute(f'DELETE FROM {table} WHERE anchor_item_id=? AND {where}', (source_id, *(row[key] for key in identity)))
                        else:
                            db.execute(f'UPDATE {table} SET anchor_item_id=? WHERE anchor_item_id=? AND {where}', (target_id, source_id, *(row[key] for key in identity)))
                db.execute('UPDATE relation_discovery_decisions SET anchor_item_id=? WHERE anchor_item_id=?', (target_id, source_id))
                target_profile = db.execute('SELECT id FROM article_profiles WHERE item_id=?', (target_id,)).fetchone()
                source_profile = db.execute('SELECT id FROM article_profiles WHERE item_id=?', (source_id,)).fetchone()
                if source_profile and target_profile:
                    db.execute('UPDATE article_profile_evidence SET profile_id=? WHERE profile_id=?', (target_profile['id'], source_profile['id']))
                    db.execute('DELETE FROM article_profiles WHERE id=?', (source_profile['id'],))
                elif source_profile:
                    db.execute('UPDATE article_profiles SET item_id=? WHERE id=?', (target_id, source_profile['id']))
                db.execute('INSERT OR IGNORE INTO collection_items SELECT collection_id,? FROM collection_items WHERE item_id=?', (target_id, source_id))
                db.execute('UPDATE items SET deleted_at=?,revision=revision+1,updated_at=? WHERE id=?', (now(), now(), source_id))
            for row in snapshot['research_conversations']:
                item_ids = [target_id if item_id in source_ids else item_id for item_id in json.loads(row['item_ids_json'])]
                db.execute('UPDATE research_conversations SET item_ids_json=?,updated_at=? WHERE id=?',
                           (dumps(list(dict.fromkeys(item_ids))), now(), row['id']))
            for session_id in affected_sessions:
                db.execute("UPDATE relation_sessions SET state='stale',updated_at=? WHERE id=? AND state NOT IN ('running','queued')", (now(), session_id))
            self._update(db, target_id, {'tags': list(dict.fromkeys(combined_tags))}, target['revision'])
            for source_id in source_ids:
                self._index(db, source_id)
            event_id = uid()
            snapshot['postRevisions'] = {}
            for table in ('terms', 'reading_cards', 'reading_sessions'):
                original_ids = {record['id'] for record in snapshot[table]}
                snapshot['postRevisions'][table] = {row['id']: row['revision'] for row in db.execute(f'SELECT id,revision FROM {table}')
                                                     if row['id'] in original_ids}
            cluster_ids = {row['cluster_id'] for row in snapshot['writing_citation_items']}
            snapshot['wordPost'] = [dict(row) for row in db.execute('SELECT * FROM writing_citation_items') if row['cluster_id'] in cluster_ids]
            snapshot.update(targetId=target_id, sourceIds=source_ids, expectedRevisions={row['id']: row['revision'] + 1 for row in snapshot['items']})
            db.execute('INSERT INTO merge_events VALUES(?,?,0,?)', (event_id, dumps(snapshot), now()))
        return {'id': event_id, 'item': self.get(target_id)}

    def undo_merge(self, event_id):
        with self.db(True) as db:
            event = db.execute('SELECT * FROM merge_events WHERE id=? AND undone=0', (event_id,)).fetchone()
            require(event, '可撤销的合并记录不存在')
            snapshot = json.loads(event['data'])
            for item_id, revision in snapshot['expectedRevisions'].items():
                require(self._get(db, item_id)['revision'] == revision, '合并后条目已编辑，请先手动核对再拆分')
            for table, revisions in snapshot.get('postRevisions', {}).items():
                for row_id, revision in revisions.items():
                    current = db.execute(f'SELECT revision FROM {table} WHERE id=?', (row_id,)).fetchone()
                    require(current and current['revision'] == revision, '合并后阅读卡、进度或术语已编辑，不能自动撤销')
            if 'wordPost' in snapshot:
                cluster_ids = {row['cluster_id'] for row in snapshot['writing_citation_items']}
                current_word = [dict(row) for row in db.execute('SELECT * FROM writing_citation_items') if row['cluster_id'] in cluster_ids]
                require(current_word == snapshot['wordPost'], '合并后 Word 引文已变更，不能自动撤销')
            for table in ('attachments', 'notes', 'provenance', 'assistant_runs', 'document_chunks',
                          'relation_evidence', 'article_profile_evidence', 'import_entries'):
                for record in snapshot.get(table, []):
                    if record['item_id'] == snapshot['targetId']:
                        continue
                    identity = ('batch_id=? AND entry_key=?', (record['batch_id'], record['entry_key'])) if table == 'import_entries' else ('id=?', (record['id'],))
                    current = db.execute(f'SELECT * FROM {table} WHERE {identity[0]}', identity[1]).fetchone()
                    require(current is not None, '合并后的附件或笔记已移除，不能自动撤销')
                    if table == 'notes':
                        require(current['revision'] == record['revision'], '合并后的笔记已编辑，不能自动撤销')
                    db.execute(f'UPDATE {table} SET item_id=? WHERE {identity[0]}', (record['item_id'], *identity[1]))
            for record in snapshot.get('ai_search_candidates', []):
                db.execute('UPDATE ai_search_candidates SET imported_item_id=? WHERE id=?', (record['imported_item_id'], record['id']))
            for record in snapshot.get('terms', []):
                if record['item_id'] != snapshot['targetId']:
                    db.execute('UPDATE terms SET item_id=?,term=?,updated_at=? WHERE id=?',
                               (record['item_id'], record['term'], now(), record['id']))
            for table in ('reading_cards', 'reading_sessions'):
                db.execute(f'DELETE FROM {table} WHERE item_id IN (' + ','.join('?' for _ in snapshot['expectedRevisions']) + ')',
                           tuple(snapshot['expectedRevisions']))
                for record in snapshot.get(table, []):
                    columns = list(record)
                    db.execute(f'INSERT INTO {table} (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                               tuple(record[key] for key in columns))
            original_target_links = {(row['session_id'], row['item_id']) for row in snapshot.get('relation_session_items', []) if row['item_id'] == snapshot['targetId']}
            for record in snapshot.get('relation_session_items', []):
                if record['item_id'] == snapshot['targetId']:
                    continue
                key = (record['session_id'], snapshot['targetId'])
                if key not in original_target_links:
                    moved = db.execute('UPDATE relation_session_items SET item_id=? WHERE session_id=? AND item_id=?',
                                       (record['item_id'], record['session_id'], snapshot['targetId']))
                    if moved.rowcount:
                        original_target_links.add(key)
                        continue
                db.execute('INSERT OR IGNORE INTO relation_session_items(session_id,item_id,profile_version,ordinal) VALUES(?,?,?,?)',
                           (record['session_id'], record['item_id'], record['profile_version'], record['ordinal']))
            for table, columns in [('writing_citation_items', ('cluster_id', 'item_id')),
                                   ('research_project_links', ('project_id', 'entity_type', 'entity_id'))]:
                originals = snapshot.get(table, [])
                target_keys = {tuple(row[key] for key in columns[:-1]) for row in originals if row[columns[-1]] == snapshot['targetId']}
                for record in originals:
                    if record[columns[-1]] == snapshot['targetId'] or (table == 'research_project_links' and record['entity_type'] != 'item'):
                        continue
                    prefix = tuple(record[key] for key in columns[:-1])
                    where = ' AND '.join(f'{key}=?' for key in columns[:-1])
                    if prefix not in target_keys:
                        moved = db.execute(f'UPDATE {table} SET {columns[-1]}=? WHERE {where} AND {columns[-1]}=?',
                                           (record[columns[-1]], *prefix, snapshot['targetId']))
                        if moved.rowcount:
                            target_keys.add(prefix)
                            continue
                    db.execute(f'INSERT OR IGNORE INTO {table} (' + ','.join(record) + ') VALUES (' + ','.join('?' for _ in record) + ')',
                               tuple(record.values()))
            for table in ('fulltext_sources', 'manual_relations', 'article_relations', 'relation_discovery_decisions'):
                for record in snapshot.get(table, []):
                    if table == 'fulltext_sources' and record['item_id'] == snapshot['targetId']:
                        continue
                    current = db.execute(f'SELECT id FROM {table} WHERE id=?', (record['id'],)).fetchone()
                    if current:
                        fields = tuple(field for field in record if field != 'id')
                        db.execute(f'UPDATE {table} SET ' + ','.join(f'{field}=?' for field in fields) + ' WHERE id=?',
                                   (*(record[field] for field in fields), record['id']))
                    else:
                        db.execute(f'INSERT INTO {table} (' + ','.join(record) + ') VALUES (' + ','.join('?' for _ in record) + ')',
                                   tuple(record.values()))
            for table in ('relation_discoveries', 'relation_discovery_pages'):
                identity = ('session_id', 'work_id', 'direction') if table == 'relation_discoveries' else ('session_id', 'direction')
                original_target = {tuple(row[key] for key in identity) for row in snapshot.get(table, []) if row['anchor_item_id'] == snapshot['targetId']}
                for record in snapshot.get(table, []):
                    if record['anchor_item_id'] == snapshot['targetId']:
                        continue
                    key = tuple(record[field] for field in identity)
                    where = ' AND '.join(f'{field}=?' for field in identity)
                    if key not in original_target:
                        moved = db.execute(f'UPDATE {table} SET anchor_item_id=? WHERE anchor_item_id=? AND {where}',
                                           (record['anchor_item_id'], snapshot['targetId'], *key))
                        if moved.rowcount:
                            original_target.add(key)
                            continue
                    db.execute(f'INSERT OR IGNORE INTO {table} (' + ','.join(record) + ') VALUES (' + ','.join('?' for _ in record) + ')',
                               tuple(record.values()))
            for record in snapshot.get('relation_discovery_imports', []):
                db.execute('''UPDATE relation_discoveries SET imported_item_id=?
                    WHERE session_id=? AND anchor_item_id=? AND work_id=? AND direction=?''',
                    (record['imported_item_id'], record['session_id'], record['anchor_item_id'], record['work_id'], record['direction']))
            for record in snapshot.get('article_profiles', []):
                if db.execute('SELECT 1 FROM article_profiles WHERE id=?', (record['id'],)).fetchone():
                    db.execute('UPDATE article_profiles SET item_id=? WHERE id=?', (record['item_id'], record['id']))
                else:
                    db.execute('INSERT INTO article_profiles (' + ','.join(record) + ') VALUES (' + ','.join('?' for _ in record) + ')', tuple(record.values()))
            for record in snapshot.get('article_profile_evidence', []):
                db.execute('UPDATE article_profile_evidence SET profile_id=? WHERE id=?', (record['profile_id'], record['id']))
            for record in snapshot.get('research_conversations', []):
                db.execute('UPDATE research_conversations SET item_ids_json=?,updated_at=? WHERE id=?',
                           (record['item_ids_json'], now(), record['id']))
            for record in snapshot['items']:
                db.execute('UPDATE items SET data=?,deleted_at=?,revision=revision+1,updated_at=? WHERE id=?', (record['data'], record['deleted_at'], now(), record['id']))
                for table in ('collection_items', 'item_tags'):
                    db.execute(f'DELETE FROM {table} WHERE item_id=?', (record['id'],))
                self._tags(db, record['id'], json.loads(record['data']).get('tags', []))
            for record in snapshot['collection_items']:
                db.execute('INSERT OR IGNORE INTO collection_items VALUES(?,?)', (record['collection_id'], record['item_id']))
            for item in snapshot['items']:
                self._index(db, item['id'])
            db.execute('UPDATE merge_events SET undone=1 WHERE id=?', (event_id,))
        return {'ok': True}

    def get_settings(self):
        with self.db() as db:
            return {row['key']: json.loads(row['value']) for row in db.execute('SELECT * FROM settings')}

    def set_settings(self, changes):
        allowed = {'journalRoot', 'online', 'attachmentMode', 'citationStyle', 'citationLocale', 'columns', 'window', 'backupDirectory', 'autoBackup',
                   'assistantBaseUrl', 'assistantModel', 'assistantTimeout', 'writingStyle', 'writingLocale', 'contactEmail'}
        require(set(changes) <= allowed, '未知设置')
        if 'online' in changes:
            require(isinstance(changes['online'], bool), '联网状态必须为布尔值')
        if 'attachmentMode' in changes:
            require(changes['attachmentMode'] in ('managed', 'linked'), '附件策略不正确')
        if 'contactEmail' in changes:
            import re
            value = str(changes['contactEmail']).strip()
            require(not value or re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', value), '联系邮箱格式不正确')
            changes['contactEmail'] = value
        with self.db(True) as db:
            for key, value in changes.items():
                db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, dumps(value)))
        return self.get_settings()

    def writing_sessions(self, limit=100):
        limit = bounded_int(limit, 100, 1, 300)
        with self.db() as db:
            rows = db.execute('''SELECT s.*,count(c.id) AS citation_count,b.anchor_key AS bibliography_anchor,b.updated_at AS bibliography_updated_at
                FROM writing_document_sessions s
                LEFT JOIN writing_citation_clusters c ON c.session_id=s.id
                LEFT JOIN writing_bibliographies b ON b.session_id=s.id
                GROUP BY s.id ORDER BY s.updated_at DESC LIMIT ?''', (limit,)).fetchall()
            return [{
                'id': row['id'], 'host': row['host'], 'documentFingerprint': row['document_fingerprint'], 'documentName': row['document_name'],
                'styleId': row['style_id'], 'locale': row['locale'], 'citationCount': row['citation_count'],
                'bibliographyAnchor': row['bibliography_anchor'] or '', 'bibliographyUpdatedAt': row['bibliography_updated_at'],
                'createdAt': row['created_at'], 'updatedAt': row['updated_at'],
            } for row in rows]

    def writing_session(self, session_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM writing_document_sessions WHERE id=?', (session_id,)).fetchone()
            if not row:
                raise AppError('NOT_FOUND', '写作文档会话不存在')
            clusters = []
            for cluster in db.execute('SELECT * FROM writing_citation_clusters WHERE session_id=? ORDER BY ordinal,id', (session_id,)):
                items = [dict(item) for item in db.execute('''SELECT item_id AS itemId,sort_order AS sortOrder,locator,label,prefix,suffix,
                    suppress_author AS suppressAuthor FROM writing_citation_items WHERE cluster_id=? ORDER BY sort_order,item_id''', (cluster['id'],))]
                clusters.append({'id': cluster['id'], 'anchorKey': cluster['anchor_key'], 'ordinal': cluster['ordinal'],
                                 'citation': json.loads(cluster['citation_json']), 'renderedText': cluster['rendered_text'], 'items': items,
                                 'createdAt': cluster['created_at'], 'updatedAt': cluster['updated_at']})
            bibliography = db.execute('SELECT * FROM writing_bibliographies WHERE session_id=?', (session_id,)).fetchone()
            return {
                'id': row['id'], 'host': row['host'], 'documentFingerprint': row['document_fingerprint'], 'documentName': row['document_name'],
                'styleId': row['style_id'], 'locale': row['locale'], 'state': json.loads(row['state_json']), 'clusters': clusters,
                'bibliography': {'anchorKey': bibliography['anchor_key'], 'styleId': bibliography['style_id'],
                                  'renderedHash': bibliography['rendered_hash'], 'updatedAt': bibliography['updated_at']} if bibliography else None,
                'createdAt': row['created_at'], 'updatedAt': row['updated_at'],
            }

    def writing_session_save(self, value):
        require(isinstance(value, dict), '写作文档状态必须为对象')
        session_id = str(value.get('id') or '').strip()
        require(bool(re.fullmatch(r'[A-Za-z0-9_-]{8,160}', session_id)), '文档会话标识不正确')
        host = str(value.get('host') or '').lower()
        require(host == 'word', '当前仅支持 Word 写作宿主')
        document_fingerprint = str(value.get('documentFingerprint') or '').strip()
        document_name = str(value.get('documentName') or '').strip()
        style_id = str(value.get('styleId') or 'gb-t-7714-2015').strip()
        locale = str(value.get('locale') or 'zh-CN').strip()
        clusters = value.get('clusters') or []
        require(len(document_fingerprint) <= 512 and len(document_name) <= 1024 and len(style_id) <= 160 and len(locale) <= 40, '写作文档字段过长')
        require(isinstance(clusters, list) and len(clusters) <= 5000, '引文组数量不正确')
        timestamp = now()
        clean_clusters = []
        seen_ids, seen_anchors = set(), set()
        for ordinal, cluster in enumerate(clusters):
            require(isinstance(cluster, dict), '引文组格式不正确')
            cluster_id = str(cluster.get('id') or '').strip()
            anchor = str(cluster.get('anchorKey') or '').strip()
            citation = cluster.get('citation') or {}
            entries = cluster.get('items') or []
            require(bool(re.fullmatch(r'[A-Za-z0-9_-]{8,160}', cluster_id)) and cluster_id not in seen_ids, '引文组标识不正确')
            require(anchor.startswith('research-library:citation:') and len(anchor) <= 240 and anchor not in seen_anchors, '引文锚点不正确')
            require(isinstance(citation, dict) and isinstance(entries, list) and 1 <= len(entries) <= 100, '引文内容不正确')
            seen_ids.add(cluster_id); seen_anchors.add(anchor)
            clean_entries, item_ids = [], set()
            for index, entry in enumerate(entries):
                require(isinstance(entry, dict), '引文条目格式不正确')
                item_id = str(entry.get('itemId') or '').strip()
                require(bool(re.fullmatch(r'[a-f0-9-]{36}', item_id)) and item_id not in item_ids, '引文条目标识不正确')
                for key in ('locator', 'label', 'prefix', 'suffix'):
                    require(len(str(entry.get(key) or '')) <= 1000, '引文定位信息过长')
                item_ids.add(item_id)
                clean_entries.append({'itemId': item_id, 'sortOrder': index, 'locator': str(entry.get('locator') or ''),
                                      'label': str(entry.get('label') or 'page'), 'prefix': str(entry.get('prefix') or ''),
                                      'suffix': str(entry.get('suffix') or ''), 'suppressAuthor': bool(entry.get('suppressAuthor'))})
            clean_clusters.append({'id': cluster_id, 'anchorKey': anchor, 'ordinal': ordinal, 'citation': citation,
                                   'items': clean_entries, 'renderedText': str(cluster.get('renderedText') or '')[:16000]})
        state = value.get('state') or {}
        require(isinstance(state, dict), '文档状态格式不正确')
        bibliography = value.get('bibliography') or {}
        require(isinstance(bibliography, dict), '参考文献状态格式不正确')
        anchor = str(bibliography.get('anchorKey') or '')
        require(len(anchor) <= 240, '参考文献锚点过长')
        with self.db(True) as db:
            db.execute('''INSERT INTO writing_document_sessions(id,host,document_fingerprint,document_name,style_id,locale,state_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET host=excluded.host,document_fingerprint=excluded.document_fingerprint,
                document_name=excluded.document_name,style_id=excluded.style_id,locale=excluded.locale,state_json=excluded.state_json,updated_at=excluded.updated_at''',
                (session_id, host, document_fingerprint, document_name, style_id, locale, dumps(state), timestamp, timestamp))
            db.execute('DELETE FROM writing_citation_clusters WHERE session_id=?', (session_id,))
            for cluster in clean_clusters:
                db.execute('''INSERT INTO writing_citation_clusters(id,session_id,anchor_key,ordinal,citation_json,rendered_text,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)''', (cluster['id'], session_id, cluster['anchorKey'], cluster['ordinal'], dumps(cluster['citation']),
                                                   cluster['renderedText'], timestamp, timestamp))
                for entry in cluster['items']:
                    require(db.execute('SELECT 1 FROM items WHERE id=? AND deleted_at IS NULL', (entry['itemId'],)).fetchone(), '引用的文献不存在或在回收站')
                    db.execute('''INSERT INTO writing_citation_items(cluster_id,item_id,sort_order,locator,label,prefix,suffix,suppress_author)
                        VALUES(?,?,?,?,?,?,?,?)''', (cluster['id'], entry['itemId'], entry['sortOrder'], entry['locator'], entry['label'],
                                                      entry['prefix'], entry['suffix'], int(entry['suppressAuthor'])))
            db.execute('''INSERT INTO writing_bibliographies(session_id,anchor_key,style_id,rendered_hash,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET anchor_key=excluded.anchor_key,style_id=excluded.style_id,
                rendered_hash=excluded.rendered_hash,updated_at=excluded.updated_at''',
                (session_id, anchor, style_id, str(bibliography.get('renderedHash') or '')[:160], timestamp))
            db.execute('INSERT INTO writing_events VALUES(?,?,?,?,?)', (uid(), session_id, str(value.get('eventType') or 'save')[:80],
                                                                         dumps({'clusters': len(clean_clusters), 'host': host}), timestamp))
        return self.writing_session(session_id)

    def writing_event(self, value):
        require(isinstance(value, dict), '写作事件格式不正确')
        session_id = value.get('sessionId')
        event_type = str(value.get('eventType') or '').strip()
        require(len(event_type) >= 2 and len(event_type) <= 80, '写作事件类型不正确')
        with self.db(True) as db:
            if session_id:
                require(db.execute('SELECT 1 FROM writing_document_sessions WHERE id=?', (session_id,)).fetchone(), '写作文档会话不存在')
            db.execute('INSERT INTO writing_events VALUES(?,?,?,?,?)', (uid(), session_id, event_type, dumps(value.get('detail') or {}), now()))
        return {'ok': True}

    @staticmethod
    def _comparison_side(value):
        require(isinstance(value, dict), '对比记录必须包含外部与本地题录')
        fields = ('title', 'authors', 'year', 'venue', 'doi', 'abstract', 'oaUrl', 'url', 'citationCount')
        clean = {}
        for field in fields:
            item = value.get(field, '')
            if field == 'citationCount':
                try:
                    clean[field] = max(0, min(int(item or 0), 100000000))
                except (TypeError, ValueError):
                    clean[field] = 0
            else:
                clean[field] = str(item or '').strip()[:20000 if field == 'abstract' else 1600]
        if clean['doi']:
            clean['doi'] = doi(clean['doi'])
        for field in ('url', 'oaUrl'):
            require(not clean[field] or re.fullmatch(r'https?://[^\s]{1,1500}', clean[field]), '链接格式不正确')
        return clean

    @staticmethod
    def _comparison_fields(external, local):
        labels = {'title': '题名', 'authors': '作者', 'year': '年份', 'venue': '期刊 / 来源', 'doi': 'DOI',
                  'abstract': '摘要', 'oaUrl': '开放获取链接'}
        fields = []
        for key, label in labels.items():
            left, right = str(external.get(key) or ''), str(local.get(key) or '')
            if left and right:
                status = 'match' if norm(left) == norm(right) else 'different'
            elif left:
                status = 'missingLocal'
            elif right:
                status = 'missingExternal'
            else:
                status = 'empty'
            fields.append({'key': key, 'label': label, 'status': status, 'external': left, 'local': right})
        return fields

    @staticmethod
    def _comparison_value(row):
        value = dict(row)
        value.update(external=json.loads(value.pop('external_json')), local=json.loads(value.pop('local_json')),
                     fields=json.loads(value.pop('fields_json')), sourceUrl=value.pop('source_url'),
                     createdAt=value.pop('created_at'), updatedAt=value.pop('updated_at'))
        value['summary'] = {status: sum(1 for field in value['fields'] if field['status'] == status)
                            for status in ('match', 'different', 'missingLocal', 'missingExternal', 'empty')}
        return value

    def comparison_list(self, limit=100):
        limit = bounded_int(limit, 100, 1, 300)
        with self.db() as db:
            rows = db.execute('SELECT * FROM search_comparisons ORDER BY updated_at DESC LIMIT ?', (limit,)).fetchall()
        return [self._comparison_value(row) for row in rows]

    def comparison_save(self, value):
        require(isinstance(value, dict), '对比记录格式不正确')
        query = str(value.get('query') or '').strip()[:800]
        source = str(value.get('source') or '外部检索').strip()[:120]
        source_url = str(value.get('sourceUrl') or '').strip()[:1600]
        require(2 <= len(query) <= 800, '检索主题应为2–800个字符')
        require(source, '请填写外部检索来源')
        require(not source_url or re.fullmatch(r'https?://[^\s]{1,1500}', source_url), '来源链接格式不正确')
        external, local = self._comparison_side(value.get('external')), self._comparison_side(value.get('local'))
        title = str(value.get('title') or external['title'] or local['title']).strip()[:1600]
        require(title, '请填写至少一侧的论文题名')
        conclusion = str(value.get('conclusion') or '').strip()[:6000]
        fields, timestamp = self._comparison_fields(external, local), now()
        record_id = str(value.get('id') or '').strip() or uid()
        require(bool(re.fullmatch(r'[A-Za-z0-9_-]{8,160}', record_id)), '对比记录标识不正确')
        with self.db(True) as db:
            db.execute('''INSERT INTO search_comparisons(id,query,title,source,source_url,external_json,local_json,fields_json,conclusion,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET query=excluded.query,title=excluded.title,source=excluded.source,
                source_url=excluded.source_url,external_json=excluded.external_json,local_json=excluded.local_json,fields_json=excluded.fields_json,
                conclusion=excluded.conclusion,updated_at=excluded.updated_at''',
                (record_id, query, title, source, source_url, dumps(external), dumps(local), dumps(fields), conclusion, timestamp, timestamp))
            row = db.execute('SELECT * FROM search_comparisons WHERE id=?', (record_id,)).fetchone()
        return self._comparison_value(row)

    def rebuild_index(self, payload, progress):
        with self.db() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM items ORDER BY id')]
        for start in range(0, len(ids), 100):
            progress(start / max(1, len(ids)), f'重建文献检索 {start}/{len(ids)}')
            with self.db(True) as db:
                for item_id in ids[start:start + 100]:
                    self._index(db, item_id)
        return {'indexed': len(ids)}
