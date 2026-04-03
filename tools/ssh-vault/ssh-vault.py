#!/usr/bin/env python3
"""SSH Vault – SSH credential vault backed by macOS Keychain.

A standalone CLI tool for managing SSH credentials stored in macOS Keychain.
Connect to remote devices via sshpass + ssh without plaintext passwords on disk.

Config lives at ~/.config/ssh-vault/config.json.
Passwords are stored in macOS Keychain under service name 'ssh-vault'.
"""

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import stat
import sys
from pathlib import Path
from typing import Any, NoReturn

__version__ = "0.1.0"

CONFIG_DIR = Path.home() / ".config" / "ssh-vault"
CONFIG_FILE = CONFIG_DIR / "config.json"
KEYCHAIN_SERVICE = "ssh-vault"
DEFAULT_SSH_OPTIONS = []


# ═══════════════════════════════════════════════════════════════════════
# Custom compact help (sshpass-style)
# ═══════════════════════════════════════════════════════════════════════

def _set_help(parser: argparse.ArgumentParser, text: str) -> None:
    """Override a parser's format_help to return compact text."""
    parser.format_help = lambda: text


# ═══════════════════════════════════════════════════════════════════════
# Error handling
# ═══════════════════════════════════════════════════════════════════════

class VaultError(Exception):
    """Raised for any user-facing error in business logic."""


def _fail(msg: str) -> NoReturn:
    """Raise a VaultError with the given message."""
    raise VaultError(msg)


def _confirm(msg: str, skip: bool = False) -> bool:
    """Prompt user for confirmation. Returns True if confirmed or skip is True."""
    if skip:
        return True
    try:
        answer = input(f"{msg} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


# ═══════════════════════════════════════════════════════════════════════
# Input validation
# ═══════════════════════════════════════════════════════════════════════

_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._@-]+$")
_MAX_PROFILE_NAME_LEN = 64
_MAX_USERNAME_LEN = 64
_MAX_HOSTNAME_LEN = 253


def _validate_profile_name(name: str) -> None:
    """Raise VaultError if *name* is not a safe profile identifier."""
    if not name:
        _fail("profile name must not be empty")
    if len(name) > _MAX_PROFILE_NAME_LEN:
        _fail(f"profile name must be at most {_MAX_PROFILE_NAME_LEN} characters")
    if not _PROFILE_NAME_RE.match(name):
        _fail("profile name may only contain letters, digits, dots, hyphens, and underscores")


def _validate_hostname(host: str) -> None:
    """Raise VaultError if *host* looks unsafe for use as an SSH target."""
    if not host or not host.strip():
        _fail("hostname must not be empty")
    if len(host) > _MAX_HOSTNAME_LEN:
        _fail(f"hostname must be at most {_MAX_HOSTNAME_LEN} characters")
    dangerous = set(" \t\n\r;|&$`\\\"'(){}[]<>!")
    bad = dangerous.intersection(host)
    if bad:
        _fail(f"hostname contains invalid characters: {bad}")


def _validate_username(name: str) -> None:
    """Raise VaultError if *name* is not a safe SSH username."""
    if not name:
        _fail("username must not be empty")
    if len(name) > _MAX_USERNAME_LEN:
        _fail(f"username must be at most {_MAX_USERNAME_LEN} characters")
    if not _USERNAME_RE.match(name):
        _fail("username may only contain letters, digits, dots, hyphens, underscores, and @")


# ═══════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════

class Config:
    """Vault configuration backed by a JSON file on disk.

    Load once with ``Config.load()``, mutate in memory, then call
    ``config.save()`` to persist.  This avoids repeated disk I/O that
    the previous ``_load()`` / ``_save()`` free-function approach incurred.
    """

    def __init__(self, data: dict[str, Any], path: Path = CONFIG_FILE) -> None:
        self._data = data
        self._path = path
        self._dir = path.parent

    # -- Construction ------------------------------------------------

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> "Config":
        """Load config from *path*. Returns an empty structure if the file
        does not exist yet."""
        if not path.exists():
            return cls({"profiles": {}}, path=path)
        # Warn if the config file is readable by group or others
        try:
            mode = path.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                print(
                    f"Warning: {path} has overly permissive permissions "
                    f"({stat.filemode(mode)}). Run: chmod 600 {path}",
                    file=sys.stderr,
                )
        except OSError:
            pass  # stat failure is non-fatal; load will fail below if unreadable
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            _fail(f"corrupt config {path}: {exc}")
        if not isinstance(data.get("profiles"), dict):
            _fail(f"corrupt config: 'profiles' key missing or invalid in {path}")
        return cls(data, path=path)

    # -- Persistence -------------------------------------------------

    def save(self) -> None:
        """Write config to disk atomically.

        Directory is created with mode 0o700, and files with mode 0o600 to
        prevent other users from reading usernames and host information.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
                f.write("\n")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self._path)

    # -- Accessors ---------------------------------------------------

    @property
    def profiles(self) -> dict[str, Any]:
        return self._data.setdefault("profiles", {})

    @property
    def settings(self) -> dict[str, Any]:
        return self._data.setdefault("settings", {})

    def require_profile(self, name: str) -> dict[str, Any]:
        """Return the profile dict for *name*, or raise VaultError."""
        try:
            return self.profiles[name]
        except KeyError:
            _fail(f"profile '{name}' not found")

    def get_ssh_options(self) -> list[str]:
        """Return SSH ``-o`` options, falling back to defaults."""
        return self.settings.get("ssh_options", DEFAULT_SSH_OPTIONS)

    def get_strict_host_key_checking(self) -> str:
        """Return the ``StrictHostKeyChecking`` policy.

        Defaults to ``accept-new`` (SSH 7.6+) which accepts keys for hosts
        seen for the first time but rejects changed keys — protecting against
        MITM attacks on known hosts.  Set to ``no`` in config to restore the
        legacy (insecure) behaviour.
        """
        value = self.settings.get("strict_host_key_checking", "accept-new")
        allowed = {"yes", "no", "ask", "accept-new"}
        if value not in allowed:
            _fail(f"invalid strict_host_key_checking value '{value}' — must be one of {allowed}")
        return value


def _read_hosts_file(path: str) -> list[str]:
    """Read hosts from a text file, one per line. Skips blanks and comments."""
    filepath = Path(path)
    if not filepath.exists():
        _fail(f"file '{path}' not found")
    hosts = []
    for raw in filepath.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            hosts.append(line)
    if not hosts:
        _fail(f"file '{path}' contains no hosts")
    return hosts




# ═══════════════════════════════════════════════════════════════════════
# Profile operations
# ═══════════════════════════════════════════════════════════════════════

def profile_add(name: str, username: str) -> None:
    _validate_profile_name(name)
    _validate_username(username)
    cfg = Config.load()
    if name in cfg.profiles:
        _fail(f"profile '{name}' already exists")
    cfg.profiles[name] = {"username": username, "hosts": []}
    cfg.save()
    print(f"Profile '{name}' created (username: {username}).")


def profile_list() -> list[dict[str, Any]]:
    """Return list of profile summaries."""
    profiles = Config.load().profiles
    return [
        {"name": name, "username": info["username"], "host_count": len(info["hosts"])}
        for name, info in sorted(profiles.items())
    ]


def _print_profiles(profiles: list[dict[str, Any]]) -> None:
    """Format and print profile list."""
    if not profiles:
        print("No profiles configured.")
        return
    print(f"{'Profile':<20} {'Username':<20} {'Hosts':>5}")
    print("-" * 47)
    for p in profiles:
        print(f"{p['name']:<20} {p['username']:<20} {p['host_count']:>5}")


def profile_edit(name: str, username: str | None = None) -> None:
    _validate_profile_name(name)
    cfg = Config.load()
    profile = cfg.require_profile(name)
    if username is not None:
        _validate_username(username)
        profile["username"] = username
        print(f"Profile '{name}' username updated to '{username}'.")
    cfg.save()


def profile_delete(name: str) -> None:
    _validate_profile_name(name)
    cfg = Config.load()
    cfg.require_profile(name)
    del cfg.profiles[name]
    cfg.save()
    print(f"Profile '{name}' deleted.")


# ═══════════════════════════════════════════════════════════════════════
# Host operations
# ═══════════════════════════════════════════════════════════════════════

def host_add(profile_name: str, host: str) -> None:
    _validate_profile_name(profile_name)
    _validate_hostname(host)
    cfg = Config.load()
    profile = cfg.require_profile(profile_name)
    if host in profile["hosts"]:
        print(f"Host '{host}' already in profile '{profile_name}'.")
        return
    profile["hosts"].append(host)
    cfg.save()
    print(f"Host '{host}' added to profile '{profile_name}'.")


def host_list(profile_name: str | None = None, pattern: str | None = None) -> list[dict[str, str]]:
    """Return list of host entries. Each entry has 'host' and 'profile' keys.

    If *pattern* is given, only hosts whose name matches the regex are returned.
    """
    cfg = Config.load()
    if profile_name:
        profile = cfg.require_profile(profile_name)
        hosts = [{"host": h, "profile": profile_name} for h in sorted(profile["hosts"])]
    else:
        hosts = sorted(
            [{"host": h, "profile": name}
             for name, info in cfg.profiles.items()
             for h in info["hosts"]],
            key=lambda x: x["host"],
        )
    if pattern:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            _fail(f"invalid regex '{pattern}': {exc}")
        hosts = [h for h in hosts if rx.search(h["host"])]
    return hosts


def _print_hosts(hosts: list[dict[str, str]], profile_name: str | None = None) -> None:
    """Format and print host list."""
    if not hosts:
        if profile_name:
            print(f"No hosts in profile '{profile_name}'.")
        else:
            print("No hosts configured.")
        return
    if profile_name:
        print(f"Hosts in profile '{profile_name}':")
        for h in hosts:
            print(f"  {h['host']}")
    else:
        print(f"{'Host':<40} {'Profile':<20}")
        print("-" * 60)
        for h in hosts:
            print(f"{h['host']:<40} {h['profile']:<20}")


def host_remove(host: str, profile_name: str | None = None) -> None:
    _validate_hostname(host)
    cfg = Config.load()
    if profile_name:
        profile = cfg.require_profile(profile_name)
    else:
        matches = [
            (name, info)
            for name, info in cfg.profiles.items()
            if host in info["hosts"]
        ]
        if not matches:
            _fail(f"host '{host}' not found in any profile")
        if len(matches) > 1:
            names = ", ".join(m[0] for m in matches)
            _fail(f"host '{host}' exists in multiple profiles: {names}. Use --profile to specify which one")
        profile_name, profile = matches[0]
    if host not in profile["hosts"]:
        _fail(f"host '{host}' not in profile '{profile_name}'")
    profile["hosts"].remove(host)
    cfg.save()
    print(f"Host '{host}' removed from profile '{profile_name}'.")


def host_bulk_add(profile_name: str, file_path: str) -> None:
    _validate_profile_name(profile_name)
    cfg = Config.load()
    profile = cfg.require_profile(profile_name)
    new_hosts = _read_hosts_file(file_path)
    for h in new_hosts:
        _validate_hostname(h)
    existing = set(profile["hosts"])
    added = 0
    for h in new_hosts:
        if h not in existing:
            profile["hosts"].append(h)
            existing.add(h)
            added += 1
    cfg.save()
    print(f"Added {added} host(s) to profile '{profile_name}' ({len(new_hosts) - added} already existed).")


def host_bulk_remove(profile_name: str, file_path: str) -> None:
    _validate_profile_name(profile_name)
    cfg = Config.load()
    profile = cfg.require_profile(profile_name)
    to_remove = set(_read_hosts_file(file_path))
    before = len(profile["hosts"])
    profile["hosts"] = [h for h in profile["hosts"] if h not in to_remove]
    removed = before - len(profile["hosts"])
    cfg.save()
    print(f"Removed {removed} host(s) from profile '{profile_name}'.")


def host_bulk_edit(profile_name: str, file_path: str) -> None:
    """Replace all hosts in a profile with the contents of a file."""
    _validate_profile_name(profile_name)
    cfg = Config.load()
    cfg.require_profile(profile_name)
    new_hosts = _read_hosts_file(file_path)
    for h in new_hosts:
        _validate_hostname(h)
    cfg.profiles[profile_name]["hosts"] = list(dict.fromkeys(new_hosts))
    cfg.save()
    print(f"Profile '{profile_name}' hosts replaced with {len(new_hosts)} host(s).")


def find_profile_for_host(host: str) -> list[tuple[str, dict[str, Any]]]:
    """Return list of (profile_name, profile_data) that contain the given host."""
    return [
        (name, info)
        for name, info in Config.load().profiles.items()
        if host in info["hosts"]
    ]


def _resolve_single_profile(host: str) -> tuple[str, dict[str, Any]]:
    """Find exactly one profile containing *host*, or fail with a clear error."""
    matches = find_profile_for_host(host)
    if not matches:
        _fail(f"host '{host}' not found in any profile")
    if len(matches) > 1:
        names = ", ".join(m[0] for m in matches)
        _fail(f"host '{host}' exists in multiple profiles: {names}. Use --profile to specify which one")
    return matches[0]


# ═══════════════════════════════════════════════════════════════════════
# Keychain
# ═══════════════════════════════════════════════════════════════════════

_SECURITY_TIMEOUT = 30  # seconds to wait for macOS Keychain CLI


def _security(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["security", *args],
            capture_output=True,
            text=True,
            timeout=_SECURITY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _fail(f"Keychain operation timed out after {_SECURITY_TIMEOUT}s (is the Keychain locked?)")


def keychain_set(profile: str, password: str) -> None:
    """Store or update a password in macOS Keychain.

    NOTE: The macOS ``security`` CLI requires the password as a command-line
    argument (``-w <password>``).  This briefly exposes the password in the
    process table.  The risk is mitigated by using list-based ``subprocess.run``
    (no shell) and keeping the window as short as possible.  A future
    improvement could use the ``keyring`` Python library which calls the
    Keychain C API directly, eliminating process-table exposure entirely.
    """
    # Try to add first; use -U (update) to avoid a delete-then-add race
    # where an interruption between delete and add would lose the password.
    result = _security(
        "add-generic-password", "-U",
        "-s", KEYCHAIN_SERVICE, "-a", profile, "-w", password,
    )
    if result.returncode != 0:
        _fail(f"storing password in Keychain: {result.stderr.strip()}")


def keychain_get(profile: str) -> str:
    """Retrieve a password from macOS Keychain."""
    result = _security("find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", profile, "-w")
    if result.returncode != 0:
        _fail(f"no Keychain entry found for profile '{profile}'")
    return result.stdout.strip()


def keychain_delete(profile: str) -> None:
    """Remove a password from macOS Keychain."""
    result = _security("delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", profile)
    if result.returncode != 0:
        print(f"Warning: could not remove Keychain entry for profile '{profile}'.", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════
# Connect
# ═══════════════════════════════════════════════════════════════════════

def connect(host: str, profile_name: str | None = None, ssh_args: list[str] | None = None) -> None:
    """Resolve credentials and exec into sshpass + ssh."""
    if not shutil.which("sshpass"):
        _fail("sshpass is not installed. Install via: brew install hudochenkov/sshpass/sshpass")

    cfg = Config.load()
    if profile_name:
        info = cfg.require_profile(profile_name)
    else:
        profile_name, info = _resolve_single_profile(host)

    username = info["username"]
    password = keychain_get(profile_name)

    os.environ["SSHPASS"] = password
    try:
        strict_policy = cfg.get_strict_host_key_checking()
        cmd = ["sshpass", "-e", "ssh", "-C", "-o", f"StrictHostKeyChecking={strict_policy}"]
        for opt in cfg.get_ssh_options():
            cmd.extend(["-o", opt])
        cmd.append(f"{username}@{host}")
        if ssh_args:
            cmd.extend(ssh_args)

        print(f"Connecting to {username}@{host} (profile: {profile_name})...")
        os.execvp("sshpass", cmd)
    finally:
        # If execvp fails or any exception occurs, scrub the password from env
        os.environ.pop("SSHPASS", None)


def multi_connect(hosts: list[str], profile_name: str | None = None) -> None:
    """Open separate tmux sessions for each host."""
    if not shutil.which("tmux"):
        _fail("tmux is not installed. Install via: brew install tmux")

    script = shutil.which("ssh-vault") or os.path.abspath(sys.argv[0])

    created = []
    for host in hosts:
        _validate_hostname(host)
        session_name = f"sv-{host}"
        cmd = ["tmux", "new-session", "-d", "-s", session_name, "--", script, "connect"]
        if profile_name:
            cmd.extend(["-p", profile_name])
        cmd.append(host)

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "duplicate session" in stderr:
                print(f"  tmux session '{session_name}' already exists, skipping.", file=sys.stderr)
            else:
                print(f"  Failed to create session for {host}: {stderr}", file=sys.stderr)
        else:
            created.append(session_name)
            print(f"  Created tmux session '{session_name}' for {host}")

    if created:
        print(f"\n{len(created)} session(s) created. Use 'tmux attach -t <name>' or 'tmux ls' to list.")


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def _build_profile_subparser(sub: argparse._SubParsersAction) -> None:
    profile_parser = sub.add_parser("profile", help="Manage credential profiles")
    profile_sub = profile_parser.add_subparsers(dest="action")

    p_add = profile_sub.add_parser("add", help="Create a new profile",
                                   usage="%(prog)s name --username USERNAME")
    p_add.add_argument("name", help="Profile name")
    p_add.add_argument("--username", "-u", required=True, help="SSH username", metavar="USERNAME")

    p_list = profile_sub.add_parser("list", help="List all profiles")
    profile_sub._name_parser_map["ls"] = p_list

    p_edit = profile_sub.add_parser("edit", help="Edit a profile",
                                    usage="%(prog)s name [--username USERNAME] [--password]")
    p_edit.add_argument("name", help="Profile name")
    p_edit.add_argument("--username", "-u", help="New username", metavar="USERNAME")
    p_edit.add_argument("--password", "-p", action="store_true", help="Update password")

    p_del = profile_sub.add_parser("remove", help="Delete a profile",
                                   usage="%(prog)s name")
    p_del.add_argument("name", help="Profile name")
    p_del.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    for alias in ("delete", "del", "rm"):
        profile_sub._name_parser_map[alias] = p_del

    profile_sub.metavar = "{add,list,edit,remove}"

    # -- compact help overrides --
    _set_help(profile_parser, """\
Usage: ssh-vault profile [-h] <action> [options]
  add <name> -u USER    Create a new profile.
  list                  List all profiles.
  edit <name> [opts]    Edit a profile.
  remove <name>         Delete a profile.
  -u, --username USER   SSH username (required for add, optional for edit).
  -p, --password        Prompt to update password (edit only).
  -y, --yes             Skip confirmation prompt (remove only).
  -h, --help            Show help (this screen).
Aliases: ls=list, rm/del/delete=remove.
""")
    _set_help(p_add, """\
Usage: ssh-vault profile add [-h] <name> -u USERNAME
  name                  Profile name.
  -u, --username USER   SSH username (required).
  -h, --help            Show help (this screen).
""")
    _set_help(p_list, """\
Usage: ssh-vault profile list [-h]
  -h, --help            Show help (this screen).
List all configured profiles.
""")
    _set_help(p_edit, """\
Usage: ssh-vault profile edit [-h] <name> [-u USERNAME] [-p]
  name                  Profile name.
  -u, --username USER   Set a new username.
  -p, --password        Prompt for a new password.
  -h, --help            Show help (this screen).
At least one of -u or -p must be given.
""")
    _set_help(p_del, """\
Usage: ssh-vault profile remove [-h] [-y] <name>
  name                  Profile name.
  -y, --yes             Skip confirmation prompt.
  -h, --help            Show help (this screen).
""")


def _build_host_subparser(sub: argparse._SubParsersAction) -> None:
    host_parser = sub.add_parser("host", help="Manage hosts within profiles")
    host_sub = host_parser.add_subparsers(dest="action")

    h_add = host_sub.add_parser("add", help="Add a host to a profile")
    h_add.add_argument("profile", help="Profile name")
    h_add.add_argument("host", help="Hostname or IP")

    h_list = host_sub.add_parser("list", help="List hosts (all or for a specific profile)")
    h_list.add_argument("profile", nargs="?", default=None, help="Profile name (omit to list all hosts)")
    host_sub._name_parser_map["ls"] = h_list

    h_rm = host_sub.add_parser("remove", help="Remove a host from a profile")
    h_rm.add_argument("host", help="Hostname or IP")
    h_rm.add_argument("--profile", "-p", default=None, help="Profile name (auto-detected if omitted)")
    h_rm.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    for alias in ("rm", "del", "delete"):
        host_sub._name_parser_map[alias] = h_rm

    bulk_parsers = {}
    for name, help_text in [("bulk-add", "Add hosts from a file"),
                            ("bulk-remove", "Remove hosts listed in a file"),
                            ("bulk-edit", "Replace all hosts from a file")]:
        p = host_sub.add_parser(name, help=help_text)
        p.add_argument("profile", help="Profile name")
        p.add_argument("--file", "-f", required=True, dest="file_path", help="File with one host per line")
        if name == "bulk-remove":
            p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
        bulk_parsers[name] = p

    host_sub.metavar = "{add,list,remove,bulk-add,bulk-remove,bulk-edit}"

    # -- compact help overrides --
    _set_help(host_parser, (
        "Usage: ssh-vault host [-h] <action> [options]\n"
        "  add <profile> <host>        Add a host to a profile.\n"
        "  list [profile]              List hosts (all or per profile).\n"
        "  remove <host> [-p PROFILE]  Remove a host from a profile.\n"
        "  bulk-add <profile> -f FILE  Add hosts from a file.\n"
        "  bulk-remove <profile> -f FILE\n"
        "                              Remove hosts listed in a file.\n"
        "  bulk-edit <profile> -f FILE Replace all hosts from a file.\n"
        "  -f, --file FILE             File with one host per line (bulk ops).\n"
        "  -p, --profile PROFILE       Profile name (remove: auto-detected if omitted).\n"
        "  -y, --yes                   Skip confirmation prompt.\n"
        "  -h, --help                  Show help (this screen).\n"
        "Aliases: ls=list, rm/del/delete=remove.\n"
    ))
    _set_help(h_add, (
        "Usage: ssh-vault host add [-h] <profile> <host>\n"
        "  profile               Profile name.\n"
        "  host                  Hostname or IP address.\n"
        "  -h, --help            Show help (this screen).\n"
    ))
    _set_help(h_list, (
        "Usage: ssh-vault host list [-h] [profile]\n"
        "  profile               Profile name (omit to list all hosts).\n"
        "  -h, --help            Show help (this screen).\n"
    ))
    _set_help(h_rm, (
        "Usage: ssh-vault host remove [-h] [-p PROFILE] [-y] <host>\n"
        "  host                  Hostname or IP to remove.\n"
        "  -p, --profile NAME    Profile name (auto-detected if omitted).\n"
        "  -y, --yes             Skip confirmation prompt.\n"
        "  -h, --help            Show help (this screen).\n"
    ))
    _set_help(bulk_parsers["bulk-add"], (
        "Usage: ssh-vault host bulk-add [-h] <profile> -f FILE\n"
        "  profile               Profile name.\n"
        "  -f, --file FILE       File with one host per line.\n"
        "  -h, --help            Show help (this screen).\n"
    ))
    _set_help(bulk_parsers["bulk-remove"], (
        "Usage: ssh-vault host bulk-remove [-h] [-y] <profile> -f FILE\n"
        "  profile               Profile name.\n"
        "  -f, --file FILE       File with one host per line.\n"
        "  -y, --yes             Skip confirmation prompt.\n"
        "  -h, --help            Show help (this screen).\n"
    ))
    _set_help(bulk_parsers["bulk-edit"], (
        "Usage: ssh-vault host bulk-edit [-h] <profile> -f FILE\n"
        "  profile               Profile name.\n"
        "  -f, --file FILE       File with one host per line.\n"
        "  -h, --help            Show help (this screen).\n"
    ))


def _build_connect_subparser(sub: argparse._SubParsersAction) -> None:
    c_parser = sub.add_parser("connect", help="SSH to a host (default command)")
    c_parser.add_argument("host", help="Hostname or IP to connect to")
    c_parser.add_argument("--profile", "-p", dest="profile_name", help="Override profile")
    c_parser.add_argument("ssh_args", nargs=argparse.REMAINDER, help="Extra args passed to ssh")
    sub._name_parser_map["c"] = c_parser

    _set_help(c_parser, (
        "Usage: ssh-vault connect [-h] [-p PROFILE] <host> [-- ssh-args...]\n"
        "  host                  Hostname or IP to connect to.\n"
        "  -p, --profile NAME    Use a specific profile instead of auto-detecting.\n"
        "  ssh-args              Additional arguments passed directly to ssh.\n"
        "  -h, --help            Show help (this screen).\n"
        "Separate extra ssh arguments with '--'.\n"
        "Note: ssh-args are forwarded verbatim — avoid untrusted input.\n"
    ))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ssh-vault",
        description="SSH credential vault backed by macOS Keychain.",
        epilog="If no command (host|profile) is specified, ssh-vault will connect to the given host by default.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    _build_profile_subparser(sub)
    _build_host_subparser(sub)
    _build_connect_subparser(sub)

    sub.metavar = "{profile,host,connect}"

    _set_help(parser, (
        f"Usage: ssh-vault [-h|--version|-l [REGEX]] <command> [options]\n"
        f"  profile               Manage credential profiles.\n"
        f"  host                  Manage hosts within profiles.\n"
        f"  connect <host>        SSH to a host using stored credentials.\n"
        f"  -l, --list [REGEX]    List all hosts, or search by regex pattern.\n"
        f"  -h, --help            Show help (this screen).\n"
        f"  --version             Print version information.\n"
        f"If no command is given, arguments are treated as hostnames to connect.\n"
        f"Multiple hosts open separate tmux sessions for each connection.\n"
    ))

    return parser

# Canonical alias mapping — applied once after parsing
_ACTION_ALIAS = {
    "rm": "remove", "del": "remove", "delete": "remove",
    "ls": "list",
}


# ── Command handlers ────────────────────────────────────────────────

def _prompt_password(prompt: str) -> str:
    """Prompt for a password twice and return it, or fail on mismatch."""
    password = getpass.getpass(prompt)
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        _fail("passwords do not match")
    return password


def _do_profile_add(args: argparse.Namespace) -> None:
    password = _prompt_password(f"Password for profile '{args.name}': ")
    profile_add(args.name, args.username)
    keychain_set(args.name, password)


def _do_profile_list(args: argparse.Namespace) -> None:
    _print_profiles(profile_list())


def _do_profile_edit(args: argparse.Namespace) -> None:
    if not args.username and not args.password:
        _fail("nothing to update — use --username and/or --password")
    profile_edit(args.name, username=args.username)
    if args.password:
        password = _prompt_password(f"New password for profile '{args.name}': ")
        keychain_set(args.name, password)
        print(f"Password updated for profile '{args.name}'.")


def _do_profile_remove(args: argparse.Namespace) -> None:
    profile_delete(args.name)
    keychain_delete(args.name)


_PROFILE_ACTIONS = {
    "add":    _do_profile_add,
    "list":   _do_profile_list,
    "edit":   _do_profile_edit,
    "remove": _do_profile_remove,
}

_PROFILE_CONFIRM = {
    "remove": lambda a: f"Delete profile '{a.name}' and its stored password?",
}

_HOST_ACTIONS = {
    "add":         lambda a: host_add(a.profile, a.host),
    "list":        lambda a: _print_hosts(host_list(a.profile), a.profile),
    "remove":      lambda a: host_remove(a.host, profile_name=a.profile),
    "bulk-add":    lambda a: host_bulk_add(a.profile, a.file_path),
    "bulk-remove": lambda a: host_bulk_remove(a.profile, a.file_path),
    "bulk-edit":   lambda a: host_bulk_edit(a.profile, a.file_path),
}

_HOST_CONFIRM = {
    "remove":      lambda a: f"Remove host '{a.host}' from profile {a.profile if a.profile else '(auto-detect)'}?",
    "bulk-remove": lambda a: f"Remove {len(_read_hosts_file(a.file_path))} host(s) from profile '{a.profile}'?",
}


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser,
              command: str, actions: dict[str, Any], confirm: dict[str, Any]) -> None:
    """Unified dispatch: show help, confirm if needed, then run the action."""
    if not args.action:
        parser.parse_args([command, "--help"])
        return
    if args.action in confirm:
        skip = getattr(args, "yes", False)
        if not _confirm(confirm[args.action](args), skip=skip):
            print("Aborted.")
            return
    actions[args.action](args)


def _cmd_profile(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    _dispatch(args, parser, "profile", _PROFILE_ACTIONS, _PROFILE_CONFIRM)


def _cmd_host(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    _dispatch(args, parser, "host", _HOST_ACTIONS, _HOST_CONFIRM)


def _cmd_connect(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> None:
    extra = args.ssh_args or None
    connect(args.host, profile_name=args.profile_name, ssh_args=extra)


_COMMANDS = {
    "profile": _cmd_profile,
    "host":    _cmd_host,
    "connect": _cmd_connect,
    "c":       _cmd_connect,
}


def _default_to_connect(argv: list[str]) -> list[str]:
    """If the first arg isn't a known subcommand or flag, assume 'connect'."""
    if not argv:
        return argv
    known = {"profile", "host", "connect", "c", "-h", "--help", "--version", "-l", "--list"}
    if argv[0] not in known:
        return ["connect"] + argv
    return argv


def _parse_implicit_hosts(argv: list[str]) -> tuple[list[str], str | None]:
    """Parse argv for an implicit host list when no subcommand is given.

    Returns ``(hosts, profile_name)``.  Returns ``([], None)`` when *argv*
    starts with a known subcommand or flag.
    """
    known = {"profile", "host", "connect", "c", "-h", "--help", "--version", "-l", "--list"}
    if not argv or argv[0] in known:
        return [], None

    profile_name: str | None = None
    hosts: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            break
        if arg in ("-p", "--profile"):
            if i + 1 < len(argv):
                profile_name = argv[i + 1]
                i += 2
                continue
            break
        if arg.startswith("-"):
            break
        hosts.append(arg)
        i += 1
    return hosts, profile_name


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    if argv is None:
        argv = sys.argv[1:]

    # Handle -l / --list shortcut before argparse
    if argv and argv[0] in ("-l", "--list"):
        pattern = argv[1] if len(argv) > 1 else None
        try:
            _print_hosts(host_list(pattern=pattern))
        except VaultError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    # Multi-host → open separate tmux sessions
    hosts, profile = _parse_implicit_hosts(argv)
    if len(hosts) > 1:
        try:
            multi_connect(hosts, profile_name=profile)
        except VaultError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    argv = _default_to_connect(argv)
    args = parser.parse_args(argv)

    # Normalize aliases to canonical action names
    if hasattr(args, "action") and args.action:
        args.action = _ACTION_ALIAS.get(args.action, args.action)

    if not args.command:
        parser.print_help()
        sys.exit(0)

    try:
        _COMMANDS[args.command](args, parser)
    except VaultError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
