#!/usr/bin/env python3
"""Tests for canvas_downloader. Run with: python -m unittest test_canvas_downloader"""

import unittest

from canvas_downloader import rewrite_embedded_file_link

BASE = "https://jhu.instructure.com"


class RewriteEmbeddedFileLink(unittest.TestCase):
    def test_canvas_file_with_query_string_appends_download_once(self):
        # Regression: a query string used to cause a doubled "/download/download".
        href = "https://jhu.instructure.com/courses/134519/files/18058517?wrap=1"
        self.assertEqual(
            rewrite_embedded_file_link(href, BASE),
            "https://jhu.instructure.com/courses/134519/files/18058517/download",
        )

    def test_canvas_file_without_query_appends_download(self):
        href = "https://jhu.instructure.com/courses/134519/files/18058517"
        self.assertEqual(
            rewrite_embedded_file_link(href, BASE),
            "https://jhu.instructure.com/courses/134519/files/18058517/download",
        )

    def test_relative_canvas_file_becomes_absolute_download(self):
        href = "/courses/134519/files/999"
        self.assertEqual(
            rewrite_embedded_file_link(href, BASE),
            "https://jhu.instructure.com/courses/134519/files/999/download",
        )

    def test_already_download_link_is_left_unchanged(self):
        # Must not append a second "/download".
        href = "https://jhu.instructure.com/courses/134519/files/18058517/download?verifier=abc"
        self.assertEqual(rewrite_embedded_file_link(href, BASE), href)

    def test_external_url_with_files_digits_is_ignored(self):
        # Regression: external URLs whose path happens to contain "/files/<digits>"
        # (e.g. energy.gov/.../files/2022-03/...) must not be treated as Canvas files.
        href = "https://www.energy.gov/sites/default/files/2022-03/DOE%20Org%20Chart.pdf"
        self.assertIsNone(rewrite_embedded_file_link(href, BASE))

    def test_external_non_file_url_is_ignored(self):
        href = "https://brand.hopkinsmedicine.org/images/PDFs/JHM-Org-Chart.pdf"
        self.assertIsNone(rewrite_embedded_file_link(href, BASE))

    def test_canvas_non_file_link_is_ignored(self):
        href = "https://jhu.instructure.com/courses/134519/assignments/42"
        self.assertIsNone(rewrite_embedded_file_link(href, BASE))


if __name__ == "__main__":
    unittest.main()
