"""The use cases of the Planner, wired once at startup."""

from dataclasses import dataclass

from mowdeck_authz import Authorizer
from mowdeck_patterns import PatternRegistry

from planner.application.access import Access
from planner.application.context import PlannerConfig
from planner.application.demo_purge import AccountPurge
from planner.application.ingest import Ingest
from planner.application.planning import Planning
from planner.application.ports import Clock, Store, TrackSource
from planner.application.templates import Templates


@dataclass(frozen=True, slots=True)
class Services:
    """Planning, templates and the snapshot ingest."""

    planning: Planning
    templates: Templates
    ingest: Ingest
    registry: PatternRegistry
    config: PlannerConfig
    purge: AccountPurge


def build_services(
    *,
    store: Store,
    authorizer: Authorizer,
    registry: PatternRegistry,
    config: PlannerConfig,
    clock: Clock,
    tracks: TrackSource | None,
) -> Services:
    """Create the use cases."""
    access = Access(authorizer)
    return Services(
        planning=Planning(store, access, registry, config, tracks),
        templates=Templates(store, access, registry, config, clock),
        ingest=Ingest(store),
        registry=registry,
        config=config,
        purge=AccountPurge(store),
    )
