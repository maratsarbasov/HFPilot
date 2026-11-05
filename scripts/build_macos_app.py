"""Build an autonomous Mac app with Python/media dependencies and HFP helpers.

Build prerequisites: project .venv, requirements-build.txt, Xcode tools, CMake,
pinned BTstack sources and blueutil. End users do not need these build tools.
OpenAI tunnel-client is deliberately installed separately by the user.
"""
import argparse
import importlib.metadata
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / '.work/macos-app'
APP = BUILD / 'HFPilot.app'


def build_icon(resources):
    iconset = BUILD / 'AppIcon.iconset'
    iconset.mkdir(parents=True, exist_ok=True)
    source = ROOT / 'macos/Resources/AppIcon.png'
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = size * scale
            suffix = '@2x' if scale == 2 else ''
            target = iconset / f'icon_{size}x{size}{suffix}.png'
            subprocess.run(['sips', '-z', str(pixels), str(pixels), str(source),
                            '--out', str(target)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['iconutil', '-c', 'icns', str(iconset),
                    '-o', str(resources / 'AppIcon.icns')], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui-only', action='store_true', help='Reuse the already frozen backend for UI iteration')
    parser.add_argument('--zip', action='store_true', help='Create a portable ZIP in dist/')
    opts = parser.parse_args()
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('This release supports building on Apple silicon Macs only.')
    dependency = ROOT / '.work/deps/btstack'
    if not opts.ui_only:
        from prepare_native import verify
        verify(dependency)
    contents = APP / 'Contents'
    binary = contents / 'MacOS/HFPilot'
    binary.parent.mkdir(parents=True, exist_ok=True)
    resources = contents / 'Resources'; resources.mkdir(exist_ok=True)
    if not opts.ui_only:
        freezer_env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(BUILD / 'freeze-cache'))
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
            '--distpath', str(BUILD / 'frozen'), '--workpath', str(BUILD / 'freeze-work'),
            str(ROOT / 'macos/HFPilotBackend.spec')], cwd=ROOT, env=freezer_env, check=True)
        if APP.exists():
            shutil.rmtree(APP)
        shutil.copytree(BUILD / 'frozen/HFPilot.app', APP, symlinks=True)
        helper_build = BUILD / 'skywalk-build'
        subprocess.run(['cmake', '-S', str(ROOT / 'macos/Skywalk'),
            '-B', str(helper_build), '-DCMAKE_BUILD_TYPE=Release'], check=True)
        subprocess.run(['cmake', '--build', str(helper_build), '--target', 'skywalk-hfp', '-j', '4'], check=True)
        helpers = contents / 'Helpers'; helpers.mkdir(exist_ok=True)
        blueutil = shutil.which('blueutil')
        if not blueutil:
            raise SystemExit('Build dependency missing: blueutil')
        shutil.copy2(helper_build / 'skywalk-hfp', helpers / 'skywalk-hfp')
        shutil.copy2(blueutil, helpers / 'blueutil')
        notices = resources / 'Licenses'; notices.mkdir(exist_ok=True)
        shutil.copy2(Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt', notices / 'Python.txt')
        shutil.copy2(ROOT / '.work/deps/btstack/LICENSE', notices / 'BTstack.txt')
        shutil.copy2(Path(blueutil).resolve().parents[1] / 'LICENSE.txt', notices / 'blueutil.txt')
        for dist in importlib.metadata.distributions():
            for item in dist.files or []:
                if any(s in item.name.lower() for s in ('license', 'copying', 'notice')) and '.dist-info/' in str(item):
                    source = Path(dist.locate_file(item))
                    if source.is_file():
                        target = notices / (dist.metadata['Name'] + '-' + dist.version) / str(item).split('.dist-info/', 1)[-1]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
    assert (contents / 'MacOS/HFPilotBackend').is_file(), 'Build the backend before --ui-only'
    shutil.copytree(ROOT / 'macos/Resources', resources, dirs_exist_ok=True)
    build_icon(resources)
    defaults = resources / 'Defaults'
    (defaults / 'prompts').mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'agent.toml', defaults / 'agent.toml')
    shutil.copy2(ROOT / 'prompts/incoming.txt', defaults / 'prompts/incoming.txt')
    os.chmod(resources / 'OpenAI Tunnel.command', 0o755)
    cache = BUILD / 'module-cache'; cache.mkdir(exist_ok=True)
    subprocess.run(['xcrun', 'swiftc', '-O', '-swift-version', '5', '-module-cache-path', str(cache),
                    '-target', platform.machine() + '-apple-macos13.0',
                    '-framework', 'Cocoa', '-framework', 'CoreBluetooth', '-framework', 'AVFoundation',
                    *[str(p) for p in sorted((ROOT / 'macos/VoiceAgent').glob('*.swift'))],
                    '-o', str(binary)], check=True)
    info = {
        'CFBundleName': 'HFPilot', 'CFBundleDisplayName': 'HFPilot',
        'CFBundleIdentifier': 'local.voice-agent.menubar', 'CFBundleExecutable': 'HFPilot',
        'CFBundleIconFile': 'AppIcon',
        'CFBundlePackageType': 'APPL', 'CFBundleVersion': '2', 'CFBundleShortVersionString': '0.1.0',
        'LSUIElement': True, 'LSMultipleInstancesProhibited': True,
        'NSPrincipalClass': 'VoiceAgentApplication',
        # Do not advertise an untested lower OS target for the frozen dependencies.
        'LSMinimumSystemVersion': platform.mac_ver()[0], 'NSHighResolutionCapable': True,
        'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
        'NSBluetoothAlwaysUsageDescription': 'HFPilot connects to your phone to answer calls and carry audio for your assistant.',
    }
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    os.chmod(binary, 0o755)
    for helper in (contents / 'Helpers').iterdir():
        subprocess.run(['codesign', '--force', '--sign', '-', str(helper)], check=True)
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(APP)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(APP)], check=True)
    if opts.zip:
        output = ROOT / 'dist'; output.mkdir(exist_ok=True)
        archive = output / ('HFPilot-macOS-' + platform.machine() + '.zip')
        archive.unlink(missing_ok=True)
        subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(APP), str(archive)], check=True)
        print(archive)
    print(APP)


if __name__ == '__main__':
    main()
