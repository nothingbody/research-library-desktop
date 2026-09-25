"""Validate the last published v2 export and the raw acquisition audit trail."""
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3


def main():
    root=Path(__file__).resolve().parent/'data'/'scholay'
    pointer=json.loads((root/'exports'/'CURRENT.json').read_text(encoding='utf-8'))
    folder=root/'exports'/pointer['generation']
    snapshot=json.loads((folder/'snapshot.json').read_text(encoding='utf-8'))
    assert snapshot==pointer,'Export pointer mismatch'
    conn=sqlite3.connect((root/'journals.sqlite3').as_uri()+'?mode=ro',uri=True)
    conn.execute('BEGIN')
    assert conn.execute('PRAGMA quick_check').fetchone()[0]=='ok','SQLite integrity failure'
    count,details,previous=0,0,0
    with gzip.open(folder/'journals.jsonl.gz','rt',encoding='utf-8') as stream:
        for line in stream:
            item=json.loads(line)
            journal_id=item['id']
            assert journal_id>previous,'Duplicate or unordered export ID'
            previous=journal_id
            assert item['listing']['unified']['id']==journal_id
            live=conn.execute('SELECT list_json,detail_json FROM journals WHERE id=?',(journal_id,)).fetchone()
            assert live is not None and json.loads(live[0])==item['listing'],'Snapshot listing differs from durable data'
            if item['detail'] is not None:
                assert item['detail']['unified']['id']==journal_id
                assert json.loads(live[1])==item['detail'],'Snapshot detail differs from durable data'
                details+=1
            count+=1
    assert (count,details)==(pointer['records'],pointer['details']),'Export counts mismatch'
    conn.rollback()
    conn.close()
    verified,network_failures=0,0
    raw_log=(root/'requests.jsonl').read_bytes()
    # The active writer may be appending its last line. Audit all complete lines.
    complete_lines=raw_log.split(b'\n')[:-1]
    for line in complete_lines:
        if not line.strip():
            continue
        request=json.loads(line)
        if request.get('raw') is None:
            assert request.get('sha256') is None
            network_failures+=1
            continue
        with gzip.open(root/request['raw'],'rb') as source:
            assert hashlib.sha256(source.read()).hexdigest()==request['sha256'],'Raw response checksum mismatch'
        verified+=1
    result={'snapshot_integrity':'passed','generation':pointer['generation'],'snapshot_records':count,
            'snapshot_details':details,'raw_responses_verified':verified,'network_failures_logged':network_failures,
            'full_acquisition_complete':json.loads((root/'manifest.json').read_text(encoding='utf-8'))['complete']}
    print(json.dumps(result))
    from robust_runtime import atomic_json
    atomic_json(root/'validation_latest.json',result)


if __name__=='__main__':
    main()
