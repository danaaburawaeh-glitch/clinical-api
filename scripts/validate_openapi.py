#!/usr/bin/env python3
"""Check ``openapi.yaml`` against the running application.

``openapi.yaml`` is hand-maintained so that GPT Builder receives a flat,
readable schema. That creates a drift risk: the file could describe an
endpoint the app no longer has, or miss a field the app now returns.

This script fails loudly on any drift. Run it in CI:

    python scripts/validate_openapi.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402
from app.models.schemas import (  # noqa: E402
    EvidenceSearchRequest,
    ManufacturerSearchRequest,
    RegulatorySearchRequest,
    SearchResponse,
    SearchResult,
    SourceVerifyResponse,
)

ROOT = Path(__file__).resolve().parent.parent
OPENAPI_FILE = ROOT / "openapi.yaml"

REQUIRED_OPERATION_IDS = {
    "searchClinicalEvidence",
    "searchRegulatoryEvidence",
    "getManufacturerDocument",
    "verifyClinicalSource",
}


def fail(message: str) -> None:
    print(f"  FAIL  {message}")
    FAILURES.append(message)


def ok(message: str) -> None:
    print(f"  ok    {message}")


FAILURES: list[str] = []


def main() -> int:
    print("Validating openapi.yaml against the live application\n")

    if not OPENAPI_FILE.exists():
        print("openapi.yaml not found")
        return 1

    try:
        spec = yaml.safe_load(OPENAPI_FILE.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(f"openapi.yaml is not valid YAML: {exc}")
        return 1
    ok("openapi.yaml parses as YAML")

    # --- version -------------------------------------------------------
    if spec.get("openapi") != "3.1.0":
        fail(f"openapi version is {spec.get('openapi')!r}, expected '3.1.0'")
    else:
        ok("declares OpenAPI 3.1.0")

    # --- operation ids -------------------------------------------------
    spec_ops = {
        operation["operationId"]
        for path in spec.get("paths", {}).values()
        for method, operation in path.items()
        if isinstance(operation, dict) and "operationId" in operation
    }
    live = app.openapi()
    live_ops = {
        operation["operationId"]
        for path in live.get("paths", {}).values()
        for method, operation in path.items()
        if isinstance(operation, dict) and "operationId" in operation
    }

    missing = REQUIRED_OPERATION_IDS - spec_ops
    if missing:
        fail(f"openapi.yaml is missing operationIds: {sorted(missing)}")
    else:
        ok(f"all required operationIds present: {sorted(REQUIRED_OPERATION_IDS)}")

    not_in_app = spec_ops - live_ops
    if not_in_app:
        fail(f"openapi.yaml declares operations the app does not serve: {sorted(not_in_app)}")
    else:
        ok("every declared operation exists in the application")

    # --- paths ---------------------------------------------------------
    for path in spec.get("paths", {}):
        if path not in live.get("paths", {}):
            fail(f"path {path} is declared but not served by the app")
    ok("all declared paths are served")

    # --- security ------------------------------------------------------
    scheme = (
        spec.get("components", {}).get("securitySchemes", {}).get("ClinicalAPIKey")
    )
    expected = {"type": "apiKey", "in": "header", "name": "X-Clinical-Key"}
    if scheme != expected:
        fail(f"ClinicalAPIKey scheme is {scheme!r}, expected {expected!r}")
    else:
        ok("ClinicalAPIKey security scheme matches X-Clinical-Key header")

    if spec.get("security") != [{"ClinicalAPIKey": []}]:
        fail("top-level security must be [{ClinicalAPIKey: []}]")
    else:
        ok("global security requirement applied")

    # --- request field coverage ---------------------------------------
    schemas = spec.get("components", {}).get("schemas", {})
    checks = [
        ("EvidenceSearchRequest", EvidenceSearchRequest),
        ("RegulatorySearchRequest", RegulatorySearchRequest),
        ("ManufacturerSearchRequest", ManufacturerSearchRequest),
    ]
    for name, model in checks:
        declared = set((schemas.get(name) or {}).get("properties", {}))
        actual = set(model.model_fields)
        missing_fields = actual - declared
        extra_fields = declared - actual
        if missing_fields:
            fail(f"{name}: fields accepted by the API but missing from the schema: "
                 f"{sorted(missing_fields)}")
        if extra_fields:
            fail(f"{name}: schema declares fields the API ignores: {sorted(extra_fields)}")
        if not missing_fields and not extra_fields:
            ok(f"{name}: {len(actual)} fields match")

    # --- response field coverage --------------------------------------
    for name, model in [
        ("SearchResult", SearchResult),
        ("SearchResponse", SearchResponse),
        ("SourceVerifyResponse", SourceVerifyResponse),
    ]:
        declared = set((schemas.get(name) or {}).get("properties", {}))
        actual = set(model.model_fields)
        # `debug` is development-only and intentionally undocumented.
        actual.discard("debug")
        missing_fields = actual - declared
        extra_fields = declared - actual
        if missing_fields:
            fail(f"{name}: fields returned by the API but undocumented: "
                 f"{sorted(missing_fields)}")
        if extra_fields:
            fail(f"{name}: schema declares fields the API never returns: "
                 f"{sorted(extra_fields)}")
        if not missing_fields and not extra_fields:
            ok(f"{name}: {len(actual)} fields match")

    # --- enum coverage --------------------------------------------------
    evidence_levels = set(
        (schemas.get("SearchResult", {}).get("properties", {})
         .get("evidence_level", {}).get("enum", []))
    )
    from app.models.schemas import EvidenceLevel

    actual_levels = {e.value for e in EvidenceLevel}
    if evidence_levels != actual_levels:
        fail(f"evidence_level enum mismatch: {sorted(evidence_levels ^ actual_levels)}")
    else:
        ok("evidence_level enum matches EvidenceLevel")

    # --- servers placeholder reminder ----------------------------------
    server_url = (spec.get("servers") or [{}])[0].get("url", "")
    if "example.com" in server_url:
        print(
            f"  note  servers[0].url is still the placeholder ({server_url}); "
            "set it to your deployed HTTPS URL before importing into GPT Builder"
        )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} problem(s) found.")
        return 1
    print("openapi.yaml is consistent with the application.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
