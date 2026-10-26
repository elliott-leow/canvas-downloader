"""Atomic download and common CLI/web workflow regression coverage."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from archive_io import atomic_write
from canvas_downloader import (api_get_paginated, download_file_by_url,
                               download_file_from_api, sanitize_filename, validate_canvas_url)
from course_archive import archive_modules


class DownloadTests(unittest.TestCase):
    def test_course_url_validation(self):
        for url in ('https://canvas.edu/courses/123', 'http://localhost:8080/courses/1/modules'):
            self.assertTrue(validate_canvas_url(url))
        for url in ('file:///courses/1', 'https://canvas.edu/?next=/courses/1',
                    'https://user:password@canvas.edu/courses/1', 'https://canvas.edu/courses/1oops',
                    'https://canvas.edu:bad/courses/1', 'https://[invalid/courses/1'):
            self.assertFalse(validate_canvas_url(url), url)
        self.assertEqual(sanitize_filename('...'), 'Untitled')
        self.assertEqual(sanitize_filename('CON.txt'), '_CON.txt')

    def test_atomic_failure_preserves_previous_file(self):
        def broken():
            yield b'new but incomplete'
            raise requests.ConnectionError('interrupted')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'lecture.pdf'
            target.write_bytes(b'original')
            with self.assertRaises(requests.ConnectionError):
                atomic_write(target, broken())
            self.assertEqual(target.read_bytes(), b'original')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_download_overwrite_collision_and_response_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            session = MagicMock()
            response = session.get.return_value
            response.status_code = 200
            response.headers = {'Content-Disposition': 'attachment; filename="lecture.pdf"'}
            response.iter_content.return_value = [b'complete']
            target = Path(directory) / 'lecture.pdf'
            target.write_bytes(b'original')
            ok, name = download_file_by_url(session, 'https://canvas.edu/files/1', directory)
            self.assertTrue(ok)
            self.assertEqual(name, 'lecture_1.pdf')
            self.assertEqual(target.read_bytes(), b'original')
            response.iter_content.side_effect = requests.ConnectionError('interrupted')
            ok, _ = download_file_by_url(session, 'https://canvas.edu/files/1', directory, overwrite=True)
            self.assertFalse(ok)
            self.assertEqual(target.read_bytes(), b'original')
            self.assertEqual(response.close.call_count, 2)

    def test_api_download_replaces_only_complete_content(self):
        with tempfile.TemporaryDirectory() as directory:
            client = MagicMock()
            metadata = MagicMock(status_code=200)
            metadata.json.return_value = {'url': 'https://cdn.edu/file', 'display_name': 'a.pdf'}
            download = MagicMock()
            download.__enter__.return_value = download
            download.iter_content.return_value = [b'pdf contents']
            client.get.side_effect = [metadata, download]
            self.assertEqual(download_file_from_api(client, 'https://canvas.edu/api/v1/files/1', directory),
                             (True, 'a.pdf'))
            self.assertEqual((Path(directory) / 'a.pdf').read_bytes(), b'pdf contents')
            download.__exit__.assert_called_once()

    def test_pagination_rejects_cross_origin_and_loops_without_mutating_params(self):
        for next_url in ('https://elsewhere.edu/api', 'https://canvas.edu/api'):
            client = MagicMock()
            response = client.get.return_value
            response.status_code = 200
            response.json.return_value = []
            response.headers = {'Link': f'<{next_url}>; rel="next"'}
            params = {'test': 'value'}
            with self.assertRaises(ValueError):
                api_get_paginated(client, 'https://canvas.edu/api', params)
            self.assertEqual(params, {'test': 'value'})
            self.assertEqual(client.get.call_count, 1)

    def test_shared_workflow_keeps_duplicate_modules_separate_and_reports_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            modules = [{'id': n, 'name': 'Week 1'} for n in (1, 2, 3)]
            def process(_session, _base, _course, _items, path, links, overwrite, failed):
                (Path(path) / 'page.txt').write_text(path)
                links.append('[Reading] https://example.edu')
                return {'files_ok': 0, 'files_fail': 0, 'pages_ok': 1, 'pages_fail': 0}
            with patch('course_archive.fetch_module_items', side_effect=[[], [], requests.ConnectionError()]), \
                 patch('course_archive.process_module_items', side_effect=process):
                result = archive_modules(None, 'https://canvas.edu', '1', modules, directory,
                                         overwrite=True, report=lambda _: None)
            self.assertEqual(result.pages_ok, 2)
            self.assertEqual(len(list(Path(directory).rglob('page.txt'))), 2)
            self.assertEqual(len(result.module_errors), 1)
            self.assertTrue(result.partial)
            self.assertEqual(len((Path(directory) / 'links.txt').read_text().splitlines()), 2)

class SyncAndCacheTests(unittest.TestCase):
    def test_discover_only_cli_succeeds(self):
        import canvas_sync
        with tempfile.TemporaryDirectory() as directory, \
             patch('sys.argv', ['canvas_sync.py', '--base-url', 'https://canvas.edu',
                               '--output', directory, '--discover-only']), \
             patch('canvas_sync.load_cookies_from_browser'), \
             patch('canvas_sync.create_session'), \
             patch('canvas_sync.Sync') as sync, patch('builtins.print'):
            sync.return_value.run.return_value = {'courses': [], 'discovered_at': 'today'}
            canvas_sync.main()
            sync.return_value.run.assert_called_once_with(True)

    def test_discovery_failure_cli_still_fails(self):
        import canvas_sync
        with tempfile.TemporaryDirectory() as directory, \
             patch('sys.argv', ['canvas_sync.py', '--base-url', 'https://canvas.edu',
                               '--output', directory, '--discover-only']), \
             patch('canvas_sync.load_cookies_from_browser'), \
             patch('canvas_sync.create_session'), \
             patch('canvas_sync.Sync') as sync, patch('builtins.print'):
            sync.return_value.run.return_value = {'state': 'login_required'}
            with self.assertRaises(SystemExit):
                canvas_sync.main()

    def test_cache_is_bounded_and_file_changes_remain_visible(self):
        from canvas_mcp import Archive
        with tempfile.TemporaryDirectory() as directory, patch('canvas_mcp.MAX_CACHE_CHARS', 15):
            root = Path(directory)
            (root / 'a.txt').write_text('a' * 10)
            (root / 'b.txt').write_text('b' * 10)
            archive = Archive(root)
            self.assertEqual(archive.text('a.txt'), 'a' * 10)
            self.assertEqual(archive.text('b.txt'), 'b' * 10)
            self.assertEqual(list(archive.cache), ['b.txt'])
            self.assertLessEqual(archive.cache_chars, 15)
            (root / 'b.txt').write_text('updated')
            self.assertEqual(archive.text('b.txt'), 'updated')
            self.assertEqual(archive.cache_chars, 7)

    def test_sync_pagination_loop_and_scheme_change(self):
        from canvas_sync import Sync
        with tempfile.TemporaryDirectory() as directory:
            for next_url in ('https://canvas.edu/courses', 'http://canvas.edu/courses'):
                session = MagicMock()
                response = session.get.return_value
                response.status_code = 200
                response.json.return_value = []
                response.links = {'next': {'url': next_url}}
                with self.assertRaises(ValueError):
                    Sync(session, 'https://canvas.edu', Path(directory)).listing('/courses')
                self.assertEqual(session.get.call_count, 1)


if __name__ == '__main__':
    unittest.main()
