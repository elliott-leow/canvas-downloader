"""Regression coverage for additive MCP extraction/readability metadata."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from canvas_mcp import Archive, MAX_BYTES
from canvas_mcp import build_server
from fastmcp import Client


DOC = '<w:p xmlns:w="urn:w"><w:r><w:t>{}</w:t></w:r></w:p>'
PPT = '<a:p xmlns:a="urn:a"><a:r><a:t>{}</a:t></a:r></a:p>'


class MCPReadabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "archive"
        (self.root / "Course").mkdir(parents=True)
        self.archive = Archive(self.root)

    def test_word_search_html_and_cache_visibility(self):
        path = self.root / "Course" / "notebook.html"
        path.write_text("<p>Acetate works.</p><script>acetate malware</script><p>Beta only.</p>")
        path.with_name("metadata.json").write_text('{"acetate": true}')
        self.assertEqual(self.archive.search("eta")["results"], [])
        self.assertEqual(len(self.archive.search("acetate")["results"]), 2)
        self.assertNotIn("malware", self.archive.text("Course/notebook.html"))
        rows = {row["id"]: row for row in self.archive.inventory()}
        self.assertTrue(rows["Course/notebook.html"]["cached"])
        self.assertTrue(rows["Course/metadata.json"]["readable"])

    def test_docx_extra_parts_and_pptx_notes_follow_slide_relation(self):
        with zipfile.ZipFile(self.root / "Course" / "extra.docx", "w") as doc:
            doc.writestr("word/document.xml", DOC.format("Body words"))
            doc.writestr("word/header2.xml", DOC.format("Header words"))
            doc.writestr("word/footer3.xml", DOC.format("Footer words"))
            doc.writestr("word/footnotes.xml", DOC.format("Footnote words"))
            doc.writestr("word/endnotes.xml", DOC.format("Endnote words"))
            doc.writestr("word/comments.xml", DOC.format("Comment words"))
        with zipfile.ZipFile(self.root / "Course" / "notes.pptx", "w") as ppt:
            ppt.writestr("ppt/slides/slide2.xml", PPT.format("Second slide"))
            ppt.writestr("ppt/slides/slide10.xml", PPT.format("Tenth slide"))
            ppt.writestr("ppt/slides/_rels/slide10.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide9.xml"/></Relationships>''')
            ppt.writestr("ppt/notesSlides/notesSlide9.xml", PPT.format("Mapped speaker note"))
        docx = self.archive.text("Course/extra.docx")
        for phrase in ("Body words", "Header words", "Footer words", "Footnote words", "Endnote words", "Comment words"):
            self.assertIn(phrase, docx)
        slides = self.archive.text("Course/notes.pptx")
        self.assertLess(slides.index("Second slide"), slides.index("Tenth slide"))
        self.assertIn("[Speaker notes]", slides)
        self.assertIn("Mapped speaker note", slides)

    def test_inventory_exposes_unreadable_and_stale_but_fetch_rejects(self):
        unsupported = self.root / "Course" / "diagram.bin"
        unsupported.write_bytes(b"binary")
        large = self.root / "Course" / "big.txt"
        large.write_bytes(b"x" * (MAX_BYTES + 1))
        (self.root / ".sync-state.json").write_text(json.dumps({"items": {
            "x": {"path": "Course/diagram.bin", "available": False, "last_seen": "2026-10-24T00:00:00Z"}
        }}))
        rows = {row["id"]: row for row in self.archive.inventory()}
        self.assertEqual(rows["Course/diagram.bin"]["reason"], "unsupported_format")
        self.assertFalse(rows["Course/diagram.bin"]["available"])
        self.assertEqual(rows["Course/diagram.bin"]["last_seen"], "2026-10-24T00:00:00Z")
        self.assertEqual(rows["Course/big.txt"]["reason"], "exceeds_25_mib_limit")
        self.assertNotIn("Course/diagram.bin", self.archive.files())
        self.assertNotIn("Course/big.txt", self.archive.files())
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            self.archive.text("Course/diagram.bin")
        with self.assertRaisesRegex(ValueError, "25 MB"):
            self.archive.text("Course/big.txt")

        async def check_tool_contract():
            async with Client(build_server(self.archive)) as client:
                listing = await client.call_tool("list_files", {"course": "Course"})
                self.assertIn("files", listing.data)  # Existing string-ID key remains intact.
                self.assertIn("inventory", listing.data)
                self.assertEqual(listing.data["inventory_total"], 2)
                self.assertEqual({item["id"] for item in listing.data["inventory"]},
                                 {"Course/big.txt", "Course/diagram.bin"})
        asyncio.run(check_tool_contract())


if __name__ == "__main__":
    unittest.main()
