"""Entry point for `python -m deskd`.

The `deskd` console script is the normal way in, but the wake driver falls back
to `python -m deskd` when deskd is importable without having been installed with
its script on PATH (a cron environment, a checkout, a vendored copy). Both must
reach the same CLI.
"""

from .cli import main

if __name__ == "__main__":
    main()
