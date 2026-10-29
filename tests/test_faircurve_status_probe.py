# SPDX-License-Identifier: Apache-2.0
"""Temporary probe for FairCurve status output in the OCP wheel."""

import importlib.metadata
import inspect
import io
import json
import re
import sys
import warnings

import OCP
import OCP.FairCurve as faircurve
from OCP.FairCurve import (
    FairCurve_AnalysisCode,
    FairCurve_Batten,
    FairCurve_MinimalVariation,
)
from OCP.gp import gp_Pnt2d
from OCP.Standard import Standard_Failure
from OCP.StdFail import StdFail_NotDone

_CONSTANTS = (
    "FairCurve_OK",
    "FairCurve_NotConverged",
    "FairCurve_InfiniteSliding",
    "FairCurve_NullHeight",
)
_CASES = (
    ("straight_default", 0.0, 0.0, 50, 1.0e-3),
    ("angled_one_iteration", 0.35, -0.25, 1, 1.0e-12),
    ("steep_zero_iterations", 1.2, -1.1, 0, 1.0e-12),
)
_DIAGNOSTICS = {
    "Ok": "FairCurve_OK",
    "NotConverged": "FairCurve_NotConverged",
    "InfiniteSliding": "FairCurve_InfiniteSliding",
    "NullHeight": "FairCurve_NullHeight",
}
_STATUS = re.compile(r"AnalysisCode\s*:\s*(Ok|NotConverged|InfiniteSliding|NullHeight)")
_PROBE_ERRORS = (
    AttributeError,
    OSError,
    OverflowError,
    RuntimeError,
    Standard_Failure,
    StdFail_NotDone,
    TypeError,
    ValueError,
)


def _signature(method):
    try:
        signature = str(inspect.signature(method))
    except (TypeError, ValueError):
        signature = getattr(method, "__text_signature__", None)
    return {"signature": signature, "doc": method.__doc__}


def _run_case(name, solver_type, case, order, constant_refs, constant_values):
    case_name, angle1, angle2, iterations, tolerance = case
    row = {
        "solver": name,
        "case": case_name,
        "order": order,
        "inputs": {
            "p1": [0, 0],
            "p2": [10, 0],
            "height": 0.1,
            "angle1": angle1,
            "angle2": angle2,
            "iterations": iterations,
            "tolerance": tolerance,
        },
        "errors": [],
    }
    solver = None
    code = None
    sentinel_name = (
        "FairCurve_NotConverged" if case_name == "straight_default" else "FairCurve_OK"
    )
    row["sentinel_name"] = sentinel_name
    try:
        solver = solver_type(gp_Pnt2d(0, 0), gp_Pnt2d(10, 0), 0.1)
        solver.SetAngle1(angle1)
        solver.SetAngle2(angle2)
    except _PROBE_ERRORS as error:
        row["errors"].append(
            {"stage": "construct", "type": type(error).__name__, "message": str(error)}
        )
    try:
        code = FairCurve_AnalysisCode(constant_values[sentinel_name])
        row["before"] = int(code)
        row["status_type"] = type(code).__name__
        row["shared_with"] = [
            key for key, value in constant_refs.items() if code is value
        ]
    except _PROBE_ERRORS as error:
        row["errors"].append(
            {
                "stage": "make_status",
                "type": type(error).__name__,
                "message": str(error),
            }
        )
    if solver is not None and code is not None and not row.get("shared_with"):
        try:
            result = solver.Compute(code, iterations, tolerance)
            row["compute_completed"] = True
            row["compute"] = (
                result if isinstance(result, (bool, int, float, str)) else repr(result)
            )
            row["compute_type"] = type(result).__name__
        except _PROBE_ERRORS as error:
            row["errors"].append(
                {
                    "stage": "compute",
                    "type": type(error).__name__,
                    "message": str(error),
                }
            )
    elif row.get("shared_with"):
        row["guard"] = "Compute skipped to protect module enum constants"
    if code is not None:
        try:
            row["after"] = int(code)
        except _PROBE_ERRORS as error:
            row["errors"].append(
                {
                    "stage": "read_status",
                    "type": type(error).__name__,
                    "message": str(error),
                }
            )
        if "before" in row and "after" in row:
            row["status_mutated"] = row["before"] != row["after"]
    if solver is not None:
        try:
            stream = io.BytesIO()
            solver.Dump(stream)
            row["dump"] = stream.getvalue().decode("utf-8", errors="replace")
            match = _STATUS.search(row["dump"])
            row["dump_status"] = match.group(1) if match else None
        except _PROBE_ERRORS as error:
            row["errors"].append(
                {"stage": "dump", "type": type(error).__name__, "message": str(error)}
            )
    if row.get("compute_completed") and row.get("dump_status") not in _DIAGNOSTICS:
        row["errors"].append(
            {
                "stage": "diagnostic",
                "message": "Compute completed without a recognized Dump status",
            }
        )
    row["constants_unchanged"] = {
        key: int(getattr(faircurve, key)) for key in _CONSTANTS
    } == constant_values
    return row


def test_faircurve_compute_status_binding_probe():
    """Record status and dump behavior for both FairCurve solver classes."""
    constant_refs = {key: getattr(faircurve, key) for key in _CONSTANTS}
    constant_values = {key: int(value) for key, value in constant_refs.items()}
    solvers = (
        ("Batten", FairCurve_Batten),
        ("MinimalVariation", FairCurve_MinimalVariation),
    )
    signatures = {name: _signature(solver.Compute) for name, solver in solvers}
    records = [
        _run_case(name, solver, case, order, constant_refs, constant_values)
        for name, solver in solvers
        for order, cases in (("forward", _CASES), ("reverse", tuple(reversed(_CASES))))
        for case in cases
    ]
    not_converged_rows = {
        name: [
            row
            for row in records
            if row["solver"] == name
            and row.get("compute_completed")
            and row.get("dump_status") == "NotConverged"
        ]
        for name, _ in solvers
    }
    not_converged_coverage = {}
    true_with_not_converged = {}
    for name, rows in not_converged_rows.items():
        not_converged_coverage[name] = (
            "missing"
            if not rows
            else (
                "qualified"
                if all(
                    not row["errors"]
                    and row["compute_type"] == "bool"
                    and row.get("after") == constant_values["FairCurve_NotConverged"]
                    and row.get("status_mutated")
                    and row["constants_unchanged"]
                    for row in rows
                )
                else "mismatch"
            )
        )
        true_with_not_converged[name] = (
            "observed" if any(row.get("compute") is True for row in rows) else "missing"
        )
    payload = {
        "marker": "FAIRCURVE_BINDING_PROBE",
        "distribution": {
            "name": importlib.metadata.distribution("cadquery-ocp-novtk").metadata[
                "Name"
            ],
            "version": importlib.metadata.version("cadquery-ocp-novtk"),
        },
        "modules": {
            "OCP": {"path": OCP.__file__, "version": getattr(OCP, "__version__", None)},
            "OCP.FairCurve": {"path": faircurve.__file__},
        },
        "python": sys.version,
        "constants_before": constant_values,
        "compute_signatures": signatures,
        "not_converged_diagnostic_coverage": not_converged_coverage,
        "true_with_not_converged": true_with_not_converged,
        "records": records,
    }
    warnings.warn(
        "FAIRCURVE_BINDING_PROBE "
        + json.dumps(payload, sort_keys=True, separators=(",", ":")),
        UserWarning,
    )

    failures = []
    for row in records:
        prefix = f"{row['solver']} {row['order']} {row['case']}"
        if row["errors"] or row.get("guard"):
            failures.append(f"{prefix}: probe failed")
        if row.get("before") != constant_values[row["sentinel_name"]]:
            failures.append(f"{prefix}: sentinel mismatch")
        if row.get("compute_type") != "bool":
            failures.append(f"{prefix}: Compute was not Boolean")
        if not row["constants_unchanged"]:
            failures.append(f"{prefix}: constants changed")
        diagnostic = row.get("dump_status")
        if row.get("compute_completed"):
            if diagnostic not in _DIAGNOSTICS:
                failures.append(f"{prefix}: missing recognized dump status")
            elif row.get("after") != constant_values[_DIAGNOSTICS[diagnostic]]:
                failures.append(f"{prefix}: dump/status mismatch")
            elif diagnostic != "Ok" and not row.get("status_mutated"):
                failures.append(f"{prefix}: status output did not change from sentinel")
        if row["case"] == "straight_default":
            if row.get("compute") is not True or diagnostic != "Ok":
                failures.append(f"{prefix}: straight case did not report success")
            if row.get("after") != constant_values["FairCurve_OK"] or not row.get(
                "status_mutated"
            ):
                failures.append(f"{prefix}: straight status was not OK")

    assert {key: int(getattr(faircurve, key)) for key in _CONSTANTS} == constant_values
    assert not failures, "; ".join(failures)
