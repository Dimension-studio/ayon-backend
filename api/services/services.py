from typing import Any

from fastapi import Depends, Path, Response

from ayon_server.addons import AddonLibrary
from ayon_server.api.dependencies import dep_current_user
from ayon_server.entities import UserEntity
from ayon_server.exceptions import ForbiddenException, NotFoundException
from ayon_server.lib.postgres import Postgres
from ayon_server.types import Field, OPModel
from ayon_server.utils import SQLTool

from .router import router


class ServiceDataModel(OPModel):
    image: str | None = Field(None, example="ayon/ftrack-addon-leecher:2.0.0")
    env: dict[str, Any] = Field(default_factory=dict)


class ServiceModel(OPModel):
    name: str = Field(...)
    hostname: str = Field(..., example="worker03")
    addon_name: str = Field(..., example="ftrack")
    addon_version: str = Field(..., example="2.0.0")
    service: str = Field(..., example="collector")
    should_run: bool = Field(...)
    is_running: bool = Field(...)
    last_seen: int | None = Field(None)
    data: ServiceDataModel = Field(default_factory=ServiceDataModel)


class ServiceListModel(OPModel):
    services: list[ServiceModel] = Field(default_factory=list)


@router.get("/services", response_model=ServiceListModel, tags=["Services"])
async def list_services(user: UserEntity = Depends(dep_current_user)):

    query = "SELECT * FROM services ORDER BY name ASC"
    services = []
    async for row in Postgres.iterate(query):
        services.append(ServiceModel(**row))

    return ServiceListModel(services=services)


#
# Spawn service
#


class SpawnServiceRequestModel(OPModel):
    addon_name: str
    addon_version: str
    service: str
    hostname: str


@router.put("/services/{name}", response_class=Response, tags=["Services"])
async def spawn_service(
    payload: SpawnServiceRequestModel,
    name: str = Path(...),
    user: UserEntity = Depends(dep_current_user),
):

    if not user.is_admin:
        raise ForbiddenException("Only admins can spawn services")

    library = AddonLibrary.getinstance()
    addon = library.addon(payload.addon_name, payload.addon_version)
    if payload.service not in addon.services:
        # TODO: be more verbose
        raise NotFoundException("This addon does not have this service")

    image = addon.services[payload.service].get("image")
    assert image is not None  # TODO: raise smarter exception

    data = {"image": image}

    await Postgres.execute(
        """
        INSERT INTO SERVICES (
            name,
            hostname,
            addon_name,
            addon_version,
            service,
            data
        )
        VALUES
            ($1, $2, $3, $4, $5, $6)
        """,
        name,
        payload.hostname,
        payload.addon_name,
        payload.addon_version,
        payload.service,
        data,
    )

    return Response(status_code=201)


@router.delete("/services/{name}", response_class=Response, tags=["Services"])
async def delete_service(
    name: str = Path(...),
    user: UserEntity = Depends(dep_current_user),
):

    if not user.is_admin:
        raise ForbiddenException("Only admins can delete services")

    await Postgres.execute("DELETE FROM services WHERE name = $1", name)

    return Response(status_code=204)


class PatchServiceRequestModel(OPModel):
    should_run: bool | None = Field(None)


@router.patch("/services/{name}", response_class=Response, tags=["Services"])
async def patch_service(
    payload: PatchServiceRequestModel,
    name: str = Path(...),
    user: UserEntity = Depends(dep_current_user),
):
    if not user.is_admin:
        raise ForbiddenException("Only admins can modify services")

    await Postgres.execute(
        *SQLTool.update(
            "services",
            f"WHERE name='{name}'",
            **payload.dict(),
        )
    )

    return Response(status_code=204)
