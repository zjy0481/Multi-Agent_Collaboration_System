"""Fetch public search results through a bounded, address-pinned HTTPS connection."""
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
from urllib.parse import urldefrag, urljoin, urlsplit

from .pdf_worker import MAX_PDF_BYTES, MAX_TEXT_CHARS
from .search import SearchError


class SourceHTTPError(SearchError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f'Source returned HTTP {status_code}')


def extract_pdf(content):
    worker = Path(__file__).with_name('pdf_worker.py')
    env = {name: os.environ[name] for name in ('SYSTEMROOT', 'WINDIR') if name in os.environ}
    try:
        result = subprocess.run([sys.executable, '-I', str(worker)], input=content, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=True)
        payload = json.loads(result.stdout)
        if 'error' in payload:
            raise SearchError(payload['error'])
        return payload['text'], payload['truncated']
    except (subprocess.SubprocessError, OSError, ValueError, KeyError):
        raise SearchError('PDF extraction failed or exceeded time limit') from None


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript'} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, address):
        super().__init__(host, port, timeout=12, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch_public_source(url):
    """Follow at most five redirects, validating and pinning every destination."""
    try:
        current = urldefrag(url)[0]
        chain = [current]
        for hop in range(6):
            parts = urlsplit(current)
            if (len(current) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in current)
                    or parts.scheme != 'https' or not parts.hostname
                    or parts.username is not None or parts.password is not None
                    or parts.port not in (None, 443)):
                raise SearchError('Source must use public HTTPS on port 443')
            addresses = {item[4][0] for item in socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)}
            if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
                raise SearchError('Source resolves to a non-public address')
            connection = PinnedHTTPS(parts.hostname, 443, sorted(addresses)[0])
            try:
                target = parts.path or '/'
                if parts.query:
                    target += '?' + parts.query
                connection.request('GET', target, headers={'Accept': 'text/html,text/plain,application/pdf', 'User-Agent': 'CourseCollaboration/1.0'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location or len(location) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in location):
                        raise SearchError('Source redirect has an invalid location')
                    if hop == 5:
                        raise SearchError('Source redirect limit exceeded')
                    next_url = urldefrag(urljoin(current, location))[0]
                    if next_url in chain:
                        raise SearchError('Source redirect loop detected')
                    chain.append(next_url)
                    current = next_url
                    continue
                if response.status != 200:
                    raise SourceHTTPError(response.status)
                kind = response.getheader('Content-Type', '').split(';')[0].strip().lower()
                if kind not in {'text/html', 'text/plain', 'application/pdf'}:
                    raise SearchError('Source content type is not supported')
                limit = MAX_PDF_BYTES if kind == 'application/pdf' else 1_000_000
                content = response.read(limit + 1)
                if len(content) > limit:
                    raise SearchError('Source response exceeds size limit')
                if kind == 'application/pdf':
                    text, truncated = extract_pdf(content)
                else:
                    decoded = content.decode('utf-8', errors='replace')
                    if kind == 'text/html':
                        parser = TextParser()
                        parser.feed(decoded)
                        decoded = '\n'.join(parser.parts)
                    text = decoded[:MAX_TEXT_CHARS]
                    truncated = len(decoded) > len(text)
                return {'url': url, 'final_url': current, 'redirect_chain': chain,
                        'content_type': kind, 'text': text, 'truncated': truncated,
                        'sha256': hashlib.sha256(content).hexdigest()}
            finally:
                connection.close()
    except SearchError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise SearchError('Source retrieval failed') from None
