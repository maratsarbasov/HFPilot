"""Regenerate bundled English GPT-Live samples (explicit, billable developer tool)."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_agent import live
from voice_agent.voice_preview import generate

ROOT = Path(__file__).resolve().parents[1]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--voice', nargs='+', required=True)
    parser.add_argument('--data-root', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'macos/Resources/Voices/en')
    parser.add_argument('--jobs', type=int, choices=range(1, 5), default=3)
    args = parser.parse_args()
    if args.data_root:
        live.ROOT = args.data_root.expanduser()
    slots = asyncio.Semaphore(args.jobs)

    async def one(voice):
        async with slots:
            path = args.output / f'{voice}.m4a'
            if path.exists():
                return
            try:
                result = await generate(voice, 'English')
                data = base64.b64decode(result.pop('audio'))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                print(json.dumps(result, ensure_ascii=False), flush=True)
            except Exception as error:
                print(json.dumps({'voice': voice, 'error': str(error)}), flush=True)
                raise

    results = await asyncio.gather(*(one(voice) for voice in args.voice), return_exceptions=True)
    if any(isinstance(result, BaseException) for result in results):
        raise SystemExit('Some samples failed; see the error records above.')


if __name__ == '__main__':
    asyncio.run(main())
