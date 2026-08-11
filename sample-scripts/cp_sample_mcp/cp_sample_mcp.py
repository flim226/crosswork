#!/usr/bin/env python3
"""
Crosswork Planning Simulation MCP Server (FastMCP).

Exposes OPM/Design RPC simulation tools to LLM clients over stdio (local) or
Streamable HTTP (remote). Upload plan content at runtime, then run route simulation,
link failure analysis, simulation analysis, and traffic growth forecasting.

Quick start (local / Cursor):
  pip install -r requirements-mcp.txt
  export CARIDEN_HOME=/path/to/cw-planning   # optional if ./cw-planning exists
  python3 cp_sample_mcp.py --transport stdio

Remote HTTP:
  export MCP_API_TOKEN=your-secret-token
  python3 cp_sample_mcp.py --transport http --allow-remote

See cp_sample_mcp.md for full documentation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCRIPT_DIR = Path(__file__).resolve().parent

CONFIG: dict[str, Any] = {
    "crosswork": {
        "host": "198.18.134.229",
        "design_api_port": 30744,
        "protocol": "ssl",
    },
    "opm": {
        "cariden_home": None,
        "design_api_timeout_s": 120,
    },
    "plan": {
        "staging_dir": None,
        "max_bytes": 52_428_800,
        "default_plan": None,
        "allowed_root": None,
        "ttl_hours": None,
        "large_upload_confirm_bytes": 10_485_760,
    },
    "mcp": {
        "host": "127.0.0.1",
        "port": 8080,
        "path": "/mcp",
        "rate_limit_rps": 5.0,
        "rate_limit_burst": 10,
    },
}

FailureSetLiteral = Literal[
    "nodes",
    "sites",
    "circuits",
    "ports",
    "portcircuits",
    "srlgs",
    "external_endpoint_members",
    "parallel_circuits",
]
GrowthMethodLiteral = Literal["COMPOUND", "SIMPLE"]
MetricLiteral = Literal["igp", "bgp", "te", "latency"]
LOCAL_OWNER_ID = "local"

FAILURE_SET_ALLOWLIST = frozenset(
    {
        "nodes",
        "sites",
        "circuits",
        "ports",
        "portcircuits",
        "srlgs",
        "external_endpoint_members",
        "parallel_circuits",
    }
)

METRIC_ALLOWLIST = frozenset({"igp", "bgp", "te", "latency"})
SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_opm_lock = threading.Lock()


def _apply_env_overrides() -> None:
    if os.environ.get("CW_HOST"):
        CONFIG["crosswork"]["host"] = os.environ["CW_HOST"]
    if os.environ.get("CW_DESIGN_API_PORT"):
        CONFIG["crosswork"]["design_api_port"] = int(os.environ["CW_DESIGN_API_PORT"])
    if os.environ.get("CARIDEN_HOME"):
        CONFIG["opm"]["cariden_home"] = os.environ["CARIDEN_HOME"]
    if os.environ.get("CW_DEFAULT_PLAN"):
        CONFIG["plan"]["default_plan"] = os.environ["CW_DEFAULT_PLAN"]
    if os.environ.get("CW_PLAN_ROOT"):
        CONFIG["plan"]["allowed_root"] = os.environ["CW_PLAN_ROOT"]
    if os.environ.get("CW_MAX_PLAN_BYTES"):
        CONFIG["plan"]["max_bytes"] = int(os.environ["CW_MAX_PLAN_BYTES"])
    if os.environ.get("CW_PLAN_TTL_HOURS"):
        CONFIG["plan"]["ttl_hours"] = float(os.environ["CW_PLAN_TTL_HOURS"])
    if os.environ.get("MCP_HOST"):
        CONFIG["mcp"]["host"] = os.environ["MCP_HOST"]
    if os.environ.get("MCP_PORT"):
        CONFIG["mcp"]["port"] = int(os.environ["MCP_PORT"])
    if os.environ.get("CW_DESIGN_API_TIMEOUT"):
        CONFIG["opm"]["design_api_timeout_s"] = int(os.environ["CW_DESIGN_API_TIMEOUT"])
    if os.environ.get("MCP_RATE_LIMIT_RPS"):
        CONFIG["mcp"]["rate_limit_rps"] = float(os.environ["MCP_RATE_LIMIT_RPS"])


def _cariden_home() -> Path:
    raw = CONFIG["opm"]["cariden_home"]
    return Path(raw).resolve() if raw else (SCRIPT_DIR / "cw-planning")


def _staging_dir() -> Path:
    raw = CONFIG["plan"]["staging_dir"]
    path = Path(raw).resolve() if raw else (SCRIPT_DIR / ".plan_staging")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _allowed_root() -> Path:
    raw = CONFIG["plan"]["allowed_root"]
    return Path(raw).resolve() if raw else SCRIPT_DIR


def _bootstrap_opm_env() -> Path:
    cariden = _cariden_home()
    if not (cariden / "lib" / "python").is_dir():
        raise RuntimeError(
            f"CARIDEN_HOME not found or invalid: {cariden}. "
            "Set CARIDEN_HOME or place cw-planning/ next to this script."
        )
    os.environ["CARIDEN_HOME"] = str(cariden)
    py_lib = str(cariden / "lib" / "python")
    if py_lib not in sys.path:
        sys.path.insert(0, py_lib)
    ld_parts = [
        str(cariden / "lib"),
        str(cariden / "lib" / "python"),
        os.environ.get("LD_LIBRARY_PATH", ""),
    ]
    existing_py = os.environ.get("PYTHONPATH", "")
    if py_lib not in existing_py.split(os.pathsep):
        os.environ["PYTHONPATH"] = os.pathsep.join(p for p in [existing_py, py_lib] if p)
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(p for p in ld_parts if p)
    return cariden


_apply_env_overrides()
CARIDEN_HOME_PATH = _bootstrap_opm_env()

from com.cisco.wae.design.model import PlanFormat, PlanKey  # noqa: E402
from com.cisco.wae.design.model.traffic import DemandTrafficKey  # noqa: E402
from com.cisco.wae.opm.network import open_plan  # noqa: E402
from com.cisco.wae.opm.network.tools.simulation_analysis import SimulationAnalysis  # noqa: E402
from fastmcp import FastMCP  # noqa: E402
from fastmcp.exceptions import ResourceError, ToolError  # noqa: E402
from fastmcp.server.auth.providers.debug import DebugTokenVerifier  # noqa: E402
from fastmcp.server.dependencies import get_access_token  # noqa: E402
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext  # noqa: E402
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware  # noqa: E402
from Ice import Unset  # noqa: E402

logger = logging.getLogger("cp_sample_mcp")


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger("cp_sample_mcp")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


_configure_logging()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UploadPlanArgs(_StrictModel):
    """Validated arguments for upload_plan."""

    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    encoding: Literal["text", "base64"] = "text"
    plan_id: str | None = Field(default=None, max_length=64)
    validate: bool = True
    confirm: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        base = Path(value).name
        if not base or not SAFE_NAME_RE.match(base):
            raise ValueError(
                "Invalid plan name. Use alphanumeric characters, dots, hyphens, underscores only."
            )
        return base

    @field_validator("plan_id")
    @classmethod
    def _validate_plan_id(cls, value: str | None) -> str | None:
        if value is not None and not SAFE_NAME_RE.match(value):
            raise ValueError("plan_id must be alphanumeric with dots, hyphens, or underscores")
        return value


class DeletePlanArgs(_StrictModel):
    """Validated arguments for delete_uploaded_plan."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=64)
    confirm: bool = False

    @field_validator("plan_id")
    @classmethod
    def _validate_plan_id(cls, value: str) -> str:
        if not SAFE_NAME_RE.match(value):
            raise ValueError("plan_id must be alphanumeric with dots, hyphens, or underscores")
        return value


class SimulationAnalysisArgs(_StrictModel):
    """Validated arguments for run_simulation_analysis."""

    model_config = ConfigDict(extra="forbid")

    plan_ref: str | None = None
    failure_sets: list[FailureSetLiteral] = Field(default_factory=lambda: ["nodes", "circuits"])
    max_fail_per_int: int = Field(default=10, ge=1, le=50)
    limit: int = Field(default=15, ge=1, le=100)


class AuditMiddleware(Middleware):
    """Structured audit logging for tool, resource, and prompt invocations."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        return await self._timed("tool", context.message.name, call_next, context)

    async def on_read_resource(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        uri = getattr(context.message, "uri", "unknown")
        return await self._timed("resource", str(uri), call_next, context)

    async def on_get_prompt(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        name = getattr(context.message, "name", "unknown")
        return await self._timed("prompt", str(name), call_next, context)

    async def _timed(
        self,
        kind: str,
        target: str,
        call_next: CallNext,
        context: MiddlewareContext,
    ) -> Any:
        start = time.perf_counter()
        owner = _owner_id()
        logger.info(
            "mcp_%s_start kind=%s target=%s owner=%s",
            kind,
            kind,
            target,
            owner,
        )
        try:
            result = await call_next(context)
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.info(
                "mcp_%s_ok kind=%s target=%s owner=%s duration_ms=%s",
                kind,
                kind,
                target,
                owner,
                elapsed_ms,
            )
            return result
        except Exception as exc:  # pylint: disable=broad-exception-caught
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.warning(
                "mcp_%s_error kind=%s target=%s owner=%s duration_ms=%s error=%s",
                kind,
                kind,
                target,
                owner,
                elapsed_ms,
                type(exc).__name__,
            )
            raise


def _owner_id() -> str:
    """Scope uploaded plans to the authenticated HTTP client or local stdio session."""
    try:
        token = get_access_token()
        if token and token.token:
            return hashlib.sha256(token.token.encode()).hexdigest()[:16]
        if token and token.client_id:
            return token.client_id
    except (RuntimeError, ValueError, AttributeError):
        pass
    return LOCAL_OWNER_ID


def _fail(message: str, hint: str | None = None) -> NoReturn:
    detail = f"{message} Hint: {hint}" if hint else message
    raise ToolError(detail)


def _resource_fail(message: str) -> NoReturn:
    raise ResourceError(message)


def _audit_event(event: str, **fields: Any) -> None:
    safe = {key: fields[key] for key in sorted(fields) if fields[key] is not None}
    logger.info("audit event=%s %s", event, " ".join(f"{k}={v!r}" for k, v in safe.items()))


@dataclass
class UploadedPlan:
    """Metadata for a plan uploaded through upload_plan."""

    plan_id: str
    name: str
    path: str
    bytes: int
    uploaded_at: str
    encoding: str
    owner_id: str = ""


class PlanRegistry:
    """Server-side staging registry for uploaded plan files."""

    def __init__(self, staging_dir: Path, index_file: Path) -> None:
        self.staging_dir = staging_dir
        self.index_file = index_file
        self._plans: dict[str, UploadedPlan] = {}
        self._load_index()

    def _load_index(self) -> None:
        if not self.index_file.is_file():
            return
        try:
            data = json.loads(self.index_file.read_text(encoding="utf-8"))
            for item in data.get("plans", []):
                item.setdefault("owner_id", "")
                plan = UploadedPlan(**item)
                if Path(plan.path).is_file():
                    self._plans[plan.plan_id] = plan
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    def _save_index(self) -> None:
        payload = {"plans": [asdict(p) for p in self._plans.values()]}
        self.index_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @staticmethod
    def _can_access(plan: UploadedPlan, owner_id: str) -> bool:
        if not plan.owner_id:
            return owner_id == LOCAL_OWNER_ID
        return plan.owner_id == owner_id

    @staticmethod
    def _sanitize_name(name: str) -> str:
        base = Path(name).name
        if not base or not SAFE_NAME_RE.match(base):
            raise ValueError(
                "Invalid plan name. Use alphanumeric characters, dots, hyphens, underscores only."
            )
        return base

    def upload(
        self,
        name: str,
        content: str,
        encoding: str = "text",
        plan_id: str | None = None,
        owner_id: str = LOCAL_OWNER_ID,
    ) -> UploadedPlan:
        """Store plan content on disk and register it for the given owner."""
        safe_name = self._sanitize_name(name)
        if encoding not in ("text", "base64"):
            raise ValueError("encoding must be 'text' or 'base64'")

        if encoding == "text":
            raw = content.encode("utf-8")
        else:
            raw = base64.b64decode(content, validate=True)
        max_bytes = int(CONFIG["plan"]["max_bytes"])
        if len(raw) > max_bytes:
            raise ValueError(f"Plan exceeds max size ({max_bytes} bytes)")

        pid = plan_id or uuid.uuid4().hex[:12]
        if not SAFE_NAME_RE.match(pid):
            raise ValueError("plan_id must be alphanumeric with dots, hyphens, or underscores")
        if pid in self._plans and not self._can_access(self._plans[pid], owner_id):
            raise ValueError(f"plan_id already owned by another client: {pid}")

        plan_dir = self.staging_dir / pid
        plan_dir.mkdir(parents=True, exist_ok=True)
        dest = plan_dir / safe_name
        dest.write_bytes(raw)

        record = UploadedPlan(
            plan_id=pid,
            name=safe_name,
            path=str(dest.resolve()),
            bytes=len(raw),
            uploaded_at=datetime.now(timezone.utc).isoformat(),
            encoding=encoding,
            owner_id=owner_id,
        )
        self._plans[pid] = record
        self._save_index()
        return record

    def get(self, plan_id: str, owner_id: str | None = None) -> UploadedPlan | None:
        """Return a plan record when it exists and the owner may access it."""
        plan = self._plans.get(plan_id)
        if plan is None:
            return None
        if owner_id is not None and not self._can_access(plan, owner_id):
            return None
        return plan

    def contains(self, plan_id: str) -> bool:
        """Return whether a plan id exists in the registry."""
        return plan_id in self._plans

    def list_plans(self, owner_id: str | None = None) -> list[UploadedPlan]:
        """List uploaded plans, optionally scoped to one owner."""
        plans = self._plans.values()
        if owner_id is not None:
            plans = [p for p in plans if self._can_access(p, owner_id)]
        return sorted(plans, key=lambda p: p.uploaded_at, reverse=True)

    def delete(self, plan_id: str, owner_id: str) -> bool:
        """Remove a plan from the registry and delete its staged files."""
        record = self._plans.get(plan_id)
        if record is None or not self._can_access(record, owner_id):
            return False
        self._plans.pop(plan_id, None)
        plan_dir = self.staging_dir / plan_id
        if plan_dir.is_dir():
            for child in plan_dir.iterdir():
                child.unlink(missing_ok=True)
            plan_dir.rmdir()
        self._save_index()
        return True

    def purge_expired(self) -> int:
        """Delete plans older than the configured TTL, if enabled."""
        ttl_hours = CONFIG["plan"]["ttl_hours"]
        if not ttl_hours:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=float(ttl_hours))
        removed = 0
        for plan_id, record in list(self._plans.items()):
            uploaded_at = datetime.fromisoformat(record.uploaded_at)
            if uploaded_at >= cutoff:
                continue
            if self.delete(plan_id, record.owner_id or LOCAL_OWNER_ID):
                removed += 1
        return removed


PLAN_REGISTRY = PlanRegistry(_staging_dir(), _staging_dir() / "index.json")


def ok(**payload: Any) -> dict[str, Any]:
    """Build a standard success response envelope."""
    return {"ok": True, **payload}


def _resolve_plan_ref(plan_ref: str | None, owner_id: str | None = None) -> Path:
    owner = owner_id if owner_id is not None else _owner_id()
    if plan_ref:
        ref = plan_ref.strip()
        if ref.startswith("upload:"):
            ref = ref[len("upload:") :]
        uploaded = PLAN_REGISTRY.get(ref, owner)
        if uploaded:
            return Path(uploaded.path)
        if PLAN_REGISTRY.contains(ref):
            raise ValueError(f"plan_ref not found or not authorized: upload:{ref}")
        candidate = Path(ref)
        if not candidate.is_absolute():
            candidate = (SCRIPT_DIR / candidate).resolve()
        else:
            candidate = candidate.resolve()
        allowed = _allowed_root()
        try:
            candidate.relative_to(allowed)
        except ValueError as exc:
            raise ValueError(f"plan_ref path outside allowed root ({allowed})") from exc
        if not candidate.is_file():
            raise FileNotFoundError(f"Plan not found: {candidate}")
        return candidate

    default = CONFIG["plan"]["default_plan"]
    if default:
        path = Path(default)
        if not path.is_absolute():
            path = (SCRIPT_DIR / path).resolve()
        if path.is_file():
            return path

    fallback = SCRIPT_DIR / "us_wan.txt"
    if fallback.is_file():
        return fallback

    raise ValueError(
        "No plan specified. Call upload_plan first and pass plan_ref, "
        "or set CW_DEFAULT_PLAN / place us_wan.txt next to this script."
    )


def _cw_connection() -> tuple[str, int, str]:
    return (
        CONFIG["crosswork"]["host"],
        int(CONFIG["crosswork"]["design_api_port"]),
        CONFIG["crosswork"]["protocol"],
    )


@contextmanager
def _open_model(plan_path: Path) -> Iterator[Any]:
    host, port, protocol = _cw_connection()
    timeout_s = int(CONFIG["opm"]["design_api_timeout_s"])
    with _opm_lock:
        start = time.perf_counter()
        with open_plan(str(plan_path), host, port, protocol) as network:
            elapsed = time.perf_counter() - start
            if elapsed > timeout_s:
                logger.warning(
                    "design_api_open_slow plan=%s elapsed_s=%.1f timeout_s=%s",
                    plan_path.name,
                    elapsed,
                    timeout_s,
                )
            yield network.model


def _parse_failure_sets(raw: str | list[str]) -> list[str]:
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    else:
        parts = [p.strip().lower() for p in raw if p.strip()]
    unknown = [p for p in parts if p not in FAILURE_SET_ALLOWLIST]
    if unknown:
        allowed = sorted(FAILURE_SET_ALLOWLIST)
        raise ValueError(f"Unknown failure sets: {unknown}. Allowed: {allowed}")
    return parts


def _fmt_iface(iface) -> str:
    return f"{iface.node.name}:{iface.name}"


def _find_circuit_by_nodes(model, node_a: str, node_b: str):
    targets = {node_a, node_b}
    for circuit in model.circuits:
        if not circuit.active:
            continue
        nodes = {circuit.interface_a.node.name, circuit.interface_b.node.name}
        if nodes == targets:
            return circuit
    raise ValueError(f"No active circuit between {node_a} and {node_b}")


def _resolve_failed_circuits(
    model,
    failed_circuits: list[str] | None,
    node_a: str | None,
    node_b: str | None,
):
    circuits = []
    if failed_circuits:
        for spec in failed_circuits:
            found = None
            for circuit in model.circuits:
                if str(circuit) == spec or getattr(circuit, "name", None) == spec:
                    found = circuit
                    break
            if found is None:
                raise ValueError(f"Circuit not found: {spec}")
            circuits.append(found)
    elif node_a and node_b:
        circuits.append(_find_circuit_by_nodes(model, node_a, node_b))
    else:
        raise ValueError("Provide failed_circuits or both node_a and node_b")
    return circuits


@dataclass
class CircuitUtil:
    """Per-circuit utilization snapshot for both interfaces."""

    circuit: str
    node_a: str
    iface_a: str
    util_a: float | None
    traffic_a: float | None
    cap_a: float | None
    node_b: str
    iface_b: str
    util_b: float | None
    traffic_b: float | None
    cap_b: float | None

    @property
    def max_util(self) -> float:
        """Highest simulated utilization across both circuit sides."""
        vals = [u for u in (self.util_a, self.util_b) if u is not None]
        return max(vals) if vals else 0.0

    @property
    def bottleneck_side(self) -> str:
        """Return node:interface for the higher-utilization side."""
        ua = self.util_a or 0.0
        ub = self.util_b or 0.0
        return f"{self.node_a}:{self.iface_a}" if ua >= ub else f"{self.node_b}:{self.iface_b}"

    @property
    def bottleneck_traffic(self) -> float | None:
        """Return traffic on the bottleneck side."""
        ua = self.util_a or 0.0
        ub = self.util_b or 0.0
        return self.traffic_a if ua >= ub else self.traffic_b

    @property
    def bottleneck_capacity(self) -> float | None:
        """Return capacity on the bottleneck side."""
        ua = self.util_a or 0.0
        ub = self.util_b or 0.0
        return self.cap_a if ua >= ub else self.cap_b


def _collect_circuit_utils(model) -> list[CircuitUtil]:
    rows: list[CircuitUtil] = []
    for circuit in model.circuits:
        if not circuit.active:
            continue
        ia = circuit.interface_a
        ib = circuit.interface_b
        rows.append(
            CircuitUtil(
                circuit=str(circuit),
                node_a=ia.node.name,
                iface_a=ia.name,
                util_a=ia.simulated_utilization,
                traffic_a=ia.simulated_traffic,
                cap_a=ia.simulated_capacity or getattr(ia, "configured_capacity", None),
                node_b=ib.node.name,
                iface_b=ib.name,
                util_b=ib.simulated_utilization,
                traffic_b=ib.simulated_traffic,
                cap_b=ib.simulated_capacity or getattr(ib, "configured_capacity", None),
            )
        )
    return rows


def _circuit_utils_to_json(rows: list[CircuitUtil], threshold: float) -> dict[str, Any]:
    oversub = sorted(
        [r for r in rows if r.max_util > threshold],
        key=lambda r: r.max_util,
        reverse=True,
    )
    return {
        "total_circuits": len(rows),
        "oversubscribed_count": len(oversub),
        "oversubscribed": [
            {
                "circuit": r.circuit,
                "max_util_percent": round(r.max_util, 2),
                "bottleneck": r.bottleneck_side,
                "traffic_mbps": r.bottleneck_traffic,
                "capacity_mbps": r.bottleneck_capacity,
            }
            for r in oversub
        ],
    }


def _iface_snapshot(model) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for circuit in model.circuits:
        if not circuit.active:
            continue
        for iface in (circuit.interface_a, circuit.interface_b):
            key = _fmt_iface(iface)
            out[key] = {
                "traffic_mbps": iface.simulated_traffic or 0.0,
                "util_percent": iface.simulated_utilization,
                "capacity_mbps": iface.simulated_capacity or circuit.capacity,
            }
    return out


def _path_of(demand) -> list[str] | None:
    if not demand.routed:
        return None
    return [_fmt_iface(i) for i in demand.route.interfaces]


def _compound_multiplier(growth_percent: float, period_inc: int, num_periods: int) -> float:
    rate = 1.0 + (growth_percent / 100.0)
    return rate ** (period_inc * num_periods)


def _rpc_avg_demand_traffic(rpc_plan) -> float | None:
    netw = rpc_plan.getNetwork()
    dtm = rpc_plan.getTrafficManager().getDemandTrafficManager()
    tl = netw.getTrafficLevelManager().getAllTrafficLevelKeys()[0]
    vals: list[float] = []
    for dkey in netw.getDemandManager().getAllDemandKeys():
        if not netw.getDemandManager().getDemand(dkey).isActive():
            continue
        key = DemandTrafficKey()
        key.dmdKey = dkey
        key.traffLvlKey = tl
        traffic = dtm.getTraffic(key)
        if traffic is not Unset and traffic is not None:
            vals.append(float(traffic))
    return sum(vals) / len(vals) if vals else None


def _apply_compound_growth_to_rpc_plan(
    rpc_plan,
    growth_percent: float,
    period_inc: int,
    num_periods: int,
) -> float:
    factor = _compound_multiplier(growth_percent, period_inc, num_periods)
    netw = rpc_plan.getNetwork()
    dtm = rpc_plan.getTrafficManager().getDemandTrafficManager()
    tl = netw.getTrafficLevelManager().getAllTrafficLevelKeys()[0]
    for dkey in netw.getDemandManager().getAllDemandKeys():
        if not netw.getDemandManager().getDemand(dkey).isActive():
            continue
        key = DemandTrafficKey()
        key.dmdKey = dkey
        key.traffLvlKey = tl
        traffic = dtm.getTraffic(key)
        if traffic is Unset or traffic is None:
            continue
        dtm.setTraffic(key, float(traffic) * factor)
    return factor


def _run_create_growth_plans(
    network,
    server_plan_path: str,
    num_periods: int,
    period_inc: int,
    growth_method: str,
):
    pm = network.service_connection.rpc_plan_manager
    before = {k.id for k in pm.getAllPlanKeys()}
    tool_rpc_plan = pm.newPlanFromFileSystem(server_plan_path)
    tool_network = tool_rpc_plan.getNetwork()
    opts = {
        "plan-file": server_plan_path,
        "create-plans-from": "DEMANDS",
        "num-periods": num_periods,
        "period-inc": period_inc,
        "growth-method": growth_method,
        "run-sim-analysis": False,
    }
    gt = network.rpc_tool_manager.newGenericTool()
    gt.runTool(tool_network, "create_growth_plans", json.dumps(opts))
    return [k.id for k in pm.getAllPlanKeys() if k.id not in before]


def _validate_certs(cariden: Path) -> list[str]:
    cert_dir = cariden / "etc" / "certs"
    required = ("designapi_user_cert.pem", "designapi_user_key.pem", "ca_cert.pem")
    return [name for name in required if not (cert_dir / name).is_file()]


def _build_auth_provider() -> DebugTokenVerifier | None:
    api_token = os.environ.get("MCP_API_TOKEN")
    if not api_token:
        return None

    def _validate(token: str) -> bool:
        return secrets.compare_digest(token, api_token)

    return DebugTokenVerifier(validate=_validate, client_id="cp-sample-mcp")


def _build_middleware() -> list[Middleware]:
    middleware: list[Middleware] = [AuditMiddleware()]
    rps = float(CONFIG["mcp"]["rate_limit_rps"])
    burst = int(CONFIG["mcp"]["rate_limit_burst"])
    if rps > 0:
        middleware.append(
            RateLimitingMiddleware(
                max_requests_per_second=rps,
                burst_capacity=burst,
                get_client_id=lambda _ctx: _owner_id(),
            )
        )
    return middleware


def _collect_link_failure_reroutes(
    model,
    before_paths: dict[str, list[str] | None],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Compare demand paths before and after a failure simulation."""
    rerouted: list[dict[str, Any]] = []
    unrouted: list[str] = []
    for demand in model.demands:
        if not demand.active:
            continue
        if before_paths.get(str(demand)) != _path_of(demand):
            rerouted.append(
                {
                    "demand": demand.name,
                    "before": before_paths.get(str(demand)),
                    "after": _path_of(demand),
                }
            )
        if not demand.routed:
            unrouted.append(demand.name)
    return rerouted, unrouted


def _iface_traffic_deltas(
    before_ifaces: dict[str, dict[str, Any]],
    after_ifaces: dict[str, dict[str, Any]],
    min_traffic_delta_mbps: float,
    top_n: int,
) -> list[dict[str, Any]]:
    """Return the largest interface traffic changes after a failure."""
    deltas: list[dict[str, Any]] = []
    for key in sorted(set(before_ifaces) | set(after_ifaces)):
        bt = before_ifaces.get(key, {}).get("traffic_mbps", 0.0)
        at = after_ifaces.get(key, {}).get("traffic_mbps", 0.0)
        bu = before_ifaces.get(key, {}).get("util_percent")
        au = after_ifaces.get(key, {}).get("util_percent")
        delta = at - bt
        if abs(delta) >= min_traffic_delta_mbps:
            deltas.append(
                {
                    "interface": key,
                    "traffic_before_mbps": round(bt, 1),
                    "traffic_after_mbps": round(at, 1),
                    "delta_mbps": round(delta, 1),
                    "util_before_percent": bu,
                    "util_after_percent": au,
                }
            )
    deltas.sort(key=lambda row: abs(row["delta_mbps"]), reverse=True)
    return deltas[:top_n]


def _oversubscribed_ifaces(
    ifaces: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return interfaces with utilization above 100%."""
    oversub = [
        {
            "interface": key,
            "traffic_mbps": values["traffic_mbps"],
            "capacity_mbps": values["capacity_mbps"],
            "util_percent": round(values["util_percent"], 2),
        }
        for key, values in ifaces.items()
        if values.get("util_percent") is not None and values["util_percent"] > 100.0
    ]
    oversub.sort(key=lambda row: row["util_percent"], reverse=True)
    return oversub


def _build_growth_rpc_plan(
    network,
    server_plan_path: str,
    growth_percent: float,
    num_periods: int,
    period_inc: int,
    growth_method: str,
    factor: float,
) -> tuple[Any, bool, bool]:
    """Run create_growth_plans and apply fallback traffic scaling when needed."""
    pm = network.service_connection.rpc_plan_manager
    base_rpc_plan = pm.newPlanFromFileSystem(server_plan_path)
    base_avg = _rpc_avg_demand_traffic(base_rpc_plan)

    new_plan_ids = _run_create_growth_plans(
        network, server_plan_path, num_periods, period_inc, growth_method
    )

    if new_plan_ids:
        pk = PlanKey()
        pk.id = new_plan_ids[-1]
        growth_rpc_plan = pm.getPlan(pk)
    else:
        growth_rpc_plan = pm.newPlanFromFileSystem(server_plan_path)

    growth_avg = _rpc_avg_demand_traffic(growth_rpc_plan)
    tool_applied = (
        base_avg is not None
        and growth_avg is not None
        and abs(growth_avg - base_avg * factor) < abs(base_avg * factor) * 0.01
    )
    fallback_applied = False
    if not tool_applied:
        _apply_compound_growth_to_rpc_plan(
            growth_rpc_plan, growth_percent, period_inc, num_periods
        )
        fallback_applied = True
    return growth_rpc_plan, tool_applied, fallback_applied


def _near_capacity_circuits(
    rows: list[CircuitUtil],
    near_capacity: float,
    threshold: float,
) -> list[dict[str, Any]]:
    """Return circuits between near_capacity and threshold utilization."""
    return sorted(
        [
            {
                "circuit": row.circuit,
                "max_util_percent": round(row.max_util, 2),
                "bottleneck": row.bottleneck_side,
            }
            for row in rows
            if near_capacity <= row.max_util <= threshold
        ],
        key=lambda row: row["max_util_percent"],
        reverse=True,
    )[:10]


def _execute_traffic_growth(
    plan_path: Path,
    growth_percent: float,
    num_periods: int,
    period_inc: int,
    growth_method: str,
    threshold: float,
    near_capacity: float,
) -> dict[str, Any]:
    """Run growth-plan simulation and return the utilization report."""
    factor = _compound_multiplier(growth_percent, period_inc, num_periods)
    host, port, protocol = _cw_connection()
    server_plan_path = f"/tmp/plan274_growth_{uuid.uuid4().hex}.txt"
    tmp = Path(tempfile.gettempdir()) / f"growth_plan_{uuid.uuid4().hex}.txt"

    with _opm_lock:
        with open_plan(str(plan_path), host, port, protocol) as network:
            model = network.model
            baseline_rows = _collect_circuit_utils(model)
            baseline_report = _circuit_utils_to_json(baseline_rows, threshold)

            for demand in model.demands:
                if demand.active:
                    demand.growth_percent = growth_percent

            network.rpc_plan.serializeToFileSystem(server_plan_path)
            growth_rpc_plan, tool_applied, fallback_applied = _build_growth_rpc_plan(
                network,
                server_plan_path,
                growth_percent,
                num_periods,
                period_inc,
                growth_method,
                factor,
            )
            tmp.write_bytes(bytes(growth_rpc_plan.serializeToBytes(PlanFormat.TxtFile)))

        with open_plan(str(tmp), host, port, protocol) as growth_net:
            grown_rows = _collect_circuit_utils(growth_net.model)
            growth_report = _circuit_utils_to_json(grown_rows, threshold)
            near = _near_capacity_circuits(grown_rows, near_capacity, threshold)

    tmp.unlink(missing_ok=True)
    return ok(
        growth_percent=growth_percent,
        num_periods=num_periods,
        period_inc=period_inc,
        growth_method=growth_method,
        traffic_multiplier=round(factor, 4),
        tool_applied_growth=tool_applied,
        compound_fallback_applied=fallback_applied,
        baseline=baseline_report,
        after_growth=growth_report,
        near_capacity=near,
    )


def _create_mcp_server() -> FastMCP:
    return FastMCP(
        "cp-sample-sim",
        instructions=(
            "Crosswork Planning network simulation. "
            "Read-only data: use resources (server://health, plan://uploads, "
            "plan://{plan_id}/summary). "
            "Mutations and simulation: use tools. "
            "Always upload_plan first when the user provides a plan file, "
            "then pass the returned plan_id as plan_ref to simulation tools."
        ),
        auth=_build_auth_provider(),
        middleware=_build_middleware(),
        strict_input_validation=True,
    )


def _health_payload(plan_ref: str | None = None) -> dict[str, Any]:
    cariden = _cariden_home()
    missing = _validate_certs(cariden)
    host, port, protocol = _cw_connection()
    payload: dict[str, Any] = {
        "cariden_home": str(cariden),
        "crosswork_host": host,
        "design_api_port": port,
        "protocol": protocol,
        "certs_ok": not missing,
        "missing_certs": missing,
    }
    if missing:
        payload["ok"] = False
        payload["error"] = "Missing mTLS certificate files"
        return payload

    try:
        path = _resolve_plan_ref(plan_ref)
        with _open_model(path) as model:
            payload["plan_ref"] = plan_ref or "default"
            payload["nodes"] = len(model.nodes)
            payload["design_api_reachable"] = True
            payload["ok"] = True
        return payload
    except (OSError, ValueError, RuntimeError, FileNotFoundError) as exc:
        payload["design_api_reachable"] = False
        payload["ok"] = False
        payload["error"] = str(exc)
        payload["hint"] = "Ensure DesignAPI is running and certs are onboarded."
        return payload


def _plan_summary_payload(plan_ref: str | None = None) -> dict[str, Any]:
    path = _resolve_plan_ref(plan_ref)
    with _open_model(path) as model:
        return ok(
            plan_ref=plan_ref or "default",
            nodes=len(model.nodes),
            circuits=len(model.circuits),
            active_circuits=sum(1 for c in model.circuits if c.active),
            demands=len(model.demands),
            active_demands=sum(1 for d in model.demands if d.active),
        )


def _list_circuits_payload(
    plan_ref: str | None = None,
    node_filter: str | None = None,
    active_only: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    path = _resolve_plan_ref(plan_ref)
    limit = min(max(limit, 1), 500)
    with _open_model(path) as model:
        rows = []
        for circuit in model.circuits:
            if active_only and not circuit.active:
                continue
            ia = circuit.interface_a
            ib = circuit.interface_b
            if node_filter:
                nf = node_filter.lower()
                if nf not in ia.node.name.lower() and nf not in ib.node.name.lower():
                    continue
            rows.append(
                {
                    "circuit": str(circuit),
                    "node_a": ia.node.name,
                    "iface_a": ia.name,
                    "node_b": ib.node.name,
                    "iface_b": ib.name,
                    "capacity_mbps": circuit.capacity,
                    "active": circuit.active,
                }
            )
            if len(rows) >= limit:
                break
        return ok(plan_ref=plan_ref, circuits=rows, count=len(rows), truncated=len(rows) >= limit)


def _list_demands_payload(
    plan_ref: str | None = None,
    source: str | None = None,
    destination: str | None = None,
    active_only: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    path = _resolve_plan_ref(plan_ref)
    limit = min(max(limit, 1), 500)
    with _open_model(path) as model:
        rows = []
        for demand in model.demands:
            if active_only and not demand.active:
                continue
            src = str(demand.source)
            dst = str(demand.destination)
            if source and source not in src:
                continue
            if destination and destination not in dst:
                continue
            rows.append(
                {
                    "name": demand.name,
                    "source": src,
                    "destination": dst,
                    "service_class": demand.service_class,
                    "traffic_mbps": demand.traffic,
                    "active": demand.active,
                }
            )
            if len(rows) >= limit:
                break
        return ok(plan_ref=plan_ref, demands=rows, count=len(rows), truncated=len(rows) >= limit)


def _uploaded_plans_payload() -> dict[str, Any]:
    owner = _owner_id()
    plans = [
        {
            "plan_id": p.plan_id,
            "plan_ref": f"upload:{p.plan_id}",
            "name": p.name,
            "bytes": p.bytes,
            "uploaded_at": p.uploaded_at,
        }
        for p in PLAN_REGISTRY.list_plans(owner)
    ]
    return ok(plans=plans, count=len(plans))


mcp = _create_mcp_server()


@mcp.resource("server://health", mime_type="application/json")
def health_resource() -> str:
    """Read-only server health, cert, and DesignAPI connectivity status."""
    return json.dumps(_health_payload())


@mcp.resource("plan://uploads", mime_type="application/json")
def uploaded_plans_resource() -> str:
    """Read-only list of plans uploaded by the current client."""
    return json.dumps(_uploaded_plans_payload())


@mcp.resource("plan://{plan_id}/summary", mime_type="application/json")
def plan_summary_resource(plan_id: str) -> str:
    """Read-only node, circuit, and demand counts for an uploaded plan."""
    try:
        return json.dumps(_plan_summary_payload(f"upload:{plan_id}"))
    except (OSError, ValueError, RuntimeError, FileNotFoundError) as exc:
        _resource_fail(str(exc))


@mcp.resource("plan://{plan_id}/circuits", mime_type="application/json")
def plan_circuits_resource(plan_id: str) -> str:
    """Read-only active circuits for an uploaded plan (limit 100)."""
    try:
        return json.dumps(_list_circuits_payload(f"upload:{plan_id}", limit=100))
    except (OSError, ValueError, RuntimeError, FileNotFoundError) as exc:
        _resource_fail(str(exc))


@mcp.resource("plan://{plan_id}/demands", mime_type="application/json")
def plan_demands_resource(plan_id: str) -> str:
    """Read-only active demands for an uploaded plan (limit 100)."""
    try:
        return json.dumps(_list_demands_payload(f"upload:{plan_id}", limit=100))
    except (OSError, ValueError, RuntimeError, FileNotFoundError) as exc:
        _resource_fail(str(exc))


@mcp.prompt
def link_failure_workflow(node_a: str, node_b: str) -> str:
    """Workflow template for analyzing a link failure between two nodes."""
    return (
        "Crosswork link failure analysis workflow:\n"
        "1. upload_plan if the user provided a new plan file\n"
        "2. Read plan://{plan_id}/circuits or list_circuits with node_filter\n"
        f"3. simulate_link_failure(node_a={node_a!r}, node_b={node_b!r})\n"
        "4. Review sample_reroutes and new_oversubscribed in the response\n"
        "5. simulate_demand_route for affected source/destination pairs"
    )


@mcp.prompt
def capacity_planning_workflow(growth_percent: float = 33.0) -> str:
    """Workflow template for traffic growth and capacity planning."""
    return (
        "Crosswork capacity planning workflow:\n"
        "1. upload_plan if needed\n"
        "2. Read plan://{plan_id}/summary for baseline counts\n"
        f"3. analyze_traffic_growth(growth_percent={growth_percent})\n"
        "4. run_simulation_analysis(failure_sets=['circuits']) for worst-case failures\n"
        "5. Summarize oversubscribed and near_capacity circuits for the user"
    )


@mcp.tool
def upload_plan(
    name: str,
    content: str,
    encoding: str = "text",
    plan_id: str | None = None,
    validate: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Upload a Crosswork plan file for simulation.

    Call this when the user provides a plan that is not already on the server.
    Returns plan_id — pass it as plan_ref to all other tools.

    Args:
        name: Filename with extension (.txt, .pln, .db) for format detection.
        content: Plan body as UTF-8 text, or base64 for binary .pln/.db files.
        encoding: 'text' for .txt plans, 'base64' for binary plans.
        plan_id: Optional custom id; auto-generated if omitted.
        validate: If true, open plan via OPM to confirm it loads.
        confirm: Required true for uploads above large_upload_confirm_bytes (10 MiB default).
    """
    try:
        args = UploadPlanArgs(
            name=name,
            content=content,
            encoding=encoding,
            plan_id=plan_id,
            validate=validate,
            confirm=confirm,
        )
    except (ValueError, TypeError) as exc:
        _fail(str(exc))

    plan_content = args.content
    plan_encoding = args.encoding
    raw_len = (
        len(content.encode("utf-8"))
        if encoding == "text"
        else len(base64.b64decode(content, validate=True))
    )
    confirm_threshold = int(CONFIG["plan"]["large_upload_confirm_bytes"])
    if raw_len > confirm_threshold and not args.confirm:
        return ok(
            preview=True,
            bytes=raw_len,
            confirm_required=True,
            message=(
                f"Upload is {raw_len} bytes. Re-call upload_plan with confirm=true to proceed."
            ),
        )

    PLAN_REGISTRY.purge_expired()
    owner = _owner_id()
    try:
        record = PLAN_REGISTRY.upload(
            args.name, plan_content, plan_encoding, args.plan_id, owner_id=owner
        )
        result = ok(
            plan_id=record.plan_id,
            plan_ref=f"upload:{record.plan_id}",
            name=record.name,
            bytes=record.bytes,
            uploaded_at=record.uploaded_at,
        )
        if args.validate:
            with _open_model(Path(record.path)) as model:
                result["nodes"] = len(model.nodes)
                result["circuits"] = len(model.circuits)
                result["demands"] = sum(1 for d in model.demands if d.active)
        _audit_event("upload_plan", plan_id=record.plan_id, bytes=record.bytes)
        return result
    except (OSError, ValueError, RuntimeError, FileNotFoundError) as exc:
        _fail(str(exc), hint="Check name, encoding, and plan content format.")


@mcp.tool
def list_uploaded_plans() -> dict[str, Any]:
    """List plans uploaded by the current client. Prefer resource plan://uploads."""
    return _uploaded_plans_payload()


@mcp.tool
def delete_uploaded_plan(plan_id: str, confirm: bool = False) -> dict[str, Any]:
    """Remove an uploaded plan from server staging. Requires confirm=true."""
    try:
        args = DeletePlanArgs(plan_id=plan_id, confirm=confirm)
    except (ValueError, TypeError) as exc:
        _fail(str(exc))

    owner = _owner_id()
    record = PLAN_REGISTRY.get(args.plan_id, owner)
    if not record:
        _fail(f"plan_id not found or not authorized: {args.plan_id}")

    if not args.confirm:
        return ok(
            preview=True,
            plan_id=args.plan_id,
            name=record.name,
            bytes=record.bytes,
            uploaded_at=record.uploaded_at,
            message="Re-call delete_uploaded_plan with confirm=true to delete.",
        )

    if PLAN_REGISTRY.delete(args.plan_id, owner):
        _audit_event("delete_uploaded_plan", plan_id=args.plan_id)
        return ok(plan_id=args.plan_id, deleted=True)
    _fail(f"plan_id not found or not authorized: {args.plan_id}")


@mcp.tool
def health_check(plan_ref: str | None = None) -> dict[str, Any]:
    """Verify MCP server config, OPM certs, and DesignAPI connectivity. Prefer server://health."""
    payload = _health_payload(plan_ref)
    if payload.get("ok"):
        return payload
    _fail(payload.get("error", "health check failed"), hint=payload.get("hint"))


@mcp.tool
def get_plan_summary(plan_ref: str | None = None) -> dict[str, Any]:
    """Return node, circuit, and demand counts. Prefer plan://{plan_id}/summary."""
    try:
        return _plan_summary_payload(plan_ref)
    except (OSError, ValueError, RuntimeError, FileNotFoundError, KeyError) as exc:
        _fail(str(exc))


@mcp.tool
def list_circuits(
    plan_ref: str | None = None,
    node_filter: str | None = None,
    active_only: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    """List circuits in a plan. Prefer plan://{plan_id}/circuits for unfiltered reads."""
    try:
        return _list_circuits_payload(plan_ref, node_filter, active_only, limit)
    except (OSError, ValueError, RuntimeError, FileNotFoundError, KeyError) as exc:
        _fail(str(exc))


@mcp.tool
def list_demands(
    plan_ref: str | None = None,
    source: str | None = None,
    destination: str | None = None,
    active_only: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    """List demands in a plan. Prefer plan://{plan_id}/demands for unfiltered reads."""
    try:
        return _list_demands_payload(plan_ref, source, destination, active_only, limit)
    except (OSError, ValueError, RuntimeError, FileNotFoundError, KeyError) as exc:
        _fail(str(exc))


@mcp.tool
def simulate_demand_route(
    source: str,
    destination: str,
    plan_ref: str | None = None,
    demand_name: str | None = None,
    service_class: str = "Default",
) -> dict[str, Any]:
    """Simulate how a demand is routed (baseline, no failures).

    Returns path interfaces, metrics, and latency. Use before simulate_link_failure
    to understand normal routing.
    """
    try:
        path = _resolve_plan_ref(plan_ref)
        with _open_model(path) as model:
            key = {
                "name": demand_name or f"{source}_{destination}",
                "source": source,
                "destination": destination,
                "service_class": service_class,
            }
            demand = model.demands[key]
            model.route_simulation = []
            model.traffic_simulation = None
            model.route_simulation.recompute()

            if not demand.routed:
                return ok(routed=False, demand=demand.name, message="Demand is not routed")

            route = demand.route
            interfaces = []
            for iface in route.interfaces:
                interfaces.append(
                    {
                        "interface": _fmt_iface(iface),
                        "traffic_share": route.interface_usage[iface],
                    }
                )
            return ok(
                demand=demand.name,
                source=source,
                destination=destination,
                traffic_mbps=demand.traffic,
                routed=True,
                path_metric=route.total_path_metric,
                latency_ms={
                    "min": route.minimum_latency,
                    "avg": route.average_latency,
                    "max": route.maximum_latency,
                },
                min_ecmp_percent=route.minimum_ecmp_percentage,
                interfaces=interfaces,
                hop_count=len(route.interfaces),
            )
    except (OSError, ValueError, RuntimeError, FileNotFoundError, KeyError) as exc:
        _fail(str(exc))


@mcp.tool
def simulate_igp_shortest_path(
    source: str,
    destination: str,
    plan_ref: str | None = None,
    metric: str = "igp",
) -> dict[str, Any]:
    """Compute shortest IGP (or latency/TE) path between two nodes."""
    try:
        metric = metric.lower()
        if metric not in METRIC_ALLOWLIST:
            _fail(f"Invalid metric: {metric}", hint=f"Allowed: {sorted(METRIC_ALLOWLIST)}")

        path = _resolve_plan_ref(plan_ref)
        with _open_model(path) as model:
            model.route_simulation = []
            model.route_simulation.recompute()
            src_node = model.nodes[source]
            dst_node = model.nodes[destination]
            igp_route = model.route_simulation.shortest_path(src_node, dst_node, metric)
            if not igp_route or not igp_route.interfaces:
                return ok(source=source, destination=destination, metric=metric, path=[])
            return ok(
                source=source,
                destination=destination,
                metric=metric,
                path=[_fmt_iface(i) for i in igp_route.interfaces],
                hop_count=len(igp_route.interfaces),
            )
    except (OSError, ValueError, RuntimeError, FileNotFoundError, KeyError) as exc:
        _fail(str(exc))


@mcp.tool
def simulate_link_failure(
    plan_ref: str | None = None,
    node_a: str | None = None,
    node_b: str | None = None,
    failed_circuits: list[str] | None = None,
    top_n: int = 25,
    min_traffic_delta_mbps: float = 1.0,
    sample_reroutes: int = 5,
) -> dict[str, Any]:
    """Simulate circuit failure(s) and report routing + interface traffic changes.

    Provide either failed_circuits or node_a+node_b (e.g. cr1.sjc and cr1.kcy).
    Compares baseline vs failure traffic simulation on all interfaces.
    """
    try:
        top_n = min(max(top_n, 1), 200)
        sample_reroutes = min(max(sample_reroutes, 0), 20)
        plan_path = _resolve_plan_ref(plan_ref)

        with _open_model(plan_path) as model:
            failed = _resolve_failed_circuits(model, failed_circuits, node_a, node_b)
            failed_names = [str(c) for c in failed]

            model.route_simulation = []
            model.traffic_simulation = None
            model.route_simulation.recompute()
            before_paths = {str(d): _path_of(d) for d in model.demands if d.active}
            before_ifaces = _iface_snapshot(model)

            model.route_simulation = failed
            model.traffic_simulation = None
            model.route_simulation.recompute()
            after_ifaces = _iface_snapshot(model)

            rerouted, unrouted = _collect_link_failure_reroutes(model, before_paths)
            deltas = _iface_traffic_deltas(
                before_ifaces,
                after_ifaces,
                min_traffic_delta_mbps,
                top_n,
            )
            oversub = _oversubscribed_ifaces(after_ifaces)
            baseline_oversub = [
                key
                for key, values in before_ifaces.items()
                if values.get("util_percent") is not None and values["util_percent"] > 100.0
            ]

            return ok(
                failed_circuits=failed_names,
                active_demands=sum(1 for d in model.demands if d.active),
                rerouted_count=len(rerouted),
                unrouted_count=len(unrouted),
                unrouted=unrouted,
                sample_reroutes=rerouted[:sample_reroutes],
                interface_changes=deltas,
                new_oversubscribed=oversub,
                baseline_oversubscribed_count=len(baseline_oversub),
            )
    except (OSError, ValueError, RuntimeError, FileNotFoundError, KeyError) as exc:
        _fail(str(exc))


@mcp.tool
def run_simulation_analysis(
    plan_ref: str | None = None,
    failure_sets: str | list[str] = "nodes,circuits",
    max_fail_per_int: int = 10,
    limit: int = 15,
) -> dict[str, Any]:
    """Run Simulation Analysis — worst-case interface utilization across failure scenarios.

    Equivalent to Design UI Simulation Analysis / CLI sim_analysis.
    failure_sets: list of failure types or comma-separated string (nodes, circuits, ...).
    """
    try:
        parsed_sets = _parse_failure_sets(failure_sets)
        args = SimulationAnalysisArgs(
            plan_ref=plan_ref,
            failure_sets=parsed_sets,  # type: ignore[arg-type]
            max_fail_per_int=max_fail_per_int,
            limit=limit,
        )
        plan_path = _resolve_plan_ref(args.plan_ref)

        with _open_model(plan_path) as model:
            sa = SimulationAnalysis(
                model,
                failure_types=list(args.failure_sets),
                max_fail_per_int=args.max_fail_per_int,
            )
            sa.cache_valid = True
            if not sa.cache_valid:
                _fail("Simulation analysis did not complete")

            ranked = []
            for iface in model.interfaces:
                util = sa.interfaces[iface].worst_case_utilization
                if util is not None:
                    ranked.append(
                        {
                            "interface": str(iface),
                            "worst_case_util_percent": round(util, 2),
                        }
                    )
            ranked.sort(key=lambda x: x["worst_case_util_percent"], reverse=True)

            return ok(
                failure_sets=list(args.failure_sets),
                scenario_count=len(sa.simulations.keys()),
                top_interfaces=ranked[: args.limit],
            )
    except (OSError, ValueError, RuntimeError, FileNotFoundError, KeyError) as exc:
        _fail(str(exc))


@mcp.tool
def analyze_traffic_growth(
    plan_ref: str | None = None,
    growth_percent: float = 33.0,
    num_periods: int = 1,
    period_inc: int = 1,
    growth_method: GrowthMethodLiteral = "COMPOUND",
    threshold: float = 100.0,
    near_capacity: float = 80.0,
) -> dict[str, Any]:
    """Forecast traffic growth via Create Growth Plans and report oversubscription.

    Sets demand.growth_percent, runs create_growth_plans GenericTool, analyzes utilization.
    On CP 8.0 Build 385 the tool may not apply traffic — compound formula fallback is used.
    """
    try:
        if num_periods < 1 or period_inc < 1:
            _fail("num_periods and period_inc must be >= 1")
        if growth_method not in ("COMPOUND", "SIMPLE"):
            _fail("growth_method must be COMPOUND or SIMPLE")

        plan_path = _resolve_plan_ref(plan_ref)
        return _execute_traffic_growth(
            plan_path,
            growth_percent,
            num_periods,
            period_inc,
            growth_method,
            threshold,
            near_capacity,
        )
    except (OSError, ValueError, RuntimeError, FileNotFoundError) as exc:
        _fail(str(exc))


def main() -> int:
    """Parse CLI arguments and start the MCP server."""
    parser = argparse.ArgumentParser(description="Crosswork Planning Simulation MCP Server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="MCP transport: stdio for Cursor local spawn (default), http for remote clients",
    )
    parser.add_argument("--host", default=CONFIG["mcp"]["host"])
    parser.add_argument("--port", type=int, default=CONFIG["mcp"]["port"])
    parser.add_argument("--path", default=CONFIG["mcp"]["path"])
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Bind HTTP to 0.0.0.0 (default: 127.0.0.1). Use behind TLS reverse proxy.",
    )
    parser.add_argument("--cw-host", help="Crosswork VM IP (overrides CW_HOST)")
    parser.add_argument("--cariden-home", help="Path to cw-planning SDK")
    args = parser.parse_args()

    if args.cw_host:
        CONFIG["crosswork"]["host"] = args.cw_host
    if args.cariden_home:
        CONFIG["opm"]["cariden_home"] = args.cariden_home
    if args.allow_remote:
        args.host = "0.0.0.0"

    if args.transport == "http" and not os.environ.get("MCP_API_TOKEN"):
        print(
            "ERROR: MCP_API_TOKEN is required for HTTP transport. "
            "Set a secret token or use --transport stdio for local clients.",
            file=sys.stderr,
        )
        return 1

    removed = PLAN_REGISTRY.purge_expired()
    if removed:
        logger.info("purged_expired_plans count=%s", removed)

    cariden = _cariden_home()
    missing = _validate_certs(cariden)
    print("Crosswork Planning MCP Server", file=sys.stderr)
    print(f"  CARIDEN_HOME: {cariden}", file=sys.stderr)
    design_api = (
        f"{CONFIG['crosswork']['host']}:{CONFIG['crosswork']['design_api_port']}"
    )
    print(f"  DesignAPI:    {design_api}", file=sys.stderr)
    print(f"  Staging:      {_staging_dir()}", file=sys.stderr)
    print(f"  Transport:    {args.transport}", file=sys.stderr)
    if args.transport == "http":
        print(f"  Listening:    http://{args.host}:{args.port}{args.path}", file=sys.stderr)
        print("  Auth:         Bearer MCP_API_TOKEN", file=sys.stderr)
    if missing:
        print(f"  WARNING: Missing certs: {missing}", file=sys.stderr)

    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        allowed_hosts = [
            f"localhost:{args.port}",
            f"127.0.0.1:{args.port}",
            f"{args.host}:{args.port}",
        ]
        mcp.run(
            transport="http",
            host=args.host,
            port=args.port,
            path=args.path,
            host_origin_protection=True,
            allowed_hosts=allowed_hosts,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
