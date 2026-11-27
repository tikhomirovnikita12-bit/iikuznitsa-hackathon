"""A source stays useful without a generated preview and uses PDF page numbering."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mechanic import sources


def test_source_viewer_without_preview_and_page_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "MATERIALS", tmp_path)
    monkeypatch.setattr(sources, "PREVIEWS", tmp_path / "previews")
    (tmp_path / sources.YAMZ236.filename).touch()
    app = FastAPI()
    app.include_router(sources.router)
    with TestClient(app) as client:
        response = client.get("/sources/yamz236-re/19")
        assert response.status_code == 200
        assert "Страница PDF 19 из 172" in response.text
        assert "Печатная нумерация" in response.text
        assert "Руководство для: ЯМЗ-236М2" in response.text
        assert 'id="source-context"' in response.text
        assert 'href="/sources/yamz236-re/19/page.pdf"' in response.text
        assert "Предпросмотр недоступен" in response.text
        assert 'class="source-image"' not in response.text
        for path in ("/sources/yamz236-re/0", "/sources/yamz236-re/173", "/sources/unknown/19"):
            assert client.get(path).status_code == 404
        (tmp_path / "previews").mkdir()
        (tmp_path / "previews" / "yamz236-re-19.png").touch()
        response = client.get("/sources/yamz236-re/19")
        assert 'class="source-image"' in response.text
        assert 'id="source-preview-note" role="status" hidden' in response.text
