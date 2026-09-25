"""Build the read-only journal projection; keeps the personal library empty."""
import argparse
import json
import time
from pathlib import Path
from client_backend.library import Library
from client_backend.journals import Journals

parser = argparse.ArgumentParser()
parser.add_argument('--library', required=True)
args = parser.parse_args()
library = Library(args.library)
library.set_settings({'journalRoot': str(Path('data/scholay').resolve())})
journals = Journals(library)
start = time.perf_counter()
last = 0
def progress(value, message):
    global last
    if value - last >= .2:
        print(message, flush=True)
        last = value
index = journals.index({}, progress)
checks = []
for params in ({'q':'0028-0836'}, {'filters': {'fqb':1,'jcr':'Q1','minImpact':10}}, {'page':2501,'pageSize':8}, {'sort':'works'}):
    tick = time.perf_counter()
    result = journals.search(params)
    checks.append({'params':params, 'total':result['total'], 'returned':len(result['items']), 'ms':round((time.perf_counter()-tick)*1000,2), 'first':result['items'][0]['name'] if result['items'] else None})
detail = journals.get(3078)
print(json.dumps({'index':index,'seconds':round(time.perf_counter()-start,2),'checks':checks,'nature':{'name':detail['name'],'series':{k:len(v) for k,v in detail['metrics'].items()}},'personalItems':library.stats()['items']},ensure_ascii=False),flush=True)
