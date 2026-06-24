import json
import fitz  # PyMuPDF

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI(title="PDF → JSON Extractor (Free)", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def extract_tables(page):
    """Try to extract tables from a page."""
    tables = []
    try:
        found = page.find_tables()
        for table in found.tables:
            data = table.extract()
            if data:
                tables.append(data)
    except Exception:
        pass
    return tables


def extract_page(page, page_num):
    """Extract all content from a single page."""
    # Plain text
    text = page.get_text("text").strip()

    # Text blocks with position info
    blocks_raw = page.get_text("blocks")
    blocks = []
    for b in blocks_raw:
        x0, y0, x1, y1, content, block_no, block_type = b
        if content.strip():
            blocks.append({
                "block": block_no,
                "type": "text" if block_type == 0 else "image",
                "x0": round(x0, 1),
                "y0": round(y0, 1),
                "x1": round(x1, 1),
                "y1": round(y1, 1),
                "text": content.strip(),
            })

    # Tables
    tables = extract_tables(page)

    # Images count
    images = page.get_images(full=False)

    return {
        "page": page_num,
        "width": round(page.rect.width, 1),
        "height": round(page.rect.height, 1),
        "text": text,
        "blocks": blocks,
        "tables": tables,
        "image_count": len(images),
    }


@app.post("/extract")
async def extract_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF.")

    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Archivo demasiado grande. Máx 20 MB.")

    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"No se pudo abrir el PDF: {str(e)}")

    meta = doc.metadata or {}
    pages = []
    full_text_parts = []

    for i, page in enumerate(doc):
        page_data = extract_page(page, i + 1)
        pages.append(page_data)
        if page_data["text"]:
            full_text_parts.append(page_data["text"])

    doc.close()

    result = {
        "filename": file.filename,
        "metadata": {
            "title":    meta.get("title") or None,
            "author":   meta.get("author") or None,
            "subject":  meta.get("subject") or None,
            "creator":  meta.get("creator") or None,
            "producer": meta.get("producer") or None,
            "created":  meta.get("creationDate") or None,
            "modified": meta.get("modDate") or None,
        },
        "total_pages": len(pages),
        "full_text": "\n\n".join(full_text_parts),
        "pages": pages,
    }

    return JSONResponse(content=result)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"message": "PDF → JSON API. POST /extract con un archivo PDF."}
