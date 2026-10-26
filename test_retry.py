"""Retry regressions: successful downloads must never be fetched a second time."""
import io
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import requests
from app import create_app
from course_archive import archive_modules, retry_failed

BASE = 'https://canvas.example.edu'


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_failed_attachment_does_not_repeat_page_or_successful_file(self):
        items = [{'type': 'File', 'content_id': 10}, {'type': 'Page', 'page_url': 'week'}]
        def file(_s, _url, directory, overwrite=False):
            (Path(directory) / 'good.pdf').write_bytes(b'original')
            return True, 'good.pdf'
        def page(_s, _base, _id, _slug, directory, overwrite=False):
            (Path(directory) / 'page.txt').write_text('page content')
            return True, 'page.txt', [BASE + '/files/20/download'], ['https://reading.example']
        def attachment(_s, _url, directory, overwrite=False):
            (Path(directory) / 'attachment.pdf').write_bytes(b'recovered')
            return True, 'attachment.pdf'
        with patch('course_archive.fetch_module_items', return_value=items) as listing, \
             patch('canvas_downloader.download_file_from_api', side_effect=file) as download, \
             patch('canvas_downloader.process_page', side_effect=page) as pages, \
             patch('canvas_downloader.download_file_by_url', return_value=(False, 'offline')) as embedded:
            result = archive_modules(None, BASE, '1', [{'id': 1, 'name': 'Week'}], self.root)
            self.assertEqual((result.files_ok, result.files_fail, result.pages_ok), (1, 1, 1))
            retry_failed(None, BASE, '1', self.root, result)  # Still offline.
            self.assertEqual((result.files_ok, result.files_fail), (1, 1))
            embedded.side_effect = attachment
            retry_failed(None, BASE, '1', self.root, result)
            retry_failed(None, BASE, '1', self.root, result)  # No pending work.
            self.assertFalse(result.partial)
            self.assertEqual((result.files_ok, result.pages_ok), (2, 1))
            self.assertEqual(listing.call_count, 1)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(pages.call_count, 1)
            self.assertEqual(embedded.call_count, 3)
            self.assertEqual((self.root / 'Week/good.pdf').read_bytes(), b'original')
            self.assertEqual(len(result.links), 1)

    def test_unavailable_module_is_retried_then_only_its_failed_files(self):
        with patch('course_archive.fetch_module_items', side_effect=[requests.ConnectionError(),
                [{'type': 'File', 'content_id': 10}]]) as listing, \
             patch('canvas_downloader.download_file_from_api', side_effect=[(False, 'offline'), (True, 'a.pdf')]) as files:
            result = archive_modules(None, BASE, '1', [{'id': 1, 'name': 'Week'}], self.root)
            self.assertEqual(result.module_errors, ['Week'])
            retry_failed(None, BASE, '1', self.root, result)
            self.assertEqual(result.module_errors, [])
            self.assertEqual(result.files_fail, 1)
            retry_failed(None, BASE, '1', self.root, result)
            self.assertFalse(result.partial)
            self.assertEqual(listing.call_count, 2)
            self.assertEqual(files.call_count, 2)

    def test_failed_page_does_not_repeat_its_already_attempted_attachments(self):
        embedded_url = BASE + '/files/1/download'
        with patch('course_archive.fetch_module_items', return_value=[{'type': 'Page', 'page_url': 'a'}]), \
             patch('canvas_downloader.process_page', side_effect=[
                 (False, 'No text', [embedded_url], []), (True, 'page.txt', [embedded_url], [])]) as pages, \
             patch('canvas_downloader.download_file_by_url', return_value=(True, 'a.pdf')) as files:
            result = archive_modules(None, BASE, '1', [{'id': 1, 'name': 'Week'}], self.root)
            retry_failed(None, BASE, '1', self.root, result)
            self.assertFalse(result.partial)
            self.assertEqual(result.files_ok, 1)
            self.assertEqual(files.call_count, 1)
            self.assertEqual(pages.call_count, 2)


class WebRetryTests(unittest.TestCase):
    def setUp(self):
        collector = patch('app.complete_course', side_effect=lambda _s, _b, _c, _o, result, _t, **_kw: result)
        collector.start()
        self.addCleanup(collector.stop)
        self.app = create_app({'TESTING': True})
        self.client = self.app.test_client()
        self.store = self.app.extensions['downloads']
        self.addCleanup(shutil.rmtree, self.store.root, True)
        self.client.get('/')
        with self.client.session_transaction() as session:
            self.token = session['csrf_token']
        self.data = dict(url=BASE + '/courses/1', auth_method='browser', browser='firefox', csrf_token=self.token)

    def test_all_failed_can_retry_and_zip_contains_only_recovered_content(self):
        def download(_s, _url, directory, overwrite=False):
            (Path(directory) / 'lecture.pdf').write_bytes(b'recovered')
            return True, 'lecture.pdf'
        with patch('app.load_cookies_from_browser'), patch('app.create_session'), \
             patch('app.fetch_course_name', return_value='Course') as title, \
             patch('app.fetch_modules', return_value=[{'id': 1, 'name': 'Week'}]) as modules, \
             patch('course_archive.fetch_module_items', return_value=[{'type': 'File', 'content_id': 1}]) as listing, \
             patch('canvas_downloader.download_file_from_api', return_value=(False, 'offline')) as files:
            response = self.client.post('/', data=self.data)
            self.assertIn(b'Retry failed items (1)', response.data)
            self.assertNotIn(b'Download ZIP', response.data)
            retry_id = re.search(rb'name="retry_id" value="([^"]+)"', response.data).group(1).decode()
            self.assertEqual(self.client.post('/', data=dict(self.data, retry_id=retry_id, csrf_token='bad')).status_code, 400)
            self.store.busy.add(retry_id)
            self.assertEqual(self.client.post('/', data=dict(self.data, retry_id=retry_id)).status_code, 409)
            self.store.busy.remove(retry_id)
            files.side_effect = download
            response = self.client.post('/', data=dict(self.data, retry_id=retry_id))
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Archive ready', response.data)
            self.assertNotIn(b'class="retry-form"', response.data)
            self.assertEqual(title.call_count, 1)
            self.assertEqual(modules.call_count, 1)
            self.assertEqual(listing.call_count, 1)
            with self.client.get('/download?id=' + retry_id) as response:
                with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                    self.assertEqual(archive.namelist(), ['Week/lecture.pdf'])
                    self.assertEqual(archive.read('Week/lecture.pdf'), b'recovered')
            # A different local browser session cannot retry this job.
            other = self.app.test_client()
            other.get('/')
            with other.session_transaction() as session:
                token = session['csrf_token']
            self.assertEqual(other.post('/', data=dict(self.data, csrf_token=token, retry_id=retry_id)).status_code, 404)
            self.store.ttl = 0
            self.assertEqual(self.client.post('/', data=dict(self.data, retry_id=retry_id)).status_code, 404)

    def test_partial_zip_keeps_successes_after_retry_and_auth_failure(self):
        calls = []
        def download(_s, url, directory, overwrite=False):
            calls.append(url)
            if url.endswith('/2') and calls.count(url) == 1:
                return False, 'offline'
            name = url.rsplit('/', 1)[-1] + '.txt'
            (Path(directory) / name).write_text(name)
            return True, name
        with patch('app.load_cookies', return_value=[]) as cookies, patch('app.create_session'), \
             patch('app.fetch_course_name', return_value='Course'), \
             patch('app.fetch_modules', return_value=[{'id': 1, 'name': 'Week'}]), \
             patch('course_archive.fetch_module_items', return_value=[{'type': 'File', 'content_id': n} for n in (1, 2)]), \
             patch('canvas_downloader.download_file_from_api', side_effect=download):
            def post(**extra):
                return self.client.post('/', data=dict(self.data, auth_method='file',
                    cookies=(io.BytesIO(b'cookies'), 'cookies.txt'), **extra))
            response = post()
            retry_id = re.search(rb'name="retry_id" value="([^"]+)"', response.data).group(1).decode()
            cookies.side_effect = SystemExit(1)
            self.assertEqual(post(retry_id=retry_id).status_code, 400)
            self.assertTrue(self.store.get(retry_id).is_file())
            self.assertEqual(len(calls), 2)
            cookies.side_effect = None
            self.assertEqual(post(retry_id=retry_id).status_code, 200)
            self.assertEqual(calls, [BASE+'/api/v1/files/1', BASE+'/api/v1/files/2', BASE+'/api/v1/files/2'])
            with self.client.get('/download?id=' + retry_id) as response:
                with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                    self.assertEqual(archive.namelist(), ['Week/1.txt', 'Week/2.txt'])
            self.assertFalse(list(self.store.root.rglob('cookies.txt')))


if __name__ == '__main__':
    unittest.main()
