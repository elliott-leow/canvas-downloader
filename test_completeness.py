"""Regression checks for coverage, preservation, identity and bounded recovery."""
from collections import Counter
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import requests

from archive_io import DownloadIndex, trim_log, course_lock
from canvas_content import rich_text, references, offline_html
from canvas_downloader import download_file_from_api, process_page
from canvas_mcp import Archive
from canvas_sync import Sync, course_catalog, select_courses
from course_archive import archive_modules, complete_course, retry_failed

BASE = 'https://canvas.example'


def response(value, url, status=200, headers=None):
    result = requests.Response()
    result.url, result.status_code = url, status
    result._content = value if isinstance(value, bytes) else json.dumps(value).encode()
    result._content_consumed = True
    result.headers.update(headers or {})
    return result


class Canvas(requests.Session):
    """A deterministic Canvas account; no real cookies or network access."""
    def __init__(self):
        super().__init__()
        self.calls = Counter()
        self.errors = {}
        self.user_id = 99
        self.course = {'id': 1, 'name': 'Course', 'syllabus_body': '<p>Course syllabus</p>'}
        self.modules = [{'id': 10, 'name': 'Week', 'position': 1}]
        self.items = [{'type': 'File', 'title': 'Lecture', 'content_id': 1},
                      {'type': 'Page', 'title': 'Intro', 'page_url': 'intro'}]
        self.payloads = {'1': b'%PDF-1.7 lecture', '2': b'\x89PNG\r\n\x1a\nimage', '4': b'outside modules', '5': b'attachment'}
        self.files = {key: {'id': int(key), 'display_name': {'1': 'lecture.pdf', '2': 'diagram.png', '4': 'extra.txt', '5': 'feedback.txt'}[key],
                           'url': BASE + f'/files/{key}/download', 'size': len(value), 'updated_at': 'v1'}
                      for key, value in self.payloads.items()}
        self.page = {'page_id': 3, 'url': 'intro', 'title': 'Intro', 'html_url': BASE + '/courses/1/pages/intro',
                     'body': '<p>Read <a href="https://reading.example/doc">this</a>.</p>'
                             '<img src="/courses/1/files/2/preview" alt="Reaction diagram">'
                             '<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>'}
        self.assignment = {'id': 40, 'name': 'Essay', 'description': '<p>Write an essay.</p>', 'points_possible': 20,
                           'submission_types': ['online_upload'], 'due_at': '2026-11-01',
                           'rubric': [{'description': 'Evidence', 'points': 20, 'ratings': [{'points': 20, 'description': 'Complete'}]}]}

    def get(self, url, **kwargs):
        self.calls[url] += 1
        if url in self.errors:
            return response({}, url, self.errors[url])
        path = url.removeprefix(BASE)
        if path == '/api/v1/users/self':
            return response({'id': self.user_id}, url)
        if path == '/api/v1/courses':
            return response([self.course] if kwargs.get('params', {}).get('enrollment_state') == 'active' else [], url)
        if path == '/api/v1/courses/1':
            return response(self.course, url)
        if re.fullmatch(r'/api/v1/files/\d+', path):
            return response(self.files[path.rsplit('/', 1)[-1]], url)
        if re.fullmatch(r'(?:/courses/1)?/files/\d+/(?:download|preview)', path):
            identifier = path.split('/')[-2]
            return response(self.payloads[identifier], url, headers={'Content-Disposition': f'attachment; filename="{self.files[identifier]["display_name"]}"'})
        if path == '/api/v1/courses/1/modules':
            return response(self.modules, url)
        if path == '/api/v1/courses/1/modules/10/items':
            return response(self.items, url)
        if path == '/api/v1/courses/1/files':
            return response([self.files[key] for key in ('1', '2', '4')], url)
        if path == '/api/v1/courses/1/pages':
            return response([self.page], url)
        if path == '/api/v1/courses/1/pages/intro':
            return response(self.page, url)
        if path == '/api/v1/courses/1/assignments':
            return response([self.assignment], url)
        if path == '/api/v1/courses/1/assignments/40/submissions/self':
            return response({'attempt': 1, 'grade': 'A', 'score': 20, 'body': '<p>My essay.</p>',
                             'attachments': [self.files['5']], 'submission_comments': [{'author_name': 'Teacher', 'comment': 'Good reasoning'}]}, url)
        if path == '/api/v1/courses/1/quizzes':
            return response([{'id': 7, 'title': 'Quiz', 'description': 'Available instructions', 'time_limit': 15}], url)
        if path == '/api/v1/calendar_events':
            return response([{'id': 8, 'title': 'Office hours', 'start_at': '2026-11-01', 'description': 'Bring questions'}], url)
        if path == '/api/v1/courses/1/discussion_topics':
            return response([{'id': 20, 'title': 'News', 'message': '<p>Announcement</p>', 'is_announcement': True}]
                            if kwargs.get('params', {}).get('only_announcements') else
                            [{'id': 21, 'title': 'Thread', 'message': '<p>Discuss</p>', 'attachments': [self.files['5']]}], url)
        if path.endswith('/view'):
            return response({'view': [{'id': 10, 'message': '<p>Answer</p>'}], 'participants': []}, url)
        if path == '/api/v1/courses/1/groups':
            return response([{'id': 60}], url)
        if path == '/api/v1/groups/60/pages':
            return response([dict(self.page, page_id=61, title='Group page')], url)
        if path == '/api/v1/groups/60/files':
            return response([self.files['4']], url)
        return response([], url)


class CompletenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.canvas = Canvas()

    def archive(self, personal=False, groups=False):
        result = archive_modules(self.canvas, BASE, '1', self.canvas.modules, self.root, report=lambda _: None)
        return complete_course(self.canvas, BASE, '1', self.root, result, 'Course', personal, groups)

    def test_whole_course_reuses_module_assets_and_preserves_fidelity(self):
        result = self.archive()
        self.assertFalse(result.partial, result.content_errors)
        self.assertEqual(self.canvas.calls[BASE + '/files/1/download'], 1)
        self.assertEqual(sum(n for url, n in self.canvas.calls.items() if re.search('/files/2/(download|preview)', url)), 1)
        self.assertEqual(len(list(self.root.rglob('lecture.pdf'))), 1)
        self.assertEqual(len(list(self.root.rglob('Intro.txt'))), 1)
        text = (self.root / 'Week/Intro.txt').read_text()
        self.assertIn('https://reading.example/doc', text)
        self.assertIn('A | B', text)
        companion = (self.root / 'Week/Intro.html').read_text()
        self.assertIn('diagram.png', companion)
        self.assertNotIn('src="/courses/', companion)
        essay = next(self.root.rglob('*Essay.txt')).read_text()
        self.assertIn('Evidence', essay)
        self.assertIn('online_upload', essay)
        self.assertTrue(list(self.root.rglob('extra.txt')))
        self.assertTrue(list(self.root.rglob('*News.txt')))
        self.assertTrue(list(self.root.rglob('*Thread.txt')))
        self.assertTrue(list(self.root.rglob('*Quiz.txt')))
        self.assertTrue(list(self.root.rglob('*Office hours.txt')))
        self.assertFalse(any('/submissions/self' in url for url in self.canvas.calls))
        before = set(path.relative_to(self.root) for path in self.root.rglob('*') if path.is_file())
        self.archive()
        self.assertEqual(before, {path.relative_to(self.root) for path in self.root.rglob('*') if path.is_file()})
        self.assertEqual(self.canvas.calls[BASE + '/files/1/download'], 1)
        self.assertEqual(sum(n for url, n in self.canvas.calls.items() if re.search('/files/2/(download|preview)', url)), 1)

    def test_no_modules_still_archives_course_and_optional_own_material(self):
        self.canvas.modules = []
        result = self.archive(personal=True, groups=True)
        self.assertFalse(result.partial, result.content_errors)
        self.assertIn('Good reasoning', next(self.root.rglob('Your submissions/40.txt')).read_text())
        self.assertTrue(list(self.root.rglob('*Group page.txt')))
        self.assertEqual(self.canvas.calls[BASE + '/files/4/download'], 1)

    def test_retry_requests_failed_category_only_and_keeps_successes(self):
        failed = BASE + '/api/v1/courses/1/pages'
        self.canvas.errors[failed] = 503
        result = self.archive()
        self.assertTrue(result.partial)
        before = self.canvas.calls.copy()
        del self.canvas.errors[failed]
        retry_failed(self.canvas, BASE, '1', self.root, result)
        self.assertFalse(result.partial, result.content_errors)
        changed = self.canvas.calls - before
        self.assertEqual(dict(changed), {failed: 1})
        self.assertEqual(self.canvas.calls[BASE + '/files/1/download'], 1)

    def test_html_and_truncated_downloads_do_not_replace_complete_files(self):
        self.archive()
        target = self.root / 'Week/lecture.pdf'
        original = target.read_bytes()
        self.canvas.payloads['1'] = b'<html>Sign in</html>'
        self.canvas.files['1']['updated_at'] = 'v2'
        ok, _ = download_file_from_api(self.canvas, BASE + '/api/v1/files/1', target.parent, overwrite=True)
        self.assertFalse(ok)
        self.assertEqual(target.read_bytes(), original)
        self.canvas.payloads['1'] = b'short'
        ok, _ = download_file_from_api(self.canvas, BASE + '/api/v1/files/1', target.parent, overwrite=True)
        self.assertFalse(ok)
        self.assertEqual(target.read_bytes(), original)

    def test_file_identity_prevents_collision_and_repeat_copies(self):
        self.canvas.files['4']['display_name'] = 'lecture.pdf'
        self.canvas._canvas_archive = DownloadIndex(self.root)
        for identifier in ('1', '4', '1'):
            self.assertTrue(download_file_from_api(self.canvas, BASE + '/api/v1/files/' + identifier, self.root, overwrite=True)[0])
        self.assertEqual(len(list(self.root.glob('*.pdf'))), 2)
        self.assertEqual({p.read_bytes() for p in self.root.glob('*.pdf')}, {self.canvas.payloads['1'], self.canvas.payloads['4']})

    def test_locally_edited_download_is_preserved_when_remote_changes(self):
        self.archive()
        target = self.root / 'Week/lecture.pdf'
        target.write_bytes(b'My annotated lecture')
        self.canvas.files['1']['updated_at'] = 'v2'
        ok, relative = download_file_from_api(self.canvas, BASE + '/api/v1/files/1', target.parent)
        self.assertTrue(ok)
        self.assertEqual(target.read_bytes(), b'My annotated lecture')
        self.assertEqual((target.parent / relative).read_bytes(), self.canvas.payloads['1'])
        self.assertNotEqual(target.name, relative)

    def test_optional_new_quizzes_absence_is_reported_without_failed_retry(self):
        self.canvas.errors[BASE + '/api/quiz/v1/courses/1/quizzes'] = 404
        result = self.archive()
        self.assertFalse(result.partial, result.content_errors)
        self.assertFalse(result.pending)
        self.assertEqual(result.unavailable_sources[0]['source'], 'new-quizzes')

    def test_html_companions_cannot_redirect_and_logs_are_bounded(self):
        html = offline_html('<meta http-equiv="refresh" content="0;url=https://evil.example"><link rel="stylesheet" href="https://evil.example/a.css"><p onclick="alert(1)">Text</p>', BASE, {})
        self.assertNotIn('refresh', html)
        self.assertNotIn('evil.example', html)
        self.assertNotIn('onclick', html)
        log = self.root / 'sync.log'
        log.write_bytes(b'old line\n' * 1000 + b'recent line\n')
        trim_log(log, limit=100)
        self.assertLessEqual(log.stat().st_size, 100)
        self.assertTrue(log.read_bytes().endswith(b'recent line\n'))

    def test_missing_course_stays_hidden_and_old_state_recovers_visibility(self):
        sync = Sync(self.canvas, BASE, self.root)
        sync.discover()
        select_courses(self.root, {'1': False})
        (self.root / 'Course').mkdir(exist_ok=True)
        (self.root / 'Course/notes.txt').write_text('private')
        with patch.object(sync, 'courses', return_value=[]):
            sync.discover()
        self.assertTrue(course_catalog(self.root)['courses'][0]['hidden'])
        self.assertFalse(Archive(self.root).files('Course'))
        (self.root / '.sync-catalog.json').write_text('{"courses": []}')
        self.assertFalse(Archive(self.root).files('Course'))

    def test_discussion_access_loss_preserves_complete_replies_and_marks_stale(self):
        sync = Sync(self.canvas, BASE, self.root)
        topic = {'id': 21, 'title': 'Thread', 'message': 'Prompt'}
        sync.discussion('1', 'Course', topic)
        path = self.root / 'Course/Canvas Sync/Discussions/21-Thread.txt'
        original = path.read_bytes()
        self.canvas.errors[BASE + '/api/v1/courses/1/discussion_topics/21/view'] = 403
        sync.attempt('1', 'discussions:21', lambda: sync.discussion('1', 'Course', topic))
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(sync.state['items']['1:discussions:21']['available'])
        self.assertTrue(sync.failed)

    def test_renames_stay_stable_and_local_edits_are_preserved(self):
        sync = Sync(self.canvas, BASE, self.root)
        sync.write_text('1:page:1', 'Course/old.txt', 'old')
        sync.write_text('1:page:1', 'Course/new.txt', 'new')
        self.assertFalse((self.root / 'Course/new.txt').exists())
        old = self.root / 'Course/old.txt'
        old.write_text('my edited notes')
        sync.write_text('1:page:1', 'Course/new.txt', 'remote')
        self.assertEqual(old.read_text(), 'my edited notes')
        self.assertEqual((self.root / 'Course/new.txt').read_text(), 'remote')

    def test_account_and_host_binding_reject_reuse(self):
        sync = Sync(self.canvas, BASE, self.root)
        sync.bind_account()
        sync.save()
        other = Canvas()
        other.user_id = 100
        with self.assertRaisesRegex(ValueError, 'different Canvas account'):
            Sync(other, BASE, self.root).bind_account()
        with self.assertRaisesRegex(ValueError, 'different Canvas host'):
            Sync(other, 'https://other.example', self.root).bind_account()

    def test_image_only_page_and_media_references_are_kept(self):
        self.canvas.page['body'] = '<img src="/files/2/preview" alt="Mechanism"><iframe src="https://video.example/123"></iframe>'
        ok, filename, assets, links = process_page(self.canvas, BASE, '1', 'intro', self.root)
        self.assertTrue(ok)
        self.assertIn('Mechanism', (self.root / filename).read_text())
        self.assertEqual(assets, [BASE + '/files/2/download'])
        self.assertIn('https://video.example/123', links)

    def test_missing_items_marked_unavailable_without_deleting_bytes(self):
        sync = Sync(self.canvas, BASE, self.root)
        sync.course(self.canvas.course)
        path = self.root / sync.state['items']['1:assignment:40']['path']
        self.canvas.assignment = None
        with patch.object(sync, 'listing', return_value=[]):
            sync.collect('1', 'assignments', '/api/v1/courses/1/assignments', lambda _: None, prefix='1:assignment:')
        self.assertTrue(path.is_file())
        self.assertFalse(sync.state['items']['1:assignment:40']['available'])

    def test_web_zip_without_modules_uses_same_collector(self):
        from app import create_app
        app = create_app({'TESTING': True})
        self.addCleanup(shutil.rmtree, app.extensions['downloads'].root, True)
        client = app.test_client()
        client.get('/')
        with client.session_transaction() as session:
            token = session['csrf_token']
        self.canvas.modules = []
        with patch('app.load_cookies_from_browser', return_value=[]), patch('app.create_session', return_value=self.canvas):
            result = client.post('/', data={'csrf_token': token, 'url': BASE + '/courses/1', 'auth_method': 'browser', 'browser': 'firefox'})
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'Archive ready', result.data)
        link = re.search(rb'href="(/download\?id=[^"]+)"', result.data).group(1).decode()
        with client.get(link) as download:
            with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
                self.assertIn('archive-status.json', archive.namelist())
                self.assertTrue(any('Essay' in name for name in archive.namelist()))
                self.assertFalse(any(part.startswith('.') for name in archive.namelist() for part in Path(name).parts))

    def test_missing_module_list_does_not_block_course_content_and_retries_once(self):
        from app import create_app
        app = create_app({'TESTING': True})
        self.addCleanup(shutil.rmtree, app.extensions['downloads'].root, True)
        client = app.test_client()
        client.get('/')
        with client.session_transaction() as session:
            token = session['csrf_token']
        data = {'csrf_token': token, 'url': BASE + '/courses/1', 'auth_method': 'browser', 'browser': 'firefox'}
        self.canvas.errors[BASE + '/api/v1/courses/1/modules'] = 403
        with patch('app.load_cookies_from_browser', return_value=[]), patch('app.create_session', return_value=self.canvas):
            result = client.post('/', data=data)
            self.assertIn(b'Archive partially complete', result.data)
            self.assertIn(b'Download ZIP', result.data)
            identifier = re.search(rb'name="retry_id" value="([^"]+)"', result.data).group(1).decode()
            before = self.canvas.calls.copy()
            del self.canvas.errors[BASE + '/api/v1/courses/1/modules']
            recovered = client.post('/', data=dict(data, retry_id=identifier))
        self.assertEqual(recovered.status_code, 200)
        self.assertIn(b'Archive ready', recovered.data)
        changes = self.canvas.calls - before
        self.assertNotIn(BASE + '/api/v1/courses/1/assignments', changes)
        self.assertNotIn(BASE + '/files/1/download', changes)

    def test_course_writes_are_serialized_across_clients_and_allow_nested_collection(self):
        other = Canvas()
        with course_lock(self.canvas, self.root):
            with course_lock(self.canvas, self.root):
                pass
            with self.assertRaises(BlockingIOError):
                archive_modules(other, BASE, '1', [], self.root)
        self.assertFalse(archive_modules(other, BASE, '1', [], self.root).partial)

    def test_malformed_module_items_are_failures_and_links_survive_partial_rerun(self):
        self.canvas.items = [{'type': 'File', 'title': 'Missing ID'}, {'type': 'Page', 'title': 'Missing slug'}]
        result = archive_modules(self.canvas, BASE, '1', self.canvas.modules, self.root, report=lambda _: None)
        self.assertTrue(result.partial)
        self.assertEqual((result.files_fail, result.pages_fail), (1, 1))
        (self.root / 'links.txt').write_text('(Week) [Reading] https://reading.example/old\n')
        self.canvas.errors[BASE + '/api/v1/courses/1/modules/10/items'] = 403
        archive_modules(self.canvas, BASE, '1', self.canvas.modules, self.root, report=lambda _: None)
        self.assertIn('https://reading.example/old', (self.root / 'links.txt').read_text())


if __name__ == '__main__':
    unittest.main()
