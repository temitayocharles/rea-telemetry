"""`python3 -m craft` — server by default, CLI when given a subcommand."""
import sys

from .cli import main as cli_main
from .server import main as server_main

if __name__ == "__main__":
    argv = sys.argv[1:]
    server_flags = ("--port", "--host", "--no-seed")
    if not argv or any(a.split("=", 1)[0] in server_flags for a in argv):
        # `python3 -m craft`, `--port 8787`, `--no-seed` → boot the server directly.
        server_main(argv)
    else:
        # anything else is a CLI subcommand (or --help); see docs/CLI.md
        cli_main(argv)
