"""All routers of Fleet; mounted under ``/api/v1`` by the kit (paths ``/api/v1/fleet/...``)."""

from fastapi import APIRouter

from fleet.adapters.api.base_stations import router as base_stations_router
from fleet.adapters.api.demo_routes import router as demo_router
from fleet.adapters.api.linked_accounts import router as linked_accounts_router
from fleet.adapters.api.mowers import router as mowers_router
from fleet.adapters.api.places import router as places_router

ROUTERS: list[APIRouter] = [
    mowers_router,
    places_router,
    linked_accounts_router,
    base_stations_router,
    demo_router,
]
