"""`python3 -m craft` — server by default, CLI when given a subcommand."""
import sys

from .cli import COMMANDS, main as cli_main
from .server import main as server_main

if __name__ == "__main__":
    argv = sys.argv[1:]
    # `python3 -m craft` and `--port 8787` boot the server; a subcommand means the CLI,
    # even when `--token` comes first: `craft --token X write …`.
    has_command = any(a in COMMANDS for a in argv) or argv[:1] in (["-h"], ["--help"])
    if not has_command:
        # `python3 -m craft`, `--port 8787`, `--auth-token …` → boot the server directly.
        server_main(argv)
    else:
        # a CLI subcommand (or --help); see docs/CLI.md
        cli_main(argv)
