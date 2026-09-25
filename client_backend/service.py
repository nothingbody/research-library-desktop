from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import html
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback

from .attachments import Attachments
from .backups import Backups
from .biblio import export_records
from .common import AppError, dumps, require
from .imports import Imports
from .jobs import Jobs
from .journals import Journals
from .library import Library
from .metadata import lookup
from .downloads import Downloads
from .fulltext import Fulltext
from .documents import Documents
from .research_ask import ResearchAsk
from .organization import Organization
from .assistant import ReadingAssistant
from .ai_search import AiSearch
from .search_evaluation import SearchEvaluation
from .relations import Relations
from .relation_discovery import RelationDiscovery
from .browser_capture import capture as browser_capture, check_duplicate as browser_check_duplicate, import_downloaded as browser_import_downloaded


class Application:
    def __init__(self, root, emit=lambda *args: None, journal_root=None):
        self.library = Library(root)
        self.lock_file = open(self.library.root / '.instance.lock', 'a+b')
        self.lock_file.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                self.lock_file.write(b'0')
                self.lock_file.flush()
                self.lock_file.seek(0)
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.lock_file.close()
                raise AppError('LIBRARY_IN_USE', '该文献库已在另一个窗口中打开')
        self.attachments = Attachments(self.library)
        self.jobs = Jobs(self.library, emit)
        self.imports = Imports(self.library, self.attachments, self.jobs)
        self.downloads = Downloads(self.library, self.attachments, self.jobs)
        self.fulltext = Fulltext(self.library, self.downloads, self.jobs)
        self.documents = Documents(self.library)
        self.backups = Backups(self.library)
        self.journals = Journals(self.library)
        self.assistant = ReadingAssistant(self.library, self.jobs)
        self.research_ask = ResearchAsk(self.library, self.documents, self.assistant, self.jobs)
        self.organization = Organization(self.library)
        self.ai_search = AiSearch(self.library, self.jobs, self.assistant)
        self.search_evaluation = SearchEvaluation(self.library, self.ai_search)
        self.relations = Relations(self.library, self.jobs, self.assistant)
        self.relation_discovery = RelationDiscovery(self.library, self.jobs, self.ai_search)
        self.emit = emit
        if journal_root and not self.library.get_settings().get('journalRoot') and (Path(journal_root) / 'journals.sqlite3').is_file():
            self.library.set_settings({'journalRoot': str(Path(journal_root).resolve())})
        self.jobs.handlers = {
            'import': self.imports.commit,
            'pdf.index': lambda p, progress: self.attachments.index(p['attachmentId'], progress),
            'journal.index': self.journals.index,
            'library.index': self.library.rebuild_index,
            'backup': self.backups.create,
            'restore': self.backups.restore,
            'pdf.download': self.downloads.pdf,
            'fulltext.obtain': self.fulltext.obtain,
            'metadata.lookup': lambda p, progress: self.lookup(p, progress),
            'assistant.run': self.assistant.run,
            'research-ask.run': self.research_ask.run,
            'ai-search.run': self.ai_search.run,
            'ai-search.expand': self.ai_search.expand,
            'ai-search.citations': self.ai_search.citations,
            'ai-search.rerank': self.ai_search.rerank,
            'ai-search.verify': self.ai_search.verify,
            'relations.profile': self.relations.run_profile,
            'relations.run': self.relations.run,
            'relations.discover': self.relation_discovery.run,
        }
        # Assistant tasks wait for Electron to inject the encrypted, memory-only key.
        self.jobs.recover(kinds={'assistant.run', 'ai-search.verify', 'ai-search.rerank', 'research-ask.run'}, submit=False)
        self.jobs.recover(kinds={'import', 'pdf.index', 'journal.index', 'library.index', 'backup', 'restore', 'pdf.download', 'fulltext.obtain', 'metadata.lookup', 'ai-search.run', 'ai-search.expand', 'ai-search.citations', 'relations.profile', 'relations.run', 'relations.discover'})
        settings = self.library.get_settings()
        if settings.get('autoBackup') and settings.get('backupDirectory') and Path(settings['backupDirectory']).is_dir():
            from datetime import datetime, timezone
            with self.library.db() as db:
                last = db.execute("SELECT created_at FROM jobs WHERE kind='backup' AND state='completed' ORDER BY created_at DESC LIMIT 1").fetchone()
            due = not last or (datetime.now(timezone.utc) - datetime.fromisoformat(last[0])).total_seconds() >= 86400
            if due:
                self.jobs.create('backup', {'directory': settings['backupDirectory']})

    def call(self, method, p):
        library, attachments = self.library, self.attachments
        routes = {
            'app.info': lambda: {'version': '0.9.27', 'root': str(library.root), 'settings': library.get_settings()},
            'library.stats': library.stats,
            'library.reindex': lambda: self.jobs.create('library.index', {}),
            'items.list': lambda: library.query(p),
            'items.get': lambda: library.get(p['id']),
            'items.create': lambda: library.create(p['data'], p.get('collectionId')),
            'browser.capture': lambda: browser_capture(library, self.fulltext, p),
            'browser.duplicates': lambda: browser_check_duplicate(library, p),
            'browser.importDownloaded': lambda: browser_import_downloaded(library, self.fulltext, p),
            'items.update': lambda: library.update(p['id'], p['patch'], p['revision']),
            'items.bulk': lambda: library.bulk(p['ids'], p['action'], p.get('value')),
            'items.duplicates': library.duplicates,
            'items.merge': lambda: library.merge(p['targetId'], p['sourceIds']),
            'items.undoMerge': lambda: library.undo_merge(p['eventId']),
            'collections.list': library.collections,
            'collections.edit': lambda: library.collection_edit(p['action'], p),
            'notes.list': lambda: library.notes_list(p.get('itemId'), p.get('q', '')),
            'notes.save': lambda: library.note_save(p),
            'notes.history': lambda: library.note_history(p['id']),
            'notes.delete': lambda: library.notes_delete(p['id']),
            'attachments.get': lambda: self.attachment_info(p['id']),
            'attachments.position': lambda: attachments.position(p['id'], p),
            'reading.get': lambda: library.reading_get(p['itemId'], p.get('attachmentId')),
            'reading.save': lambda: library.reading_save(p),
            'reading.activity': lambda: library.reading_activity(p),
            'terms.list': lambda: library.terms_list(p['itemId'], p.get('q', '')),
            'terms.save': lambda: library.terms_save(p),
            'terms.delete': lambda: library.terms_delete(p['id'], p['revision']),
            'assistant.status': self.assistant.status,
            'assistant.settings': lambda: self.assistant.settings(p),
            'assistant.test': self.assistant.test,
            'assistant.run': lambda: self.assistant.submit(p),
            'assistant.runs': lambda: self.assistant.runs(p['itemId'], p.get('limit', 30), p.get('task'), p.get('publicOnly', False)),
            'assistant.translation.page': lambda: self.assistant.translation_page(p),
            'assistant.apply': lambda: self.assistant.apply(p),
            # These routes are only called by Electron main after safeStorage decrypts the key.
            'assistant.configure': lambda: self.assistant.configure(p.get('apiKey')),
            'assistant.clear': self.assistant.clear,
            'aiSearch.create': lambda: self.ai_search.create(p),
            'aiSearch.list': lambda: self.ai_search.list(p.get('limit', 100)),
            'aiSearch.get': lambda: self.ai_search.get(p['sessionId']),
            'aiSearch.savePlan': lambda: self.ai_search.save_plan(p),
            'aiSearch.run': lambda: self.ai_search.submit(p),
            'aiSearch.expand': lambda: self.ai_search.submit_expand(p),
            'aiSearch.citationExpand': lambda: self.ai_search.submit_citations(p),
            'aiSearch.citationLinks': lambda: self.ai_search.citation_links(p),
            'aiSearch.rerank': lambda: self.ai_search.submit_rerank(p),
            'aiSearch.results': lambda: self.search_results(p),
            'aiSearch.evidence': lambda: self.ai_search.evidence(p['candidateId']),
            'aiSearch.intro': lambda: self.ai_search.intro(p),
            'aiSearch.journalMatch': lambda: self.journal_match(p['candidateId']),
            'aiSearch.import': lambda: self.ai_search.import_candidates(p),
            'aiSearch.cancel': lambda: self.ai_search.cancel(p),
            'aiSearch.verify': lambda: self.ai_search.submit_verify(p),
            'aiSearch.decision': lambda: self.ai_search.decision(p),
            'searchEvaluation.report': lambda: self.search_evaluation.report(p),
            'searchEvaluation.save': lambda: self.search_evaluation.save(p),
            'relations.profile.get': lambda: self.relations.profile_get(p['itemId']),
            'relations.profile.run': lambda: self.relations.submit_profile(p),
            'relations.create': lambda: self.relations.create(p),
            'relations.list': lambda: self.relations.list(p.get('limit', 100)),
            'relations.get': lambda: self.relations.get(p['sessionId']),
            'relations.runs': lambda: self.relations.runs(p['sessionId']),
            'relations.diff': lambda: self.relations.diff(p),
            'relations.history': lambda: self.relations.history(p['relationId']),
            'relations.run': lambda: self.relations.submit(p),
            'relations.results': lambda: self.relations.results(p),
            'relations.evidence': lambda: self.relations.evidence(p['relationId']),
            'relations.confirm': lambda: self.relations.confirm(p),
            'relations.export': lambda: self.relations.export(p),
            'relations.forItem': lambda: self.relations.for_item(p['itemId'], p.get('limit', 20)),
            'relations.discover': lambda: self.relation_discovery.submit(p),
            'relations.discoveries': lambda: self.relation_discovery.list(p),
            'relations.discoveryProgress': lambda: self.relation_discovery.progress(p),
            'relations.discoveryDecide': lambda: self.relation_discovery.decide(p),
            'relations.manualList': lambda: self.relations.manual_list(p['sessionId']),
            'relations.manualAdd': lambda: self.relations.manual_add(p),
            'relations.manualRemove': lambda: self.relations.manual_remove(p),
            'relations.graphExport': lambda: self.relation_discovery.export_graph(p, self.relations),
            'attachments.download': lambda: self.jobs.create('pdf.download', p),
            'attachments.setRole': lambda: attachments.set_role(p['id'], p['role']),
            'fulltext.sources': lambda: self.fulltext.sources(p['itemId']),
            'fulltext.add': lambda: self.fulltext.add(p),
            'fulltext.obtain': lambda: self.fulltext.submit(p),
            'documents.search': lambda: self.documents.search(p),
            'researchAsk.create': lambda: self.research_ask.create(p),
            'researchAsk.list': lambda: self.research_ask.list(p.get('limit', 100)),
            'researchAsk.get': lambda: self.research_ask.get(p['conversationId']),
            'researchAsk.send': lambda: self.research_ask.send(p),
            'researchAsk.saveClaim': lambda: self.research_ask.save_claim(p),
            'annotations.search': lambda: self.organization.annotation_search(p),
            'annotations.exportText': lambda: self.organization.annotation_export(p),
            'projects.create': lambda: self.organization.project_create(p),
            'projects.list': lambda: self.organization.project_list(p.get('archived', False)),
            'projects.get': lambda: self.organization.project_get(p['projectId']),
            'projects.archive': lambda: self.organization.project_archive(p),
            'projects.link': lambda: self.organization.project_link(p),
            'smartCollections.save': lambda: self.organization.smart_save(p),
            'smartCollections.list': lambda: self.organization.smart_list(),
            'smartCollections.results': lambda: self.organization.smart_results(p),
            'annotations.list': lambda: attachments.list_annotations(p['attachmentId']),
            'annotations.save': lambda: attachments.save_annotation(p['attachmentId'], p['data']),
            'annotations.delete': lambda: attachments.delete_annotation(p['id'], p['revision']),
            'annotations.excerpt': lambda: attachments.excerpt_note(p['id'], p.get('noteId')),
            'imports.commit': lambda: self.jobs.create('import', p),
            'jobs.list': self.jobs.list,
            'jobs.action': lambda: self.jobs.action(p['id'], p['action']),
            'journals.stats': self.journals.stats,
            'journals.list': lambda: self.journals.search(p),
            'journals.get': lambda: self.journals.get(p['id']),
            'journals.catalog': self.journals.catalog,
            'journals.link': lambda: self.journals.link(library.get(p['itemId'])),
            'journals.related': lambda: self.journals.related(p['id']),
            'journals.ensure': lambda: self.journals.ensure(p['id'], p.get('kind', 'detail')),
            'journals.index': lambda: self.jobs.create('journal.index', {}),
            'collector.control': lambda: self.journals.collector(p.get('action', 'status')),
            'settings.get': library.get_settings,
            'settings.save': lambda: library.set_settings(p),
            'writing.sessions': lambda: library.writing_sessions(p.get('limit', 100)),
            'writing.session': lambda: library.writing_session(p['sessionId']),
            'writing.session.save': lambda: library.writing_session_save(p),
            'writing.event': lambda: library.writing_event(p),
            'comparison.list': lambda: library.comparison_list(p.get('limit', 100)),
            'comparison.save': lambda: library.comparison_save(p),
            'metadata.lookup': lambda: self.metadata(p),
            'metadata.apply': lambda: self.apply_metadata(p),
            'export.text': lambda: export_records([library.get(i) for i in p['ids']], p['format']),
            # Paths only originate from Electron native dialogs / its attachment protocol.
            '_imports.preview': lambda: self.imports.preview(p['paths'], p.get('itemId')),
            '_attachments.path': lambda: {'path': str(attachments.path(p['id'])), **self.attachment_info(p['id'])},
            '_attachments.relink': lambda: attachments.relink(p['id'], p['path']),
            '_attachments.export': lambda: attachments.export(p['id'], p['path']),
            '_backup.create': lambda: self.jobs.create('backup', p),
            '_backup.verify': lambda: self.backups.verify(p['path']),
            '_backup.restore': lambda: self.jobs.create('restore', p),
        }
        if method not in routes:
            raise AppError('METHOD_NOT_FOUND', '不支持的操作')
        result = routes[method]()
        if method in {'items.create', 'browser.capture', 'browser.importDownloaded', 'items.update', 'items.bulk', 'items.merge', 'items.undoMerge', 'collections.edit', 'attachments.setRole',
                      'notes.save', 'notes.delete', 'annotations.excerpt', 'metadata.apply', 'settings.save', 'reading.save',
                      'reading.activity', 'terms.save', 'terms.delete', 'assistant.settings', 'assistant.run', 'assistant.translation.page', 'assistant.apply',
                      'aiSearch.create', 'aiSearch.savePlan', 'aiSearch.run', 'aiSearch.expand', 'aiSearch.citationExpand', 'aiSearch.rerank', 'aiSearch.intro', 'aiSearch.import', 'aiSearch.cancel', 'aiSearch.verify', 'aiSearch.decision', 'searchEvaluation.save',
                      'fulltext.add', 'fulltext.obtain',
                      'researchAsk.create', 'researchAsk.send', 'researchAsk.saveClaim',
                      'projects.create', 'projects.archive', 'projects.link', 'smartCollections.save',
                      'relations.profile.run', 'relations.create', 'relations.run', 'relations.confirm', 'relations.export', 'relations.discover', 'relations.discoveryDecide', 'relations.manualAdd', 'relations.manualRemove',
                      'writing.session.save', 'writing.event', 'comparison.save'}:
            self.emit('data.changed', {'method': method})
        return result

    def search_results(self, params):
        result = self.ai_search.results(params)
        identifiers = {item.get('issn') for item in result['items'] if item.get('issn')}
        if not identifiers:
            return result
        try:
            matches = self.journals.match_issns(identifiers)
        except AppError as exc:
            result['journalIndexMessage'] = str(exc)
            return result
        for item in result['items']:
            identifier = str(item.get('issn') or '').strip().upper().replace('-', '')
            if identifier in matches:
                item['journalMatch'] = matches[identifier]
        return result

    def journal_match(self, candidate_id):
        candidate = self.ai_search.evidence(candidate_id)
        identifier = candidate.get('issn')
        return self.journals.match_issn(identifier) if identifier else {'matched': False, 'reason': '来源未提供 ISSN'}

    def attachment_info(self, attachment_id):
        row = self.attachments.record(attachment_id)
        return {k: v for k, v in row.items() if k not in ('path', 'text_json')} | {
            'itemId': row['item_id'], 'position': json.loads(row['position_json']), 'exists': Path(row['path']).is_file()}

    def metadata(self, params):
        require(self.library.get_settings().get('online', True), '联网已关闭，请先在设置中开启')
        job = self.jobs.create('metadata.lookup', params)
        for _ in range(600):
            with self.library.db() as db:
                row = db.execute('SELECT state,result,error FROM jobs WHERE id=?', (job['jobId'],)).fetchone()
            if row['state'] == 'completed':
                return json.loads(row['result'])
            if row['state'] in ('failed', 'cancelled'):
                error = json.loads(row['error'] or '{}')
                raise AppError(error.get('code', 'METADATA_FAILED'), error.get('message', '元数据任务已取消'))
            time.sleep(.1)
        raise AppError('TIMEOUT', '元数据任务仍在队列中，可在数据中心查看结果')

    def lookup(self, params, progress):
        require(self.library.get_settings().get('online', True), '联网已关闭')
        progress(.1, '查询 DOI / PMID 元数据')
        result = lookup(params['identifier'])
        progress(.95, '元数据已获取，等待人工核对')
        return result

    def apply_metadata(self, p):
        fields = {'title', 'author', 'issued', 'container-title', 'DOI', 'ISSN', 'ISBN', 'volume', 'issue', 'page', 'publisher', 'abstract', 'URL', 'type'}
        require(set(p['patch']) <= fields, '元数据字段不支持')
        with self.library.db(True) as db:
            self.library._update(db, p['id'], p['patch'], p['revision'])
            from .common import uid, now
            db.execute('INSERT INTO provenance VALUES(?,?,?,?,?)', (uid(), p['id'], p.get('source', 'metadata'), dumps(p['patch']), now()))
        return self.library.get(p['id'])

    def close(self):
        self.jobs.close()
        self.lock_file.close()


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser()
    parser.add_argument('--library', required=True)
    parser.add_argument('--journals')
    args = parser.parse_args()
    output_lock = threading.Lock()
    def send(data):
        with output_lock:
            print(dumps(data), flush=True)
    application = Application(args.library, lambda event, data: send({'event': event, 'data': data}), args.journals)
    send({'event': 'ready', 'data': {'root': str(application.library.root)}})
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='rpc')
    def handle(request):
        try:
            result = application.call(request['method'], request.get('params') or {})
            send({'id': request['id'], 'result': result})
        except Exception as exc:
            if not isinstance(exc, AppError):
                traceback.print_exc(file=sys.stderr)
            send({'id': request.get('id'), 'error': {'code': getattr(exc, 'code', 'INTERNAL_ERROR'), 'message': str(exc)[:1000],
                                                   'details': getattr(exc, 'details', None), 'retryable': getattr(exc, 'retryable', False)}})
    try:
        for line in sys.stdin:
            if len(line) > 16 * 1024 * 1024:
                send({'error': {'code': 'TOO_LARGE', 'message': '请求过大'}})
                continue
            try:
                request = json.loads(line)
                if request.get('method') == '_shutdown':
                    break
                pool.submit(handle, request)
            except ValueError:
                send({'error': {'code': 'INVALID_JSON', 'message': '请求格式不正确'}})
    finally:
        pool.shutdown(wait=True)
        application.close()


if __name__ == '__main__':
    main()
