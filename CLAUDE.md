# fritz-watchdog

Watchdog that reconnects (optionally reboots) a FRITZ!Box via TR-064 when the internet is down. Overview, settings and
deployment are in README.md. Code: `app/fritz_watchdog.py` (logic), `app/tr064.py` + `app/fritz_common.py` (TR-064 client),
tests in `tests/` (`python -m unittest discover -s tests -v`).

## Rules

- **This repository is public.** Never commit secrets or details of a private network: no IP addresses other than public
  probe targets, no MAC addresses, user or device names, e-mail addresses, host-specific paths or personal setup notes.
  Examples stay generic (`fritz.box`, `/mnt/POOL/...`). Private notes go to `CLAUDE.local.md` (git-ignored).
- Before every commit, check the staged diff for leaks (private IP ranges, MACs, e-mail addresses, passwords).
- Never read or print the credential file. It is consumed only by `fritz_common.py`.
- `ForceTermination`, `RequestConnection` and `Reboot` interrupt the internet of the whole household. Run them against a real
  box only after an explicit OK; otherwise use `--dry-run`, `--check-box` or the unit tests.
- Standard library only; the code must run on the stock `python:3.13-alpine` image.

## Git workflow

- No direct commits to `main`. One branch per change: `feat/…`, `fix/…`, `docs/…`, `test/…`, `chore/…`.
- Conventional Commits in English, imperative, subject ≤ 72 characters, body explains why.
- Show the changes to the owner and wait for an OK before committing. Then: commit, push, open a PR, squash-merge,
  delete the branch, pull `main`.
