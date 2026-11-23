"""Fetch public search results through a bounded, address-pinned HTTPS connection."""
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

from .search import SearchError


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
    try:
        parts = urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.port not in (None, 443):
            raise SearchError('Source must use public HTTPS on port 443')
        addresses = {item[4][0] for item in socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise SearchError('Source resolves to a non-public address')
        connection = PinnedHTTPS(parts.hostname, 443, sorted(addresses)[0])
        try:
            target = parts.path or '/'
            if parts.query:
                target += '?' + parts.query
            connection.request('GET', target, headers={'Accept': 'text/html,text/plain', 'User-Agent': 'CourseCollaboration/1.0'})
            response = connection.getresponse()
            if response.status != 200:
                raise SearchError(f'Source returned HTTP {response.status}; redirects are not followed')
            kind = response.getheader('Content-Type', '').split(';')[0].strip().lower()
            if kind not in {'text/html', 'text/plain'}:
                raise SearchError('Source content type is not supported')
            content = response.read(1_000_001)
            if len(content) > 1_000_000:
                raise SearchError('Source response exceeds size limit')
            decoded = content.decode('utf-8', errors='replace')
            if kind == 'text/html':
                parser = TextParser()
                parser.feed(decoded)
                decoded = '\n'.join(parser.parts)
            text = decoded[:40000]
            return {'url': url, 'content_type': kind, 'text': text, 'truncated': len(decoded) > len(text),
                    'sha256': hashlib.sha256(content).hexdigest()}
        finally:
            connection.close()
    except SearchError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise SearchError('Source retrieval failed') from None
