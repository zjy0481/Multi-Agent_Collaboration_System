"""Isolated, time-limited PDF text extraction worker; stdin and stdout are private IPC."""
import io
import json
import sys

MAX_PDF_BYTES = 5_000_000
MAX_TEXT_CHARS = 40_000
MAX_PAGES = 20


def extract(data):
    from pypdf import PdfReader, apply_configuration

    with apply_configuration(maximum_declared_stream_length=MAX_PDF_BYTES,
                             array_based_stream_maximum_output_length=MAX_PDF_BYTES,
                             zlib_maximum_output_length=MAX_PDF_BYTES,
                             lzw_maximum_output_length=MAX_PDF_BYTES,
                             run_length_maximum_output_length=MAX_PDF_BYTES,
                             brotli_maximum_output_length=MAX_PDF_BYTES,
                             jbig2_maximum_output_length=MAX_PDF_BYTES,
                             stream_decoding_work_maximum_length=10_000_000,
                             stream_parsing_work_maximum_count=200_000,
                             page_tree_maximum_entries=5000,
                             page_tree_maximum_page_entries=100_000,
                             jbig2dec_binary=None):
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return {'error': 'PDF is encrypted'}
        count = len(reader.pages)
        parts = []
        length = 0
        truncated = count > MAX_PAGES
        for index in range(min(count, MAX_PAGES)):
            page = reader.pages[index]
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > MAX_PDF_BYTES:
                return {'error': 'PDF content stream exceeds size limit'}
            text = page.extract_text() or ''
            if parts:
                text = '\n' + text
            remaining = MAX_TEXT_CHARS - length
            parts.append(text[:remaining])
            length += len(parts[-1])
            truncated = truncated or len(text) > remaining
            if length == MAX_TEXT_CHARS:
                truncated = truncated or index + 1 < count
                break
        text = ''.join(parts)
        if not text.strip():
            return {'error': 'PDF has no extractable text (possibly scanned)'}
        return {'text': text, 'truncated': truncated}


def main():
    try:
        content = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
        result = (extract(content) if len(content) <= MAX_PDF_BYTES
                  else {'error': 'PDF exceeds size limit'})
    except Exception:
        result = {'error': 'PDF is unreadable or exceeds extraction limits'}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
