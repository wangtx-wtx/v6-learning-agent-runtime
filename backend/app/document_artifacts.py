"""Structured learning documents rendered locally to deterministic HTML and PDF."""
from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import ARTIFACTS_DIR
from .database import execute, fetch_one, insert
from .integrations.schemas import StructuredDocument

TEMPLATE_ID = "british_newspaper_v1"
TEMPLATE_VERSION = 1

#: V6 Phase 4：模型教学补充的可见标识（HTML 与 PDF 都必须出现该**文字**，
#: 不能只靠颜色区分 —— PDF 打印可能丢失颜色）。
AI_EXPLANATION_LABEL = "模型教学补充（非课堂原话）"

#: 每种 block 类型对应的 ``data-claim-type``（缺省等于 block 类型）。
_CLAIM_TYPE_BY_BLOCK = {
    "ai_explanation": "ai_explanation",
}


def _legacy_document(title: str, body: str, outline: list | None = None, kind: str = "lesson_note") -> dict:
    blocks = []
    if outline:
        blocks.append({"type": "key_point", "title": "内容提要", "items": [str(x) for x in outline], "content": "", "rows": [], "source_refs": []})
    for part in [p.strip() for p in (body or "").split("\n\n") if p.strip()]:
        blocks.append({"type": "paragraph", "title": "", "content": part, "items": [], "rows": [], "source_refs": []})
    return {"title": title or ("课堂笔记" if kind == "lesson_note" else "复习讲义"),
            "subtitle": "课堂笔记" if kind == "lesson_note" else "复习讲义",
            "deck": "依据课程材料整理的结构化学习文档。",
            "sections": [{"title": "正文", "blocks": blocks or [{"type": "paragraph", "content": "暂无正文", "title": "", "items": [], "rows": [], "source_refs": []}]}],
            "sources": []}


def normalize_document(raw: dict | None, *, title: str, body: str, outline: list | None = None,
                       kind: str = "lesson_note") -> dict:
    candidate = raw or _legacy_document(title, body, outline, kind)
    doc = StructuredDocument.model_validate(candidate).model_dump()
    if not doc["title"]:
        doc["title"] = title
    # Extra semantic guard: schemas cannot smuggle exercises through titles/types.
    forbidden = ("自测题", "参考答案", "答案解析", "self-test", "self test")
    for section in doc["sections"]:
        section["blocks"] = [b for b in section["blocks"] if not any(x in (b.get("title") or "").lower() for x in forbidden)]
    return doc


def _e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _block_html(block: dict) -> str:
    typ = block["type"]
    title = block.get("title") or ""
    labels = {"definition": "定义", "theorem": "定理", "formula": "公式", "derivation": "推导",
              "example": "例析", "procedure": "步骤", "comparison": "辨析", "table": "表格",
              "figure": "图示", "quote": "原声", "key_point": "要点", "notice": "注意",
              "pitfall": "易错点", "exception": "例外", "memory_tip": "记忆提示", "summary": "小结",
              "source_note": "资料注", "ai_explanation": "模型教学补充"}
    heading = f'<h3>{_e(title or labels.get(typ, ""))}</h3>' if title or typ != "paragraph" else ""
    content = f'<div class="content">{_e(block.get("content")).replace(chr(10), "<br>")}</div>' if block.get("content") else ""
    items = block.get("items") or []
    listing = "<ol>" + "".join(f"<li>{_e(x)}</li>" for x in items) + "</ol>" if items else ""
    rows = block.get("rows") or []
    table = ""
    if rows:
        table = "<table>" + "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>" for row in rows) + "</table>"
    refs = block.get("source_refs") or []
    citation = "".join(
        f'<span class="source">{_e(r.get("source_id") or ("CHUNK#" + str(r.get("chunk_id"))))} {_e(r.get("locator"))}</span>'
        for r in refs if r.get("source_id") or r.get("chunk_id") is not None)
    # Evidence V2 稳定标识：ai_explanation 必须可被机器识别（不依赖颜色）。
    claim_type = block.get("claim_type") or _CLAIM_TYPE_BY_BLOCK.get(typ)
    claim_attr = f' data-claim-type="{_e(claim_type)}"' if claim_type else ""
    claim_key = block.get("claim_key") or ""
    claim_key_attr = f' data-claim-key="{_e(claim_key)}"' if claim_key else ""
    badge = ""
    if typ == "ai_explanation":
        badge = f'<div class="claim-badge">{_e(AI_EXPLANATION_LABEL)}</div>'
    return (f'<article class="flow-item block block-{_e(typ)}"{claim_attr}{claim_key_attr}>'
            f'{badge}{heading}{content}{listing}{table}{citation}</article>')


def render_html(document: dict) -> str:
    payload = json.dumps(document, ensure_ascii=False).replace("</", "<\\/")
    source = []
    for section in document.get("sections", []):
        source.append(f'<div class="flow-item section-title"><span>{_e(section.get("title"))}</span></div>')
        source.extend(_block_html(b) for b in section.get("blocks", []))
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(document.get("title"))}</title><style>
@page{{size:A4;margin:0}}*{{box-sizing:border-box}}body{{margin:0;background:#d8d5cc;color:#171713;font-family:"Noto Serif CJK SC","Songti SC",SimSun,serif}}
#source{{display:none}}#pages{{padding:12mm 0}}.sheet{{width:210mm;height:297mm;margin:0 auto 10mm;background:#f6f0df;padding:10mm 12mm 9mm;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 2mm 8mm #0003;position:relative}}
.mast{{border-top:3px double #171713;border-bottom:1px solid #171713;padding:3mm 0 2mm;text-align:center}}.kicker{{font:700 9px Arial;letter-spacing:.28em;text-transform:uppercase}}h1{{font-size:30px;line-height:1.05;margin:1.5mm 0}}.deck{{font-size:11px;color:#585347}}
.page-body{{height:244mm;overflow:hidden;padding-top:5mm;column-count:2;column-gap:8mm;column-rule:1px solid #b6ae9b}}.section-title{{column-span:all;border-top:2px solid #171713;border-bottom:1px solid #171713;text-align:center;margin:0 0 4mm;padding:1.4mm;font:bold 12px Arial;letter-spacing:.12em}}
.block{{break-inside:avoid;margin:0 0 4mm;padding:0 0 3mm;border-bottom:1px solid #c5bdab;font-size:10.5px;line-height:1.58}}.block h3{{font:bold 12px Arial;margin:0 0 1.5mm;text-transform:uppercase;letter-spacing:.06em}}.block ol{{margin:1mm 0;padding-left:5mm}}.block li{{margin-bottom:1mm}}.block-definition,.block-theorem,.block-key_point{{border-left:3px solid #172f4f;padding-left:3mm}}.block-notice,.block-pitfall,.block-exception{{border:1px solid #8a2a22;padding:3mm;background:#efe1d2}}.block-formula{{text-align:center;background:#e8e2d2;padding:3mm;font-family:Cambria,serif}}.block-quote{{font-style:italic;padding-left:4mm;border-left:1px solid #777}}
.block-ai_explanation{{border:1px dashed #4a4a44;padding:3mm;background:#eee9db}}
.block-ai_explanation .claim-badge{{font:700 8px Arial;letter-spacing:.12em;text-transform:uppercase;border:1px solid #4a4a44;padding:.6mm 1.6mm;display:inline-block;margin-bottom:1.5mm}}
table{{border-collapse:collapse;width:100%;font-size:9px}}td{{border:1px solid #777;padding:1.5mm}}.source{{display:block;margin-top:1.5mm;font:8px Arial;color:#766f61}}.folio{{margin-top:auto;border-top:1px solid #171713;padding-top:2mm;display:flex;justify-content:space-between;font:8px Arial;letter-spacing:.08em}}
@media print{{body{{background:white}}#pages{{padding:0}}.sheet{{margin:0;box-shadow:none;break-after:page}}.sheet:last-child{{break-after:auto}}}}
</style></head><body><script id="document-data" type="application/json">{payload}</script>
<div id="source">{''.join(source)}</div><main id="pages"></main><script>
const src=[...document.querySelectorAll('#source>.flow-item')], pages=document.querySelector('#pages');
function page(){{const n=pages.children.length+1,s=document.createElement('section');s.className='sheet';s.innerHTML=`<header class="mast"><div class="kicker">V5 Learning Chronicle · Structured Edition</div><h1>{_e(document.get('title'))}</h1><div class="deck">{_e(document.get('subtitle'))} · {_e(document.get('deck'))}</div></header><div class="page-body"></div><footer class="folio"><span>V5 STUDY AGENT</span><span>PAGE ${{n}}</span></footer>`;pages.append(s);return s.querySelector('.page-body')}}
let body=page();for(const item of src){{const clone=item.cloneNode(true);body.append(clone);if(body.scrollHeight>body.clientHeight){{clone.remove();body=page();body.append(clone)}}}}document.body.dataset.rendered='ready';
</script></body></html>'''


def _chromium() -> str:
    candidates = [os.getenv("V5_CHROMIUM_PATH"), shutil.which("chrome"), shutil.which("msedge"),
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]
    for value in candidates:
        if value and Path(value).is_file():
            return str(value)
    raise RuntimeError("未找到 Chrome/Edge；可设置 V5_CHROMIUM_PATH")


def _render_pdf(html_path: Path, pdf_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="v5-render-") as td:
        tmp = Path(td) / "document.pdf"
        cmd = [_chromium(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
               "--run-all-compositor-stages-before-draw", "--virtual-time-budget=5000",
               f"--print-to-pdf={tmp}", html_path.resolve().as_uri()]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        if proc.returncode or not tmp.exists() or tmp.read_bytes()[:4] != b"%PDF":
            raise RuntimeError((proc.stderr or proc.stdout).decode(errors="replace")[-500:])
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, pdf_path)


def render_artifact(*, owner_type: str, owner_id: int, document: dict, render_pdf: bool = True) -> dict:
    if owner_type not in ("note", "review"):
        raise ValueError("owner_type 必须是 note 或 review")
    doc = StructuredDocument.model_validate(document).model_dump()
    encoded = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    folder = (ARTIFACTS_DIR / ("notes" if owner_type == "note" else "reviews") / f"{owner_type}-{owner_id}").resolve()
    folder.relative_to(ARTIFACTS_DIR.resolve())
    folder.mkdir(parents=True, exist_ok=True)
    html_path, pdf_path = folder / "document.html", folder / "document.pdf"
    key, dtype = ("note_id", "lesson_note") if owner_type == "note" else ("review_id", "review_handout")
    row = fetch_one(f"SELECT * FROM document_artifacts WHERE {key}=? AND template_id=? AND template_version=?", (owner_id, TEMPLATE_ID, TEMPLATE_VERSION))
    if row:
        aid = row["id"]
        execute("UPDATE document_artifacts SET structured_json=?,content_hash=?,status='rendering',error=NULL,updated_at=datetime('now','localtime') WHERE id=?", (encoded, digest, aid))
    else:
        aid = insert(f"INSERT INTO document_artifacts ({key},document_type,template_id,template_version,structured_json,content_hash,status) VALUES (?,?,?,?,?,?,'rendering')", (owner_id, dtype, TEMPLATE_ID, TEMPLATE_VERSION, encoded, digest))
    try:
        tmp_html = html_path.with_suffix(".html.tmp")
        tmp_html.write_text(render_html(doc), encoding="utf-8")
        os.replace(tmp_html, html_path)
        if render_pdf:
            _render_pdf(html_path, pdf_path)
        status = "ready" if pdf_path.exists() else "partial"
        execute("UPDATE document_artifacts SET html_path=?,pdf_path=?,status=?,updated_at=datetime('now','localtime') WHERE id=?",
                (str(html_path), str(pdf_path) if pdf_path.exists() else None, status, aid))
    except Exception as exc:
        execute("UPDATE document_artifacts SET html_path=?,status='failed',error=?,updated_at=datetime('now','localtime') WHERE id=?", (str(html_path) if html_path.exists() else None, str(exc)[:500], aid))
        raise
    return fetch_one("SELECT * FROM document_artifacts WHERE id=?", (aid,)) or {}


def artifact_public(row: dict) -> dict:
    return {k: row.get(k) for k in ("id", "note_id", "review_id", "document_type", "template_id", "template_version", "content_hash", "status", "error", "created_at", "updated_at")} | {"has_html": bool(row.get("html_path")), "has_pdf": bool(row.get("pdf_path"))}


def safe_artifact_file(row: dict, fmt: str) -> Path:
    value = row.get(f"{fmt}_path")
    if not value:
        raise FileNotFoundError(fmt)
    path = Path(value).resolve()
    path.relative_to(ARTIFACTS_DIR.resolve())
    if not path.is_file():
        raise FileNotFoundError(fmt)
    return path
