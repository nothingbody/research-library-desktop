"""Finite live throughput trial; only adjusts the local collector's shared ceiling."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
import urllib.request

from robust_runtime import atomic_json, now


def call(root, action=None, maximum=None):
    handoff=json.loads((root/'handoff.json').read_text(encoding='utf-8'))
    request=urllib.request.Request(handoff['control_url'] if action else handoff['status_url'],
        data=json.dumps({'action':action,'max_rps':maximum}).encode() if action else None,
        headers={'Origin':'https://www.scholay.com','Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=10) as response:
        return json.load(response)


def counts(root):
    with sqlite3.connect((root/'journals.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        return {'details':db.execute("SELECT COUNT(*) FROM journals WHERE detail_status='ok'").fetchone()[0],
                'records':db.execute('SELECT COUNT(*) FROM journals').fetchone()[0],
                'completed_tasks':db.execute("SELECT COUNT(*) FROM tasks WHERE state='complete'").fetchone()[0],
                'blocked':db.execute("SELECT COUNT(*) FROM tasks WHERE kind='detail' AND state='blocked'").fetchone()[0],
                'bad_tasks':db.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('blocked','failed')").fetchone()[0],
                'bad_partitions':db.execute("SELECT COUNT(*) FROM partitions WHERE state='blocked'").fetchone()[0]}


def report(root, started, duration, initial, target, offset):
    requests=[]
    with (root/'requests.jsonl').open('rb') as stream:
        stream.seek(offset)
        for line in stream:
            if not line.endswith(b'\n'):
                break
            row=json.loads(line)
            requests.append(row)
    final=counts(root)
    latencies=sorted(row['latency_ms'] for row in requests)
    errors=sum(row['category']!='success' for row in requests)
    limits=sum(row.get('http_status') in (429,503) for row in requests)
    return {'started_at':started,'finished_at':now(),'target_rps':target,'duration_seconds':round(duration,2),
            'details_before':initial['details'],'details_after':final['details'],
            'committed_details':final['details']-initial['details'],
            'committed_rps':round((final['details']-initial['details'])/max(1,duration),3),
            'committed_work_rps':round((final['completed_tasks']-initial['completed_tasks'])/max(1,duration),3),
            'new_journals':final['records']-initial['records'],
            'new_journals_rps':round((final['records']-initial['records'])/max(1,duration),3),
            'new_bad_tasks':final['bad_tasks']-initial['bad_tasks'],
            'new_bad_partitions':final['bad_partitions']-initial['bad_partitions'],
            'detail_requests':sum(row.get('endpoint')=='get' for row in requests),
            'search_requests':sum(row.get('endpoint')=='search' for row in requests),
            'new_blocked_details':final['blocked']-initial['blocked'],
            'requests':len(requests),'errors':errors,'error_fraction':errors/max(1,len(requests)),
            'retry_requests':sum(row['attempt']>1 for row in requests), 'http_429_503':limits,
            'latency_p95_ms':latencies[int((len(latencies)-1)*.95)] if latencies else None}


def acceptable(result, target, seconds):
    # For mixed workloads, a verified page is one completed request, not 50 details.
    mixed=result.get('workload')=='mixed'
    throughput=result['committed_work_rps'] if mixed else result['committed_rps']
    return (result['duration_seconds']>=seconds and throughput>=target*.8
            and (not mixed or result['committed_rps']>=max(result.get('minimum_detail_rps',0),target*.6))
            and result.get('new_bad_tasks',0)<=0 and result.get('new_bad_partitions',0)<=0
            and result['error_fraction']<=.01 and result['http_429_503']==0
            and result['latency_p95_ms'] is not None and result['latency_p95_ms']<=1500)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--target',type=float,required=True,choices=[6,8,10,12])
    parser.add_argument('--fallback',type=float,required=True,choices=[4,6,8,10])
    parser.add_argument('--minimum-detail-rps',type=float,default=0)
    parser.add_argument('--seconds',type=int,default=300)
    args=parser.parse_args()
    if args.seconds<300 or args.fallback>=args.target:
        parser.error('Each stage requires at least 300 seconds and a lower fallback')
    root=Path(__file__).resolve().parent/'data'/'scholay'
    baseline=call(root)
    identity=baseline['started_at']
    result={'started_at':now(),'target_rps':args.target,'accepted':False}
    try:
        call(root,'configure_rate',args.target)
        deadline=time.monotonic()+600
        while True:
            state=call(root)
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(state['heartbeat_at'])).total_seconds()
            if state['started_at']!=identity or age>30 or state['status']!='running':
                raise RuntimeError('Collector changed, heartbeat stale or acquisition not running')
            if state['rate']['limit_rps']>=args.target:
                break
            if time.monotonic()>deadline:
                raise RuntimeError('Target rate not reached during warmup')
            atomic_json(root/'speed_trial_status.json',{'phase':'warmup','target_rps':args.target,'at':now(),'rate':state['rate']['limit_rps']})
            print(json.dumps({'phase':'warmup','rate':state['rate']['limit_rps'],'target':args.target}),flush=True)
            time.sleep(10)
        initial=counts(root)
        offset=(root/'requests.jsonl').stat().st_size
        started=now()
        start=time.monotonic()
        while True:
            time.sleep(min(10,max(.1,args.seconds-(time.monotonic()-start))))
            state=call(root)
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(state['heartbeat_at'])).total_seconds()
            if state['started_at']!=identity or age>30 or state['status']!='running':
                raise RuntimeError('Collector changed, heartbeat stale or acquisition not running')
            result=report(root,started,time.monotonic()-start,initial,args.target,offset)
            result['workload']='mixed' if (baseline.get('partitioning') or {}).get('enabled') else 'details'
            result['minimum_detail_rps']=args.minimum_detail_rps
            result['network_mode']=baseline.get('network_mode','thread')
            result['pipeline']=state.get('pipeline')
            result['phase']='measuring'
            atomic_json(root/'speed_trial_status.json',result)
            print(json.dumps(result),flush=True)
            if result['http_429_503'] or result['duration_seconds']>=args.seconds:
                break
        result['accepted']=acceptable(result,args.target,args.seconds)
    except Exception as exc:
        result.update(accepted=False,error=type(exc).__name__+': '+str(exc))
    finally:
        if not result.get('accepted'):
            try:
                call(root,'configure_rate',args.fallback)
                result['fallback_rps']=args.fallback
            except Exception as exc:
                result['fallback_error']=type(exc).__name__+': '+str(exc)
        result.update(phase='finished',finished_at=now())
        atomic_json(root/'speed_trial_status.json',result)
        with (root/'speed_trials.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(result,ensure_ascii=False)+'\n')
        print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if result.get('accepted') else 1


if __name__=='__main__':
    raise SystemExit(main())
