import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

import httpx
from fastmcp import Client
from pypdf import PdfWriter

from canvas_mcp import Archive, PROJECT, build_server


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "downloads"
        (self.root / "Chemistry").mkdir(parents=True)
        self.note = self.root / "Chemistry" / "lecture.txt"
        self.note.write_text("Aspirin synthesis uses acetic anhydride.")
        self.archive = Archive(self.root)

    def test_boundary_and_symlinks(self):
        outside = Path(self.tmp.name) / "private.txt"
        outside.write_text("private")
        (self.root / "Chemistry" / "link.txt").symlink_to(outside)
        (self.root / "Chemistry" / "alias.txt").symlink_to(self.note)
        for value in ["../private.txt", str(outside), "Chemistry/link.txt", "Chemistry/alias.txt"]:
            with self.assertRaises(ValueError):
                self.archive.text(value)
        self.assertEqual(self.archive.files(), ["Chemistry/lecture.txt"])

    def test_hidden_courses_cannot_be_searched_or_fetched(self):
        (self.root / ".sync-catalog.json").write_text(json.dumps({"courses": [
            {"id": "1", "name": "Chemistry", "folder": "Chemistry", "past": True}]}))
        self.assertEqual(self.archive.files(), [])
        with self.assertRaises(ValueError):
            self.archive.text("Chemistry/lecture.txt")
        (self.root / ".sync-preferences.json").write_text(json.dumps({"overrides": {"1": True}}))
        self.assertEqual(self.archive.files(), ["Chemistry/lecture.txt"])

    def test_search_updates_and_deletion(self):
        self.assertEqual(len(self.archive.search("aspirin")["results"]), 1)
        self.note.write_text("Alkene stereochemistry")
        self.assertEqual(self.archive.search("aspirin")["results"], [])
        self.assertEqual(len(self.archive.search("alkene")["results"]), 1)
        self.note.unlink()
        with self.assertRaises(ValueError):
            self.archive.text("Chemistry/lecture.txt")

    def test_extract_formats_and_errors(self):
        with zipfile.ZipFile(self.root / "Chemistry" / "notes.docx", "w") as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="urn:w"><w:p><w:r><w:t>Carbonyl reaction</w:t></w:r></w:p></w:document>')
        with zipfile.ZipFile(self.root / "Chemistry" / "slides.pptx", "w") as z:
            for i in [10, 2]:
                z.writestr(f"ppt/slides/slide{i}.xml", f'<a:p xmlns:a="urn:a"><a:r><a:t>Slide {i}</a:t></a:r></a:p>')
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(self.root / "Chemistry" / "blank.pdf")
        self.assertIn("Carbonyl reaction", self.archive.text("Chemistry/notes.docx"))
        slides = self.archive.text("Chemistry/slides.pptx")
        self.assertLess(slides.index("Slide 2"), slides.index("Slide 10"))
        self.assertIn("[Page 1]", self.archive.text("Chemistry/blank.pdf"))
        (self.root / "Chemistry" / "broken.pdf").write_text("not a pdf")
        self.assertEqual(len(self.archive.search("carbonyl")["extraction_errors"]), 1)

    def test_mcp_tools_and_pagination(self):
        async def check():
            async with Client(build_server(self.archive)) as client:
                tools = await client.list_tools()
                self.assertEqual({t.name for t in tools}, {"list_courses", "list_files", "search", "fetch", "sync_status", "list_available_courses", "set_course_sync", "sync_courses", "get_course_roles"})
                self.assertTrue(all(t.annotations.readOnlyHint for t in tools if t.name not in {"set_course_sync", "sync_courses"}))
                self.assertFalse(next(t for t in tools if t.name == "set_course_sync").annotations.readOnlyHint)
                found = await client.call_tool("search", {"query": "aspirin"})
                self.assertEqual(found.data["results"][0]["id"], "Chemistry/lecture.txt")
                page = await client.call_tool("fetch", {"id": "Chemistry/lecture.txt", "max_chars": 7})
                self.assertEqual(page.data["text"], "Aspirin")
                self.assertEqual(page.data["next_offset"], 7)
        asyncio.run(check())

    def test_mcp_course_selection(self):
        (self.root / ".sync-catalog.json").write_text(json.dumps({"courses": [
            {"id": "1", "name": "Chemistry", "folder": "Chemistry", "past": True}]}))
        async def check():
            async with Client(build_server(self.archive)) as client:
                initial = await client.call_tool("list_available_courses", {})
                self.assertTrue(initial.data["courses"][0]["hidden"])
                enabled = await client.call_tool("set_course_sync", {"course_ids": ["1"], "enabled": True})
                self.assertFalse(enabled.data["courses"][0]["hidden"])
                found = await client.call_tool("search", {"query": "aspirin"})
                self.assertEqual(len(found.data["results"]), 1)
                await client.call_tool("set_course_sync", {"course_ids": ["1"], "enabled": False})
                hidden = await client.call_tool("search", {"query": "aspirin"})
                self.assertEqual(hidden.data["results"], [])
        asyncio.run(check())

    def test_authenticated_http(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token_file = Path(self.tmp.name) / "token"
        token_file.write_text("t" * 48)
        process = subprocess.Popen([sys.executable, str(PROJECT / "canvas_mcp.py"),
                                    "--transport", "http", "--root", str(self.root),
                                    "--port", str(port), "--token-file", str(token_file)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}/mcp"
        try:
            for _ in range(100):
                if process.poll() is not None:
                    self.fail("HTTP server exited during startup")
                try:
                    response = httpx.post(url, json={}, timeout=0.2)
                    break
                except httpx.TransportError:
                    time.sleep(0.1)
            else:
                self.fail("HTTP server did not start")
            self.assertEqual(response.status_code, 401)
            bad = httpx.post(url, json={}, headers={"Authorization": "Bearer invalid"})
            self.assertEqual(bad.status_code, 401)

            async def check():
                async with Client(url, auth="t" * 48) as client:
                    result = await client.call_tool("list_courses", {})
                    self.assertEqual(result.data["courses"][0]["name"], "Chemistry")
            asyncio.run(check())
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
