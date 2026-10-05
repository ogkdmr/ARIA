#!/usr/bin/env python3
"""Validate GMP v3 contract fixtures and profile references.

Dependency-free validator (stdlib only). It checks:
1) v3 envelope files exist
2) core-v3 plus core-v3-companion profile required schema paths resolve
3) schema and fixture files are valid JSON (including companion/schemas)
4) fixture payloads satisfy recursive required/type/enum/const/allOf/oneOf/
   if-then-else/$ref rules
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
SCHEMAS_COMMON = CONTRACTS / "schemas" / "common"
SCHEMAS_EVENTS = CONTRACTS / "schemas" / "events"
SCHEMAS_COMPANION = CONTRACTS / "companion" / "schemas"
FIXTURES = CONTRACTS / "fixtures" / "v3"
FIXTURES_COMPANION = CONTRACTS / "fixtures" / "companion"
PROFILE_V3 = CONTRACTS / "profiles" / "core-v3.json"
PROFILE_V3_COMPANION = CONTRACTS / "profiles" / "core-v3-companion.json"
OPENAPI_V3 = CONTRACTS / "openapi" / "gmp-core-v3.yaml"


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _collect_schema_store() -> tuple[dict[str, dict], dict[Path, dict]]:
    by_id: dict[str, dict] = {}
    by_path: dict[Path, dict] = {}
    paths: list[Path] = list(SCHEMAS_COMMON.glob("*.json")) + list(SCHEMAS_EVENTS.glob("*.json"))
    if SCHEMAS_COMPANION.exists():
        paths.extend(SCHEMAS_COMPANION.glob("*.json"))
    for schema_path in paths:
        schema = _read_json(schema_path)
        by_path[schema_path.resolve()] = schema
        schema_id = schema.get("$id")
        if isinstance(schema_id, str):
            by_id[schema_id] = schema
    return by_id, by_path


def _matches_type(value: object, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def _equals_const(value: object, expected: object) -> bool:
    """JSON equality for const: Python treats True == 1, JSON Schema does not."""
    if isinstance(value, bool) or isinstance(expected, bool):
        return isinstance(value, bool) and isinstance(expected, bool) and value is expected
    return value == expected


def _resolve_pointer(document: dict, pointer: str) -> dict | None:
    """Walk an RFC 6901 JSON pointer (e.g. '/properties/defaultTier') within a document."""
    if pointer in ("", "/"):
        return document
    current: object = document
    for raw_token in pointer.lstrip("/").split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                return None
            current = current[int(token)]
        elif isinstance(current, dict):
            if token not in current:
                return None
            current = current[token]
        else:
            return None
    return current if isinstance(current, dict) else None


def _resolve_ref(ref: str, current_schema_path: Path, schemas_by_id: dict[str, dict], schemas_by_path: dict[Path, dict]) -> tuple[dict | None, Path | None]:
    base, _, pointer = ref.partition("#")
    if not base:
        # Same-document pointer; resolve against the document currently being walked.
        document = schemas_by_path.get(current_schema_path.resolve())
        if document is None or not pointer:
            return None, None
        return _resolve_pointer(document, pointer), current_schema_path
    if base.startswith("http://") or base.startswith("https://"):
        document = schemas_by_id.get(base)
        target_path = None
    else:
        target_path = (current_schema_path.parent / base).resolve()
        document = schemas_by_path.get(target_path)
    if document is None or not pointer:
        return document, target_path
    return _resolve_pointer(document, pointer), target_path


def _validate_instance(
    instance: object,
    schema: dict,
    schema_path: Path,
    context: str,
    schemas_by_id: dict[str, dict],
    schemas_by_path: dict[Path, dict],
) -> list[str]:
    errors: list[str] = []

    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str):
            return [f"{context}: invalid $ref type"]
        resolved, resolved_path = _resolve_ref(ref, schema_path, schemas_by_id, schemas_by_path)
        if resolved is None:
            return [f"{context}: unresolved $ref '{ref}'"]
        next_path = resolved_path if resolved_path is not None else schema_path
        return _validate_instance(instance, resolved, next_path, context, schemas_by_id, schemas_by_path)

    if "oneOf" in schema:
        one_of = schema.get("oneOf")
        if isinstance(one_of, list) and one_of:
            for idx, subschema in enumerate(one_of):
                if isinstance(subschema, dict):
                    branch_errs = _validate_instance(
                        instance,
                        subschema,
                        schema_path,
                        f"{context}.oneOf[{idx}]",
                        schemas_by_id,
                        schemas_by_path,
                    )
                    if not branch_errs:
                        return []
            return [f"{context}: no oneOf branch validated"]
        return errors

    # allOf does not short-circuit: sibling keywords (properties, required,
    # additionalProperties) on the same schema must still be evaluated.
    if "allOf" in schema:
        all_of = schema.get("allOf", [])
        if isinstance(all_of, list):
            for idx, subschema in enumerate(all_of):
                if isinstance(subschema, dict):
                    errors.extend(
                        _validate_instance(
                            instance,
                            subschema,
                            schema_path,
                            f"{context}.allOf[{idx}]",
                            schemas_by_id,
                            schemas_by_path,
                        )
                    )

    # A failing "if" is not itself an error; it only selects "then" or "else".
    if "if" in schema and isinstance(schema["if"], dict):
        if_errors = _validate_instance(
            instance,
            schema["if"],
            schema_path,
            f"{context}.if",
            schemas_by_id,
            schemas_by_path,
        )
        branch_key = "else" if if_errors else "then"
        branch = schema.get(branch_key)
        if isinstance(branch, dict):
            errors.extend(
                _validate_instance(
                    instance,
                    branch,
                    schema_path,
                    f"{context}.{branch_key}",
                    schemas_by_id,
                    schemas_by_path,
                )
            )

    if "const" in schema and not _equals_const(instance, schema["const"]):
        errors.append(f"{context}: value '{instance}' does not equal const '{schema['const']}'")

    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        if not any(_matches_type(instance, t) for t in expected_type if isinstance(t, str)):
            errors.append(f"{context}: expected type one of {expected_type}, got {type(instance).__name__}")
            return errors
    elif isinstance(expected_type, str):
        if not _matches_type(instance, expected_type):
            errors.append(f"{context}: expected type '{expected_type}', got {type(instance).__name__}")
            return errors

    if "enum" in schema and isinstance(schema["enum"], list):
        if instance not in schema["enum"]:
            errors.append(f"{context}: value '{instance}' not in enum {schema['enum']}")

    if isinstance(instance, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for field in required:
                if isinstance(field, str) and field not in instance:
                    errors.append(f"{context}: missing required field '{field}'")

        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, value in instance.items():
                if key in properties and isinstance(properties[key], dict):
                    errors.extend(
                        _validate_instance(
                            value,
                            properties[key],
                            schema_path,
                            f"{context}.{key}",
                            schemas_by_id,
                            schemas_by_path,
                        )
                    )
                elif schema.get("additionalProperties") is False:
                    errors.append(f"{context}: unexpected field '{key}'")
            additional = schema.get("additionalProperties")
            if isinstance(additional, dict):
                for key, value in instance.items():
                    if key not in properties:
                        errors.extend(
                            _validate_instance(
                                value,
                                additional,
                                schema_path,
                                f"{context}.{key}",
                                schemas_by_id,
                                schemas_by_path,
                            )
                        )

    if isinstance(instance, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for idx, item in enumerate(instance):
                errors.extend(
                    _validate_instance(
                        item,
                        items_schema,
                        schema_path,
                        f"{context}[{idx}]",
                        schemas_by_id,
                        schemas_by_path,
                    )
                )

    return errors


def _without(document: dict, *fields: str) -> dict:
    return {key: value for key, value in document.items() if key not in fields}


def _check_execution_context_run_class(
    schemas_by_id: dict[str, dict],
    schemas_by_path: dict[Path, dict],
) -> list[str]:
    """RFC 017 negative checks: execution_context_run_class_enforced.

    A passing fixture suite cannot show that the runClass conditional binds,
    because an engine that ignores if/then also passes it. Each mutated
    context below must be rejected, and the controls must be accepted.
    """
    schema_path = (SCHEMAS_COMMON / "execution-context.schema.json").resolve()
    try:
        schema = _read_json(schema_path)
        reproducible = _read_json(FIXTURES / "execution-context-reproducible.request.json")
        analysis = _read_json(FIXTURES / "execution-context-analysis.request.json")
        operational = _read_json(FIXTURES / "execution-context-operational.request.json")
    except Exception as exc:
        return [f"RFC 017 run-class checks could not load inputs: {exc}"]

    def validate(instance: dict, against: dict, label: str) -> list[str]:
        return _validate_instance(instance, against, schema_path, label, schemas_by_id, schemas_by_path)

    errors: list[str] = []
    must_reject = {
        "reproducible-minus-seedList": _without(reproducible, "seedList"),
        "reproducible-minus-dataVersion": _without(reproducible, "dataVersion"),
        "analysis-minus-dataVersion": _without(analysis, "dataVersion"),
    }
    for label, instance in must_reject.items():
        if not validate(instance, schema, label):
            errors.append(f"RFC 017: {label} was accepted; the runClass conditional is not enforced")

    must_accept = {
        "operational-explicit-without-seed-or-dataVersion": {**operational, "runClass": "operational"},
        "analysis-without-seedList": analysis,
    }
    for label, instance in must_accept.items():
        rejection = validate(instance, schema, label)
        if rejection:
            errors.append(f"RFC 017: {label} was rejected; seedList/dataVersion were re-required unconditionally: {rejection}")

    # The rejection above must come from the conditional, not from a re-widened
    # base required array: with allOf removed the same mutation has to pass.
    unconditional = _without(schema, "allOf")
    if validate(must_reject["reproducible-minus-seedList"], unconditional, "base-schema-control"):
        errors.append("RFC 017: base ExecutionContext schema rejects a context without seedList; required must stay [configHash, environmentHash]")

    return errors


def main() -> int:
    errors: list[str] = []

    if not OPENAPI_V3.exists():
        errors.append(f"Missing required API spec: {OPENAPI_V3}")
    if not PROFILE_V3.exists():
        errors.append(f"Missing required profile: {PROFILE_V3}")
    if not PROFILE_V3_COMPANION.exists():
        errors.append(f"Missing companion profile: {PROFILE_V3_COMPANION}")

    try:
        profile = _read_json(PROFILE_V3)
    except Exception as exc:
        errors.append(f"Failed to parse {PROFILE_V3}: {exc}")
        profile = {}

    companion_profile: dict = {}
    if PROFILE_V3_COMPANION.exists():
        try:
            companion_profile = _read_json(PROFILE_V3_COMPANION)
        except Exception as exc:
            errors.append(f"Failed to parse {PROFILE_V3_COMPANION}: {exc}")

    for rel in profile.get("requiredSchemas", []):
        full_path = (PROFILE_V3.parent / rel).resolve()
        if not full_path.exists():
            errors.append(f"Missing required schema from profile: {rel}")
        else:
            try:
                _read_json(full_path)
            except Exception as exc:
                errors.append(f"Failed to parse schema {rel}: {exc}")

    for rel in companion_profile.get("requiredSchemas", []) if companion_profile else []:
        full_path = (PROFILE_V3_COMPANION.parent / rel).resolve()
        if not full_path.exists():
            errors.append(f"Missing required schema from companion profile: {rel}")
        else:
            try:
                _read_json(full_path)
            except Exception as exc:
                errors.append(f"Failed to parse companion schema {rel}: {exc}")

    schemas_by_id, schemas_by_path = _collect_schema_store()

    direct_mappings = {
        "capability-token-mint.request.json": SCHEMAS_COMMON / "capability-token.schema.json",
        "memory-record.request.json": SCHEMAS_COMMON / "memory-record.schema.json",
        "usage-meter.response.json": SCHEMAS_COMMON / "usage-meter.schema.json",
        "budget-policy.request.json": SCHEMAS_COMMON / "budget-policy.schema.json",
        "workflow-state.response.json": SCHEMAS_COMMON / "workflow-state.schema.json",
        "reasoning-node-event.json": SCHEMAS_EVENTS / "reasoning-node-event.schema.json",
        "tool-call-event.json": SCHEMAS_EVENTS / "tool-call-event.schema.json",
        "tool-call-event-accounting.json": SCHEMAS_EVENTS / "tool-call-event.schema.json",
        "run-lifecycle-cancelled.json": SCHEMAS_EVENTS / "run-lifecycle-event.schema.json",
        "agent-message.request.json": SCHEMAS_COMMON / "agent-message.schema.json",
        "agent-message-run-correlated.request.json": SCHEMAS_COMMON / "agent-message.schema.json",
        "handoff.request.json": SCHEMAS_COMMON / "handoff.schema.json",
        "handoff-attenuated.request.json": SCHEMAS_COMMON / "handoff.schema.json",
        "consensus-decision.response.json": SCHEMAS_COMMON / "consensus-decision.schema.json",
        "side-effect-manifest.request.json": SCHEMAS_COMMON / "side-effect-manifest.schema.json",
        "approval-gate.request.json": SCHEMAS_COMMON / "approval-gate.schema.json",
        "intervention-record.request.json": SCHEMAS_COMMON / "intervention-record.schema.json",
        "identity-session.response.json": SCHEMAS_COMMON / "identity-session.schema.json",
        "events-journal-append.request.json": SCHEMAS_EVENTS / "event-envelope.schema.json",
        "events-journal-attempt.request.json": SCHEMAS_EVENTS / "event-envelope.schema.json",
        "classification-shift-event.json": SCHEMAS_EVENTS / "event-envelope.schema.json",
        "execution-context-operational.request.json": SCHEMAS_COMMON / "execution-context.schema.json",
        "execution-context-analysis.request.json": SCHEMAS_COMMON / "execution-context.schema.json",
        "execution-context-reproducible.request.json": SCHEMAS_COMMON / "execution-context.schema.json",
    }

    for fixture_name, schema_path in direct_mappings.items():
        fixture_path = FIXTURES / fixture_name
        if not fixture_path.exists():
            errors.append(f"Missing fixture: {fixture_path}")
            continue
        if not schema_path.exists():
            errors.append(f"Missing schema: {schema_path}")
            continue
        try:
            fixture = _read_json(fixture_path)
            schema = _read_json(schema_path)
            errors.extend(
                _validate_instance(
                    fixture,
                    schema,
                    schema_path.resolve(),
                    str(fixture_path),
                    schemas_by_id,
                    schemas_by_path,
                )
            )
        except Exception as exc:
            errors.append(f"Validation error for {fixture_path}: {exc}")

    errors.extend(_check_execution_context_run_class(schemas_by_id, schemas_by_path))

    wrapper_schemas = {
        "runs-plan.response.json": {
            "type": "object",
            "required": ["negotiation", "costLatencyHint"],
            "properties": {
                "negotiation": {"$ref": "https://gmp.dev/contracts/schemas/common/negotiation-hint.schema.json"},
                "costLatencyHint": {"$ref": "https://gmp.dev/contracts/schemas/common/cost-latency-hint.schema.json"},
            },
            "additionalProperties": False,
        },
        "checkpoint-list.response.json": {
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"$ref": "https://gmp.dev/contracts/schemas/common/checkpoint.schema.json"},
                }
            },
            "additionalProperties": False,
        },
        "supervision-queue.response.json": {
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"$ref": "https://gmp.dev/contracts/schemas/common/supervision-queue-item.schema.json"},
                }
            },
            "additionalProperties": False,
        },
        "divergence-alert.response.json": {
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"$ref": "https://gmp.dev/contracts/schemas/common/divergence-alert.schema.json"},
                }
            },
            "additionalProperties": False,
        },
        # RFC 005 append projection: clients MUST omit eventId/occurredAt/source.
        "events-append.request.json": {
            "type": "object",
            "required": ["eventType", "payload"],
            "properties": {
                "eventType": {"type": "string"},
                "payload": {"type": "object"},
                "runId": {"type": "string"},
                "agentId": {"type": "string"},
                "correlationId": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "additionalProperties": False,
        },
        # RFC 005 list projection: EventListItem items plus cursor pagination envelope.
        "events-list.response.json": {
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["eventId", "eventType", "occurredAt", "source", "payload"],
                        "properties": {
                            "eventId": {"type": "string"},
                            "eventType": {"type": "string"},
                            "occurredAt": {"type": "string", "format": "date-time"},
                            "source": {"type": "string"},
                            "payload": {"type": "object"},
                            "correlationId": {"type": "string"},
                            "runId": {"type": "string"},
                            "agentId": {"type": "string"},
                            "metadata": {"type": "object"},
                        },
                        "additionalProperties": False,
                    },
                },
                "nextCursor": {"type": ["string", "null"]},
                "total": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        "events-list-no-total.response.json": {
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["eventId", "eventType", "occurredAt", "source", "payload"],
                        "properties": {
                            "eventId": {"type": "string"},
                            "eventType": {"type": "string"},
                            "occurredAt": {"type": "string", "format": "date-time"},
                            "source": {"type": "string"},
                            "payload": {"type": "object"},
                            "correlationId": {"type": "string"},
                            "runId": {"type": "string"},
                            "agentId": {"type": "string"},
                            "metadata": {"type": "object"},
                        },
                        "additionalProperties": False,
                    },
                },
                "nextCursor": {"type": ["string", "null"]},
                "total": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        "runs-submit.request.json": {
            "type": "object",
            "required": ["capabilityId", "executionContext", "idempotencyKey"],
            "properties": {
                "capabilityId": {"type": "string"},
                "planId": {"type": "string"},
                "planRef": {"type": "string", "format": "uri"},
                "planDigest": {"type": "string", "pattern": "^[a-fA-F0-9]{64}$"},
                "executionContext": {"$ref": "https://gmp.dev/contracts/schemas/common/execution-context.schema.json"},
                "idempotencyKey": {"type": "string"},
                "targetStrategy": {
                    "type": "object",
                    "required": ["mode", "targets"],
                    "properties": {
                        "mode": {"type": "string"},
                        "targets": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    }

    for fixture_name, wrapper_schema in wrapper_schemas.items():
        fixture_path = FIXTURES / fixture_name
        if not fixture_path.exists():
            errors.append(f"Missing fixture: {fixture_path}")
            continue
        try:
            fixture = _read_json(fixture_path)
            errors.extend(
                _validate_instance(
                    fixture,
                    wrapper_schema,
                    (SCHEMAS_COMMON / "inline-wrapper.schema.json").resolve(),
                    str(fixture_path),
                    schemas_by_id,
                    schemas_by_path,
                )
            )
        except Exception as exc:
            errors.append(f"Failed to parse fixture {fixture_path}: {exc}")

    companion_schema_dir = SCHEMAS_COMPANION
    companion_mappings: list[tuple[str, Path]] = [
        ("campaign-plan.example.json", companion_schema_dir / "campaign-plan.schema.json"),
        ("data-movement-intent.example.json", companion_schema_dir / "data-movement-intent.schema.json"),
        ("eval-publication.example.json", companion_schema_dir / "eval-publication.schema.json"),
        ("run-invocation.example.json", companion_schema_dir / "run-invocation.schema.json"),
        ("execution-attempt.example.json", companion_schema_dir / "execution-attempt.schema.json"),
        ("execution-attempt-retry.example.json", companion_schema_dir / "execution-attempt.schema.json"),
        ("execution-attempt-lost.example.json", companion_schema_dir / "execution-attempt.schema.json"),
    ]
    if not FIXTURES_COMPANION.exists():
        errors.append(f"Missing companion fixtures directory: {FIXTURES_COMPANION}")
    else:
        for fixture_name, schema_path in companion_mappings:
            fixture_path = FIXTURES_COMPANION / fixture_name
            if not fixture_path.exists():
                errors.append(f"Missing companion fixture: {fixture_path}")
                continue
            if not schema_path.exists():
                errors.append(f"Missing companion schema: {schema_path}")
                continue
            try:
                fixture = _read_json(fixture_path)
                schema = _read_json(schema_path)
                errors.extend(
                    _validate_instance(
                        fixture,
                        schema,
                        schema_path.resolve(),
                        str(fixture_path),
                        schemas_by_id,
                        schemas_by_path,
                    )
                )
            except Exception as exc:
                errors.append(f"Validation error for {fixture_path}: {exc}")

    if errors:
        print("v3 contract validation failed:\n")
        for err in errors:
            print(f"- {err}")
        return 1

    print("v3 contract validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
