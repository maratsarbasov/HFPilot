"""ChatGPT MCP Events: verified, signed HTTPS webhooks with a durable outbox."""
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
from urllib.parse import urlsplit

import aiohttp

from .mcp_state import canonical


class CallbackError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('Callback verification failed: ' + reason)


def signing_key(secret):
    try:
        if not isinstance(secret, str) or not secret.startswith('whsec_'):
            raise ValueError()
        key = base64.b64decode(secret[6:], validate=True)
        if not 24 <= len(key) <= 64:
            raise ValueError()
        return key
    except (ValueError, TypeError):
        raise ValueError('Signing secret must be whsec_ plus base64 of 24–64 bytes') from None


def public_address(host):
    address = ipaddress.ip_address(host)
    if not address.is_global or address.is_multicast or address.is_unspecified:
        raise CallbackError('non_public_address')
    if isinstance(address, ipaddress.IPv6Address):
        if any(address in ipaddress.ip_network(network) for network in ('64:ff9b::/96', '64:ff9b:1::/48')):
            raise CallbackError('translation_address')
        if address.ipv4_mapped:
            public_address(str(address.ipv4_mapped))
        if address.sixtofour or address.teredo:
            raise CallbackError('transition_address')


def validate_url(url):
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or
                parts.password or parts.fragment or parts.port not in (None, 443)):
            raise ValueError()
        try:
            ipaddress.ip_address(parts.hostname)
        except ValueError:
            if parts.hostname.lower() == 'localhost' or parts.hostname.endswith('.local'):
                raise CallbackError('non_public_address')
        else:
            public_address(parts.hostname)
    except (ValueError, TypeError):
        raise CallbackError('invalid_https_url') from None


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, family=family)
        results = []
        for af, _, proto, _, address in infos:
            public_address(address[0])
            results.append(dict(hostname=host, host=address[0], port=port,
                                family=af, proto=proto, flags=socket.AI_NUMERICHOST))
        return results

    async def close(self):
        pass


async def secure_post(url, body, headers):
    validate_url(url)
    # Fresh connection: validate each resolved destination, preserve hostname for TLS.
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(connector=connector,
            timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as session:
        async with session.post(url, data=body, headers=headers, allow_redirects=False) as response:
            raw = await response.content.read(16385)
            if len(raw) > 16384:
                raise CallbackError('response_too_large')
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                data = None
            return response.status, data


def signed_headers(sub, event_id, body, now=None):
    now = int(time.time() if now is None else now)
    message = event_id.encode() + b'.' + str(now).encode() + b'.' + body
    keys = [sub['secret']]
    if sub.get('old_secret') and sub.get('rotation_until', 0) > now:
        keys.append(sub['old_secret'])
    signatures = ['v1,' + base64.b64encode(hmac.digest(signing_key(key), message, 'sha256')).decode()
                  for key in keys]
    return {'Content-Type': 'application/json', 'webhook-id': event_id,
            'webhook-timestamp': str(now), 'webhook-signature': ' '.join(signatures),
            'X-MCP-Subscription-Id': sub['id']}


EVENTS = [
    {'name': 'call.completed', 'description': 'An incoming or outgoing call attempt has finished and report '
        'generation has completed or failed. Includes the outcome and available summary. Use this to notify '
        'the user of results, including unanswered or failed calls. Use get_call if more detail is needed.',
     'delivery': ['webhook'],
     'inputSchema': {'type': 'object', 'properties': {
         'call_id': {'type': 'string', 'description': 'Optional: receive completion only for this known call.'},
         'direction': {'type': 'string', 'enum': ['incoming', 'outgoing'],
                       'description': 'Optional: restrict notifications to incoming or outgoing calls.'}},
         'additionalProperties': False},
     'payloadSchema': {'type': 'object', 'properties': {
         'call_id': {'type': 'string'}, 'direction': {'type': 'string'},
         'number': {'type': ['string', 'null']}, 'context_revision': {'type': ['string', 'null']},
         'result': {'type': 'object'}},
         'required': ['call_id', 'direction', 'number', 'context_revision', 'result'],
         'additionalProperties': False}},
    {'name': 'call.started', 'description': 'HFPilot has begun preparing an incoming or outgoing conversation. '
        'Use for an early activity notification. It does not prove the call connected or that anyone answered; '
        'wait for call.completed or read get_call for the outcome.', 'delivery': ['webhook'],
     'inputSchema': {'type': 'object', 'properties': {
         'direction': {'type': 'string', 'enum': ['incoming', 'outgoing'],
                       'description': 'Optional: restrict notifications to incoming or outgoing calls.'}}, 'additionalProperties': False},
     'payloadSchema': {'type': 'object', 'properties': {
         'call_id': {'type': 'string'}, 'direction': {'type': 'string'},
         'number': {'type': ['string', 'null']}},
         'required': ['call_id', 'direction', 'number'], 'additionalProperties': False}},
]


class Events:
    def __init__(self, state, post=secure_post):
        self.state, self.post = state, post
        self.verified = {}

    def identity(self, params):
        name, args, delivery = params.get('name'), params.get('arguments', {}), params.get('delivery', {})
        definition = next((e for e in EVENTS if e['name'] == name), None)
        if not definition or not isinstance(args, dict) or set(args) - set(definition['inputSchema']['properties']):
            raise ValueError('Unknown event or filter')
        if any(not isinstance(v, str) for v in args.values()):
            raise ValueError('Event filters must be strings')
        if 'direction' in args and args['direction'] not in ('incoming', 'outgoing'):
            raise ValueError('Invalid direction filter')
        if not isinstance(delivery, dict) or delivery.get('mode') != 'webhook':
            raise ValueError('Only webhook delivery is supported')
        url = delivery.get('url')
        validate_url(url)
        # This endpoint has one authenticated owner, not a multi-tenant account system.
        ident = 'sub_' + hashlib.sha256(canonical(['owner', name, args, url]).encode()).hexdigest()
        return ident, name, args, delivery

    async def subscribe(self, params):
        ident, name, args, delivery = self.identity(params)
        secret = delivery.get('secret')
        signing_key(secret)
        ttl = params.get('ttlMs', 86400000)
        if ttl is None:
            ttl = 86400000  # Finite grant even when the client requests no expiry.
        if type(ttl) is not int or ttl <= 0:
            raise ValueError('ttlMs must be a positive integer or null')
        expires = time.time() + min(ttl / 1000, 86400)
        sub = {'id': ident, 'name': name, 'arguments': args, 'url': delivery['url'],
               'secret': secret, 'expires_at': expires}
        cache_key = hashlib.sha256(canonical([sub['url'], secret]).encode()).hexdigest()
        if self.verified.get(cache_key, 0) < time.time():
            challenge = secrets.token_urlsafe(32)
            body = canonical({'type': 'verification', 'challenge': challenge}).encode()
            eid = 'msg_verification_' + secrets.token_hex(16)
            try:
                status, data = await self.post(sub['url'], body, signed_headers(sub, eid, body))
            except TimeoutError:
                raise CallbackError('timeout') from None
            except (aiohttp.ClientError, OSError):
                raise CallbackError('connection_failed') from None
            if not 200 <= status < 300 or not isinstance(data, dict) or not isinstance(data.get('challenge'), str) or not hmac.compare_digest(data['challenge'].encode(), challenge.encode()):
                raise CallbackError('challenge_failed')
            self.verified[cache_key] = time.time() + 300
        old = self.state.data['subscriptions'].get(ident)
        if old and old['secret'] != secret:
            sub.update(old_secret=old['secret'], rotation_until=time.time() + 300)
        self.state.data['subscriptions'][ident] = sub
        self.state.save()
        return {'id': ident, 'refreshBefore': datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                'cursor': None, 'truncated': False}

    def unsubscribe(self, params):
        ident, _, _, _ = self.identity(params)
        self.state.data['subscriptions'].pop(ident, None)
        for delivery in self.state.data['outbox'].values():
            if delivery['subscription_id'] == ident and delivery['status'] == 'pending':
                delivery['status'] = 'unsubscribed'
        self.state.save()
        return {}

    async def deliver(self):
        for delivery in list(self.state.data['outbox'].values()):
            if delivery['status'] != 'pending' or delivery['next_at'] > time.time():
                continue
            sub = self.state.data['subscriptions'].get(delivery['subscription_id'])
            if not sub or sub['expires_at'] <= time.time():
                delivery['status'] = 'expired'
                self.state.save()
                continue
            event = self.state.data['events'][delivery['event_id']]
            body = canonical(event).encode()
            if len(body) > 262144:
                delivery['status'] = 'payload_too_large'
                self.state.save()
                continue
            status = 0
            try:
                status, _ = await self.post(sub['url'], body, signed_headers(sub, event['eventId'], body))
            except (aiohttp.ClientError, OSError, ValueError):
                pass  # Do not persist exception URLs or subscription secrets.
            delivery['attempts'] += 1
            delivery['http_status'] = status
            if 200 <= status < 300:
                delivery['status'] = 'delivered'
            elif status in (410, 413) or delivery['attempts'] >= 8 or (400 <= status < 500 and status not in (408, 429)):
                delivery['status'] = 'failed'
                if status == 410:
                    self.state.data['subscriptions'].pop(sub['id'], None)
            else:
                delivery['next_at'] = time.time() + min(300, 2 ** delivery['attempts'])
            self.state.save()

    async def run(self):
        while True:
            await self.deliver()
            await asyncio.sleep(1)
