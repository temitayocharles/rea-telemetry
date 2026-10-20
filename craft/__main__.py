"""`python3 -m craft` — server by default, CLI when given a subcommand."""
import sys

from .cli import main as cli_main
from .server import main as server_main

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        # `python3 -m craft`, `--port 8787`, `--no-seed` → boot the server directly.
        server_main(argv)
    else:
        cli_main(argv)
