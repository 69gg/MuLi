from __future__ import annotations

import json
from typing import Literal

import pytest
from pydantic import ValidationError

from muli.compatibility import CompatibilityManifest, FormatContracts, FormatSupport
from muli.format_admission import CURRENT_FORMAT_STATE
from muli.managed_transaction import (
    HealthProofV2,
    ManagedAction,
    RoleBinding,
    ServiceIdentityV2,
    TransactionRecordV2,
    TransitionV2,
    advance_record_v2,
    compatibility_digest,
    format_digest,
    parse_record_v2,
    prepare_record_v2,
    raw_snapshot,
)
from muli.management_transaction import (
    ClockReading,
    Deadline,
    JournalLimits,
    ProjectionReceipt,
    Revision,
    Role,
    ServiceIdentity,
    SnapshotReference,
    TransactionError,
    TransactionRecord,
    _snapshot,
)

CONFIG = b'{ "credential":"private-fixture", "port":8765 }\n'
UNIT = b"[Service]\nExecStart=/retained/python -I /retained/launcher.py\n"


def manifest() -> CompatibilityManifest:
    support = FormatSupport(reads=(1,), writes=1)
    return CompatibilityManifest(
        metadata_version=1,
        distribution="muli-agent",
        release="2.22.0",
        handoff_protocol=2,
        requires_migration=False,
        formats=FormatContracts(
            database=support,
            objects=support,
            configuration=support,
            ingress=support,
            outbox=support,
            memory=support,
            install_profile=support,
        ),
    )


def deadline(*, now: float = 100.0) -> Deadline:
    return Deadline(
        created=ClockReading(boot_id="kernel", wall=now + 1000.0, monotonic=now),
        seconds=180.0,
        rollback_reserve=60.0,
    )


def identity() -> ServiceIdentityV2:
    return ServiceIdentityV2(
        deployment="d" * 64, store_instance="store", main_session="main", origin_boot="origin"
    )


def binding(slot: str, *, legacy: bool = False, configuration: bytes = CONFIG) -> RoleBinding:
    return RoleBinding(
        entry="legacy" if legacy else "managed",
        installation=slot * 64,
        compatibility=compatibility_digest(manifest()),
        configuration=_snapshot(configuration, 4096),
        selector=None
        if legacy
        else _snapshot(
            json.dumps({"deployment": "d" * 64, "installation": slot * 64}).encode(), 4096
        ),
        unit=raw_snapshot(
            b"[Service]\nExecStart=/original/python -m muli\n" if legacy else UNIT, 4096
        ),
    )


def prepared(
    action: ManagedAction = "switch",
    *,
    request: str = "request",
    generation: str | None = None,
    previous: TransactionRecord | TransactionRecordV2 | None = None,
    projected: ProjectionReceipt | None = None,
    until: Deadline | None = None,
    base: RoleBinding | None = None,
    target: RoleBinding | None = None,
    principal: ServiceIdentityV2 | None = None,
) -> TransactionRecordV2:
    first = base or binding("a", legacy=action == "adopt")
    second = target or (
        binding("b")
        if action == "switch"
        else binding("a", configuration=b'{"port":8766}')
        if action == "configure"
        else binding("a")
    )
    return prepare_record_v2(
        request_id=request,
        action=action,
        identity=principal or identity(),
        base=first,
        target=second,
        observed_format=CURRENT_FORMAT_STATE,
        base_manifest=manifest(),
        target_manifest=manifest(),
        deadline=until or deadline(),
        generation=generation,
        previous=previous,
        projected=projected,
    )


def move(record: TransactionRecordV2, transition: TransitionV2) -> TransactionRecordV2:
    return advance_record_v2(record, record.position, transition)


def verifying(record: TransactionRecordV2) -> TransactionRecordV2:
    for transition in (
        TransitionV2(phase="draining"),
        TransitionV2(phase="activating", intent="stop_base"),
        TransitionV2(phase="activating", intent="install_target"),
        TransitionV2(phase="activating", intent="start_target"),
        TransitionV2(phase="verifying"),
    ):
        record = move(record, transition)
    return record


def proof(
    record: TransactionRecordV2,
    *,
    role: Role = "target",
    boot: str = "fresh",
    admission: Literal["paused", "released", "released_degraded", "blocked"] = "paused",
) -> HealthProofV2:
    selected = record.binding(role)
    challenge = record.challenge if role == "target" else record.rollback_challenge
    assert challenge is not None
    return HealthProofV2(
        generation=record.generation,
        challenge=challenge,
        boot=boot,
        role=role,
        deployment=record.identity.deployment,
        installation=selected.installation,
        compatibility=selected.compatibility,
        configuration=selected.configuration.digest,
        selector=selected.selector.digest if selected.selector else None,
        unit=selected.unit.digest,
        store_instance=record.identity.store_instance,
        main_session=record.identity.main_session,
        format_digest=format_digest(record.observed_format),
        core_ready=True,
        admission=admission,
    )


def receipt(record: TransactionRecord | TransactionRecordV2) -> ProjectionReceipt:
    return ProjectionReceipt(
        **record.position.model_dump(), store_instance=record.identity.store_instance
    )


def test_both_metadata_digests_use_the_existing_canonical_json_semantics() -> None:
    declaration, state = manifest(), CURRENT_FORMAT_STATE
    for model, digest in (
        (declaration, compatibility_digest(declaration)),
        (state, format_digest(state)),
    ):
        reordered = json.dumps(model.model_dump(), sort_keys=True, indent=2).encode()
        assert digest == _snapshot(reordered, 16_384).semantic_digest


def test_unit_reference_preserves_exact_non_json_bytes_and_bounded_size() -> None:
    raw = raw_snapshot(UNIT, len(UNIT))
    assert raw.size == len(UNIT)
    assert raw != raw_snapshot(UNIT + b"\n", len(UNIT) + 1)
    with pytest.raises(TransactionError, match="snapshot_budget"):
        raw_snapshot(UNIT, len(UNIT) - 1)
    with pytest.raises(TransactionError, match="invalid_snapshot"):
        raw_snapshot(b"", 4096)


@pytest.mark.parametrize("action", ["adopt", "switch", "configure", "restart"])
def test_each_action_roundtrips_without_exposing_private_snapshots(action: ManagedAction) -> None:
    record = prepared(action)
    assert parse_record_v2(record.model_dump_json().encode()) == record
    assert record.protocol == 2 and record.phase == "prepared" and record.boot_role is None
    public = json.dumps(record.public())
    assert all(
        value not in public for value in (record.challenge, "private-fixture", "installation")
    )
    with pytest.raises(ValidationError):
        record.phase = "committed"  # type: ignore[misc]


@pytest.mark.parametrize("protocol", [1, True, 2.0, "2", None])
def test_protocol_number_is_explicit_strict_and_cannot_select_v1(protocol: object) -> None:
    record = prepared()
    values = record.model_dump()
    values["protocol"] = protocol
    with pytest.raises(TransactionError, match="invalid_record"):
        parse_record_v2(json.dumps(values).encode())
    with pytest.raises(ValidationError):
        TransactionRecordV2.model_validate(values)
    healthy = proof(verifying(record)).model_dump()
    healthy["protocol"] = protocol
    with pytest.raises(ValidationError):
        HealthProofV2.model_validate(healthy)


@pytest.mark.parametrize("change", ["missing_protocol", "extra", "nested_extra", "duplicate"])
def test_parser_rejects_incomplete_or_ambiguous_private_record(change: str) -> None:
    values = prepared().model_dump()
    if change == "missing_protocol":
        del values["protocol"]
    elif change == "extra":
        values["approved"] = True
    elif change == "nested_extra":
        values["target"]["trusted"] = True
    data = json.dumps(values).encode()
    if change == "duplicate":
        data = b'{"protocol":1,' + data[1:]
    with pytest.raises(TransactionError, match="invalid_record"):
        parse_record_v2(data)


def test_record_read_budget_precedes_json_parsing() -> None:
    limits = JournalLimits(max_record_bytes=4096)
    with pytest.raises(TransactionError, match="record_budget"):
        parse_record_v2(b"{" + b" " * limits.max_record_bytes, limits=limits)


@pytest.mark.parametrize("legacy,selector", [(False, None), (True, _snapshot(b"{}", 4096))])
def test_entry_type_and_selector_presence_are_bound(
    legacy: bool, selector: SnapshotReference | None
) -> None:
    values = binding("a", legacy=legacy).model_dump()
    values["selector"] = selector
    with pytest.raises(ValidationError, match="invalid_role_selector"):
        RoleBinding.model_validate(values)


@pytest.mark.parametrize("change", ["installation", "compatibility", "configuration"])
def test_adoption_cannot_also_change_program_or_configuration(change: str) -> None:
    record = prepared("adopt")
    values = record.model_dump()
    values["target"][change] = prepared("switch").target.model_dump()[change]
    if change == "compatibility":
        values["target"][change] = "c" * 64
    if change == "configuration":
        values["target"][change] = _snapshot(b'{"changed":true}', 4096).model_dump()
    with pytest.raises(ValidationError, match="invalid_adoption"):
        TransactionRecordV2.model_validate(values)


@pytest.mark.parametrize(
    "action,change",
    [
        ("switch", "same_slot"),
        ("switch", "unit"),
        ("configure", "installation"),
        ("configure", "selector"),
        ("restart", "configuration"),
    ],
)
def test_managed_actions_cannot_silently_change_other_bound_effects(
    action: ManagedAction, change: str
) -> None:
    values = prepared(action).model_dump()
    if change == "same_slot":
        values["target"]["installation"] = values["base"]["installation"]
    elif change in {"configuration", "unit", "selector"}:
        values["target"][change]["digest"] = "c" * 64
    else:
        values["target"][change] = "c" * 64
    with pytest.raises(ValidationError):
        TransactionRecordV2.model_validate(values)


def test_same_configuration_switch_still_freezes_distinct_selection_and_role() -> None:
    record = prepared()
    assert record.base.configuration == record.target.configuration
    assert record.base.selector != record.target.selector
    assert record.base.installation != record.target.installation
    record = verifying(record)
    assert record.boot_role == "target"
    assert record.accepts(proof(record), "target")
    assert not record.accepts(proof(record), "base")


def test_only_the_exact_boot_window_can_accept_a_selected_role() -> None:
    record = prepared()
    healthy = proof(verifying(record))
    assert not record.accepts(healthy, "target")
    assert not record.accepts(healthy, "base")
    for transition, expected in (
        (TransitionV2(phase="draining"), None),
        (TransitionV2(phase="activating", intent="stop_base"), None),
        (TransitionV2(phase="activating", intent="install_target"), None),
        (TransitionV2(phase="activating", intent="start_target"), "target"),
    ):
        record = move(record, transition)
        assert record.boot_role == expected


@pytest.mark.parametrize("generation", ["", "A" * 32, "a" * 31, "a" * 33, "../" + "a" * 29])
def test_generation_for_snapshot_names_is_bounded_lowercase_hex(generation: str) -> None:
    with pytest.raises(TransactionError, match="invalid_generation"):
        prepared(generation=generation)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", "another-generation"),
        ("challenge", "stale-nonce"),
        ("boot", "origin"),
        ("role", "base"),
        ("role", "origin"),
        ("deployment", "c" * 64),
        ("installation", "a" * 64),
        ("compatibility", "c" * 64),
        ("configuration", "c" * 64),
        ("selector", None),
        ("selector", "c" * 64),
        ("unit", "c" * 64),
        ("store_instance", "another-store"),
        ("main_session", "another-main"),
        ("format_digest", "c" * 64),
        ("core_ready", False),
        ("admission", "released"),
    ],
)
def test_health_proof_must_match_every_frozen_fact(field: str, value: object) -> None:
    record = verifying(prepared())
    values = proof(record).model_dump()
    values[field] = value
    assert not record.accepts(HealthProofV2.model_validate(values), "target")


def test_nonce_confirmation_then_release_preserves_the_durable_decision() -> None:
    record = verifying(prepared())
    committed = move(record, TransitionV2(phase="committed", proof=proof(record)))
    assert not committed.resolved and committed.activation == "pending"
    released = move(
        committed,
        TransitionV2(
            phase="committed", activation="released", proof=proof(committed, admission="released")
        ),
    )
    assert released.resolved and released.health is not None
    assert released.deadline == record.deadline and released.identity == record.identity
    with pytest.raises(TransactionError, match="invalid_release_proof"):
        move(
            released,
            TransitionV2(
                phase="committed",
                activation="released",
                proof=proof(released, admission="released"),
            ),
        )


def test_adoption_rollback_proves_legacy_role_without_requiring_a_selector() -> None:
    record = verifying(prepared("adopt"))
    record = move(
        record, TransitionV2(phase="rolling_back", intent="stop_target", code="health_failed")
    )
    challenge = record.rollback_challenge
    assert challenge is not None and challenge != record.challenge and record.boot_role is None
    for intent in ("restore_base", "start_base", "verify_base"):
        record = move(record, TransitionV2(phase="rolling_back", intent=intent))  # type: ignore[arg-type]
    healthy = proof(record, role="base", boot="restored")
    assert healthy.selector is None and record.boot_role == "base"
    assert record.accepts(healthy, "base") and not record.accepts(healthy, "target")
    record = move(record, TransitionV2(phase="rolled_back", proof=healthy))
    assert record.failure_code == "health_failed" and not record.resolved
    record = move(
        record,
        TransitionV2(
            phase="rolled_back",
            activation="released_degraded",
            proof=proof(record, role="base", boot="restored", admission="released_degraded"),
        ),
    )
    assert record.resolved and record.base.entry == "legacy"


def test_blocked_activation_requires_a_new_boot_before_paused_reproof() -> None:
    record = verifying(prepared())
    record = move(record, TransitionV2(phase="committed", proof=proof(record)))
    record = move(
        record, TransitionV2(phase="committed", activation="blocked", code="activation_failed")
    )
    with pytest.raises(TransactionError, match="invalid_release_proof"):
        move(record, TransitionV2(phase="committed", proof=proof(record)))
    resumed = move(record, TransitionV2(phase="committed", proof=proof(record, boot="fresh-again")))
    assert resumed.phase == "committed" and resumed.activation == "pending"
    assert resumed.failure_code == "activation_failed"


def test_invalid_revision_or_skipped_intent_never_changes_the_input_record() -> None:
    record = prepared()
    with pytest.raises(TransactionError, match="stale_revision"):
        advance_record_v2(
            record,
            Revision(generation=record.generation, revision=2),
            TransitionV2(phase="draining"),
        )
    with pytest.raises(TransactionError, match="invalid_transition"):
        move(record, TransitionV2(phase="verifying"))
    stopping = move(
        move(record, TransitionV2(phase="draining")),
        TransitionV2(phase="activating", intent="stop_base"),
    )
    with pytest.raises(TransactionError, match="invalid_intent_transition"):
        move(stopping, TransitionV2(phase="activating", intent="start_target"))
    assert record.revision == 1 and record.phase == "prepared"


def test_prepare_retry_keeps_original_deadline_nonce_and_revision() -> None:
    record = prepared(generation="1" * 32)
    retried = prepared(previous=record, generation=record.generation, until=deadline(now=120.0))
    assert retried is record
    assert (
        retried.deadline.remaining(ClockReading(boot_id="kernel", wall=1120.0, monotonic=120.0))
        == 160.0
    )
    with pytest.raises(TransactionError, match="request_conflict"):
        prepared(previous=record, generation="2" * 32)
    with pytest.raises(TransactionError, match="request_conflict"):
        prepared(previous=record, target=binding("c"))


def test_successor_requires_exact_projection_and_same_store_and_main() -> None:
    record = move(prepared(), TransitionV2(phase="aborted", code="cancelled"))
    with pytest.raises(TransactionError, match="projection_required"):
        prepared(request="next", previous=record)
    bad = ProjectionReceipt(generation=record.generation, revision=1, store_instance="store")
    with pytest.raises(TransactionError, match="projection_required"):
        prepared(request="next", previous=record, projected=bad)
    for field in ("store_instance", "main_session"):
        values = identity().model_dump()
        values[field] = "different"
        with pytest.raises(TransactionError, match="store_changed"):
            prepared(
                request="next",
                previous=record,
                projected=receipt(record),
                principal=ServiceIdentityV2.model_validate(values),
            )
    with pytest.raises(TransactionError, match="generation_conflict"):
        prepared(
            request="next", previous=record, projected=receipt(record), generation=record.generation
        )
    successor = prepared(request="next", previous=record, projected=receipt(record))
    assert successor.generation != record.generation and successor.revision == 1


def test_v1_unresolved_or_unprojected_predecessor_is_never_reinterpreted_as_v2() -> None:
    old = TransactionRecord(
        request_id="old",
        generation="1" * 32,
        revision=1,
        action="restart",
        identity=ServiceIdentity(
            installation="a" * 64,
            service="b" * 64,
            store_instance="store",
            main_session="main",
            origin_boot="origin",
        ),
        base=_snapshot(CONFIG, 4096),
        target=_snapshot(CONFIG, 4096),
        deadline=deadline(),
        challenge="old-challenge",
    )
    with pytest.raises(TransactionError, match="transaction_in_progress"):
        prepared("adopt", previous=old)
    with pytest.raises(TransactionError, match="invalid_record"):
        parse_record_v2(old.model_dump_json().encode())
    with pytest.raises(ValidationError):
        TransactionRecord.model_validate(prepared().model_dump())
    resolved = TransactionRecord.model_validate(
        {**old.model_dump(), "phase": "aborted", "code": "cancelled"}
    )
    with pytest.raises(TransactionError, match="projection_required"):
        prepared("adopt", previous=resolved)
    assert prepared("adopt", previous=resolved, projected=receipt(resolved)).protocol == 2


@pytest.mark.parametrize("problem", ["digest", "protocol", "migration", "format"])
def test_prepare_requires_actual_matching_compatible_declarations(problem: str) -> None:
    declaration = manifest().model_dump()
    if problem == "protocol":
        declaration["handoff_protocol"] = 1
    elif problem == "migration":
        declaration["requires_migration"] = True
    elif problem == "format":
        declaration["formats"]["outbox"] = {"reads": (1, 2), "writes": 2}
    candidate = CompatibilityManifest.model_validate(declaration)
    selected = binding("b").model_dump()
    selected["compatibility"] = "c" * 64 if problem == "digest" else compatibility_digest(candidate)
    with pytest.raises(TransactionError):
        prepare_record_v2(
            request_id="request",
            action="switch",
            identity=identity(),
            base=binding("a"),
            target=RoleBinding.model_validate(selected),
            observed_format=CURRENT_FORMAT_STATE,
            base_manifest=manifest(),
            target_manifest=candidate,
            deadline=deadline(),
        )
