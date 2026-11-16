"""Copy IPv6 IGP/TE metrics to IP metrics in a plan file

Used as a Crosswork Planning external script. The Collector framework
passes positional arguments: base planfile path, output planfile path,
and possibly extra args (ignored).

For reference please see:
https://www.cisco.com/c/en/us/td/docs/cloud-systems-management/crosswork-planning/7-2/setup-guide/cisco-crosswork-planning-7-2-collection-setup-and-administration/m-collectors-in-cp.html#run-external-scripts

"""

import argparse
import logging
import os
import platform
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Dict, Iterator, Optional

ENV_LOG_LEVEL = "CW_LOG_LEVEL"
SCRIPT_VERSION = "2.0"
CHANGE_LOG_LIMIT = 50  # Per-interface change lines logged at INFO; the rest only with --debug

EXIT_CODES = {
    "unknown": 1,
    "config": 2,
    "parse": 7,
    "io": 9,
    "dependency": 10,
}

RUN_ID = uuid.uuid4().hex[:8]

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised when script arguments are inconsistent."""


class PlanError(RuntimeError):
    """Raised when the OPM library fails to load or write a plan."""
    def __init__(self, message: str, error_class: str):
        super().__init__(message)
        self.error_class = error_class


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = RUN_ID
        return True


class _UTCFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z"


def setup_logging(debug: bool) -> None:
    """Log UTC timestamps, level, and run ID on every line."""
    level_name = "DEBUG" if debug else os.environ.get(ENV_LOG_LEVEL, "INFO").upper()
    level = getattr(logging, level_name, None)
    invalid_level = not isinstance(level, int)
    if invalid_level:
        level = logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_UTCFormatter("%(asctime)s %(levelname)-5s run=%(run_id)s %(message)s"))
    handler.addFilter(_RunIdFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    if invalid_level:
        logger.warning(kv(phase="start", invalid_log_level=level_name, using="INFO"))


def kv(**fields: object) -> str:
    """Format fields as key=value pairs; None becomes n/a, values with spaces are quoted."""
    parts = []
    for key, value in fields.items():
        if value is None:
            text = "n/a"
        elif isinstance(value, bool):
            text = str(value).lower()
        else:
            text = str(value)
        if text == "" or any(c in text for c in ' \t\n"'):
            text = '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
        parts.append(f"{key}={text}")
    return " ".join(parts)


class RunStats:
    """Durations and fields collected from each phase, used for the summary line."""
    def __init__(self) -> None:
        self.durations: Dict[str, int] = {}
        self.fields: Dict[str, Dict[str, object]] = {}


def classify_error(exc: BaseException) -> str:
    """Map an exception to an error_class used in logs and exit codes."""
    if isinstance(exc, ConfigError):
        return "config"
    if isinstance(exc, PlanError):
        return exc.error_class
    if isinstance(exc, ImportError):
        return "dependency"
    if isinstance(exc, OSError):
        return "io"
    return "unknown"


@contextmanager
def phase(stats: RunStats, name: str, **fields: object) -> Iterator[Dict[str, object]]:
    """Time a phase and log one status line; the yielded dict adds fields to that line."""
    extra: Dict[str, object] = dict(fields)
    stats.fields[name] = extra
    logger.debug(kv(phase=name, status="start", **fields))
    start = time.monotonic()
    try:
        yield extra
    except Exception as exc:
        stats.durations[name] = int((time.monotonic() - start) * 1000)
        line = {"phase": name, "status": "failed", "error_class": classify_error(exc),
                "duration_ms": stats.durations[name]}
        line.update({k: v for k, v in extra.items() if k not in line})
        line["error"] = str(exc)
        logger.error(kv(**line))
        try:
            exc._logged = True  # type: ignore[attr-defined]
        except AttributeError:
            pass
        raise
    stats.durations[name] = int((time.monotonic() - start) * 1000)
    line = {"phase": name, "status": "ok", "duration_ms": stats.durations[name]}
    line.update({k: v for k, v in extra.items() if k not in line})
    logger.info(kv(**line))


def load_plan(src: str):
    """Open *src* with the OPM library and return the Network object."""
    from com.cisco.wae.opm.network import Network

    try:
        return Network(src)
    except Exception as exc:
        raise PlanError(f"could not load plan {src}: {exc}", "parse") from exc


def copy_ipv6_metrics(network, ph: Dict[str, object]) -> None:
    """Copy IPv6 IGP/TE metrics to IP metrics on every interface and record counts in *ph*."""
    counts = {
        "nodes": 0, "interfaces": 0, "v6_igp_present": 0, "v6_te_present": 0,
        "igp_updated": 0, "te_updated": 0, "interfaces_updated": 0, "interface_errors": 0,
    }
    changes_logged = 0
    first_error: Optional[str] = None

    for node in network.model.nodes:
        counts["nodes"] += 1
        for interface in node.interfaces:
            counts["interfaces"] += 1
            try:
                # rpc_record is used for v6 metrics since this is not exposed in OPM on node.interfaces
                rec = interface.rpc_record
                v6igp = rec.ipv6IGPMetric
                v6te = rec.ipv6TEMetric
                changed = []
                for metric, v6_value, attr in (("igp", v6igp, "igp_metric"), ("te", v6te, "te_metric")):
                    if not isinstance(v6_value, int) or isinstance(v6_value, bool):
                        continue
                    counts[f"v6_{metric}_present"] += 1
                    old_value = getattr(interface, attr)
                    if v6_value == old_value:
                        continue
                    setattr(interface, attr, v6_value)
                    counts[f"{metric}_updated"] += 1
                    changed.append((metric, old_value, v6_value))
            except Exception as exc:  # isolate one bad interface; keep processing the rest
                counts["interface_errors"] += 1
                if first_error is None:
                    first_error = f"{node.name} {interface.name}: {exc}"
                    logger.warning(kv(phase="copy_metrics", warning="could not read or update interface metrics",
                                      node=node.name, interface=interface.name, error=str(exc)))
                else:
                    logger.debug(kv(phase="copy_metrics", node=node.name, interface=interface.name, error=str(exc)))
                continue

            if changed:
                counts["interfaces_updated"] += 1
            for metric, old_value, new_value in changed:
                level = logging.INFO if changes_logged < CHANGE_LOG_LIMIT else logging.DEBUG
                logger.log(level, kv(phase="copy_metrics", node=node.name, interface=interface.name,
                                     metric=metric, old=old_value, new=new_value))
                changes_logged += 1

    ph.update(counts)
    if changes_logged > CHANGE_LOG_LIMIT:
        ph["change_lines_suppressed"] = changes_logged - CHANGE_LOG_LIMIT
    if first_error is not None:
        ph["first_error"] = first_error

    if counts["nodes"] == 0:
        logger.warning(kv(phase="copy_metrics", warning="plan has no nodes; upstream collection may be empty"))
    elif counts["interfaces"] == 0:
        logger.warning(kv(phase="copy_metrics", warning="plan has nodes but no interfaces"))
    elif counts["v6_igp_present"] == 0 and counts["v6_te_present"] == 0:
        logger.warning(kv(phase="copy_metrics",
                          warning="no interface has IPv6 IGP or TE metrics; check the upstream collector collects IPv6"))


def write_plan(network, dest: str) -> None:
    """Write *network* to *dest*."""
    try:
        network.write(dest)
    except Exception as exc:
        raise PlanError(f"could not write plan {dest}: {exc}", "io") from exc
    if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        raise PlanError(f"network.write returned but {dest} is missing or empty", "io")


def run_update(args: argparse.Namespace, stats: RunStats) -> None:
    """Load the plan, copy IPv6 metrics to IP metrics, and write the result."""
    logger.info(kv(
        phase="start", script="update_interface_metric.py", script_version=SCRIPT_VERSION,
        python=platform.python_version(), host=socket.gethostname(), pid=os.getpid(), cwd=os.getcwd(),
        src=args.base_planfile, out=args.output_planfile, extra_args=len(args.extra),
    ))

    if not os.path.isfile(args.base_planfile):
        raise ConfigError(f"Base plan file not found: {args.base_planfile}")
    out_dir = os.path.dirname(os.path.abspath(args.output_planfile))
    if not os.path.isdir(out_dir):
        raise ConfigError(f"Output directory does not exist: {out_dir}")

    with phase(stats, "load_plan", path=args.base_planfile) as ph:
        ph["bytes"] = os.path.getsize(args.base_planfile)
        network = load_plan(args.base_planfile)

    with phase(stats, "copy_metrics") as ph:
        copy_ipv6_metrics(network, ph)

    with phase(stats, "write_plan", path=args.output_planfile) as ph:
        write_plan(network, args.output_planfile)
        ph["out_bytes"] = os.path.getsize(args.output_planfile)


ERROR_HINTS = {
    "config": "check the base and output plan file paths passed by the Collector framework",
    "dependency": "run inside Crosswork Planning, where the OPM Python library (com.cisco.wae.opm) is available",
    "parse": "the base plan could not be opened; check the upstream collector output",
    "io": "check output file path, permissions, and free disk space",
}


def log_summary(stats: RunStats, status: str, exit_code: int, error_class: Optional[str],
                total_ms: int) -> None:
    fields: Dict[str, object] = {"phase": "summary", "status": status, "exit": exit_code}
    if error_class:
        fields["error_class"] = error_class
    fields["total_ms"] = total_ms
    for name in ("load_plan", "copy_metrics", "write_plan"):
        if name in stats.durations:
            fields[f"{name}_ms"] = stats.durations[name]
    fields["plan_bytes"] = stats.fields.get("load_plan", {}).get("bytes")
    copy = stats.fields.get("copy_metrics", {})
    for key in ("nodes", "interfaces", "igp_updated", "te_updated", "interfaces_updated", "interface_errors"):
        fields[key] = copy.get(key)
    fields["out_bytes"] = stats.fields.get("write_plan", {}).get("out_bytes")
    level = {"ok": logging.INFO, "partial": logging.WARNING}.get(status, logging.ERROR)
    logger.log(level, kv(**fields))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy IPv6 IGP/TE metrics to IP IGP/TE metrics in a plan file",
        epilog=f"Log verbosity: --debug, or set {ENV_LOG_LEVEL} (DEBUG, INFO, WARNING, ERROR).",
    )
    parser.add_argument("base_planfile", help="Source plan file")
    parser.add_argument("output_planfile", help="Output plan file")
    parser.add_argument("extra", nargs="*", help="Extra Collector framework arguments (ignored)")
    parser.add_argument("--debug", action="store_true",
                        help=f"Enable DEBUG logging (overrides {ENV_LOG_LEVEL})")

    args = parser.parse_args()
    setup_logging(args.debug)

    stats = RunStats()
    status = "interrupted"
    exit_code = 130
    error_class: Optional[str] = None
    start = time.monotonic()
    try:
        run_update(args, stats)
        interface_errors = stats.fields.get("copy_metrics", {}).get("interface_errors")
        status, exit_code = ("partial" if interface_errors else "ok"), 0
    except Exception as exc:
        error_class = classify_error(exc)
        status, exit_code = "failed", EXIT_CODES[error_class]
        fields: Dict[str, object] = {"phase": "error", "error_class": error_class}
        if not getattr(exc, "_logged", False):
            fields["error"] = str(exc)
        hint = ERROR_HINTS.get(error_class)
        if hint:
            fields["hint"] = hint
        logger.error(kv(**fields))
        logger.debug("Traceback for run %s", RUN_ID, exc_info=True)
    finally:
        try:
            log_summary(stats, status, exit_code, error_class, int((time.monotonic() - start) * 1000))
        except Exception:  # logging must never change the exit code
            pass
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
