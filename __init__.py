import sys

# Register the action only inside KiCad (pcbnew is already loaded there);
# `python3 -m rendr` (command line) must not.
if "pcbnew" in sys.modules:
    from .plugin import register

    register()
