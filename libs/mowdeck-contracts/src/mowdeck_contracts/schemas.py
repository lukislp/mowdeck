"""Export the JSON Schemas of all contract models.

Usage::

    python -m mowdeck_contracts.schemas libs/mowdeck-contracts/schemas

writes one file per model to ``<dir>/v1/<Model>.json``. The committed schemas are compared with
a fresh export in the test suite, so CI fails when a model changes without regenerating them.
"""

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mowdeck_contracts import v1
from mowdeck_contracts.v1 import realtime

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

V1_MODELS: Mapping[str, type[BaseModel]] = {
    "EventEnvelope": v1.EventEnvelope[dict[str, Any]],
    "CommandEnvelope": v1.CommandEnvelope,
    "CommandResult": v1.CommandResult,
    "MowerState": v1.MowerState,
    "GeoPosition": v1.GeoPosition,
    "MowerCapabilities": v1.MowerCapabilities,
    "MowPattern": v1.MowPattern,
    "MowerMap": v1.MowerMap,
    "GeoReference": v1.GeoReference,
    "MowerMapSnapshot": v1.MowerMapSnapshot,
    "MowerCapabilitiesSnapshot": v1.MowerCapabilitiesSnapshot,
    "MowPath": v1.MowPath,
    "MowerPathSnapshot": v1.MowerPathSnapshot,
    "PathPreviewRequest": v1.PathPreviewRequest,
    "PathPreviewGatewayRequest": v1.PathPreviewGatewayRequest,
    "PathPreviewReply": v1.PathPreviewReply,
    "MowerRegistration": v1.MowerRegistration,
    "MowerListRequest": v1.MowerListRequest,
    "MowerListReply": v1.MowerListReply,
    "MowerCredentialsRequest": v1.MowerCredentialsRequest,
    "MowerCredentialsReply": v1.MowerCredentialsReply,
    "MowerConnectionEvent": v1.MowerConnectionEvent,
    "LinkStatus": v1.LinkStatus,
    "FirmwareInfo": v1.FirmwareInfo,
    "BridgeScanRequest": v1.BridgeScanRequest,
    "BridgeScanReply": v1.BridgeScanReply,
    "FirmwareCheckRequest": v1.FirmwareCheckRequest,
    "FirmwareCheckReply": v1.FirmwareCheckReply,
    "BaseStationRef": v1.BaseStationRef,
    "BaseStationState": v1.BaseStationState,
    "AreaNamesChanged": v1.AreaNamesChanged,
    "AccountCreated": v1.AccountCreated,
    "UserChanged": v1.UserChanged,
    "GrantsChanged": v1.GrantsChanged,
    "ApiTokenRevoked": v1.ApiTokenRevoked,
    "NotificationSent": v1.NotificationSent,
    "NotificationRaised": v1.NotificationRaised,
    "WebPushPayload": v1.WebPushPayload,
    "CommandRequest": v1.CommandRequest,
    "CommandAccepted": v1.CommandAccepted,
    "CommandView": v1.CommandView,
    "BulkCommandAccepted": v1.BulkCommandAccepted,
    "IssueCommandRequest": v1.IssueCommandRequest,
    "IssueCommandReply": v1.IssueCommandReply,
    "ResolveTargetRequest": v1.ResolveTargetRequest,
    "ResolveTargetReply": v1.ResolveTargetReply,
    "ScheduleRunRecorded": v1.ScheduleRunRecorded,
    "ScheduleDisabled": v1.ScheduleDisabled,
    "DriveStartRequest": v1.DriveStartRequest,
    "DriveStartReply": v1.DriveStartReply,
    "DriveStopRequest": v1.DriveStopRequest,
    "DriveStopReply": v1.DriveStopReply,
    "DriveSessionView": v1.DriveSessionView,
    "BladeHoursThreshold": v1.BladeHoursThreshold,
    "CameraSession": v1.CameraSession,
    "CameraSessionRequest": v1.CameraSessionRequest,
    "CameraSessionReply": v1.CameraSessionReply,
    "CameraReleaseRequest": v1.CameraReleaseRequest,
    "CameraReleaseReply": v1.CameraReleaseReply,
    "RelayAttachRequest": v1.RelayAttachRequest,
    "RelayAttachReply": v1.RelayAttachReply,
    "RelayRenewRequest": v1.RelayRenewRequest,
    "RelayRenewReply": v1.RelayRenewReply,
    "RelayDetachRequest": v1.RelayDetachRequest,
    "RelayDetachReply": v1.RelayDetachReply,
    "VisionFrameSetRequest": v1.VisionFrameSetRequest,
    "VisionFrameSetReply": v1.VisionFrameSetReply,
    "CameraRelayStarted": v1.CameraRelayStarted,
    "CameraRelayStopped": v1.CameraRelayStopped,
    "CameraRelayLeaseOpened": v1.CameraRelayLeaseOpened,
    "CameraRelayLeaseClosed": v1.CameraRelayLeaseClosed,
    "CloudDiscoverRequest": v1.CloudDiscoverRequest,
    "CloudDiscoverReply": v1.CloudDiscoverReply,
    "EntitlementCheckRequest": v1.EntitlementCheckRequest,
    "EntitlementCheckReply": v1.EntitlementCheckReply,
    "EntitlementSnapshot": v1.EntitlementSnapshot,
    "PlanChanged": v1.PlanChanged,
    "PlanSetRequest": v1.PlanSetRequest,
    "PlanSetReply": v1.PlanSetReply,
    "SubscriptionChanged": v1.SubscriptionChanged,
    "SpotMowPlan": v1.SpotMowPlan,
    "SpotRunStatus": v1.SpotRunStatus,
    "DriveCalibration": v1.DriveCalibration,
    "DriveCalibrationStatus": v1.DriveCalibrationStatus,
    "DriveSpeedSetting": v1.DriveSpeedSetting,
    "PositionTrackRequest": v1.PositionTrackRequest,
    "PositionTrackReply": v1.PositionTrackReply,
    "PhotoRequest": v1.PhotoRequest,
    "PhotoReply": v1.PhotoReply,
    "VisionProposalCreated": v1.VisionProposalCreated,
    "VisionProposalUpdated": v1.VisionProposalUpdated,
    "CameraSnapshotRequest": v1.CameraSnapshotRequest,
    "RealtimeClientMessage": realtime.RealtimeClientMessage,
    "RealtimeServerMessage": realtime.RealtimeServerMessage,
    **{model.__name__: model for model in v1.COMMAND_MODELS},
    "Principal": v1.Principal,
    "PrincipalRef": v1.PrincipalRef,
    "ModuleDescriptor": v1.ModuleDescriptor,
    "ProblemDetails": v1.ProblemDetails,
}
"""Schema name to model, for namespace ``v1``."""


def build_schemas() -> dict[str, dict[str, Any]]:
    """Return ``{"v1/<Name>.json": schema}`` for every exported model."""
    result: dict[str, dict[str, Any]] = {}
    for name, model in V1_MODELS.items():
        schema = model.model_json_schema(mode="validation")
        schema["title"] = name
        result[f"v1/{name}.json"] = {"$schema": JSON_SCHEMA_DIALECT, **schema}
    return result


def render(schema: Mapping[str, Any]) -> str:
    """Render a schema deterministically (sorted keys, two space indent, trailing newline)."""
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def export_schemas(out_dir: Path | str) -> list[Path]:
    """Write all schemas below ``out_dir`` and remove stale ``*.json`` files there.

    Returns the written paths in a stable order.
    """
    root = Path(out_dir)
    schemas = build_schemas()
    written: list[Path] = []
    for relative, schema in sorted(schemas.items()):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(schema), encoding="utf-8", newline="\n")
        written.append(path)
    expected = {p.resolve() for p in written}
    for version_dir in {p.parent for p in written}:
        for stale in version_dir.glob("*.json"):
            if stale.resolve() not in expected:
                stale.unlink()
    return written


def main(argv: Sequence[str] | None = None) -> int:
    """Command line entry point: ``python -m mowdeck_contracts.schemas [out_dir]``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1 or (args and args[0] in {"-h", "--help"}):
        print("usage: python -m mowdeck_contracts.schemas [OUT_DIR]  (default: ./schemas)")
        return 0 if args and args[0] in {"-h", "--help"} else 2
    out_dir = Path(args[0]) if args else Path("schemas")
    paths = export_schemas(out_dir)
    print(f"wrote {len(paths)} schemas to {out_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
