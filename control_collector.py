"""Control the local collector without accessing its authentication credentials."""
import argparse
import json
from pathlib import Path
import urllib.request


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['status','pause','resume','retry_failed','export','stop','configure_rate','configure_scheduler','start_partitions','audit_coverage','pause_partitions','resume_partitions'])
    parser.add_argument('--max-rps',type=float)
    parser.add_argument('--scope',choices=['pilot','all'],default='all')
    parser.add_argument('--scheduler-mode',choices=['balanced','discovery_first'],default='discovery_first')
    args=parser.parse_args()
    if args.action=='configure_rate' and (args.max_rps is None or not .5<=args.max_rps<=12):
        parser.error('configure_rate requires --max-rps between 0.5 and 12')
    root=Path(__file__).resolve().parent/'data'/'scholay'
    handoff=json.loads((root/'handoff.json').read_text(encoding='utf-8'))
    headers={'Origin':'https://www.scholay.com','Content-Type':'application/json'}
    request=urllib.request.Request(handoff['status_url'] if args.action=='status' else handoff['control_url'],
        data=None if args.action=='status' else json.dumps({'action':args.action,'max_rps':args.max_rps,'scope':args.scope,'scheduler_mode':args.scheduler_mode}).encode(),headers=headers,
        method='GET' if args.action=='status' else 'POST')
    with urllib.request.urlopen(request,timeout=10) as response:
        print(json.dumps(json.load(response),ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
