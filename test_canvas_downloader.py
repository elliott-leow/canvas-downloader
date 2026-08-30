#!/usr/bin/env python3
"""Tests for canvas_downloader. Run with: python -m unittest test_canvas_downloader"""

import unittest

from canvas_downloader import process_page

from canvas_downloader import external_link_target, rewrite_embedded_file_link

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

    def test_preview_suffix_is_normalised_to_download(self):
        # Regression: Canvas emits "/files/<id>/preview" for inline previews.
        # Appending to the full path produced ".../preview/download" -> 404.
        href = "https://jhu.instructure.com/courses/134519/files/18058517/preview"
        self.assertEqual(
            rewrite_embedded_file_link(href, BASE),
            "https://jhu.instructure.com/courses/134519/files/18058517/download",
        )

    def test_protocol_relative_url_is_resolved(self):
        # Regression: "//host/..." starts with "/", so naive concatenation
        # produced "https://jhu.instructure.com//jhu.instructure.com/...".
        href = "//jhu.instructure.com/courses/1/files/2?wrap=1"
        self.assertEqual(
            rewrite_embedded_file_link(href, BASE),
            "https://jhu.instructure.com/courses/1/files/2/download",
        )

    def test_host_comparison_is_case_insensitive(self):
        # Hostnames are case-insensitive; this is still our own Canvas instance.
        href = "https://JHU.instructure.com/courses/1/files/2"
        self.assertEqual(
            rewrite_embedded_file_link(href, BASE),
            "https://JHU.instructure.com/courses/1/files/2/download",
        )

    def test_canvas_non_file_link_is_ignored(self):
        href = "https://jhu.instructure.com/courses/134519/assignments/42"
        self.assertIsNone(rewrite_embedded_file_link(href, BASE))


class ExternalLinkTarget(unittest.TestCase):
    def test_off_instance_link_is_returned_absolute(self):
        href = "https://www.energy.gov/sites/default/files/2022-03/Chart.pdf"
        self.assertEqual(external_link_target(href, BASE), href)

    def test_same_instance_link_is_ignored(self):
        # Module items already record Canvas's own pages.
        href = "https://jhu.instructure.com/courses/134519/assignments/42"
        self.assertIsNone(external_link_target(href, BASE))

    def test_relative_link_is_ignored_as_same_instance(self):
        self.assertIsNone(external_link_target("/courses/1/quizzes/2", BASE))

    def test_mailto_and_anchor_are_ignored(self):
        self.assertIsNone(external_link_target("mailto:prof@jhu.edu", BASE))
        self.assertIsNone(external_link_target("#section-2", BASE))


class ProcessPageLinkSplit(unittest.TestCase):
    """process_page must split page links into downloads vs. recorded references."""

    BODY = (
        '<a href="https://jhu.instructure.com/courses/1/files/2?wrap=1">Syllabus</a>'
        '<a href="/courses/1/files/3/preview">Slides</a>'
        '<a href="https://www.energy.gov/x.pdf">DOE</a>'
        '<a href="https://www.energy.gov/x.pdf">DOE again</a>'
        '<a href="mailto:prof@jhu.edu">Email</a>'
        '<p>Some body text.</p>'
    )

    def _run(self):
        import tempfile

        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(_self): return {"title": "Week 1", "body": ProcessPageLinkSplit.BODY}

        class S:
            def get(self, url, **kw): return R()

        return process_page(S(), BASE, "1", "week-1", tempfile.mkdtemp())

    def test_canvas_files_become_downloads(self):
        ok, _, embedded, _ = self._run()
        self.assertTrue(ok)
        self.assertEqual(embedded, [
            "https://jhu.instructure.com/courses/1/files/2/download",
            "https://jhu.instructure.com/courses/1/files/3/download",
        ])

    def test_external_links_are_collected_and_deduped(self):
        ok, _, _, external = self._run()
        self.assertTrue(ok)
        self.assertEqual(external, ["https://www.energy.gov/x.pdf"])


if __name__ == "__main__":
    unittest.main()
