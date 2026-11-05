"""Bundled, self-contained backend entry point. No system Python is required."""
import json
import multiprocessing
import sys

if getattr(sys, 'frozen', False):
    # An app copied to another Mac cannot rely on Homebrew's OpenSSL CA files.
    # Inject before aiohttp/urllib imports, as recommended for applications.
    import truststore
    truststore.inject_into_ssl()

from voice_agent.paths import bootstrap


def main():
    multiprocessing.freeze_support()
    bootstrap()
    mode = sys.argv.pop(1) if len(sys.argv) > 1 else 'help'
    if mode == 'bench':
        from voice_agent.app_control import main as run
    elif mode == 'server':
        from voice_agent.mcp_server import main as run
    elif mode == 'tunnel':
        from voice_agent.tunnel_client import main as run
    elif mode == 'tunnel-managed':
        from voice_agent.tunnel import main as run
    elif mode == 'check':
        # Import and exercise the media stack without Bluetooth, an API key or a call.
        import aiohttp, aiortc, av, numpy, pylibsrtp, cryptography
        from voice_agent.config import load_config
        from voice_agent.media import gain_pcm
        from voice_agent.paths import ROOT, native_helper
        from pathlib import Path
        assert gain_pcm(bytes(320), 1) == bytes(320)
        load_config()
        helpers = {name: Path(native_helper(name)).is_file() for name in ('skywalk-hfp', 'blueutil')}
        assert all(helpers.values())
        import ssl
        result = {'packaged': bool(getattr(sys, 'frozen', False)), 'media_imports': True,
                  'data_directory': str(ROOT), 'helpers': helpers,
                  'native_tls': ssl.SSLContext.__module__.startswith('truststore.')}
        if '--https-check' in sys.argv:
            # No key or account data: an unauthenticated request must return 401.
            import asyncio
            async def https_check():
                async with aiohttp.ClientSession(trust_env=False,
                        timeout=aiohttp.ClientTimeout(total=15)) as session:
                    async with session.get('https://api.openai.com/v1/models') as response:
                        assert response.status == 401, response.status
                return True
            result['unauthenticated_https'] = asyncio.run(https_check())
        print(json.dumps(result))
        return 0
    else:
        print('HFPilot backend: bench, server, tunnel, check')
        return 0
    return run()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled.', file=sys.stderr)
        sys.exit(130)
