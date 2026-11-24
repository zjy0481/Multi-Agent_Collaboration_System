import io
from unittest.mock import patch

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from collaboration_runtime.search import SearchError
from collaboration_runtime.source_fetch import extract_pdf, fetch_public_source, SourceHTTPError


def pdf(text=True, encrypted=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    if text:
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 10 100 Td (Hello PDF source) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
    if encrypted:
        writer.encrypt('secret')
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pdf_extracts_text_and_rejects_empty_encrypted_and_invalid():
    text, truncated = extract_pdf(pdf())
    assert 'Hello PDF source' in text and not truncated
    for content, message in [(pdf(False), 'no extractable text'), (pdf(encrypted=True), 'encrypted'), (b'bad', 'unreadable')]:
        with pytest.raises(SearchError, match=message):
            extract_pdf(content)


def test_pdf_timeout():
    import subprocess
    with patch('collaboration_runtime.source_fetch.subprocess.run', side_effect=subprocess.TimeoutExpired('worker', 5)):
        with pytest.raises(SearchError, match='time limit'):
            extract_pdf(b'pdf')


def test_http_status_is_available():
    error = SourceHTTPError(403)
    assert error.status_code == 403 and str(error) == 'Source returned HTTP 403'


def test_pdf_fetch_preserves_raw_hash_and_enforces_limit(monkeypatch):
    import hashlib
    import collaboration_runtime.source_fetch as fetch
    body = pdf()
    class Response:
        status = 200
        def getheader(self, name, default=None): return 'application/pdf'
        def read(self, limit): return body[:limit]
    class Connection:
        def __init__(self, *args): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(fetch.socket, 'getaddrinfo', lambda *a, **kw: [(None, None, None, None, ('8.8.8.8', 443))])
    monkeypatch.setattr(fetch, 'PinnedHTTPS', Connection)
    result = fetch_public_source('https://public.example/a.pdf')
    assert result['sha256'] == hashlib.sha256(body).hexdigest()
    assert 'Hello PDF source' in result['text']
    body = b'x' * 5_000_001
    with pytest.raises(SearchError, match='size limit'):
        fetch_public_source('https://public.example/a.pdf')
