"""Entry point of the window app in a standalone build."""

import os
import sys

# A windowed app has no console, so sys.stdout and sys.stderr are None, and a
# library that writes to them fails ("'NoneType' object has no attribute
# 'write'"). print() hides this by doing nothing. Send them nowhere instead,
# before anything is imported that keeps a reference to them.
for _name in ('stdout', 'stderr'):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, 'w', encoding='utf-8'))

from takeout_organizer.cli import main  # noqa: E402

raise SystemExit(main(["gui"]))
