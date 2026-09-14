"""模型输出归一化回归（V5.6.4）。

主模型（deepseek-flash）偶尔把 ``source_refs`` 写成字符串数组或纯数字，
旧实现会直接判定 schema 失败，导致 note_writer 退化到备用模型。
归一化只纠正“形状”，不补造内容；真正的类型/缺字段错误仍须拒绝。
"""
import json
import unittest

from app.dag import SchemaValidationError
from app.integrations.schemas import (
    NoteWriterOut,
    ReviewWriterOut,
    parse_model_output,
)


class TestSourceRefNormalization(unittest.TestCase):
    def test_string_and_int_source_refs_are_normalized(self):
        raw = {
            "title": "无穷级数",
            "body": "正文",
            "document": {
                "title": "D",
                "sections": [{
                    "title": "S",
                    "blocks": [{
                        "type": "paragraph",
                        "content": "c",
                        "source_refs": ["课堂转写 08:14", 12, {"chunk_id": 7, "quote": "q"}],
                    }],
                }],
                "sources": ["PPT 第 3 页"],
            },
        }
        out = parse_model_output(NoteWriterOut, json.dumps(raw, ensure_ascii=False), "note_writer")
        refs = out["document"]["sections"][0]["blocks"][0]["source_refs"]
        self.assertEqual(refs[0]["quote"], "课堂转写 08:14")
        self.assertEqual(refs[1]["chunk_id"], 12)
        self.assertEqual(refs[2]["chunk_id"], 7)
        self.assertEqual(out["document"]["sources"][0]["quote"], "PPT 第 3 页")

    def test_chunk_prefixed_string_maps_to_chunk_id(self):
        raw = {
            "outline": [],
            "materials": "",
            "document": {
                "title": "R",
                "sections": [{
                    "title": "S",
                    "blocks": [{"type": "formula", "content": "x", "source_refs": ["chunk 3"]}],
                }],
            },
        }
        out = parse_model_output(ReviewWriterOut, json.dumps(raw, ensure_ascii=False), "review_writer")
        self.assertEqual(
            out["document"]["sections"][0]["blocks"][0]["source_refs"][0]["chunk_id"], 3
        )

    def test_string_block_becomes_paragraph(self):
        raw = {
            "title": "T",
            "body": "b",
            "document": {"sections": [{"title": "S", "blocks": ["纯文本段落"]}]},
        }
        out = parse_model_output(NoteWriterOut, json.dumps(raw, ensure_ascii=False), "note_writer")
        block = out["document"]["sections"][0]["blocks"][0]
        self.assertEqual(block["type"], "paragraph")
        self.assertEqual(block["content"], "纯文本段落")

    def test_unknown_block_type_still_rejected(self):
        raw = {
            "title": "T",
            "document": {"sections": [{"blocks": [{"type": "not_a_real_type"}]}]},
        }
        with self.assertRaises(SchemaValidationError):
            parse_model_output(NoteWriterOut, json.dumps(raw), "note_writer")

    def test_string_chunk_id_is_coerced(self):
        """模型常把 chunk_id 写成字符串；不规整会让真实引用被判为不存在。"""
        raw = {
            "title": "T",
            "body": "b",
            "document": {"sections": [{"title": "S", "blocks": [
                {"type": "paragraph", "content": "c", "source_refs": [
                    {"chunk_id": "788", "quote": "q1"},
                    {"chunkId": "chunk 12", "quote": "q2"},
                    {"chunk_id": "无", "quote": "q3"},
                    {"chunk_id": 788.0, "quote": "q4"},
                ]},
            ]}]},
            "evidence": [{"chunk_id": "788", "quote": "e1"}, {"chunkId": 790, "quote": "e2"}],
        }
        out = parse_model_output(NoteWriterOut, json.dumps(raw, ensure_ascii=False), "note_writer")
        refs = out["document"]["sections"][0]["blocks"][0]["source_refs"]
        self.assertEqual([r["chunk_id"] for r in refs], [788, 12, None, 788])
        self.assertEqual([e.get("chunk_id") for e in out["evidence"]], [788, 790])

    def test_non_json_still_rejected(self):
        with self.assertRaises(SchemaValidationError):
            parse_model_output(NoteWriterOut, "这不是 JSON", "note_writer")


if __name__ == "__main__":
    unittest.main()
