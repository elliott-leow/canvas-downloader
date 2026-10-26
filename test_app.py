"""Web regression tests; no real browser cookies or Canvas account required."""
import io
from pathlib import Path
import re
import shutil
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from app import create_app
from course_archive import ArchiveResult


class WebTests(unittest.TestCase):
    def setUp(self):
        # These route tests isolate the module workflow. The full course collector
        # is exercised with a deterministic Canvas account in test_completeness.
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
        self.data = {'url': 'https://canvas.example.edu/courses/123',
                     'auth_method': 'browser', 'browser': 'firefox', 'csrf_token': self.token}

    def test_form_and_validation(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'csrf_token', response.data)
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        with patch('app.load_cookies_from_browser') as cookies:
            for change in ({'url': 'file:///courses/1'}, {'auth_method': 'oops'},
                           {'browser': 'oops'}, {'csrf_token': ''},
                           {'auth_method': 'file'}):
                with self.subTest(change=change):
                    response = self.client.post('/', data=dict(self.data, **change))
                    self.assertEqual(response.status_code, 400)
            cookies.assert_not_called()

    def test_cookie_error_retains_form_choice_and_cleans_up(self):
        with patch('app.load_cookies', side_effect=SystemExit(1)):
            response = self.client.post('/', data=dict(self.data, auth_method='file',
                cookies=(io.BytesIO(b'bad cookies'), 'cookies.txt')))
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'value="file" checked', response.data)
        self.assertEqual(list(self.store.root.iterdir()), [])

    def test_large_upload_is_friendly(self):
        response = self.client.post('/', data=dict(self.data, auth_method='file',
            cookies=(io.BytesIO(b'x' * (2 * 1024 * 1024 + 1)), 'cookies.txt')))
        self.addCleanup(response.request.environ['wsgi.input'].close)
        self.assertEqual(response.status_code, 413)
        self.assertIn(b'too large', response.data)

    def test_successful_zip_has_exact_counts_and_no_cookie_file(self):
        def archive(_session, _base, _id, _modules, output):
            output.mkdir(exist_ok=True)
            (output / 'lecture.txt').write_text('Lecture material')
            return ArchiveResult(files_ok=7, pages_ok=3, module_errors=['Week 2'])
        client = MagicMock()
        with patch('app.load_cookies_from_browser'), patch('app.create_session', return_value=client), \
             patch('app.fetch_course_name', return_value='Biology'), \
             patch('app.fetch_modules', return_value=[{'id': 1}]), \
             patch('app.archive_modules', side_effect=archive):
            response = self.client.post('/', data=self.data)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Week 2', response.data)
        self.assertIn(b'>7<', response.data)
        self.assertNotIn(str(self.store.root).encode(), response.data)
        link = re.search(rb'href="(/download\?id=[^"]+)"', response.data).group(1).decode()
        for _ in range(2):
            download = self.client.get(link)
            self.assertEqual(download.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
                self.assertEqual(archive.namelist(), ['lecture.txt'])
            download.close()
        client.__exit__.assert_called_once()
        self.store.ttl = 0
        self.assertEqual(self.client.get(link).status_code, 404)
        self.assertEqual(list(self.store.root.iterdir()), [])

    def test_download_cannot_accept_a_filesystem_path(self):
        for query in ('dir=/tmp', 'id=../../etc/passwd', 'id=unknown'):
            self.assertEqual(self.client.get('/download?' + query).status_code, 404)

    def test_canvas_failure_cleans_work_directory(self):
        with patch('app.load_cookies_from_browser'), patch('app.create_session'), \
             patch('app.fetch_course_name', side_effect=SystemExit(1)):
            response = self.client.post('/', data=self.data)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(list(self.store.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
