import json
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from app.database import fetch_one, insert
from app.document_artifacts import normalize_document, render_artifact, render_html, safe_artifact_file
from app.integrations.schemas import StructuredDocument
from tests.test_v564_gateway_mode import Base


class DocumentArtifactTests(Base):
    def test_schema_rejects_self_test_block(self):
        with self.assertRaises(Exception):
            StructuredDocument.model_validate({"title": "x", "sections": [{"title": "x", "blocks": [{"type": "self_test"}]}]})

    def test_legacy_content_has_no_exercise_section(self):
        doc = normalize_document(None, title="级数", body="定义与解释。", kind="lesson_note")
        text = render_html(doc)
        self.assertIn("级数", text)
        self.assertNotIn("自测题", text)
        self.assertIn('id="document-data"', text)

    def test_files_and_database_record_are_created(self):
        note_id = insert("INSERT INTO notes(title,body,status,version,created_at) VALUES (?,?,?,1,datetime('now'))", ("样例", "正文", "draft"))
        doc = normalize_document(None, title="样例", body="正文", kind="lesson_note")

        def fake_pdf(_html: Path, pdf: Path):
            writer = PdfWriter()
            writer.add_blank_page(width=595, height=842)
            with pdf.open("wb") as handle:
                writer.write(handle)

        with patch("app.document_artifacts._render_pdf", fake_pdf):
            artifact = render_artifact(owner_type="note", owner_id=note_id, document=doc)
        self.assertEqual("ready", artifact["status"])
        self.assertTrue(safe_artifact_file(artifact, "html").is_file())
        self.assertTrue(safe_artifact_file(artifact, "pdf").is_file())
        stored = fetch_one("SELECT structured_json FROM document_artifacts WHERE id=?", (artifact["id"],))
        self.assertEqual("样例", json.loads(stored["structured_json"])["title"])


if __name__ == "__main__":
    unittest.main()
