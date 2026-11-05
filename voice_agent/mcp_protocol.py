"""MCP revision handling shared by discovery and the HTTP endpoint."""
import base64
import binascii

MODERN = '2026-07-28'
LEGACY = ('2025-03-26', '2025-06-18', '2025-11-25')
SUPPORTED = (MODERN, *reversed(LEGACY))
PREFIX = 'io.modelcontextprotocol/'
SERVER_INFO = {'name': 'HFPilot', 'version': '0.2.2'}


def failure(message, code, text, data=None):
    ident = message.get('id') if isinstance(message, dict) else None
    error = {'code': code, 'message': text}
    if data is not None:
        error['data'] = data
    return {'jsonrpc': '2.0', 'id': ident if type(ident) in (int, str) else None, 'error': error}


def modern_request(message, headers):
    """Return (modern, error), validating metadata before any tool can execute."""
    params = message.get('params', {}) if isinstance(message, dict) else {}
    meta = params.get('_meta', {}) if isinstance(params, dict) else {}
    version = meta.get(PREFIX + 'protocolVersion') if isinstance(meta, dict) else None
    header = headers.get('MCP-Protocol-Version')
    modern = (version is not None or (header is not None and header not in LEGACY)
              or isinstance(message, dict) and message.get('method') == 'server/discover')
    if not modern:
        return False, None
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
        return True, failure(message, -32600, 'Invalid Request')
    if (not isinstance(version, str) or not isinstance(meta.get(PREFIX + 'clientCapabilities'), dict)):
        return True, failure(message, -32602, 'Protocol version and client capabilities are required in params._meta')
    if header != version or headers.get('Mcp-Method') != message['method']:
        return True, failure(message, -32020, 'Missing or mismatched MCP-Protocol-Version or Mcp-Method header')
    if version != MODERN:
        return True, failure(message, -32022, 'Unsupported protocol version',
                             {'supported': list(SUPPORTED), 'requested': version})
    if message['method'] in ('tools/call', 'prompts/get', 'resources/read'):
        field = 'uri' if message['method'] == 'resources/read' else 'name'
        name = headers.get('Mcp-Name')
        try:
            if name and name.startswith('=?base64?') and name.endswith('?='):
                name = base64.b64decode(name[9:-2], validate=True).decode('utf-8')
        except (binascii.Error, UnicodeDecodeError):
            name = None
        if not isinstance(name, str) or name != params.get(field):
            return True, failure(message, -32020, 'Missing or mismatched Mcp-Name header')
    return True, None


def complete(result, method):
    result = {**result, 'resultType': 'complete',
              '_meta': {**result.get('_meta', {}), PREFIX + 'serverInfo': SERVER_INFO}}
    if method in ('server/discover', 'tools/list'):
        result.update(ttlMs=0, cacheScope='private')
    return result
