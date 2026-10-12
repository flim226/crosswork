#!/usr/bin/env bash
#
# Submit native mate_convert as a Crosswork Planning CLI_JOB over REST.
#
# Shell equivalent of mate_convert.py, built on the CP 8.0 REST calls shown in
# https://xrdocs.io/automation/tutorials/using-cp8-rest-apis
#
# Converts plan files between .txt / .pln / .db and between schema/release
# versions by staging the plan in Design userspace, submitting an immediate
# CLI_JOB (script=mate_convert), polling until completion, and extracting the
# converted file from the result tar.
#
# Requires bash 3.2+, curl 7.55+, jq 1.6+, and tar.
#
# Credentials are never hardcoded. When --username, --password, and --jwt are
# all omitted, ~/.crosswork/<ip>.jwt is used if it exists (the path
# cw_get_jwt.py writes). Otherwise --jwt, CLI flags, CW_USERNAME / CW_PASSWORD,
# or an interactive prompt. The password and JWT are passed to curl on stdin or
# through a mode-600 header file, never on the curl command line.
#
# HTTPS only, TLS 1.2 or newer, redirects are not followed. Certificate
# verification stays on unless -k/--insecure is set; point CURL_CA_BUNDLE at
# the Crosswork CA certificate to verify a private CA.

set -euo pipefail
# Byte-wise character classes so [A-Za-z] allowlists cannot match locale letters.
export LC_ALL=C

PROG=$(basename -- "$0")

DEFAULT_PORT=30603
DEFAULT_TIMEOUT=30
DEFAULT_PROFILE="4000m vCPU, 16Gi Mem"
DEFAULT_POLL_INTERVAL=5
DEFAULT_JOB_TIMEOUT=300
MAX_HTTP_TIMEOUT=86400
MAX_JOB_TIMEOUT=86400
MAX_POLL_INTERVAL=3600
MAX_JSON_BYTES=$((2 * 1024 * 1024))
MAX_TRANSFER_BYTES=$((1024 * 1024 * 1024))
MAX_TAR_MEMBERS=4096
MAX_TOKEN_CHARS=16384

SAFE_NAME_RE='^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$'
VERSION_RE='^[0-9][0-9._-]{0,31}$'
PROFILE_RE='^[0-9]+m vCPU,[[:space:]]*[0-9]+Gi Mem$'
HOST_LABEL_RE='^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$'
POSITIVE_INT_RE='^[1-9][0-9]{0,17}$'
NUMBER_RE='^([0-9]+(\.[0-9]*)?|\.[0-9]+)$'

usage() {
  cat <<EOF
usage: $PROG [-h] [--ip IP] [--port PORT] [--username USERNAME]
       [--password PASSWORD] [--jwt <file>] [--http-timeout <sec>] [-k]
       [--job-name <name>] [--profile <profile>] [--job-priority {LOW,MEDIUM,HIGH}]
       [--poll-interval <sec>] [--timeout <sec>] [--keep-tar <path>]
       -plan-file <filename> -out-file <filename> [-to-version <version>]
       [-version-type <release|schema>] [-include-netint <true|false>]
       [-simple-txt-out-file <true|false>] [-verbosity <1-60>]
       [-log-file <filename>] [-suppress-progress <true|false>]
       [-options-file <filename>] [-no-global-options <true|false>]
EOF
}

help_text() {
  usage
  cat <<EOF

Submit native mate_convert as a Crosswork Planning CLI_JOB. Converts plan files
between .txt / .pln / .db and between schema/release versions.

Crosswork connection:
  --ip IP                 Crosswork controller IP or hostname (or set CW_HOST)
  --port PORT             Crosswork HTTPS port (default: $DEFAULT_PORT)
  -u, --username USERNAME Username (or set CW_USERNAME)
  -p, --password PASSWORD Password (or set CW_PASSWORD; prompting avoids
                          exposing it in the process list)
  -j, --jwt <file>        Path to a JWT file (default when no credentials are
                          given: ~/.crosswork/<ip>.jwt)
  --http-timeout <sec>    HTTP connect/stall timeout in seconds (default: $DEFAULT_TIMEOUT)
  -k, --insecure          Disable TLS certificate verification (lab only; not
                          recommended). Prefer CURL_CA_BUNDLE=<crosswork-ca.pem>

Job control:
  --job-name <name>       Job name (default: CLI: mate_convert <plan> -> <out>)
  --profile <profile>     requestedProfile (default: $DEFAULT_PROFILE)
  --job-priority {LOW,MEDIUM,HIGH}
                          Job priority (default: HIGH)
  --poll-interval <sec>   Seconds between job-status polls (default: $DEFAULT_POLL_INTERVAL)
  --timeout <sec>         Max seconds to wait for job completion (default: $DEFAULT_JOB_TIMEOUT)
  --keep-tar <path>       Save the job result tar to this path

mate_convert:
  -plan-file <filename>   Local plan path (imported) or userspace PLANFILE name
  -out-file <filename>    Local output path; extension selects format (.pln,
                          .txt, .db). Use '-' for stdout
  -to-version <version>   Version to convert to: current (default), keep, or a
                          release/schema id (e.g. 7.9)
  -version-type <release|schema>
                          How -to-version is interpreted (default release)
  -include-netint <true|false>
                          Include <NetInt*> tables in output (default true in tool)
  -simple-txt-out-file <true|false>
                          Remove empty tables/columns from .txt output
  -verbosity <1-60>       Log verbosity (default 30 in tool)
  -log-file <filename>    Log file for warnings and errors (created on the engine)
  -suppress-progress <true|false>
                          Suppress progress output (default true in tool)
  -options-file <filename>
                          Read additional options from file (local files are
                          imported and staged)
  -no-global-options <true|false>
                          Do not load global options file

Credentials: ~/.crosswork/<ip>.jwt when --username, --password, and
--jwt are all omitted and that file exists. Otherwise --jwt, CLI flags,
CW_USERNAME/CW_PASSWORD, or an interactive prompt.

CP 8.0 mate_convert options (from live -help on Build 385):

  -plan-file, -out-file          required (stdin '-' is not supported)
  -to-version                    current (default, 7.10.-1) | keep | e.g. 7.9
  -version-type                  release (default) | schema
  -include-netint                true (default) | false
  -simple-txt-out-file           true | false (default false)
  plus generic: -verbosity, -log-file, -suppress-progress,
                -options-file, -no-global-options

File names sent to the engine must be 1-255 characters of letters, digits,
'.', '_' or '-', and must start with a letter or digit. Uploads and the
result tar are limited to $MAX_TRANSFER_BYTES bytes.

Examples:
  export CURL_CA_BUNDLE=\$PWD/cw-ca.pem
  $PROG --ip "\$CW_HOST" -plan-file us_wan.txt -out-file /tmp/us_wan.pln
  $PROG --ip 198.18.134.229 -k -plan-file us_wan.txt -out-file us_wan.pln -to-version keep
  $PROG --ip 198.18.134.229 --jwt ~/.crosswork/198.18.134.229.jwt \\
      -plan-file us_wan.txt -out-file us_wan_7.9.txt -to-version 7.9
EOF
}

usage_error() {
  usage >&2
  printf '%s: error: %s\n' "$PROG" "$1" >&2
  exit 2
}

log() { printf '%s\n' "$*" >&2; }

# Collapse untrusted text onto one printable line.
one_line() {
  local limit=${2:-300} text
  text=$(printf '%s' "$1" | LC_ALL=C tr -c '[:print:]' ' ' | tr -s ' ')
  text=${text# }
  text=${text% }
  if ((${#text} > limit)); then
    text="${text:0:limit}..."
  fi
  printf '%s' "$text"
}

trim() {
  local text=$1
  text=${text#"${text%%[![:space:]]*}"}
  printf '%s' "${text%"${text##*[![:space:]]}"}"
}

die() {
  printf 'Error: %s\n' "$(one_line "$*" 500)" >&2
  exit 1
}

file_size() { wc -c <"$1" | tr -d '[:space:]'; }

file_mode() { stat -c '%a' -- "$1" 2>/dev/null || stat -f '%Lp' -- "$1"; }

# --- validation ------------------------------------------------------------

positive_number() {
  local name=$1 value=$2 upper=$3
  if [[ ! $value =~ $NUMBER_RE ]] ||
    ! awk -v v="$value" -v u="$upper" 'BEGIN { exit !(v > 0 && v <= u) }'; then
    die "$name must be greater than 0 and at most $upper"
  fi
}

require_positive_int() {
  [[ $1 =~ $POSITIVE_INT_RE ]] || die "invalid $2"
}

validate_port() {
  if [[ ! $1 =~ ^[0-9]{1,5}$ ]] || ((10#$1 < 1 || 10#$1 > 65535)); then
    die "port must be an integer from 1 to 65535, got: '$1'"
  fi
}

# Print a hostname or IP that is safe to place in an https URL.
validate_host() {
  local host=$1 literal octet label all_digits=1
  local -a parts
  [[ -n $host && ${#host} -le 253 && $host =~ ^[[:graph:]]+$ ]] ||
    die "controller host must be a hostname or IP address"
  literal=$host
  if [[ $literal == \[*\] ]]; then
    literal=${literal:1:${#literal}-2}
  fi
  if [[ $literal == *:* ]]; then
    [[ ${#literal} -le 45 && $literal =~ ^[0-9A-Fa-f:.]+$ && $literal == *:*:* ]] ||
      die "controller host must be a hostname or IP address"
    printf '[%s]' "$literal"
    return
  fi
  IFS=. read -r -a parts <<<"$host"
  if [[ $host =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
    for octet in "${parts[@]}"; do
      if [[ ! $octet =~ ^(0|[1-9][0-9]{0,2})$ ]] || ((octet > 255)); then
        die "controller host must be a hostname or IP address"
      fi
    done
    printf '%s' "$host"
    return
  fi
  for label in "${parts[@]}"; do
    [[ $label =~ $HOST_LABEL_RE ]] || die "controller host must be a hostname or IP address"
    [[ $label =~ ^[0-9]+$ ]] || all_digits=0
  done
  ((all_digits == 0)) || die "controller host must be a hostname or IP address"
  printf '%s' "$host"
}

# Last path component, restricted to a shell-safe allowlist. Names starting
# with '-' are rejected so the engine cannot read them as flags.
safe_basename() {
  local path=$1 name
  name=${path%/}
  name=${name##*/}
  [[ $name =~ $SAFE_NAME_RE ]] || die "file name must be 1-255 characters of letters, digits, '.', '_' or '-', must start with a letter or digit, and must not include a path: $(one_line "${path:-<empty>}" 120)"
  printf '%s' "$name"
}

require_safe_name() {
  [[ $1 =~ $SAFE_NAME_RE ]] || die "invalid $2"
}

validate_bool() {
  local lowered
  lowered=$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')
  [[ $lowered == true || $lowered == false ]] ||
    usage_error "argument $1: invalid choice: '$2' (choose from 'true', 'false')"
  printf '%s' "$lowered"
}

validate_to_version() {
  local text lowered
  text=$(one_line "$1" 64)
  [[ -n $text ]] || die "to_version must be non-empty"
  lowered=$(printf '%s' "$text" | tr '[:upper:]' '[:lower:]')
  if [[ $lowered == current || $lowered == keep ]]; then
    printf '%s' "$lowered"
    return
  fi
  [[ $text =~ $VERSION_RE ]] || die "to_version contains unsupported characters: '$1'"
  printf '%s' "$text"
}

validate_token() {
  local token=$1 label=$2
  [[ -n $token && ${#token} -le $MAX_TOKEN_CHARS && $token =~ ^[[:graph:]]+$ ]] ||
    die "Could not extract $label from the SSO response"
}

# --- HTTP ------------------------------------------------------------------

# http <context> <body-file> <curl-args...>
# Writes the response body to <body-file> and fails on a non-2xx status.
http() {
  local context=$1 out=$2 code body
  shift 2
  if ! code=$(curl "${CURL_OPTS[@]}" -o "$out" -w '%{http_code}' "$@"); then
    die "$context: HTTP request failed"
  fi
  if [[ ! $code =~ ^2[0-9][0-9]$ ]]; then
    case $context in
      get_ticket | get_token) die "$context returned HTTP $code" ;;
      *) ;;
    esac
    body=$(head -c 4096 "$out" 2>/dev/null || true)
    die "$context returned HTTP $code: $(one_line "${body:-<empty response body>}" 500)"
  fi
}

# api_json <context> <body-file> <curl-args...>
# Like http, then checks the body is a bounded JSON object.
api_json() {
  local context=$1 out=$2 size
  http "$@"
  size=$(file_size "$out")
  ((size > 0)) || die "$context returned an empty body"
  ((size <= MAX_JSON_BYTES)) || die "$context response exceeds $MAX_JSON_BYTES bytes"
  jq -e 'type == "object"' "$out" >/dev/null 2>&1 ||
    die "$context returned invalid JSON or a JSON value that is not an object"
}

# Print a token from a raw-text body or from <field> in a JSON object body.
token_from_body() {
  local file=$1 field=$2
  if jq -e . "$file" >/dev/null 2>&1; then
    jq -r --arg f "$field" 'if type == "object" then (.[$f] // "" | tostring) else "" end' "$file"
  else
    trim "$(head -c "$MAX_JSON_BYTES" "$file")"
  fi
}

# --- authentication --------------------------------------------------------

resolve_credentials() {
  CRED_USER=$(trim "${USERNAME_ARG:-${CW_USERNAME:-}}")
  if [[ -z $CRED_USER ]]; then
    printf 'Username: ' >&2
    IFS= read -r CRED_USER </dev/tty || true
    CRED_USER=$(trim "$CRED_USER")
  fi
  if [[ -z $CRED_USER || ${#CRED_USER} -gt 256 || $CRED_USER =~ [[:cntrl:]] ]]; then
    die "username is empty or invalid"
  fi

  CRED_PASS=${PASSWORD_ARG:-$ENV_PASSWORD}
  unset PASSWORD_ARG ENV_PASSWORD
  if [[ -z $CRED_PASS ]]; then
    printf 'Password: ' >&2
    IFS= read -rs CRED_PASS </dev/tty || true
    printf '\n' >&2
  fi
  if [[ -z $CRED_PASS || ${#CRED_PASS} -gt 1024 || $CRED_PASS =~ [[:cntrl:]] ]]; then
    die "password is empty or invalid"
  fi
}

# Two-step Crosswork SSO: credentials -> TGT -> JWT.
get_jwt() {
  local ticket token location
  resolve_credentials
  log "Authenticating to $HOST..."

  http get_ticket "$WORK/ticket.body" -D "$WORK/ticket.hdr" -X POST \
    --data-urlencode "username=$CRED_USER" \
    --data-urlencode "password@-" \
    "$BASE/crosswork/sso/v1/tickets" < <(printf '%s' "$CRED_PASS")
  unset CRED_PASS

  location=$(awk 'tolower($0) ~ /^location:/ { sub(/^[^:]*:[ \t]*/, ""); print }' \
    "$WORK/ticket.hdr" | tr -d '\r' | tail -n 1)
  if [[ -n $location ]]; then
    while [[ $location == */ ]]; do location=${location%/}; done
    ticket=${location##*/}
  else
    ticket=$(token_from_body "$WORK/ticket.body" ticket)
  fi
  validate_token "$ticket" ticket

  http get_token "$WORK/token.body" -X POST \
    --data-urlencode "tgt@-" \
    --data-urlencode "service=$BASE/app-dashboard" \
    "$BASE/crosswork/sso/v2/tickets/jwt" < <(printf '%s' "$ticket")
  token=$(token_from_body "$WORK/token.body" token)
  rm -f "$WORK/ticket.hdr" "$WORK/ticket.body" "$WORK/token.body"
  validate_token "$token" token
  TOKEN=$token
}

read_jwt_file() {
  local path=$1 mode size
  [[ -f $path ]] || die "JWT file not found: $path"
  mode=$(file_mode "$path")
  if ((8#${mode: -2} != 0)); then
    log "WARNING: JWT file $path is readable by group or others; chmod 600 $path"
  fi
  size=$(file_size "$path")
  ((size <= MAX_TOKEN_CHARS)) || die "JWT file is too large: $path"
  TOKEN=$(tr -d '[:space:]' <"$path")
  [[ -n $TOKEN ]] || die "JWT file is empty: $path"
  log "Using JWT from $path"
  validate_token "$TOKEN" JWT
}

default_jwt_path() {
  local name=$HOST_ARG
  if [[ $name == \[*\] ]]; then
    name=${name:1:${#name}-2}
  fi
  [[ -n $name && $name != */* && $name != . && $name != .. ]] ||
    die "controller host must be a hostname or IP address"
  printf '%s/.crosswork/%s.jwt' "$HOME" "$name"
}

resolve_token() {
  local default_jwt=""
  if [[ -z $JWT_ARG && -z $USERNAME_ARG && -z $PASSWORD_ARG ]]; then
    default_jwt=$(default_jwt_path)
  fi
  if [[ -n $JWT_ARG ]]; then
    read_jwt_file "${JWT_ARG/#\~/$HOME}"
  elif [[ -n $default_jwt && -f $default_jwt ]]; then
    read_jwt_file "$default_jwt"
  else
    get_jwt
  fi
  printf 'Authorization: Bearer %s\n' "$TOKEN" >"$AUTH_HDR"
  unset TOKEN
}

# --- userspace files -------------------------------------------------------

check_upload_size() {
  local path=$1 size
  [[ -f $path ]] || die "file not found: $path"
  size=$(file_size "$path")
  ((size > 0)) || die "refusing to upload an empty file: $(basename -- "$path")"
  ((size <= MAX_TRANSFER_BYTES)) || die "file exceeds $MAX_TRANSFER_BYTES bytes: $(basename -- "$path")"
}

# Print "<fileId> <storedName>" from a file/import response.
parse_import_file_info() {
  local resp=$1 fallback=$2 info id name
  info=$(jq -r '
    .data as $d
    | (if ($d | type) == "object" then $d.additionalInfo else null end) as $x
    | (if ($x | type) == "string" and ($x | test("\\S")) then ($x | fromjson) else $x end) as $extra
    | if ($extra | type) == "object" and $extra.fileId != null then $extra
      elif ($d | type) == "object" and $d.fileId != null then $d
      else empty end
    | "\(.fileId) \(.fileName // "" | tostring)"' "$resp" 2>/dev/null) ||
    die "import response additionalInfo is not valid JSON"
  [[ -n $info ]] || die "import response missing fileId"
  id=${info%% *}
  name=${info#* }
  [[ -n $name ]] || name=$fallback
  require_positive_int "$id" fileId
  require_safe_name "$name" fileName
  printf '%s %s' "$id" "$name"
}

# import_to_userspace <path> <name> <category> -> prints "<fileId> <storedName>"
import_to_userspace() {
  local path=$1 name=$2 category=$3 abs link
  check_upload_size "$path"
  case $path in
    /*) abs=$path ;;
    *) abs=$PWD/$path ;;
  esac
  # Upload through a symlink with a validated name so that curl -F never parses
  # ';' or ',' from an arbitrary local path.
  mkdir -p "$WORK/upload"
  link=$WORK/upload/$name
  rm -f "$link"
  ln -s "$abs" "$link"
  api_json import_to_userspace "$WORK/import.json" -H @"$AUTH_HDR" \
    -H "Accept: application/json" \
    -F "file=@$link;type=application/octet-stream" \
    -F "fileName=$name" \
    -F "category=$category" \
    "$GW/design/v1/file/import"
  rm -f "$link"
  parse_import_file_info "$WORK/import.json" "$name"
}

# search_userspace_file <name> <category> -> prints "<fileId> <storedName>" or nothing
search_userspace_file() {
  local name=$1 category=$2
  api_json search_userspace_file "$WORK/search.json" -H @"$AUTH_HDR" \
    -H "Accept: application/json" --get \
    --data-urlencode "page=0" \
    --data-urlencode "size=100" \
    --data-urlencode "fileCategories=$category" \
    --data-urlencode "fileName=$name" \
    "$GW/design/v1/file/search"
  jq -r --arg n "$name" '
    [.data.content[]? | select(type == "object" and .fileName == $n and .fileId != null)][0]
    // empty | "\(.fileId) \(.fileName)"' "$WORK/search.json"
}

# Import a local plan, or locate an existing userspace PLANFILE by basename.
resolve_userspace_plan() {
  local plan=$1 basename found id
  basename=$(safe_basename "$plan")
  if [[ -f $plan ]]; then
    log "Importing plan $basename to userspace..."
    PLAN_INFO=$(import_to_userspace "$plan" "$basename" PLANFILE)
    return
  fi
  found=$(search_userspace_file "$basename" PLANFILE)
  if [[ -n $found ]]; then
    id=${found%% *}
    require_positive_int "$id" fileId
    require_safe_name "${found#* }" fileName
    PLAN_INFO=$found
    return
  fi
  die "plan file not found locally or in userspace: $(one_line "$plan" 200)"
}

# --- job -------------------------------------------------------------------

stage_job_input() {
  local -a params=()
  local id fs_id
  for id in "$@"; do
    require_positive_int "$id" fileId
    params+=(--data-urlencode "fileId=$id")
  done
  api_json stage_job_input "$WORK/stage.json" -H @"$AUTH_HDR" \
    -H "Accept: application/json" --get "${params[@]}" \
    "$GW/design/v1/jobs/input"
  fs_id=$(jq -r '.data.fsId // "" | tostring' "$WORK/stage.json")
  [[ -n $fs_id && ${#fs_id} -le 1024 && $fs_id =~ ^[[:graph:]]+$ ]] ||
    die "jobs/input returned an invalid fsId"
  FS_ID=$fs_id
}

submit_cli_job() {
  local job_id
  jq -n \
    --arg jobName "$JOB_NAME" \
    --arg fsId "$FS_ID" \
    --arg jobConfig "$JOB_CONFIG" \
    --arg inPlanFileId "$STORED_NAME" \
    --arg requestedProfile "$PROFILE" \
    --arg jobPriority "$PRIORITY" \
    '{jobName: $jobName, jobType: "CLI_JOB", fsId: $fsId, jobConfig: $jobConfig,
      inPlanFileId: $inPlanFileId, requestedProfile: $requestedProfile,
      jobPriority: $jobPriority}' >"$WORK/job.json"
  api_json submit_cli_job "$WORK/submit.json" -H @"$AUTH_HDR" \
    -H "Accept: application/json" -H "Content-Type: application/json" \
    -X POST -d @"$WORK/job.json" "$GW/design/v1/jobs"
  job_id=$(jq -r '
    .data
    | if type != "object" then empty
      elif .jobId != null then .jobId
      elif .id != null then .id
      else ((.message // "" | tostring | capture("Job Id:\\s*(?<id>[0-9]+)"; "i").id) // empty)
      end' "$WORK/submit.json" 2>/dev/null || true)
  [[ -n $job_id ]] || die "could not parse job id from the jobs response"
  require_positive_int "$job_id" jobId
  JOB_ID=$job_id
}

poll_job() {
  local start now seen status last_status="" detail filter
  filter=$(jq -n --arg id "$JOB_ID" \
    '{filters: [{condition: {column: "jobId", operator: "EQUALS", value: $id}, conjuction: "AND"}]}')
  printf '%s' "$filter" >"$WORK/filter.json"
  start=$(date +%s)
  while :; do
    now=$(date +%s)
    awk -v e="$((now - start))" -v t="$JOB_TIMEOUT" 'BEGIN { exit !(e < t) }' || break
    api_json poll_job "$WORK/poll.json" -H @"$AUTH_HDR" \
      -H "Accept: application/json" -H "Content-Type: application/json" \
      -X POST -d @"$WORK/filter.json" \
      "$GW/design/v1/jobs/search?page=0&size=1"
    jq -c '.data.content[0] // empty | select(type == "object")' "$WORK/poll.json" >"$WORK/jobstate.json"
    seen=$(jq -r '.jobId // empty' "$WORK/jobstate.json")
    if [[ -n $seen && $seen == "$JOB_ID" ]]; then
      status=$(one_line "$(jq -r '.jobStatus // "" | tostring' "$WORK/jobstate.json")" 40)
      if [[ $status != "$last_status" ]]; then
        log "Job $JOB_ID status: $status"
        last_status=$status
      fi
      case $status in
        COMPLETED | FAILED | ABORTED | CANCELLED)
          JOB_STATUS=$status
          return
          ;;
        *) ;;
      esac
    fi
    sleep "$POLL_INTERVAL"
  done
  detail=""
  [[ -z $last_status ]] || detail=" (last status $last_status)"
  die "timed out waiting for job $JOB_ID after ${JOB_TIMEOUT}s$detail"
}

# extract_member <tar> <basename> <dest>
# Writes one regular-file member matched by basename. Absolute paths and '..'
# components are ignored; the archive is never extracted to disk as a whole.
extract_member() {
  local tarf=$1 wanted=$2 dest=$3 i name normalized base type size skip part
  local -a names=() types=() parts=()
  if ! tar -tf "$tarf" >"$WORK/tar.list" 2>/dev/null ||
    ! tar -tvf "$tarf" >"$WORK/tar.vlist" 2>/dev/null; then
    die "job result is not a readable tar archive"
  fi
  while IFS= read -r name; do names+=("$name"); done <"$WORK/tar.list"
  while IFS= read -r name; do types+=("${name:0:1}"); done <"$WORK/tar.vlist"
  ((${#names[@]} <= MAX_TAR_MEMBERS)) || die "result tar has too many members"
  # Names and types are paired by line; a member name containing a newline
  # would shift the pairing.
  ((${#names[@]} == ${#types[@]})) || die "result tar has an unsupported member name"

  for ((i = 0; i < ${#names[@]}; i++)); do
    name=${names[i]}
    normalized=${name//\\//}
    [[ $normalized != /* ]] || continue
    IFS=/ read -r -a parts <<<"$normalized"
    base=""
    skip=0
    for part in "${parts[@]}"; do
      case $part in
        "" | .) ;;
        ..) skip=1 ;;
        *) base=$part ;;
      esac
    done
    ((skip == 0)) && [[ $base == "$wanted" ]] || continue
    type=${types[i]:-}
    [[ $type == - ]] || die "'$wanted' is not a regular file in the result tar"
    [[ $name != *[\*\?\[\\]* ]] || die "'$wanted' has an unsupported path in the result tar"
    { tar -xOf "$tarf" "$name" 2>/dev/null || true; } |
      head -c "$((MAX_TRANSFER_BYTES + 1))" >"$dest"
    size=$(file_size "$dest")
    ((size > 0 && size <= MAX_TRANSFER_BYTES)) ||
      die "'$wanted' in the result tar is empty or exceeds the size limit"
    return
  done
  die "'$wanted' not found in job result tar"
}

export_converted_member() {
  local tar_id=$1 size
  require_positive_int "$tar_id" fileId
  http export_userspace_file "$WORK/result.tar" -H @"$AUTH_HDR" -H "Accept: */*" \
    --max-filesize "$MAX_TRANSFER_BYTES" --get --data-urlencode "fileId=$tar_id" \
    "$GW/design/v1/file/export"
  size=$(file_size "$WORK/result.tar")
  ((size <= MAX_TRANSFER_BYTES)) ||
    die "export_userspace_file response exceeds $MAX_TRANSFER_BYTES bytes"
  if [[ -n $KEEP_TAR ]]; then
    (umask "$USER_UMASK" && mkdir -p -- "$(dirname -- "$KEEP_TAR")" &&
      cat "$WORK/result.tar" >"$KEEP_TAR") || die "could not write $KEEP_TAR"
    log "Saved result tar to $KEEP_TAR"
  fi
  extract_member "$WORK/result.tar" "$ENGINE_OUT" "$WORK/converted"
}

write_converted_output() {
  local size
  if [[ $OUT_FILE == - ]]; then
    cat "$WORK/converted"
    return
  fi
  size=$(file_size "$WORK/converted")
  (umask "$USER_UMASK" && mkdir -p -- "$(dirname -- "$OUT_FILE")" &&
    cat "$WORK/converted" >"$OUT_FILE") || die "could not write $OUT_FILE"
  log "Wrote converted plan to $OUT_FILE ($size bytes)"
}

# --- options ---------------------------------------------------------------

# Basename written on the engine; derived from -out-file or the plan.
engine_out_file_name() {
  local plan_name stem suffix
  if [[ $OUT_FILE == - ]]; then
    plan_name=$(safe_basename "$PLAN_FILE")
    if [[ $plan_name == *.* && -n ${plan_name##*.} ]]; then
      stem=${plan_name%.*}
      suffix=.${plan_name##*.}
    else
      stem=${plan_name%.}
      suffix=.txt
    fi
    safe_basename "${stem:-converted}_converted$suffix"
  else
    safe_basename "$OUT_FILE"
  fi
}

# jobConfig.options for native mate_convert. -plan-file and -out-file are
# injected by the engine from inPlanFileId and jobConfig.outFile.
build_mate_convert_options() {
  local -a args=()
  local i value
  if [[ -n $TO_VERSION ]]; then
    value=$(validate_to_version "$TO_VERSION")
    args+=(-to-version "$value")
  fi
  [[ -z $VERSION_TYPE ]] || args+=(-version-type "$VERSION_TYPE")
  [[ -z $INCLUDE_NETINT ]] || args+=(-include-netint "$INCLUDE_NETINT")
  [[ -z $SIMPLE_TXT ]] || args+=(-simple-txt-out-file "$SIMPLE_TXT")
  if [[ -n $VERBOSITY ]]; then
    if [[ ! $VERBOSITY =~ ^[0-9]{1,2}$ ]] || ((10#$VERBOSITY < 1 || 10#$VERBOSITY > 60)); then
      die "verbosity must be an integer between 1 and 60"
    fi
    args+=(-verbosity "$((10#$VERBOSITY))")
  fi
  if [[ -n $LOG_FILE ]]; then
    value=$(safe_basename "$LOG_FILE")
    args+=(-log-file "$value")
  fi
  [[ -z $SUPPRESS_PROGRESS ]] || args+=(-suppress-progress "$SUPPRESS_PROGRESS")
  if [[ -n $OPTIONS_FILE ]]; then
    value=$(safe_basename "$OPTIONS_FILE")
    args+=(-options-file "$value")
  fi
  [[ -z $NO_GLOBAL_OPTIONS ]] || args+=(-no-global-options "$NO_GLOBAL_OPTIONS")

  for ((i = 1; i < ${#args[@]}; i += 2)); do
    [[ ${args[i]} =~ $SAFE_NAME_RE ]] || die "refusing an unsafe mate_convert option"
  done
  OPTIONS_STR=${args[*]:-}
  ((${#OPTIONS_STR} <= 4000)) || die "mate_convert options are too long"
}

# --- argument parsing ------------------------------------------------------

HOST_ARG=${CW_HOST:-}
PORT=$DEFAULT_PORT
USERNAME_ARG=""
PASSWORD_ARG=""
# Keep the password out of the environment inherited by curl, jq, and tar.
ENV_PASSWORD=${CW_PASSWORD:-}
unset CW_PASSWORD
JWT_ARG=""
HTTP_TIMEOUT=$DEFAULT_TIMEOUT
INSECURE=0
JOB_NAME=""
PROFILE=$DEFAULT_PROFILE
PRIORITY=HIGH
POLL_INTERVAL=$DEFAULT_POLL_INTERVAL
JOB_TIMEOUT=$DEFAULT_JOB_TIMEOUT
KEEP_TAR=""
PLAN_FILE=""
OUT_FILE=""
TO_VERSION=""
VERSION_TYPE=""
INCLUDE_NETINT=""
SIMPLE_TXT=""
VERBOSITY=""
LOG_FILE=""
SUPPRESS_PROGRESS=""
OPTIONS_FILE=""
NO_GLOBAL_OPTIONS=""
HAVE_PLAN=0
HAVE_OUT=0

while (($#)); do
  arg=$1
  has_inline=0
  if [[ $arg == -*=* ]]; then
    inline=${arg#*=}
    arg=${arg%%=*}
    has_inline=1
  fi
  case $arg in
    -h | --help | -k | --insecure)
      ((has_inline == 0)) || usage_error "argument $arg: ignored explicit argument '$inline'"
      case $arg in
        -h | --help)
          help_text
          exit 0
          ;;
        *) INSECURE=1 ;;
      esac
      shift
      continue
      ;;
  esac
  if ((has_inline)); then
    value=$inline
    shift
  else
    (($# >= 2)) || usage_error "argument $arg: expected one argument"
    value=$2
    shift 2
  fi
  case $arg in
    --ip) HOST_ARG=$value ;;
    --port) PORT=$value ;;
    -u | --username) USERNAME_ARG=$value ;;
    -p | --password) PASSWORD_ARG=$value ;;
    -j | --jwt) JWT_ARG=$value ;;
    --http-timeout) HTTP_TIMEOUT=$value ;;
    --job-name) JOB_NAME=$value ;;
    --profile) PROFILE=$value ;;
    --job-priority) PRIORITY=$value ;;
    --poll-interval) POLL_INTERVAL=$value ;;
    --timeout) JOB_TIMEOUT=$value ;;
    --keep-tar) KEEP_TAR=$value ;;
    -plan-file | --plan-file)
      PLAN_FILE=$value
      HAVE_PLAN=1
      ;;
    -out-file | --out-file)
      OUT_FILE=$value
      HAVE_OUT=1
      ;;
    -to-version | --to-version) TO_VERSION=$value ;;
    -version-type | --version-type)
      [[ $value == release || $value == schema ]] ||
        usage_error "argument $arg: invalid choice: '$value' (choose from 'release', 'schema')"
      VERSION_TYPE=$value
      ;;
    -include-netint | --include-netint) INCLUDE_NETINT=$(validate_bool "$arg" "$value") ;;
    -simple-txt-out-file | --simple-txt-out-file) SIMPLE_TXT=$(validate_bool "$arg" "$value") ;;
    -verbosity | --verbosity) VERBOSITY=$value ;;
    -log-file | --log-file) LOG_FILE=$value ;;
    -suppress-progress | --suppress-progress) SUPPRESS_PROGRESS=$(validate_bool "$arg" "$value") ;;
    -options-file | --options-file) OPTIONS_FILE=$value ;;
    -no-global-options | --no-global-options) NO_GLOBAL_OPTIONS=$(validate_bool "$arg" "$value") ;;
    *) usage_error "unrecognized arguments: $arg" ;;
  esac
done

((HAVE_PLAN && HAVE_OUT)) || usage_error "the following arguments are required: -plan-file, -out-file"
[[ -n $HOST_ARG ]] || usage_error "--ip is required (or set CW_HOST)"
[[ $PLAN_FILE != - ]] ||
  usage_error "-plan-file '-' (stdin) is not supported; provide a local path or userspace filename"
[[ -n $OUT_FILE ]] || die "invalid out_file"
if [[ -n $PASSWORD_ARG ]]; then
  log "WARNING: --password exposes the password in the process list; prefer a prompt or CW_PASSWORD"
fi

for tool in curl jq tar; do
  command -v "$tool" >/dev/null 2>&1 || die "required command not found: $tool"
done

HOST=$(validate_host "$HOST_ARG")
validate_port "$PORT"
[[ $HTTP_TIMEOUT =~ ^[0-9]+$ ]] || usage_error "argument --http-timeout: invalid int value: '$HTTP_TIMEOUT'"
positive_number http_timeout "$HTTP_TIMEOUT" "$MAX_HTTP_TIMEOUT"
PROFILE=$(one_line "$PROFILE" 64)
[[ $PROFILE =~ $PROFILE_RE ]] ||
  die "requestedProfile must match '<N>m vCPU, <N>Gi Mem', got: '$PROFILE'"
PRIORITY=$(printf '%s' "$PRIORITY" | tr '[:lower:]' '[:upper:]')
case $PRIORITY in
  LOW | MEDIUM | HIGH) ;;
  *) die "job_priority must be one of LOW, MEDIUM, HIGH, got: '$PRIORITY'" ;;
esac
positive_number poll_interval "$POLL_INTERVAL" "$MAX_POLL_INTERVAL"
positive_number timeout "$JOB_TIMEOUT" "$MAX_JOB_TIMEOUT"

ENGINE_OUT=$(engine_out_file_name)
build_mate_convert_options
JOB_CONFIG=$(jq -cn --arg outFile "$ENGINE_OUT" --arg options "$OPTIONS_STR" \
  '{scriptType: "CLI", script: "mate_convert", outFile: $outFile, options: $options}')
if [[ -z $JOB_NAME ]]; then
  JOB_NAME="CLI: mate_convert $(safe_basename "$PLAN_FILE") -> $ENGINE_OUT"
fi
JOB_NAME=$(one_line "$JOB_NAME" 200)
[[ -n $JOB_NAME ]] || die "job name is empty"

if ((INSECURE)); then
  log "WARNING: SSL verification disabled"
fi

# --- main ------------------------------------------------------------------

BASE="https://$HOST:$PORT"
GW="$BASE/cp/apiGateway"
# -q must stay first: it stops ~/.curlrc from overriding TLS and redirect settings.
CURL_OPTS=(-q -sS --proto "=https" --proto-redir "=https" --tlsv1.2 --max-redirs 0
  --connect-timeout "$HTTP_TIMEOUT" --speed-limit 1 --speed-time "$HTTP_TIMEOUT")
((INSECURE == 0)) || CURL_OPTS+=(-k)

USER_UMASK=$(umask)
umask 077
WORK=$(mktemp -d "${TMPDIR:-/tmp}/mate_convert.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
AUTH_HDR=$WORK/auth.hdr

resolve_token

resolve_userspace_plan "$PLAN_FILE"
FILE_ID=${PLAN_INFO%% *}
STORED_NAME=${PLAN_INFO#* }
STAGE_IDS=("$FILE_ID")
if [[ -n $OPTIONS_FILE && -f $OPTIONS_FILE ]]; then
  opt_name=$(safe_basename "$OPTIONS_FILE")
  log "Importing $opt_name to userspace..."
  opt_info=$(import_to_userspace "$OPTIONS_FILE" "$opt_name" OTHERS)
  STAGE_IDS+=("${opt_info%% *}")
fi

log "Staging job input (fileId=$FILE_ID)..."
stage_job_input "${STAGE_IDS[@]}"

log "Submitting CLI_JOB mate_convert (fsId=$(one_line "$FS_ID" 80))..."
submit_cli_job
log "Submitted job $JOB_ID"

poll_job
TAR_ID=$(jq -r '.outputTarFileId // 0 | tostring' "$WORK/jobstate.json")
[[ $TAR_ID =~ ^[0-9]+$ ]] || TAR_ID=0
if [[ $JOB_STATUS != COMPLETED || $TAR_ID == 0 ]]; then
  result=$(one_line "$(jq -r '.jobResult // "" | if type == "string" then . else tojson end' "$WORK/jobstate.json")" 300)
  detail=""
  [[ -z $result ]] || detail=" jobResult=$result"
  die "job $JOB_ID ended with status=$JOB_STATUS outputTarFileId=$TAR_ID$detail"
fi

log "Exporting result tar fileId=$TAR_ID..."
export_converted_member "$TAR_ID"
write_converted_output
