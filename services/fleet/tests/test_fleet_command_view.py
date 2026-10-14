"""``CommandView.end_job`` tells a dock that ended the job from one that kept it;
``CommandView.pattern`` shows the pattern a start was sent with."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import ReturnToDock, StartMow

from fleet.application.commands import audit_form, view
from fleet.domain.models import CommandRecord

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _record(kind: str, command: dict[str, object]) -> CommandRecord:
    return CommandRecord(
        command_id=uuid4(),
        account_id=uuid4(),
        mower_id=uuid4(),
        kind=kind,
        command=command,
        priority="normal",
        issued_by_subject=uuid4(),
        issued_by={"subject": str(uuid4()), "kind": "user"},
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        status="pending",
        correlation_id="c-1",
    )


@pytest.mark.parametrize("end_job", [True, False])
def test_return_to_dock_view_carries_end_job(end_job: bool) -> None:
    command = ReturnToDock(end_job=end_job).model_dump(mode="json")
    assert view(_record("return_to_dock", command)).end_job is end_job


def test_older_records_and_other_kinds() -> None:
    assert view(_record("return_to_dock", {"kind": "return_to_dock"})).end_job is False
    assert view(_record("pause_mow", {"kind": "pause_mow"})).end_job is None


def test_start_view_carries_the_pattern() -> None:
    pattern = {"name": "diagonal", "angle_deg": 45.0, "border_laps": 1}
    command, _ = audit_form(StartMow(area_ids=["1"], pattern=pattern))
    assert view(_record("start_mow", command)).pattern == pattern
    plain, _ = audit_form(StartMow(area_ids=["1"]))
    assert view(_record("start_mow", plain)).pattern is None
    assert view(_record("pause_mow", {"kind": "pause_mow"})).pattern is None
