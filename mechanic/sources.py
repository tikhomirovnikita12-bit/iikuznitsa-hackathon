from dataclasses import dataclass
from html import escape
from io import BytesIO

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pypdf import PdfReader, PdfWriter

from .config import MATERIALS, PREVIEWS
from .contracts import Reference


@dataclass(frozen=True)
class Manual:
    slug: str
    name: str
    label: str
    filename: str
    pages: int
    engines: tuple[str, ...]

    def reference(self, page: int) -> Reference:
        return Reference(manual=self.name, pdf_page=page)


YAMZ236 = Manual(
    "yamz236-re", "236-3902150-Б РЭ", "ЯМЗ-236. Руководство по эксплуатации",
    "ЯМЗ_236_руководство по эксплуатации.pdf", 172, ("ЯМЗ-236М2",),
)
ZMZ5234 = Manual(
    "zmz5234", "Двигатели ЗМЗ-5234.10 со сцеплением. Руководство по ремонту",
    "ЗМЗ-5234.10. Руководство по ремонту", "zmz-5234_rk_2025.pdf", 158, ("ЗМЗ-5234.10",),
)
ZMZ409 = Manual(
    "zmz409", "Руководство по эксплуатации, техническому обслуживанию и ремонту двигателей ЗМЗ-40904.10 и ЗМЗ-40905.10",
    "ЗМЗ-40904.10 и ЗМЗ-40905.10. Эксплуатация, ТО и ремонт",
    "zmz-40904-40905_rk_2025.pdf", 238, ("ЗМЗ-40904.10", "ЗМЗ-40905.10"),
)
MANUALS = (
    YAMZ236,
    Manual("yamz236-rk", "236.01.01 РК", "ЯМЗ-236. Руководство по ремонту",
           "ЯМЗ_236_руководство по РЕМОНТУ.pdf", 287, ("ЯМЗ-236М2",)),
    Manual("yamz536-re", "536.3902150 РЭ", "ЯМЗ-536. Руководство по эксплуатации",
           "yamz-536_re_2016.pdf", 285, ("ЯМЗ-536",)),
    Manual("yamz536-rk", "536.01.02 РК", "ЯМЗ-536. Руководство по ремонту",
           "yamz-536_rk_2013.pdf", 603, ("ЯМЗ-536",)),
    ZMZ5234,
    ZMZ409,
)
BY_SLUG = {manual.slug: manual for manual in MANUALS}
BY_NAME = {name: manual for manual in MANUALS for name in (manual.name, manual.filename)}
router = APIRouter()


def valid_reference(reference: Reference, engine: str) -> bool:
    manual = BY_NAME.get(reference.manual)
    return bool(manual and engine in manual.engines and reference.pdf_page <= manual.pages)


def catalog() -> list[dict]:
    return [
        {"manual": manual.name, "label": manual.label, "filename": manual.filename,
         "slug": manual.slug, "pages": manual.pages, "engine_models": manual.engines}
        for manual in MANUALS
    ]


def find_manual(slug: str, page: int = 1) -> Manual:
    manual = BY_SLUG.get(slug)
    if manual is None or not 1 <= page <= manual.pages or not (MATERIALS / manual.filename).is_file():
        raise HTTPException(404, "Страница руководства не найдена")
    return manual


@router.get("/manuals/{slug}.pdf", include_in_schema=False)
def original(slug: str):
    manual = find_manual(slug)
    return FileResponse(MATERIALS / manual.filename, media_type="application/pdf",
                        filename=manual.filename, content_disposition_type="inline")


@router.get("/sources/{slug}/{page}/page.pdf", include_in_schema=False)
def single_page(slug: str, page: int):
    manual = find_manual(slug, page)
    with (MATERIALS / manual.filename).open("rb") as source:
        writer = PdfWriter()
        writer.add_page(PdfReader(source).pages[page - 1])
        output = BytesIO()
        writer.write(output)
    return Response(output.getvalue(), media_type="application/pdf")


@router.get("/sources/{slug}/{page}", response_class=HTMLResponse, include_in_schema=False)
def source_page(slug: str, page: int):
    manual = find_manual(slug, page)
    label = escape(manual.label)
    file = escape(manual.filename)
    preview = PREVIEWS / f"{slug}-{page}.png"
    image = (
        f'<img class="source-image" src="/sources/{slug}/{page}/preview.png" '
        f'alt="{label}, страница PDF {page}">' if preview.is_file() else ""
    )
    return HTMLResponse(f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Страница PDF {page} · {label}</title><link rel="stylesheet" href="/assets/styles.css">
</head><body class="source-body"><main class="source-main">
  <header class="source-header">
    <p class="source-position">Страница PDF {page} из {manual.pages}</p>
    <h1>{label}</h1>
    <p class="source-filename">{file}</p>
    <nav class="source-links" aria-label="Руководство в PDF">
      <a class="button primary" href="/manuals/{slug}.pdf#page={page}">Открыть оригинал PDF</a>
      <a class="button secondary" href="/sources/{slug}/{page}/page.pdf">Открыть только страницу {page}</a>
    </nav>
  </header>
  {image}
</main></body></html>""")


@router.get("/sources/{slug}/{page}/preview.png", include_in_schema=False)
def source_image(slug: str, page: int):
    find_manual(slug, page)
    preview = PREVIEWS / f"{slug}-{page}.png"
    if not preview.is_file():
        raise HTTPException(404, "Предпросмотр страницы отсутствует")
    return FileResponse(preview, media_type="image/png")
