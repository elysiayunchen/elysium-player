# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（FlacMusic 桌面版）。

为什么不直接命令行打包：
conda 环境里同时存在 OpenSSL 1.1（Python 3.9 自带 libssl-1_1-x64.dll）和
OpenSSL 3（libssl-3-x64.dll）。PyInstaller 全量收集会把两套都塞进去，
运行时 _ssl.pyd 加载错版本，请求直接报 `[ASN1: NOT_ENOUGH_DATA]`。
这里在 Analysis 之后把 OpenSSL 3 的两颗 DLL 剔掉。
"""
import os

# web/ 是整个目录。datas 里的 tuple 形式只认单文件 ('a.txt', 'dest')，
# 传目录名 ('web', 'web') 会被当成一个普通文件处理 → 整目录静默丢失，
# EXE 里一个 web 资源都没有。这里手动遍历，保证目录内容全进包。
def _collect_tree(src, dest):
    out = []
    for root, _dirs, files in os.walk(src):
        for f in files:
            full = os.path.join(root, f)
            # dest 给目录，不要带文件名 —— PyInstaller 会自己补原名，
            # 带了就变成 'web/app.js/app.js' 这种双层路径，运行时找不到。
            rel_dir = os.path.relpath(root, src)
            out.append((full, os.path.join(dest, rel_dir)))
    return out


_WEB = _collect_tree(os.path.join(SPECPATH, 'web'), 'web')
assert _WEB, 'web/ 目录为空，美术资源与前端会全部丢失'

# 歌单导入规则（providers/*.json）必须随包带走，否则打包版会静默降级到内置默认规则，
# 接口追踪 / 热更新就失去意义。目录不存在时留空，不阻断打包。
_PDIR = os.path.join(SPECPATH, 'providers')
_PROVIDERS = _collect_tree(_PDIR, 'providers') if os.path.isdir(_PDIR) else []

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=_WEB + _PROVIDERS + [(os.path.join(SPECPATH, 'app.png'), '.')],
    hiddenimports=[
        'importer',
        'webview.platforms.edgechromium',
        'clr_loader',
        'pythonnet',
        'mutagen',
        'mutagen.flac',
        'mutagen.mp3',
        'mutagen.mp4',
        'mutagen.id3',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'matplotlib', 'numpy', 'pandas'],
    noarchive=False,
    optimize=0,
)

_CONFLICT = ('libssl-1_1', 'libcrypto-1_1', 'portablegit')
a.binaries = [b for b in a.binaries
              if not any(k in f"{b[0]} {b[1]}".lower() for k in _CONFLICT)]

# 关键修复：PyInstaller 会顺着 PATH 从 PortableGit\mingw64\bin 抓到 OpenSSL 3.5.7，
# 和内嵌 Python 3.9 的 _ssl.pyd（链接 OpenSSL 3.0.x）版本不匹配，
# 运行时加载 Windows 证书库直接抛 ssl.SSLError: [ASN1: NOT_ENOUGH_DATA]。
# 这里显式用 conda 自带的 OpenSSL。
import sys as _sys

for _dll in ('libssl-3-x64.dll', 'libcrypto-3-x64.dll'):
    _src = os.path.join(_sys.prefix, 'Library', 'bin', _dll)
    if os.path.isfile(_src):
        a.binaries.append((_dll, _src, 'BINARY'))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Elysium',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
)
