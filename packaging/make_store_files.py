"""Write the winget manifests and the Homebrew cask for a published release.

Usage (from the repository root, after the release builds are attached):
    python packaging/make_store_files.py 0.2.0

It reads the checksums the release workflow attached to the release, and writes

    packaging/winget/manifests/r/regmibishal1/TakeoutOrganizer/<version>/  (3 files)
    packaging/homebrew/Casks/takeout-organizer.rb

Check the winget files with `winget validate --manifest <that folder>` and
send them as a pull request to microsoft/winget-pkgs (same path). The cask goes
in the tap repository regmibishal1/homebrew-tap, as Casks/takeout-organizer.rb.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
REPO = 'https://github.com/regmibishal1/takeout-organizer'
PACKAGE_ID = 'regmibishal1.TakeoutOrganizer'
SCHEMA = '1.10.0'
SHORT = ("Fix the dates and locations in your Google Photos Takeout. Free, offline, and your "
         "original files are never changed.")
LONG = ("Takeout Organizer puts the dates, places and captions from a Google Photos Takeout "
        "back into the photos themselves with ExifTool (included), and files them in folders "
        "by date. It previews every change first, accounts for every file, finds duplicates, "
        "and can undo a run.")


def asset_url(version: str, name: str) -> str:
    return f"{REPO}/releases/download/v{version}/{name}"


def release_date(version: str) -> str:
    """The day the release was published, from GitHub."""
    api = REPO.replace('https://github.com/', 'https://api.github.com/repos/')
    with urllib.request.urlopen(f"{api}/releases/tags/v{version}", timeout=30) as reply:
        return json.load(reply)['published_at'][:10]


def checksum(version: str, archive: str) -> str:
    """The SHA-256 from the .sha256 file the release workflow attached."""
    with urllib.request.urlopen(asset_url(version, archive + '.sha256'), timeout=30) as reply:
        text = reply.read().decode('ascii')
    match = re.match(r'\s*([0-9a-fA-F]{64})\b', text)
    if not match:
        raise SystemExit(f"No checksum in {archive}.sha256: {text!r}")
    return match.group(1)


def winget(version: str, sha256: str, released: str) -> dict:
    """The three manifest files, by name."""
    header = f"# yaml-language-server: $schema=https://aka.ms/winget-manifest.{{}}.{SCHEMA}.schema.json\n"
    common = f"PackageIdentifier: {PACKAGE_ID}\nPackageVersion: {version}\n"
    archive = f"Takeout-Organizer-{version}-windows-x64.zip"
    return {
        f"{PACKAGE_ID}.yaml": header.format('version') + common + (
            "DefaultLocale: en-US\n"
            f"ManifestType: version\nManifestVersion: {SCHEMA}\n"),
        f"{PACKAGE_ID}.installer.yaml": header.format('installer') + common + (
            "InstallerType: zip\n"
            "NestedInstallerType: portable\n"
            "NestedInstallerFiles:\n"
            "- RelativeFilePath: Takeout Organizer\\takeout-organizer.exe\n"
            "  PortableCommandAlias: takeout-organizer\n"
            "- RelativeFilePath: Takeout Organizer\\Takeout Organizer.exe\n"
            "  PortableCommandAlias: takeout-organizer-window\n"
            f"ReleaseDate: {released}\n"
            "Installers:\n"
            "- Architecture: x64\n"
            f"  InstallerUrl: {asset_url(version, archive)}\n"
            f"  InstallerSha256: {sha256.upper()}\n"
            f"ManifestType: installer\nManifestVersion: {SCHEMA}\n"),
        f"{PACKAGE_ID}.locale.en-US.yaml": header.format('defaultLocale') + common + (
            "PackageLocale: en-US\n"
            "Publisher: Bishal Regmi\n"
            "PublisherUrl: https://github.com/regmibishal1\n"
            f"PublisherSupportUrl: {REPO}/issues\n"
            "PackageName: Takeout Organizer\n"
            f"PackageUrl: {REPO}\n"
            "License: MIT\n"
            f"LicenseUrl: {REPO}/blob/main/LICENSE\n"
            f"ShortDescription: {SHORT}\n"
            f"Description: {LONG}\n"
            "Moniker: takeout-organizer\n"
            "Tags:\n- google-photos\n- takeout\n- exif\n- exiftool\n- metadata\n"
            "- photo-organizer\n"
            f"ReleaseNotesUrl: {REPO}/releases/tag/v{version}\n"
            f"ManifestType: defaultLocale\nManifestVersion: {SCHEMA}\n"),
    }


def cask(version: str, sha256: str) -> str:
    return f'''cask "takeout-organizer" do
  version "{version}"
  sha256 "{sha256.lower()}"

  url "{REPO}/releases/download/v#{{version}}/Takeout-Organizer-#{{version}}-macos-arm64.zip"
  name "Takeout Organizer"
  desc "Fix the dates and locations in a Google Photos Takeout"
  homepage "{REPO}"

  depends_on arch: :arm64
  depends_on :macos

  app "Takeout Organizer.app"
  binary "#{{appdir}}/Takeout Organizer.app/Contents/MacOS/takeout-organizer"

  zap trash: "~/Library/Application Support/takeout-organizer"

  caveats <<~EOS
    Takeout Organizer is not notarized. The first time, right-click it in
    Applications and choose Open, or install with --no-quarantine.
  EOS
end
'''


def main(version: str) -> None:
    released = release_date(version)
    windows = checksum(version, f"Takeout-Organizer-{version}-windows-x64.zip")
    mac = checksum(version, f"Takeout-Organizer-{version}-macos-arm64.zip")
    folder = os.path.join(ROOT, 'packaging', 'winget', 'manifests', 'r', 'regmibishal1',
                          'TakeoutOrganizer', version)
    os.makedirs(folder, exist_ok=True)
    for name, text in winget(version, windows, released).items():
        with open(os.path.join(folder, name), 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
    casks = os.path.join(ROOT, 'packaging', 'homebrew', 'Casks')
    os.makedirs(casks, exist_ok=True)
    with open(os.path.join(casks, 'takeout-organizer.rb'), 'w', encoding='utf-8',
              newline='\n') as f:
        f.write(cask(version, mac))
    print(f"winget: {folder}\nHomebrew: {os.path.join(casks, 'takeout-organizer.rb')}")


if __name__ == '__main__':
    if len(sys.argv) != 2 or not re.fullmatch(r'\d+\.\d+\.\d+', sys.argv[1]):
        raise SystemExit("Usage: python packaging/make_store_files.py VERSION (for example 0.2.0)")
    main(sys.argv[1])
