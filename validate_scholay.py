"""Read-only integrity validation for a finished local acquisition batch."""
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3


def main():
    root = Path(__file__).resolve().parent / 'data' / 'scholay'
    conn = sqlite3.connect((root / 'journals.sqlite3').as_uri() + '?mode=ro', uri=True)
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'SQLite integrity failure'
    records = {row[0]: row for row in conn.execute('SELECT id,list_json,detail_json,detail_status FROM journals')}
    details = 0
    for journal_id, listing, detail, status in records.values():
        assert json.loads(listing)['unified']['id'] == journal_id, 'Listing identity mismatch'
        if status == 'ok':
            assert json.loads(detail)['unified']['id'] == journal_id, 'Detail identity mismatch'
            details += 1
    exported = {}
    with (root / 'journals.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            record = json.loads(line)
            journal_id = record['id']
            assert journal_id not in exported, 'Duplicate export ID'
            assert record['listing'] == json.loads(records[journal_id][1])
            assert record['detail'] == (json.loads(records[journal_id][2]) if records[journal_id][2] else None)
            exported[journal_id] = True
    assert set(exported) == set(records), 'Export/database ID mismatch'
    raw_count = 0
    with (root / 'requests.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            request = json.loads(line)
            with gzip.open(root / request['raw'], 'rb') as source:
                assert hashlib.sha256(source.read()).hexdigest() == request['sha256'], 'Raw response hash mismatch'
            raw_count += 1
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['unique_journals'] == len(records)
    assert manifest['details_saved'] == details
    if manifest['complete']:
        assert manifest['list_exhausted'] and not manifest['list_blocker'] and not manifest['detail_blocker']
        assert len(records) == details == manifest['expected_journals']
    print(json.dumps({'local_integrity': 'passed', 'records': len(records), 'details': details,
                      'raw_responses_verified': raw_count, 'full_acquisition_complete': manifest['complete']}))
    conn.close()


if __name__ == '__main__':
    main()
