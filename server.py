from __future__ import annotations
import io, os, tempfile, shutil
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.concurrency import run_in_threadpool
from PIL import Image, ImageOps
import fitz

app = FastAPI(docs_url=None, redoc_url=None)
ROOT = Path(__file__).parent
LIMIT = int(os.environ.get("MAX_UPLOAD_MB", "30")) * 1024 * 1024
MAX_FILES = 30
IMG_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
PROFILES = {"high": (2400, 90), "balanced": (1600, 75), "small": (1100, 55)}
COMPRESS = {"high": (200, 82), "balanced": (150, 65), "small": (100, 45)}

@app.middleware("http")
async def headers(request, call_next):
    if request.url.path.startswith("/api/") and request.method == "POST":
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > LIMIT + 1048576:
            return Response("Слишком большой файл", status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response

@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "index.html").read_text(encoding="utf-8")

@app.get("/api/health")
def health():
    return {"ok": True}

async def upload_to_disk(upload: UploadFile, path: Path, remaining: int) -> int:
    length = 0
    with path.open("wb") as dest:
        while chunk := await upload.read(1024 * 1024):
            length += len(chunk)
            if length > remaining:
                raise HTTPException(413, "Превышен лимит 30 МБ на загрузку.")
            dest.write(chunk)
    await upload.close()
    if length == 0:
        raise HTTPException(400, "Пустой файл.")
    return length

def result(data: bytes, orig: int, pages: int, reduced: bool | None = None):
    h = {"Content-Disposition": 'attachment; filename="result.pdf"', "X-Original-Bytes": str(orig),
         "X-Result-Bytes": str(len(data)), "X-Pages": str(pages)}
    if reduced is not None:
        h["X-Reduced"] = "1" if reduced else "0"
    return Response(data, media_type="application/pdf", headers=h)

def create_pdf(paths: list[Path], profile: str) -> bytes:
    limit, quality = PROFILES[profile]
    doc = fitz.open()
    try:
        for path in paths:
            try:
                with Image.open(path) as src:
                    image = ImageOps.exif_transpose(src).convert("RGB")
                    image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
                    out = io.BytesIO()
                    image.save(out, "JPEG", quality=quality, optimize=True)
                    width, height = image.size
            except Exception as exc:
                raise ValueError(f"Не удалось прочитать {path.name}") from exc
            paper = fitz.paper_size("a4")
            pw, ph = (paper[1], paper[0]) if width > height else paper
            page = doc.new_page(width=pw, height=ph)
            page.insert_image(fitz.Rect(24, 24, pw-24, ph-24), stream=out.getvalue(), keep_proportion=True)
        return doc.tobytes(garbage=4, deflate=True, use_objstms=1)
    finally:
        doc.close()

def compress_pdf(path: Path, profile: str) -> tuple[bytes, int]:
    orig = path.read_bytes()
    dpi, quality = COMPRESS[profile]
    with fitz.open(stream=orig, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("Документ защищён паролем.")
        if doc.page_count == 0:
            raise ValueError("В документе нет страниц.")
        pages = doc.page_count
        # Recompress images while preserving vector content and selectable text.
        doc.rewrite_images(dpi_threshold=None, dpi_target=dpi, quality=quality,
                           lossy=True, lossless=True, bitonal=False, color=True, gray=True)
        candidate = doc.tobytes(garbage=4, deflate=True, deflate_images=True,
                                deflate_fonts=True, use_objstms=1)
    return (candidate if len(candidate) < len(orig) else orig), pages

@app.post("/api/create")
async def api_create(files: list[UploadFile] = File(...), quality: str = Form("balanced")):
    if quality not in PROFILES:
        raise HTTPException(400, "Неизвестный режим качества.")
    if not files or len(files) > MAX_FILES:
        raise HTTPException(400, "Можно загрузить не более 30 фотографий.")
    with tempfile.TemporaryDirectory() as temp:
        paths = []
        total = 0
        for i, upload in enumerate(files):
            ext = Path(upload.filename or "").suffix.lower()
            if ext not in IMG_TYPES:
                raise HTTPException(400, "Неподдерживаемый формат изображения.")
            path = Path(temp) / f"photo_{i:03d}{ext}"
            total += await upload_to_disk(upload, path, LIMIT - total)
            paths.append(path)
        try:
            data = await run_in_threadpool(create_pdf, paths, quality)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return result(data, total, len(paths))

@app.post("/api/compress")
async def api_compress(file: UploadFile = File(...), quality: str = Form("balanced")):
    if quality not in COMPRESS:
        raise HTTPException(400, "Неизвестный режим качества.")
    if Path(file.filename or "").suffix.lower() != ".pdf":
        raise HTTPException(400, "Выберите PDF-файл.")
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "input.pdf"
        size = await upload_to_disk(file, path, LIMIT)
        try:
            data, pages = await run_in_threadpool(compress_pdf, path, quality)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception:
            raise HTTPException(400, "Не удалось обработать PDF.")
        return result(data, size, pages, len(data) < size)
