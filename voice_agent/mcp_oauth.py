"""Single-owner OAuth authorization-code/PKCE for a personal MCP endpoint.

The owner approves in a browser with the local token file's value. API keys are
never used here. OAuth tokens/codes are stored only as hashes, with an audience.
"""
import base64
import hashlib
import hmac
import html
import ipaddress
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

from aiohttp import web

from .mcp_connections import public_origin


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def redirect_origin(uri):
    """A CSP source for the registered redirect; never interpolate raw metadata."""
    parsed = urlsplit(uri)
    host = (parsed.hostname or '').encode('idna').decode('ascii')
    if ':' in host:
        host = '[' + str(ipaddress.IPv6Address(host)) + ']'
    elif not re.fullmatch(r'[A-Za-z0-9.-]+', host):
        raise ValueError('Invalid redirect host')
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
        raise ValueError('Invalid redirect origin')
    port = ':' + str(parsed.port) if parsed.port is not None else ''
    return parsed.scheme + '://' + host + port


class OAuth:
    def __init__(self, state, base_url, owner_token):
        self.state, self.owner_token = state, owner_token
        self.data = state.data.setdefault('oauth', {'clients': {}, 'codes': {}, 'tokens': {}})
        self.forms = {}
        self.set_origin(base_url)

    def set_origin(self, value):
        self.base = public_origin(value) if value else None
        self.resource = self.base + '/mcp' if self.base else None
        self.forms.clear()

    def accepted(self, token):
        grant = self.data['tokens'].get(digest(token))
        return bool(self.base and grant and grant['kind'] == 'access' and grant['expires'] > time.time()
                    and grant['resource'] == self.resource)

    def routes(self, app):
        app.router.add_get('/.well-known/oauth-protected-resource', self.protected)
        app.router.add_get('/.well-known/oauth-protected-resource/mcp', self.protected)
        app.router.add_get('/.well-known/oauth-authorization-server', self.metadata)
        app.router.add_post('/oauth/register', self.register)
        app.router.add_get('/oauth/authorize', self.authorize)
        app.router.add_post('/oauth/authorize', self.approve)
        app.router.add_post('/oauth/token', self.token)

    async def protected(self, request):
        return web.json_response({'resource': self.resource, 'authorization_servers': [self.base],
                                  'scopes_supported': ['voice-agent'], 'bearer_methods_supported': ['header']})

    async def metadata(self, request):
        return web.json_response({'issuer': self.base, 'authorization_endpoint': self.base + '/oauth/authorize',
            'token_endpoint': self.base + '/oauth/token', 'registration_endpoint': self.base + '/oauth/register',
            'response_types_supported': ['code'], 'grant_types_supported': ['authorization_code', 'refresh_token'],
            'token_endpoint_auth_methods_supported': ['none'], 'code_challenge_methods_supported': ['S256'],
            'scopes_supported': ['voice-agent']})

    async def register(self, request):
        try:
            data = await request.json()
            uris = data['redirect_uris']
            if not isinstance(uris, list) or not 1 <= len(uris) <= 10:
                raise ValueError()
            for uri in uris:
                p = urlsplit(uri)
                if (not p.hostname or p.fragment or p.username or len(uri) > 2048 or
                    not (p.scheme == 'https' or (p.scheme == 'http' and p.hostname in ('localhost', '127.0.0.1', '::1')))):
                    raise ValueError()
                redirect_origin(uri)
            if data.get('token_endpoint_auth_method', 'none') != 'none':
                raise ValueError()
            if len(self.data['clients']) >= 100:
                return web.json_response({'error': 'temporarily_unavailable'}, status=429)
            ident = secrets.token_urlsafe(24)
            client = {'client_id': ident, 'client_name': str(data.get('client_name', 'MCP client'))[:200],
                      'redirect_uris': uris, 'token_endpoint_auth_method': 'none',
                      'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code']}
            self.data['clients'][ident] = client
            self.state.save()
            return web.json_response(client, status=201, headers={'Cache-Control': 'no-store'})
        except (ValueError, KeyError, TypeError, AttributeError):
            return web.json_response({'error': 'invalid_client_metadata'}, status=400)

    async def authorize(self, request):
        p = dict(request.query)
        client = self.data['clients'].get(p.get('client_id'))
        if (not client or p.get('redirect_uri') not in client['redirect_uris'] or
            p.get('response_type') != 'code' or p.get('resource') != self.resource or
            p.get('code_challenge_method') != 'S256' or
            not re.fullmatch(r'[A-Za-z0-9_-]{43}', p.get('code_challenge', '')) or
            p.get('scope', 'voice-agent') != 'voice-agent'):
            return web.Response(status=400, text='Invalid authorization request')
        self.forms = {k: v for k, v in self.forms.items() if v['expires'] > time.time()}
        if len(self.forms) >= 100:
            return web.Response(status=429)
        nonce = secrets.token_urlsafe(32)
        self.forms[nonce] = {'params': p, 'expires': time.time() + 300}
        markup = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>HFPilot</title>'
            '<body><h1>Connect HFPilot</h1><p>Application: <b>' + html.escape(client['client_name']) +
            '</b></p><p>Return address: ' + html.escape(p['redirect_uri']) + '</p>'
            '<p>This allows the application to place calls, read their results and edit incoming-call instructions.</p>'
            '<p>In HFPilot, open Settings → Assistant connections → Copy Access Code, then paste that code below.</p>'
            '<form method="post" action="/oauth/authorize"><input type="hidden" name="nonce" value="' + nonce +
            '"><input type="password" name="owner_token" autocomplete="off" required>'
            '<button type="submit">Allow Connection</button></form></body></html>')
        response = web.Response(text=markup, content_type='text/html', headers={
            # Chrome checks the final 303 destination against form-action too.
            # Allow this validated client's callback origin, not arbitrary sites.
            'Cache-Control': 'no-store', 'Content-Security-Policy':
                "default-src 'none'; form-action 'self' " + redirect_origin(p['redirect_uri']) + "; frame-ancestors 'none'",
            # no-referrer makes a native form POST send Origin: null, which our
            # CSRF check correctly rejects. Keep the origin without leaking the
            # authorization URL's path/query in Referer.
            'X-Frame-Options': 'DENY', 'Referrer-Policy': 'strict-origin'})
        # Bind approval to the browser that opened the authorization page (login CSRF).
        response.set_cookie('voice_oauth', nonce, max_age=300, secure=True, httponly=True, samesite='Lax')
        return response

    async def approve(self, request):
        if request.headers.get('Origin') != self.base:
            return web.Response(status=403, text='Invalid form origin. Restart authorization from your MCP client.')
        p = await request.post()
        nonce = p.get('nonce', '')
        form = self.forms.get(nonce)
        if not form or form['expires'] <= time.time() or not hmac.compare_digest(nonce, request.cookies.get('voice_oauth', '')):
            return web.Response(status=400, text='Authorization expired; reconnect from your client')
        if not hmac.compare_digest(str(p.get('owner_token', '')).encode(), self.owner_token.encode()):
            return web.Response(status=403, text='Invalid owner token')
        self.forms.pop(nonce)
        params = form['params']
        code = secrets.token_urlsafe(32)
        self.data['codes'][digest(code)] = {**params, 'expires': time.time() + 60}
        self.state.save()
        redirect = params['redirect_uri']
        query = {'code': code}
        if 'state' in params:
            query['state'] = params['state']
        location = redirect + ('&' if '?' in redirect else '?') + urlencode(query)
        response = web.Response(status=303, headers={'Location': location, 'Cache-Control': 'no-store',
                                                    'Referrer-Policy': 'no-referrer'})
        response.del_cookie('voice_oauth')
        return response

    async def token(self, request):
        p = await request.post()
        client = self.data['clients'].get(p.get('client_id'))
        if not client or p.get('resource') != self.resource:
            return web.json_response({'error': 'invalid_client'}, status=400)
        if p.get('grant_type') == 'authorization_code':
            grant = self.data['codes'].get(digest(str(p.get('code', ''))))
            verifier = str(p.get('code_verifier', ''))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            valid = bool(grant and re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier) and
                         grant['redirect_uri'] == p.get('redirect_uri') and
                         hmac.compare_digest(challenge, grant['code_challenge']))
        elif p.get('grant_type') == 'refresh_token':
            grant = self.data['tokens'].get(digest(str(p.get('refresh_token', ''))))
            valid = bool(grant and grant['kind'] == 'refresh')
        else:
            return web.json_response({'error': 'unsupported_grant_type'}, status=400)
        if not valid or grant['expires'] <= time.time() or grant['client_id'] != p['client_id'] or grant['resource'] != self.resource:
            return web.json_response({'error': 'invalid_grant'}, status=400)
        if p['grant_type'] == 'authorization_code':
            self.data['codes'].pop(digest(p['code']))
        else:
            self.data['tokens'].pop(digest(p['refresh_token']))
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        for token, kind, lifetime in ((access, 'access', 3600), (refresh, 'refresh', 2592000)):
            self.data['tokens'][digest(token)] = {'kind': kind, 'expires': time.time() + lifetime,
                'client_id': p['client_id'], 'resource': self.resource}
        self.state.save()
        return web.json_response({'access_token': access, 'refresh_token': refresh, 'token_type': 'Bearer',
                                  'expires_in': 3600, 'scope': 'voice-agent'},
                                 headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})
