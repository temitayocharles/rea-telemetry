# Browser checks

The Python suite covers the API and the layout maths; a jsdom smoke test covers
view rendering. Neither proves what a person sees. This directory holds the
checks that need a real engine.

```bash
python3 -m craft --port 8787 &            # from the repo root
cd tests/browser
npm install
npx playwright install chromium
BASE=http://127.0.0.1:8787 node verify.mjs
```

Exit code `0` means every check passed. Screenshots of every view, every
viewport and every failure land in `artifacts/` (git-ignored), along with
`report.md`.

To check the sign-in gate, start a second server with a token and point the
harness at it:

```bash
python3 -m craft --port 8788 --auth-token s3cret &
BASE=http://127.0.0.1:8788 AUTH_TOKEN=s3cret node verify.mjs
```

`CHROME_PATH=/path/to/chrome` uses a local binary instead of Playwright's
download.

Nothing here is a runtime dependency of the app; `craft/` remains Python
standard library only.
