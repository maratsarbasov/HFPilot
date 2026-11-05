from pathlib import Path
from PyInstaller.utils.hooks import collect_all, copy_metadata

root = Path(SPECPATH).parent
datas = copy_metadata('aiortc') + copy_metadata('aiohttp')
binaries, hiddenimports = [], []
for package in ('av', 'pylibsrtp'):
    data, binary, imports = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += imports
a = Analysis([str(root / 'macos/backend_main.py')], pathex=[str(root)],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='HFPilotBackend',
          console=True, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name='HFPilotBackend')
# BUNDLE places code/data in Apple's standard locations and fixes dylib paths.
# The build script adds the native Swift main executable to this same bundle.
app = BUNDLE(coll, name='HFPilot.app', bundle_identifier='local.voice-agent.menubar')
