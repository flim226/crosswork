# AGENTS.md

Guidance for agents editing the Crosswork Network Controller (CNC) CLI scripts
in this directory. `cw_get_jwt.py` is the reference for style and structure.

## Runtime context

- Standalone CLI run from an operator workstation with `python3`
  (shebang `#!/usr/bin/env python3`). Target Python 3.6+.
- Dependencies: the standard library plus `requests` and `urllib3`. Do not add
  other third-party packages without updating `README.md` prerequisites.
- CNC API endpoints are reached at `https://<ip>:30603` (`BASE_PORT`).
  Authentication is a two-step SSO flow:
  1. `POST /crosswork/sso/v1/tickets` (form-encoded `username`, `password`)
     returns a ticket-granting ticket in the `Location` header, JSON `ticket`,
     or plain text body.
  2. `POST /crosswork/sso/v2/tickets/jwt` (form-encoded `tgt`,
     `service=<base_url>/app-dashboard`) returns the JWT in JSON `token` or
     plain text body.

## File layout

Order sections like `cw_get_jwt.py`:

1. Shebang and a one-line module docstring.
2. Imports: stdlib alphabetically (`import x` lines, then `from x import y`),
   blank line, then third-party (`requests`, `urllib3`).
3. Module constants in `UPPER_SNAKE_CASE` (`BASE_PORT`, `CONNECT_TIMEOUT`,
   `ENV_USERNAME`, `ENV_PASSWORD`).
4. Custom exception subclassing `RuntimeError` (`CrossworkAuthError`).
5. Private helpers prefixed with `_` (`_TimeoutAdapter`, `_create_session`).
6. Public API step functions (`check_response`, `get_ticket`, `get_token`),
   then a top-level convenience function (`get_jwt(ip, username, password,
   verify_ssl=True)`) that other scripts can import.
7. Output/utility functions (`decode_jwt`), then CLI-only private helpers
   (`_resolve_credentials`, `_write_token_file`).
8. `main()` that builds the `argparse` parser and dispatches modes, followed
   by `if __name__ == "__main__": main()`.

## Code style

- 4-space indents, `snake_case` names, two blank lines between top-level defs.
- Use f-strings for formatting.
- Type hints on function parameters and return values (`-> str`,
  `-> None`, `-> requests.Session`).
- Docstrings: one-line imperative summary in triple double quotes. Add a
  short explanatory paragraph only when behavior is non-obvious (e.g.
  credential priority in `_resolve_credentials`).
- Comments are sparse and mark modes or intent (`# Decode mode: ...`,
  `# Auth mode: ...`); never restate the code.
- Keep public functions importable and free of `sys.exit()`/`print()` side
  effects where possible; raise exceptions and let `main()` handle exit codes.
- Parse API responses defensively: try the header, then `resp.json()`
  (catching `ValueError`), then fall back to `resp.text.strip()`.

## HTTP

- Always create requests through `_create_session()` so every call gets the
  default `CONNECT_TIMEOUT` via `_TimeoutAdapter` and a consistent
  `session.verify` setting.
- SSL verification is on by default. Disabling it is opt-in via
  `-k/--insecure`, which suppresses `InsecureRequestWarning` and prints
  `WARNING: SSL verification disabled` to stderr.
- Call `check_response(resp, "<function_name>")` after every request. It
  raises `CrossworkAuthError` with the context, `HTTP <code> <reason>`, and a
  response body truncated to 500 characters.

## CLI conventions

- Use `argparse` with `RawDescriptionHelpFormatter` and an `epilog` that
  explains credential resolution order and mode selection.
- Provide long and short flags (`--username/-u`, `--password/-p`,
  `-f/--filename`, `-k/--insecure`). Reference env var names in help text via
  the constants.
- Keep backward-compatible forms (e.g. the legacy positional `IP` alongside
  `--ip`) and reject ambiguous combinations with `parser.error(...)`.
- Choose the mode from the arguments given: an IP means authenticate and save;
  no IP plus `-f` means decode the file.
- Normal progress goes to stdout (`Authenticating to <ip>...`,
  `JWT written to <path>`). Errors go to stderr as `Error: <message>` followed
  by `sys.exit(1)`. Catch `(CrossworkAuthError, requests.RequestException,
  OSError)` at the top level.

## Security

- Never hardcode credentials, tokens, or hostnames with credentials. Example
  values belong only in `README.md` placeholders.
- Resolve credentials in order: CLI flag, then environment variable
  (`CW_USERNAME`, `CW_PASSWORD`), then interactive prompt (`input()` for the
  username, `getpass.getpass()` for the password). The password must never be
  required on the command line.
- Never print passwords or raw tokens. Decoding prints header and payload
  claims only, not the signature.
- Write tokens with `_write_token_file()` (`os.open` with mode `0o600`) and
  create `~/.crosswork` with mode `0o700`. Do not use plain `open()` for
  secret files.
- `decode_jwt` only inspects claims; it does not verify the signature. Do not
  present decoded output as proof of validity.

## Docs

- Keep `README.md` in sync when flags, defaults, credential resolution,
  output paths, or prerequisites change. Update the options table and usage
  examples together.
