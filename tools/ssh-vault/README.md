# SSH-VAULT(1) — SSH credential vault backed by macOS Keychain

## NAME

**ssh-vault** — manage SSH credentials in macOS Keychain and connect to remote hosts without plaintext passwords on disk

## SYNOPSIS

```
ssh-vault [--help] [--version] [-l [REGEX]]
ssh-vault profile {add,list,edit,remove} [options]
ssh-vault host {add,list,remove,bulk-add,bulk-remove,bulk-edit} [options]
ssh-vault connect <host> [--profile <name>] [-- <ssh-args>...]
ssh-vault <host> [--profile <name>] [-- <ssh-args>...]
ssh-vault <host1> <host2> ... [--profile <name>]
```

## DESCRIPTION

**ssh-vault** is a standalone Python CLI tool that stores SSH credentials in
macOS Keychain and connects to remote devices via **sshpass**(1) + **ssh**(1).
No plaintext passwords are ever written to disk.

Profiles group a username and password with one or more hosts. When connecting,
**ssh-vault** looks up the host across all profiles (or uses an explicit
`--profile`) to resolve credentials automatically.

If the first argument is not a recognised command or flag, **ssh-vault** treats
it as a hostname and implicitly runs `connect`. When multiple hostnames are
given without a command, **ssh-vault** opens a separate **tmux**(1) session for
each host (named `sv-<host>`), allowing parallel connections.

## GLOBAL OPTIONS

**--help**, **-h**
: Show the top-level help message and exit.

**--version**
: Print the version number and exit.

**--list**, **-l** *[REGEX]*
: List all hosts across all profiles. If an optional *REGEX* pattern is given,
only hosts whose name matches (case-insensitive) are shown.

## COMMANDS

### profile add

```
ssh-vault profile add <name> --username <USERNAME>
```

Create a new credential profile. Prompts interactively for a password, which is
stored in macOS Keychain.

**name**
: A short identifier for the profile (e.g. `lab-routers`).

**--username**, **-u** *USERNAME*
: SSH username to associate with this profile. Required. Must contain only
letters, digits, dots, hyphens, underscores, and `@` (max 64 characters).

---

### profile list

```
ssh-vault profile list
```

List all configured profiles in a table showing the profile name, username, and
host count.

---

### profile edit

```
ssh-vault profile edit <name> [--username <USERNAME>] [--password]
```

Update an existing profile. At least one of `--username` or `--password` must be
given.

**name**
: The profile to edit.

**--username**, **-u** *USERNAME*
: Set a new SSH username.

**--password**, **-p**
: Prompt for a new password and update the Keychain entry.

---

### profile remove

```
ssh-vault profile remove <name> [--yes]
```

Delete a profile and its stored password from Keychain. Prompts for confirmation
unless `--yes` is given.

**name**
: The profile to delete.

**--yes**, **-y**
: Skip the confirmation prompt.

---

### host add

```
ssh-vault host add <profile> <host>
```

Add a single host to a profile.

**profile**
: The target profile name.

**host**
: Hostname or IP address.

---

### host list

```
ssh-vault host list [profile]
```

List hosts. If *profile* is given, lists only hosts in that profile. Otherwise
lists all hosts across all profiles with their associated profile name.

**profile** *(optional)*
: Restrict output to this profile.

---

### host remove

```
ssh-vault host remove <host> [--profile <name>] [--yes]
```

Remove a host. If `--profile` is omitted, **ssh-vault** auto-detects the
profile. Fails if the host exists in multiple profiles without `--profile`.

**host**
: Hostname or IP to remove.

**--profile**, **-p** *name*
: Explicitly specify the profile (required when the host exists in multiple
profiles).

**--yes**, **-y**
: Skip the confirmation prompt.

---

### host bulk-add

```
ssh-vault host bulk-add <profile> --file <path>
```

Add hosts from a text file. Hosts that already exist in the profile are silently
skipped.

**profile**
: The target profile name.

**--file**, **-f** *path*
: Path to a hosts file (see **HOSTS FILE FORMAT** below).

---

### host bulk-remove

```
ssh-vault host bulk-remove <profile> --file <path> [--yes]
```

Remove hosts listed in a text file from a profile.

**profile**
: The target profile name.

**--file**, **-f** *path*
: Path to a hosts file.

**--yes**, **-y**
: Skip the confirmation prompt.

---

### host bulk-edit

```
ssh-vault host bulk-edit <profile> --file <path>
```

Replace **all** hosts in a profile with the contents of a text file. Existing
hosts are discarded.

**profile**
: The target profile name.

**--file**, **-f** *path*
: Path to a hosts file.

---

### connect

```
ssh-vault connect <host> [--profile <name>] [-- <ssh-args>...]
```

SSH into a host using stored credentials. The profile is auto-detected from the
host unless `--profile` is given.

**host**
: Hostname or IP to connect to.

**--profile**, **-p** *name*
: Use a specific profile instead of auto-detecting.

**ssh-args**
: Additional arguments passed directly to **ssh**(1). Separate them with `--`.
These are forwarded verbatim — avoid passing untrusted input.

The connection is made by replacing the current process with:

```
sshpass -e ssh -C -o StrictHostKeyChecking=<policy> [-o <ssh_options>...] user@host [ssh-args]
```

`<policy>` defaults to `accept-new` (see **CONFIGURATION** below).

The password is passed to **sshpass** via the `SSHPASS` environment variable
(never on the command line).

## ALIASES

Several shorthand aliases are accepted but hidden from help output:

| Alias | Equivalent |
|-------|------------|
| `ssh-vault c` | `ssh-vault connect` |
| `ssh-vault -l` | `ssh-vault host list` (with optional regex) |
| `ssh-vault <host>` | `ssh-vault connect <host>` |
| `ssh-vault <h1> <h2> ...` | Multi-host connect via **tmux**(1) |
| `profile ls` | `profile list` |
| `profile rm` | `profile remove` |
| `profile del` | `profile remove` |
| `profile delete` | `profile remove` |
| `host ls` | `host list` |
| `host rm` | `host remove` |
| `host del` | `host remove` |
| `host delete` | `host remove` |

## FILES

**~/.config/ssh-vault/config.json**
: Profile and host metadata. Passwords are **not** stored here. The directory is
created with mode `0700` and the file with mode `0600` to prevent other users
from reading usernames and host information. The file is written atomically
(via a temporary file and rename) to prevent corruption. On load, **ssh-vault**
warns to standard error if the file permissions have been loosened beyond `0600`.

Example structure:

```json
{
  "profiles": {
    "lab-routers": {
      "hosts": ["10.0.0.1", "10.0.0.2"],
      "username": "admin"
    }
  },
  "settings": {
    "ssh_options": ["UserKnownHostsFile=/dev/null"],
    "strict_host_key_checking": "accept-new"
  }
}
```

**macOS Keychain** (service: `ssh-vault`)
: Passwords are stored and retrieved using the macOS `security` command-line
tool under the service name `ssh-vault`, keyed by profile name.

## HOSTS FILE FORMAT

Bulk operations (`bulk-add`, `bulk-remove`, `bulk-edit`) accept a plain-text
file with one host per line. Blank lines and lines starting with `#` are
ignored.

```
# Lab routers
10.0.0.1
10.0.0.2
router-a.lab
router-b.lab
```

## CONFIGURATION

### SSH Options

Additional SSH `-o` options can be configured in `config.json` under the
`settings.ssh_options` key. Each entry is passed as a separate `-o` flag to
**ssh**(1).

If the key is absent, no extra `-o` options are added (the default list is
empty).

To add options:

```json
"ssh_options": [
  "UserKnownHostsFile=/dev/null",
  "LogLevel=ERROR"
]
```

### Strict Host Key Checking

The `settings.strict_host_key_checking` key controls the
`StrictHostKeyChecking` SSH option. Allowed values are `yes`, `no`, `ask`, and
`accept-new`.

If the key is absent, the default is **`accept-new`** (SSH 7.6+), which accepts
keys for hosts seen for the first time but rejects changed keys — protecting
against MITM attacks on known hosts. Set to `no` to restore the legacy
(insecure) behaviour of accepting all keys unconditionally.

```json
"strict_host_key_checking": "accept-new"
```

## ENVIRONMENT

**SSHPASS**
: Set automatically by **ssh-vault** before exec-ing **sshpass**(1). Contains the
profile password for the duration of the connection. Removed from the
environment if the connection fails before exec. Not intended for user use.

## EXIT STATUS

| Code | Meaning |
|------|---------|
| **0** | Success |
| **1** | Error (missing profile, host not found, corrupt config, etc.) |

Errors are printed to standard error prefixed with `Error:`.

## EXAMPLES

Create a profile and add hosts:

```
ssh-vault profile add lab-routers --username admin
ssh-vault host add lab-routers 10.0.0.1
ssh-vault host add lab-routers 10.0.0.2
```

Bulk-add hosts from a file:

```
ssh-vault host bulk-add lab-routers --file hosts.txt
```

Connect to a host (auto-detect profile):

```
ssh-vault 10.0.0.1
```

Connect with explicit profile and extra SSH flags:

```
ssh-vault connect 10.0.0.1 --profile lab-routers -- -p 2222 -L 8080:localhost:80
```

List everything:

```
ssh-vault profile list
ssh-vault host list
ssh-vault host list lab-routers
```

Quick host search with regex:

```
ssh-vault -l
ssh-vault -l "10\.0\."
```

Connect to multiple hosts in parallel (opens tmux sessions):

```
ssh-vault 10.0.0.1 10.0.0.2 10.0.0.3
ssh-vault -p lab-routers 10.0.0.1 10.0.0.2
tmux ls                    # list sessions
tmux attach -t sv-10.0.0.1 # attach to a session
```

Delete a profile without confirmation:

```
ssh-vault profile remove lab-routers --yes
```

## PREREQUISITES

- **macOS** (uses the `security` CLI for Keychain access)
- **Python 3.10+**
- **sshpass** — install via Homebrew:
  ```
  brew install hudochenkov/sshpass/sshpass
  ```
- **tmux** *(optional)* — required only for multi-host connections:
  ```
  brew install tmux
  ```

## BUGS

Report issues at the project repository.

## SEE ALSO

**ssh**(1), **sshpass**(1), **security**(1), **tmux**(1)
