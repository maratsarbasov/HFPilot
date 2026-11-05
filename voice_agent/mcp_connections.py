"""Public addresses are suggestions until the local owner pins an HTTPS origin."""
import ipaddress
import re
import time
from urllib.parse import urlsplit

from .mcp_state import atomic_text


def public_origin(value):
    message = 'Enter a public HTTPS address ending in /mcp, or just its HTTPS origin.'
    if not isinstance(value, str):
        raise ValueError(message)
    value = value.strip()
    if not re.fullmatch(r'https://[a-zA-Z0-9.\-:\[\]]+(?:/mcp/?)?/?', value, re.ASCII):
        raise ValueError(message)
    try:
        url = urlsplit(value)
        host, port = url.hostname, url.port
        if not host or len(host) > 253 or port == 0 or url.path not in ('', '/', '/mcp', '/mcp/') or url.netloc.endswith(':'):
            raise ValueError()
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            labels = host.split('.')
            if (len(labels) < 2 or labels[-1] in ('local', 'localhost') or not labels[-1].isalpha()
                    or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', s) for s in labels)):
                raise ValueError()
        else:
            if not ip.is_global or ip.is_multicast:
                raise ValueError()
            host = '[' + ip.compressed + ']' if ip.version == 6 else ip.compressed
        # Reject authorities urlsplit tolerates, such as "[ipv6]junk".
        original = url.netloc.lower()
        if original != host and original != f'{host}:{port}':
            raise ValueError()
        return 'https://' + host + (f':{port}' if port and port != 443 else '')
    except ValueError:
        raise ValueError(message) from None


def saved_origin(root):
    try:
        return public_origin((root / 'public-url').read_text())
    except FileNotFoundError:
        return None


def save_origin(root, value):
    base = public_origin(value)
    atomic_text(root / 'public-url', base + '\n')
    return base


def request_origin(request):
    # Never fetch or echo this name. Host usually survives Cloudflare, some
    # proxies rewrite Host and carry the original authority in X-Forwarded-Host.
    for key in ('Host', 'X-Forwarded-Host'):
        values = request.headers.getall(key, [])
        if len(values) == 1 and not any(c in values[0] for c in '/,'):
            try:
                return public_origin('https://' + values[0])
            except ValueError:
                pass
    return None


class Suggestions:
    def __init__(self):
        self.origin = self.dismissed = None
        self.expires = 0

    def current(self):
        return self.origin if time.monotonic() < self.expires else None

    def observe(self, origin):
        if origin and not self.current() and not (origin == self.dismissed and time.monotonic() < self.expires):
            self.origin, self.expires = origin, time.monotonic() + 180

    def dismiss(self):
        self.dismissed, self.origin = self.origin, None
        self.expires = time.monotonic() + 180
