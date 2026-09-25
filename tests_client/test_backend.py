from contextlib import closing
import json
import sqlite3
import tempfile
import time
from pathlib import Path
import unittest

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject

from client_backend.attachments import Attachments
from client_backend.backups import Backups
from client_backend.biblio import parse_bib, parse_ris, parse_file, export_records
from client_backend.common import AppError, sha256
from client_backend.imports import Imports
from client_backend.journals import Journals
from client_backend.jobs import Jobs
from client_backend.library import Library
from client_backend.service import Application


def fixture_pdf(path, pages=2):
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    for index in range(pages):
        page = writer.add_blank_page(612, 792)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 22 Tf 52 720 Td (Research Library - Verification Document) Tj '
                         '/F1 11 Tf 0 -36 Td (This document is generated only for desktop application testing.) Tj '
                         '0 -25 Td (It is not the original paper or a research result.) Tj '
                         '0 -40 Td (Attention connects queries, keys, and values.) Tj '
                         '0 -22 Td (Select this sentence to create a highlight annotation.) Tj '
                         '0 -22 Td (Annotations and reading positions are stored in the local library.) Tj '
                         f'0 -45 Td (Page {index + 1}: searchable verificationfixture content.) Tj ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer.add_metadata({'/Title': 'Desktop verification fixture', '/Author': 'Research Library QA'})
    writer.add_outline_item('Introduction', 0)
    if pages > 1:
        writer.add_outline_item('Details', 1)
    with open(path, 'wb') as out:
        writer.write(out)


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = Library(self.root / 'library')
        self.attachments = Attachments(self.library)
        self.pdf = self.root / 'fixture.pdf'
        fixture_pdf(self.pdf)

    def tearDown(self):
        self.temp.cleanup()

    def item(self, title='Attention fixture', **extra):
        return self.library.create({'title': title, 'author': [{'family': 'Wang', 'given': 'Mei'}], 'year': '2024', **extra})

    def test_revision_search_and_bulk_atomicity(self):
        i = self.item(tags=['中文标签'], DOI='https://doi.org/10.1234/EXAMPLE')
        j = self.library.update(i['id'], {'abstract': '这是中文摘要', 'readingState': 'read'}, 1)
        self.assertEqual(j['revision'], 2)
        with self.assertRaises(AppError):
            self.library.update(i['id'], {'title': 'old'}, 1)
        self.assertEqual(self.library.query({'q': '中文摘要'})['total'], 1)
        self.assertEqual(self.library.query({'q': '10.1234/example', 'field': 'doi'})['total'], 1)
        with self.assertRaises(AppError):
            self.library.bulk([i['id'], 'missing'], 'trash')
        self.assertIsNone(self.library.get(i['id'])['deletedAt'])
        self.library.bulk([i['id']], 'trash')
        self.assertEqual(self.library.query({})['total'], 0)
        self.assertEqual(self.library.query({'view': 'trash'})['total'], 1)
        self.library.bulk([i['id']], 'restore')
        self.assertEqual(self.library.query({'q': "' OR 1=1 --", 'field': 'title'})['total'], 0)

    def test_collection_cycle_and_delete_preserves_items(self):
        a = self.library.collection_edit('create', {'name': 'Parent'})['id']
        b = self.library.collection_edit('create', {'name': 'Child', 'parentId': a})['id']
        with self.assertRaises(AppError): self.library.collection_edit('move', {'id': a, 'parentId': b})
        item = self.item()
        self.library.bulk([item['id']], 'collection', b)
        self.library.collection_edit('delete', {'id': b})
        self.assertEqual(self.library.query({})['total'], 1)

    def test_attachment_dedup_text_annotation_export_and_relink(self):
        i = self.item()
        a = self.attachments.add(i['id'], self.pdf)
        self.assertTrue(self.attachments.add(i['id'], self.pdf)['duplicate'])
        self.attachments.index(a['id'])
        self.assertEqual(self.library.query({'q': 'verificationfixture'})['total'], 1)
        record = self.attachments.record(a['id'])
        original_hash = sha256(self.attachments.path(a['id']))
        ann = self.attachments.save_annotation(a['id'], {'version': record['version'], 'type': 'highlight', 'pageIndex': 0, 'rects': [[50, 580, 380, 602]], 'quote': 'Attention', 'color': '#ffff00', 'comment': '检验'})
        with self.assertRaises(AppError): self.attachments.save_annotation(a['id'], {**ann, 'version': 'old'})
        note = self.attachments.excerpt_note(ann['id'])
        self.assertIn('research://attachment/', note['content'])
        export = self.root / 'annotated.pdf'
        self.attachments.export(a['id'], export)
        self.assertEqual(PdfReader(export).pages[0]['/Annots'][0].get_object()['/Subtype'], '/Highlight')
        self.assertEqual(sha256(self.attachments.path(a['id'])), original_hash)
        replacement = self.root / 'new.pdf'
        fixture_pdf(replacement, 3)
        linked = self.attachments.relink(a['id'], replacement)
        self.assertEqual(len(self.library.get(i['id'])['attachments']), 2)
        self.assertEqual(len(self.attachments.list_annotations(a['id'])), 1)

    def test_notes_history_merge_undo(self):
        a, b = self.item(DOI='10.1/same'), self.item(DOI='10.1/same', tags=['tag'])
        self.attachments.add(b['id'], self.pdf)
        note = self.library.note_save({'itemId': b['id'], 'title': 'Note', 'content': 'first'})
        note = self.library.note_save({**note, 'content': 'second'})
        self.assertEqual(self.library.note_history(note['id'])[0]['content'], 'first')
        self.assertEqual(len(self.library.duplicates()), 1)
        merged = self.library.merge(a['id'], [b['id']])
        self.assertEqual(self.library.get(a['id'])['noteCount'], 1)
        self.assertEqual(self.library.get(a['id'])['tags'], ['tag'])
        self.library.undo_merge(merged['id'])
        self.assertEqual(self.library.get(b['id'])['noteCount'], 1)
        self.assertEqual(self.library.get(a['id'])['tags'], [])
        self.assertEqual(self.library.query({})['total'], 2)

    def test_bib_ris_json_roundtrip(self):
        source = '@article{Wang2024, title={中文 Research}, author={Wang, Mei and {WHO}}, year={2024}, journal={Journal}, doi={10.1/abc}, customfield={preserve me}}'
        items = parse_bib(source)
        exported = export_records(items, 'bibtex')
        self.assertIn('preserve me', exported)
        self.assertEqual(parse_bib(exported)[0]['title'], '中文 Research')
        ris = export_records(items, 'ris')
        self.assertEqual(parse_ris(ris)[0]['author'][0]['family'], 'Wang')
        path = self.root / 'utf16.ris'
        path.write_text(ris, encoding='utf-16')
        self.assertEqual(parse_file(path)[0]['title'], '中文 Research')
        self.assertIn('中文', export_records(items, 'json'))
        latex = parse_bib('@article{x,title={Full date},author={{Research and Development} and Wang, Mei},journaltitle={Journal},date={2024-03-11}}')[0]
        self.assertEqual(latex['author'][0]['literal'], 'Research and Development')
        self.assertEqual(latex['issued']['date-parts'][0], [2024, 3, 11])
        self.assertIn('2024-03-11', export_records([latex], 'biblatex'))

    def test_merge_undo_refuses_to_remove_later_collection_edit(self):
        a, b = self.item(), self.item()
        event = self.library.merge(a['id'], [b['id']])
        collection = self.library.collection_edit('create', {'name':'New collection'})
        self.library.bulk([a['id']], 'collection', collection['id'])
        with self.assertRaises(AppError): self.library.undo_merge(event['id'])
        self.assertIn(collection['id'], self.library.get(a['id'])['collections'])

    def test_import_resumable_and_duplicate(self):
        jobs = Jobs(self.library)
        jobs.handlers['pdf.index'] = lambda p, progress: self.attachments.index(p['attachmentId'], progress)
        imports = Imports(self.library, self.attachments, jobs)
        try:
            preview = imports.preview([str(self.pdf)])
            result = imports.commit({'batchId': preview['batchId']}, lambda *a: None)
            self.assertFalse(result['errors'])
            again = imports.commit({'batchId': preview['batchId']}, lambda *a: None)
            self.assertEqual(result['itemIds'], again['itemIds'])
            self.assertEqual(self.library.stats()['items'], 1)
            self.assertTrue(imports.preview([str(self.pdf)])['entries'][0]['duplicate'])
        finally:
            jobs.close()

    def test_backup_restore_and_tamper(self):
        i = self.item()
        a = self.attachments.add(i['id'], self.pdf, 'linked')
        self.library.note_save({'itemId': i['id'], 'title': '中文', 'content': 'test'})
        backups = Backups(self.library)
        backup = backups.create({'directory': str(self.root)}, lambda *a: None)
        self.assertTrue(backups.verify(backup['path'])['verified'])
        restored = backups.restore({'backup': backup['path'], 'directory': str(self.root)}, lambda *a: None)
        fresh = Library(restored['path'])
        self.assertEqual(fresh.stats()['notes'], 1)
        record = Attachments(fresh).record(a['id'])
        self.assertEqual(record['mode'], 'managed')
        self.assertTrue(Path(record['path']).is_file())
        self.pdf.unlink()
        self.assertTrue(Path(record['path']).is_file())
        manifest = Path(backup['path']) / 'manifest.json'
        value = json.loads(manifest.read_text(encoding='utf-8'))
        value['objects'][0]['path'] = '../fixture.pdf'
        manifest.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(AppError): backups.verify(backup['path'])

    def test_journal_index_far_pages_missing_metrics_and_descending_ids(self):
        source = self.root / 'journals'
        source.mkdir()
        with closing(sqlite3.connect(source / 'journals.sqlite3')) as db:
            db.execute('CREATE TABLE journals(id INTEGER PRIMARY KEY, canonical_name TEXT,list_json TEXT,detail_json TEXT,detail_status TEXT,fetched_at TEXT)')
            for n in range(10001, 20006):
                unified = {'canonical_name': 'Journal ' + str(n), 'jcr_impact_factor': n % 30 or None, 'jcr_quartile': 'Q1', 'fqb_major_quartile': 1, 'print_issn': '1234-567X', 'country': 'CN', 'fqb_major_category': '测试', 'openalex_works_count': n}
                db.execute('INSERT INTO journals VALUES(?,?,?,NULL,?,?)', (n, unified['canonical_name'], json.dumps({'unified': unified}), 'pending', '2026-09-21'))
            db.commit()
        self.library.set_settings({'journalRoot': str(source)})
        journals = Journals(self.library)
        first = journals.index({}, lambda *a: None)
        self.assertEqual(first['count'], 10005)
        self.assertEqual(journals.search({'page': 1001, 'pageSize': 8})['items'].__len__(), 8)
        self.assertEqual(journals.search({'q': '1234567x', 'filters': {'fqb': '1', 'jcr': 'Q1'}})['total'], 10005)
        with closing(sqlite3.connect(source / 'journals.sqlite3')) as db:
            db.execute('INSERT INTO journals VALUES(1,?,?,NULL,?,?)', ('Earliest', json.dumps({'unified': {'canonical_name': 'Earliest'}}), 'pending', 'now'))
            db.commit()
        journals.index({}, lambda *a: None)
        self.assertEqual(journals.search({'q': 'Earliest'})['total'], 1)
        self.assertEqual(journals.search({'indexVersion': first['generation']})['total'], 10005)
        self.assertEqual(journals.catalog()['country'][0]['count'], 10005)
        self.assertEqual(journals.get(1)['name'], 'Earliest')

    def test_service_whitelist_and_offline_metadata(self):
        app = Application(self.root / 'service')
        try:
            app.call('settings.save', {'online': False})
            with self.assertRaises(AppError): app.call('metadata.lookup', {'identifier': '10.1000/example'})
            with self.assertRaises(AppError): app.call('sql.execute', {'sql': 'DROP TABLE items'})
            self.assertEqual(app.call('library.stats', {})['items'], 0)
        finally:
            app.close()

    def test_writing_sessions_preserve_citation_options(self):
        first, second = self.item('First source'), self.item('Second source')
        session = self.library.writing_session_save({
            'id': 'session-12345678', 'host': 'word', 'documentFingerprint': 'unsaved:fixture', 'documentName': 'fixture.docx',
            'styleId': 'gb-t-7714-2015', 'locale': 'zh-CN', 'state': {'version': 1}, 'eventType': 'refresh',
            'bibliography': {'anchorKey': 'research-library:bibliography'},
            'clusters': [{
                'id': 'cluster-12345678', 'anchorKey': 'research-library:citation:cluster-12345678',
                'citation': {'properties': {'noteIndex': 0}}, 'renderedText': '[1,2]',
                'items': [
                    {'itemId': first['id'], 'locator': '12', 'label': 'page', 'prefix': '见', 'suffix': '', 'suppressAuthor': False},
                    {'itemId': second['id'], 'locator': '', 'label': 'page', 'prefix': '', 'suffix': '及其讨论', 'suppressAuthor': True},
                ],
            }],
        })
        self.assertEqual(session['citationCount'] if 'citationCount' in session else len(session['clusters']), 1)
        saved = self.library.writing_session('session-12345678')
        self.assertEqual(saved['clusters'][0]['items'][0]['locator'], '12')
        self.assertTrue(saved['clusters'][0]['items'][1]['suppressAuthor'])
        self.assertEqual(self.library.writing_sessions()[0]['documentName'], 'fixture.docx')
        with self.assertRaises(AppError):
            self.library.writing_session_save({'id': 'bad', 'host': 'word', 'documentFingerprint': '', 'clusters': []})

    def test_search_comparison_preserves_fields_and_reports_difference(self):
        saved = self.library.comparison_save({
            'query': '多车间、多目标作业调度', 'source': '阙问 Paper', 'sourceUrl': 'https://qiewenpaper.com/app/search',
            'external': {'title': '无关的快速检索结果', 'authors': '甲', 'year': '2026', 'venue': '', 'doi': ''},
            'local': {'title': 'An improved memetic algorithm for multi-objective resource-constrained flexible job shop inverse scheduling problem: An application for machining workshop',
                      'authors': 'Yu', 'year': '2024', 'venue': 'Journal of Manufacturing Systems', 'doi': 'https://doi.org/10.1016/j.jmsy.2024.03.005'},
            'conclusion': '外部快速检索未召回主题相关论文；本地公开学术源返回可核验候选。',
        })
        self.assertEqual(saved['fields'][0]['status'], 'different')
        self.assertEqual(saved['local']['doi'], '10.1016/j.jmsy.2024.03.005')
        self.assertEqual(self.library.comparison_list()[0]['id'], saved['id'])


if __name__ == '__main__':
    unittest.main()
