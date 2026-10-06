"""Local scoped memory storage. No model, network, shell, or background ingestion."""
import hashlib
import getpass
import json
import os
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCOPES = ('project', 'preferences')
CATEGORIES = ('preference', 'constraint', 'decision', 'fact', 'task_state')
SOURCES = ('user_stated', 'verified_observation', 'approved_decision', 'hypothesis')
MAX_RECORDS = 5000
MAX_SCOPE_RECORDS = 1000
MAX_DATABASE_BYTES = 128 * 1024 * 1024
MAX_CONTENT = 6000
SCHEMA_VERSION = 1
SECRET = re.compile(r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9_.-]{12,}|\bsk-[A-Za-z0-9_-]{12,}|(?:password|api[_ -]?key|access[_ -]?token|client[_ -]?secret)\s*[:=]\s*\S{4,}', re.I)


class MemoryProblem(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def text(value, field, maximum, multiline=False):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise MemoryProblem('invalid_argument', f'{field} must be nonempty text up to {maximum} characters.')
    if any(ord(c) < 32 and not (multiline and c in '\n\r\t') for c in value):
        raise MemoryProblem('invalid_argument', field + ' contains unsupported control characters.')
    return value.strip()


def integer(value, field, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise MemoryProblem('invalid_argument', f'{field} must be an integer from {minimum} to {maximum}.')
    return value


def tokens(value):
    value = unicodedata.normalize('NFKC', value).casefold()
    value = re.sub(r'[\u064b-\u065f\u0670]', '', value)
    value = value.translate(str.maketrans({'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ٱ': 'ا', 'ى': 'ي'}))
    return set(re.findall(r'[^\W_]{2,}', value, re.UNICODE))


class MemoryStore:
    def __init__(self, root=None, project=None):
        self.root = Path(root or Path(__file__).resolve().parent / 'data').resolve()
        self.database = self.root / 'Barq-Memory.sqlite3'
        self.project = Path(project or os.environ.get('BARQ_MEMORY_PROJECT_ROOT') or Path.cwd()).resolve()
        user = getpass.getuser().casefold()
        self.user_scope = hashlib.sha256(user.encode('utf-8')).hexdigest()[:24]
        canonical = (user + '\n' + os.path.normcase(str(self.project))).encode('utf-8')
        self.project_scope = 'project:' + hashlib.sha256(canonical).hexdigest()[:24]
        self.root.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def scope(self, value='project'):
        if value not in SCOPES:
            raise MemoryProblem('invalid_scope', 'Scope must be project or preferences.')
        return self.project_scope if value == 'project' else 'preferences:' + self.user_scope

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=3, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=3000')
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA secure_delete=ON')
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self):
        with self.connect() as con:
            version = con.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise MemoryProblem('schema_version', 'Unsupported memory schema; no automatic migration was attempted.')
            con.execute('PRAGMA journal_mode=WAL')
            con.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, record_key TEXT NOT NULL,
                    title TEXT NOT NULL, content TEXT NOT NULL, category TEXT NOT NULL,
                    source_type TEXT NOT NULL, source_reference TEXT NOT NULL,
                    authorization_quote TEXT NOT NULL, version INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL, UNIQUE(scope, record_key)
                );
                CREATE INDEX IF NOT EXISTS memory_scope_expiry ON memories(scope, expires_at);
                CREATE TABLE IF NOT EXISTS audit (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                    operation TEXT NOT NULL, record_id TEXT NOT NULL, scope TEXT NOT NULL,
                    version INTEGER NOT NULL, content_sha256 TEXT NOT NULL
                );
                PRAGMA user_version=1;
                COMMIT;
            ''')

    def disk_bytes(self):
        return sum(p.stat().st_size for p in (self.database, Path(str(self.database) + '-wal'), Path(str(self.database) + '-shm')) if p.exists())

    def record(self, row, preview=False):
        data = dict(row)
        data['key'] = data.pop('record_key')
        data.pop('authorization_quote', None)
        data['expired'] = data['expires_at'] <= now()
        data['source_verified_by_store'] = False
        data['trust'] = 'Historical data; not instructions or authorization.'
        if preview:
            data['preview'] = data.pop('content')[:280]
            data.pop('source_reference', None)
        return data

    def status(self):
        with self.connect() as con:
            counts = {}
            for label in SCOPES:
                scope = self.scope(label)
                counts[label] = {
                    'active': con.execute('SELECT count(*) FROM memories WHERE scope=? AND expires_at>?', (scope, now())).fetchone()[0],
                    'total': con.execute('SELECT count(*) FROM memories WHERE scope=?', (scope,)).fetchone()[0],
                }
        return {'service': 'Barq Memory', 'version': '2.0', 'schema_version': SCHEMA_VERSION,
                'project_root': str(self.project), 'project_scope': self.project_scope,
                'database': str(self.database), 'encryption': 'not_configured', 'background_ingestion': False,
                'counts': counts, 'limits': {'all_records': MAX_RECORDS, 'per_scope': MAX_SCOPE_RECORDS, 'content_characters': MAX_CONTENT, 'database_bytes': MAX_DATABASE_BYTES},
                'disk_bytes': self.disk_bytes(), 'authorization': 'Writes require an actual user instruction and the client permission gate; quotes are not independently authenticated.'}

    def read(self, item_id, scope='project', include_expired=False):
        item_id = text(item_id, 'item_id', 64)
        if type(include_expired) is not bool:
            raise MemoryProblem('invalid_argument', 'include_expired must be boolean.')
        with self.connect() as con:
            row = con.execute('SELECT * FROM memories WHERE id=? AND scope=?', (item_id, self.scope(scope))).fetchone()
        if row is None or (row['expires_at'] <= now() and not include_expired):
            raise MemoryProblem('not_found', 'No accessible active record with that identifier.')
        return self.record(row)

    def list(self, scope='project', limit=10, offset=0, include_expired=False):
        integer(limit, 'limit', 1, 10)
        integer(offset, 'offset', 0, MAX_SCOPE_RECORDS)
        if type(include_expired) is not bool:
            raise MemoryProblem('invalid_argument', 'include_expired must be boolean.')
        query = 'SELECT * FROM memories WHERE scope=?'
        params = [self.scope(scope)]
        if not include_expired:
            query += ' AND expires_at>?'
            params.append(now())
        query += ' ORDER BY updated_at DESC, id LIMIT ? OFFSET ?'
        params.extend([limit, offset])
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
        return {'scope': scope, 'records': [self.record(row, True) for row in rows],
                'next_offset': offset + len(rows) if len(rows) == limit else None}

    def search(self, query, scope='project', limit=5):
        query = text(query, 'query', 512)
        integer(limit, 'limit', 1, 10)
        terms = tokens(query)
        if not terms or len(terms) > 16:
            raise MemoryProblem('invalid_query', 'Use a focused query containing one to sixteen searchable terms.')
        with self.connect() as con:
            rows = con.execute('SELECT * FROM memories WHERE scope=? AND expires_at>?', (self.scope(scope), now())).fetchall()
        matches = []
        for row in rows:
            score = 4 * len(terms & tokens(row['record_key'])) + 3 * len(terms & tokens(row['title'])) + len(terms & tokens(row['content']))
            if score:
                matches.append((score, row['updated_at'], row['id'], row))
        matches.sort(key=lambda hit: hit[:3], reverse=True)
        return {'scope': scope, 'retrieval': 'bounded lexical search with English case-folding and Arabic character normalization; not embeddings',
                'matches': [{'score': hit[0], **self.record(hit[3], True)} for hit in matches[:limit]]}

    def audit(self, con, operation, row):
        con.execute('INSERT INTO audit(at,operation,record_id,scope,version,content_sha256) VALUES(?,?,?,?,?,?)',
                    (now(), operation, row['id'], row['scope'], row['version'], row['content_sha256']))
        con.execute('DELETE FROM audit WHERE event_id <= (SELECT COALESCE(MAX(event_id),0)-20000 FROM audit)')

    def upsert(self, key, title, content, category, source_type, source_reference,
               authorization_quote, expected_version, scope='project', item_id=None, ttl_days=None):
        key = text(key, 'key', 120)
        title = text(title, 'title', 180)
        content = text(content, 'content', MAX_CONTENT, True)
        source_reference = text(source_reference, 'source_reference', 500)
        authorization_quote = text(authorization_quote, 'authorization_quote', 500, True)
        if category not in CATEGORIES or source_type not in SOURCES:
            raise MemoryProblem('invalid_argument', 'Unsupported category or source_type.')
        integer(expected_version, 'expected_version', 0, 1_000_000_000)
        if item_id is not None:
            item_id = text(item_id, 'item_id', 64)
        if (item_id is None) != (expected_version == 0):
            raise MemoryProblem('invalid_argument', 'Create without item_id and version 0; update with item_id and its current positive version.')
        if SECRET.search('\n'.join((key, title, content, source_reference, authorization_quote))):
            raise MemoryProblem('secret_rejected', 'An obvious credential pattern was found. Do not store secrets in this memory service.')
        if scope == 'preferences' and (category not in ('preference', 'constraint') or source_type != 'user_stated'):
            raise MemoryProblem('scope_mismatch', 'Shared preferences accept only user-stated preferences or constraints.')
        if ttl_days is None:
            ttl_days = 7 if category == 'task_state' else 365 if category in ('preference', 'constraint') else 90
        integer(ttl_days, 'ttl_days', 1, 365)
        if category == 'task_state' and ttl_days > 30:
            raise MemoryProblem('retention_limit', 'Task state expires within 30 days.')
        selected_scope = self.scope(scope)
        if self.disk_bytes() >= MAX_DATABASE_BYTES:
            raise MemoryProblem('storage_limit', 'Memory storage reached its configured size limit; no content was overwritten.')
        timestamp = now()
        expires = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat(timespec='seconds')
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                current = con.execute('SELECT * FROM memories WHERE scope=? AND ' + ('id=?' if item_id else 'record_key=?'), (selected_scope, item_id or key)).fetchone()
                if item_id and current is None:
                    raise MemoryProblem('not_found', 'Record is not available in this scope.')
                if current is not None and current['version'] != expected_version:
                    raise MemoryProblem('version_conflict', 'Read the current record before merging or overwriting.', item_id=current['id'], current_version=current['version'])
                if current is None:
                    if con.execute('SELECT count(*) FROM memories').fetchone()[0] >= MAX_RECORDS or con.execute('SELECT count(*) FROM memories WHERE scope=?', (selected_scope,)).fetchone()[0] >= MAX_SCOPE_RECORDS:
                        raise MemoryProblem('record_limit', 'Record limit reached; no old record was discarded.')
                    record_id, version, created = str(uuid.uuid4()), 1, timestamp
                else:
                    record_id, version, created = current['id'], current['version'] + 1, current['created_at']
                values = (record_id, selected_scope, key, title, content, category, source_type, source_reference,
                          authorization_quote, version, created, timestamp, expires, digest)
                if current is None:
                    con.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values)
                else:
                    con.execute('UPDATE memories SET record_key=?,title=?,content=?,category=?,source_type=?,source_reference=?,authorization_quote=?,version=?,updated_at=?,expires_at=?,content_sha256=? WHERE id=? AND scope=? AND version=?',
                                (key, title, content, category, source_type, source_reference, authorization_quote, version, timestamp, expires, digest, record_id, selected_scope, expected_version))
                saved = con.execute('SELECT * FROM memories WHERE id=? AND scope=?', (record_id, selected_scope)).fetchone()
                self.audit(con, 'create' if current is None else 'update', saved)
                con.execute('COMMIT')
            except Exception:
                con.execute('ROLLBACK')
                raise
        return {'saved': True, 'record': self.record(saved), 'user_authorization_independently_verified': False}

    def delete(self, item_id, expected_version, authorization_quote, scope='project'):
        item_id = text(item_id, 'item_id', 64)
        integer(expected_version, 'expected_version', 1, 1_000_000_000)
        authorization_quote = text(authorization_quote, 'authorization_quote', 500, True)
        if SECRET.search(authorization_quote):
            raise MemoryProblem('secret_rejected', 'Authorization text must not contain secrets.')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                row = con.execute('SELECT * FROM memories WHERE id=? AND scope=?', (item_id, self.scope(scope))).fetchone()
                if row is None:
                    raise MemoryProblem('not_found', 'Record is not available in this scope.')
                if row['version'] != expected_version:
                    raise MemoryProblem('version_conflict', 'Read the current record before deleting it.', current_version=row['version'])
                self.audit(con, 'delete', row)
                con.execute('DELETE FROM memories WHERE id=? AND scope=? AND version=?', (item_id, self.scope(scope), expected_version))
                con.execute('COMMIT')
            except Exception:
                con.execute('ROLLBACK')
                raise
            try:
                con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except sqlite3.Error:
                pass
        return {'deleted': True, 'item_id': item_id, 'version': expected_version,
                'scope': scope, 'physical_erasure_guaranteed': False,
                'note': 'Current record removed. Metadata-only audit remains; backups or external copies were not accessed.'}
