# PyInstaller build of Takeout Organizer: one folder holding the window app
# ("Takeout Organizer") and the command-line tool ("takeout-organizer"), with
# ExifTool and every license notice inside. On macOS the folder is wrapped in
# "Takeout Organizer.app".
#
# From the repository root, after packaging/collect_licenses.py:
#     EXIFTOOL_DIR=<folder with exiftool> pyinstaller packaging/takeout-organizer.spec
# The release workflow (.github/workflows/release-builds.yml) does this.

import os
import re
import sys

from PyInstaller.utils.hooks import collect_all, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
with open(os.path.join(ROOT, 'src', 'takeout_organizer', '__init__.py'), encoding='utf-8') as f:
    VERSION = re.search(r'__version__ = "([^"]+)"', f.read()).group(1)

datas, binaries, hiddenimports = [], [], []
for package in ('sv_ttk', 'tzfpy', 'tzdata', 'pi_heif'):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

# Version metadata, so `diagnose` can name the versions inside the build.
datas += copy_metadata('pillow')
datas += [
    (os.environ['EXIFTOOL_DIR'], 'exiftool'),
    # Place names for --place-names (GeoNames, CC BY 4.0).
    (os.path.join(ROOT, 'src', 'takeout_organizer', 'data'), 'takeout_organizer/data'),
    # The window's translations.
    (os.path.join(ROOT, 'src', 'takeout_organizer', 'locale'), 'takeout_organizer/locale'),
    (os.path.join(ROOT, 'build', 'licenses'), 'licenses'),
    (os.path.join(ROOT, 'LICENSE'), '.'),
    (os.path.join(ROOT, 'THIRD_PARTY_NOTICES.md'), '.'),
]


def analysis(script):
    return Analysis(
        [os.path.join(SPECPATH, script)],
        pathex=[os.path.join(ROOT, 'src')],
        datas=datas,
        binaries=binaries,
        hiddenimports=hiddenimports + ['takeout_organizer.gui'],
        # pillow-heif carries the GPL x265 encoder; the build decodes with
        # pi-heif only.
        excludes=['pillow_heif', 'pytest'],
    )


gui = analysis('gui_main.py')
cli = analysis('cli_main.py')
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True,
              name='Takeout Organizer', console=False)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True,
              name='takeout-organizer', console=True)
# The window app comes first: it is the one the macOS .app starts.
coll = COLLECT(gui_exe, gui.binaries, gui.datas, cli_exe, cli.binaries, cli.datas,
               name='Takeout Organizer')

if sys.platform == 'darwin':
    app = BUNDLE(coll, name='Takeout Organizer.app',
                 bundle_identifier='io.github.regmibishal1.takeout-organizer',
                 info_plist={'CFBundleShortVersionString': VERSION,
                             'NSHighResolutionCapable': True})
