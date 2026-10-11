# Releasing

How a version of Takeout Organizer gets from `main` to the people who use it.

## A release

1. On a branch, set the version in `pyproject.toml` and `src/takeout_organizer/__init__.py`,
   and turn `## [Unreleased]` in `CHANGELOG.md` into `## [X.Y.Z] - YYYY-MM-DD`. Merge it to
   `main` and wait for CI.
2. Tag it (`git tag -a vX.Y.Z -m "Takeout Organizer X.Y.Z"`, then push the tag) and publish a
   GitHub release for the tag, with the changelog entry as its notes and the wheel and sdist
   attached (`uv build` or `python -m build` from a clean checkout of the tag).
3. Publishing the release starts two workflows:
   - **Release builds** builds the Windows and macOS apps with ExifTool inside, tests them on
     a small Takeout and attaches them with their `.sha256` files.
   - **Publish to PyPI** sends the wheel and sdist to PyPI, once it is switched on (below).
4. When the apps are attached, update the package manager files (below).

## Publishing to PyPI

PyPI trusts the workflow `.github/workflows/publish-pypi.yml` directly (trusted publishing),
so no API token is stored. Once:

1. Sign in at [pypi.org](https://pypi.org/) and open **Your account**, then **Publishing**.
   Add a pending publisher: project name `takeout-organizer`, owner `regmibishal1`, repository
   `takeout-organizer`, workflow `publish-pypi.yml`, environment `pypi`.
2. In the GitHub repository, **Settings**, **Environments**, **New environment**: name it
   `pypi`. Requiring your approval for it is a good idea; each publish then waits for you.
3. **Settings**, **Secrets and variables**, **Actions**, **Variables**: add `PUBLISH_TO_PYPI`
   with the value `true`.
4. To publish a release that came out before, open **Actions**, **Publish to PyPI**, **Run
   workflow**, and give its tag (for example `v0.1.0`).

The workflow checks that the version in `pyproject.toml` matches the tag before it builds,
and checks the files with `twine` before it sends them. Afterwards, change "The package is not
on PyPI yet" in the README to `pipx install takeout-organizer`.

## winget and Homebrew

```bash
python packaging/make_store_files.py X.Y.Z
```

reads the checksums the release workflow attached and writes:

- `packaging/winget/manifests/r/regmibishal1/TakeoutOrganizer/X.Y.Z/`: the winget manifests
  for the Windows app (`takeout-organizer` for the command line, `takeout-organizer-window`
  for the window). Check them with `winget validate --manifest <that folder>`, then open a
  pull request to [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs) that adds
  the folder at the same path (or use `wingetcreate submit <that folder>`). After it is
  merged, `winget install regmibishal1.TakeoutOrganizer` works.
- `packaging/homebrew/Casks/takeout-organizer.rb`: the Homebrew cask for the macOS app. CI
  checks it with `brew style` and `brew audit`. It goes in a tap: create the public repository
  `regmibishal1/homebrew-tap` once, with this file as `Casks/takeout-organizer.rb`, and update
  it for each release. Then
  `brew install --cask regmibishal1/tap/takeout-organizer` works.
