from typing import Any

from fastapi import Header, Path
from nxtools import logging

from ayon_server.api.clientinfo import ClientInfo
from ayon_server.api.dependencies import AccessToken, CurrentUser, UserName
from ayon_server.api.responses import EmptyResponse
from ayon_server.auth.session import Session
from ayon_server.auth.utils import validate_password
from ayon_server.config import ayonconfig
from ayon_server.entities import UserEntity
from ayon_server.events import EventStream
from ayon_server.exceptions import (
    BadRequestException,
    ConflictException,
    ForbiddenException,
    NotFoundException,
)
from ayon_server.helpers.project_list import get_project_list
from ayon_server.lib.postgres import Postgres
from ayon_server.lib.redis import Redis
from ayon_server.types import USER_NAME_REGEX, Field, OPModel
from ayon_server.utils import get_nickname, obscure

from .avatar import REDIS_NS, obtain_avatar
from .router import router

#
# [GET] /api/users/me
#


@router.get("/me", response_model_exclude_none=True)
async def get_current_user(
    user: CurrentUser,
) -> UserEntity.model.main_model:  # type: ignore
    """
    Return the current user information (based on the Authorization header).
    This is used for a profile page as well as as an initial check to ensure
    the user is still logged in.
    """
    return user.payload


#
# [GET] /api/users/{username}
#


@router.get("/{user_name}", response_model_exclude_none=True)
async def get_user(
    user: CurrentUser, user_name: UserName
) -> UserEntity.model.main_model | dict[str, str]:  # type: ignore
    """
    Return the current user information (based on the Authorization header).
    This is used for a profile page as well as as an initial check to ensure
    the user is still logged in.
    """

    if user_name == user.name:
        return user.as_user(user)

    result = await UserEntity.load(user_name)

    if user.is_manager:
        return result.payload

    if (
        user.is_guest
        and user.name != result.name
        and result.data.get("createdBy") != user.name
    ):
        result.name = get_nickname(result.name)
        if result.attrib.email:
            result.attrib.email = obscure(result.attrib.email)
        if result.attrib.fullName:
            result.attrib.fullName = obscure(result.attrib.fullName)
        result.attrib.avatarUrl = None

    # To normal users, show only colleague's name
    return {"name": result.name}


class NewUserModel(UserEntity.model.post_model):  # type: ignore
    password: str | None = Field(None, description="Password for the new user")
    api_key: str | None = Field(None, description="API Key for the new service user")


def validate_user_data(data: dict[str, Any]) -> None:
    try:
        if default_access_groups := data.get("defaultAccessGroups"):
            assert isinstance(
                default_access_groups, list
            ), "defaultAccessGroups must be a list"
            assert all(
                isinstance(access_group, str) for access_group in default_access_groups
            ), "defaultAccessGroups must be a list of str"

        if access_groups := data.get("accessGroups"):
            assert isinstance(access_groups, dict), "accessGroups must be a dict"
            ag_to_remove = []
            for project, ag_list in access_groups.items():
                assert isinstance(project, str), "project name must be a string"
                assert isinstance(ag_list, list), "access group list must be a list"
                assert all(
                    isinstance(ag, str) for ag in ag_list
                ), "acces group list must be a list of str"
                if not ag_list:
                    ag_to_remove.append(project)
            for project in ag_to_remove:
                del access_groups[project]

    except AssertionError as e:
        raise BadRequestException(str(e)) from e


@router.put("/{user_name}")
async def create_user(
    put_data: NewUserModel,
    user: CurrentUser,
    user_name: UserName,
    x_sender: str | None = Header(default=None),
) -> EmptyResponse:
    """Create a new user."""

    if not user.is_manager:
        raise ForbiddenException

    validate_user_data(put_data.data)

    if user.is_guest:
        put_data.data["isGuest"] = True

    try:
        nuser = await UserEntity.load(user_name)
    except NotFoundException:
        nuser = UserEntity(put_data.dict() | {"name": user_name})
        nuser.created_by = user.name
    else:
        raise ConflictException("User already exists")

    if put_data.password:
        if nuser.is_service:
            raise BadRequestException("Service users cannot have passwords")
        nuser.set_password(put_data.password, complexity_check=not user.is_admin)

    if put_data.api_key:
        if not nuser.is_service:
            raise BadRequestException("Only service users can have API keys")
        nuser.set_api_key(put_data.api_key)

    event: dict[str, Any] = {
        "topic": "entity.user.created",
        "description": f"User {user_name} created",
        "summary": {"entityName": user.name},
    }

    await nuser.save()
    await EventStream.dispatch(
        sender=x_sender,
        user=user.name,
        **event,
    )
    return EmptyResponse()


@router.delete("/{user_name}")
async def delete_user(
    user: CurrentUser,
    user_name: UserName,
    x_sender: str | None = Header(default=None),
) -> EmptyResponse:
    if not user.is_manager:
        raise ForbiddenException

    target_user = await UserEntity.load(user_name)

    event: dict[str, Any] = {
        "description": f"User {user_name} deleted",
        "summary": {"entityName": user_name},
    }
    if ayonconfig.audit_trail:
        event["payload"] = {
            "entityData": target_user.dict_simple(),
        }

    await target_user.delete()
    await EventStream.dispatch(
        "entity.user.deleted",
        sender=x_sender,
        user=user.name,
        **event,
    )
    return EmptyResponse()


@router.patch("/{user_name}")
async def patch_user(
    payload: UserEntity.model.patch_model,  # type: ignore
    user: CurrentUser,
    user_name: UserName,
    access_token: AccessToken,
) -> EmptyResponse:
    if user_name == user.name and (not user.is_manager):
        # Normal users can only patch their attributes
        # (such as full name and email)
        payload.data = {}
        payload.active = None
    elif not user.is_manager:
        raise ForbiddenException("Only managers can modify other users")

    payload.data["updatedBy"] = user.name
    target_user = await UserEntity.load(user_name)

    if target_user.is_admin and (not user.is_admin):
        raise ForbiddenException("Admins can only be modified by other admins")

    if user.is_guest:
        # Guests can only modify themselves and users they created
        if (
            target_user.name != user.name
            and target_user.data.get("createdBy") != user.name
        ):
            raise ForbiddenException(
                "Guests can only modify themselves and their guests"
            )
        # user cannot change any user's guest status
        payload.data.pop("isGuest", None)
        payload.data.pop("isDeveloper", None)

    if not user.is_admin:
        # Non-admins cannot change any user's admin status
        payload.data.pop("isAdmin", None)
        payload.data.pop("isDeveloper", None)
    elif target_user.name == user.name:
        # Admins cannot demote themselves
        payload.data.pop("isAdmin", None)

    if not user.is_manager:
        # Non-managers cannot change any user's manager status
        payload.data.pop("isManager", None)
    elif target_user.name == user.name:
        # Managers cannot demote themselves
        payload.data.pop("isManager", None)

    validate_user_data(payload.data)

    attrib_dict = payload.attrib.dict(exclude_unset=True)
    avatar_changed = False
    if (
        "avatarUrl" in attrib_dict
        and attrib_dict["avatarUrl"] != target_user.attrib.avatarUrl
    ):
        url = attrib_dict["avatarUrl"]
        if (url) and not (url.startswith("http://") or url.startswith("https://")):
            raise BadRequestException("Invalid avatar URL")
        avatar_changed = True

    target_user.patch(payload)
    await target_user.save()

    if avatar_changed:
        logging.debug(f"User {user_name} avatar changed, updating cache")
        avatar_bytes = await obtain_avatar(user_name)
        await Redis.set(REDIS_NS, user_name, avatar_bytes)

    async for session in Session.list(user_name):
        token = session.token
        if not target_user.active:
            await Session.delete(token)
        else:
            await Session.update(token, target_user)

    return EmptyResponse()


#
# Change password
#


class ChangePasswordRequestModel(OPModel):
    password: str | None = Field(
        None,
        description="New password",
        example="5up3r5ecr3t_p455W0rd.123",
    )
    api_key: str | None = Field(
        None,
        title="API Key",
        description="API Key to set to a service user",
        example="1cb4f6a89012a4b6d8a01ee4f67ae0fb",
        regex=r"^[0-9a-f]{32}$",
    )


@router.patch("/{user_name}/password")
async def change_password(
    patch_data: ChangePasswordRequestModel,
    user: CurrentUser,
    user_name: UserName,
) -> EmptyResponse:
    patch_data_dict = patch_data.dict(exclude_unset=True)

    if "password" in patch_data_dict:
        if (user_name != user.name) and not (user.is_manager):
            # users can only change their own password
            # managers can change any password
            raise ForbiddenException()

        target_user = await UserEntity.load(user_name)
        target_user.set_password(
            patch_data.password,
            complexity_check=not user.is_admin,
        )

        await target_user.save()
        return EmptyResponse()

    elif "api_key" in patch_data_dict:
        if not user.is_admin:
            raise ForbiddenException()

        target_user = await UserEntity.load(user_name)
        if not target_user.is_service:
            raise BadRequestException(f"{user_name} is not a service account")
        target_user.set_api_key(patch_data.api_key)

        await target_user.save()
        return EmptyResponse()

    raise BadRequestException("No password or API key provided")


class CheckPasswordRequestModel(OPModel):
    password: str = Field(..., title="Password", example="5up3r5ecr3t_p455W0rd.123")


@router.post("/{user_name}/checkPassword")
async def check_password(
    post_data: CheckPasswordRequestModel,
    user: CurrentUser,
    user_name: UserName,
) -> EmptyResponse:
    validate_password(post_data.password)
    return EmptyResponse()


#
# Change login name
#


class ChangeUserNameRequestModel(OPModel):
    new_name: str = Field(
        ...,
        description="New user name",
        example="EvenBetterUser",
        regex=USER_NAME_REGEX,
    )


@router.patch("/{user_name}/rename")
async def change_user_name(
    patch_data: ChangeUserNameRequestModel,
    user: CurrentUser,
    user_name: UserName,
    x_sender: str | None = Header(default=None),
) -> EmptyResponse:
    if not user.is_manager:
        raise ForbiddenException

    event: dict[str, Any] = {
        "topic": "entity.user.renamed",
        "description": f"Renamed user {user_name} to {patch_data.new_name}",
        "summary": {"entityName": user_name},
        "payload": {"oldValue": user_name, "newValue": patch_data.new_name},
    }

    async with Postgres.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE users SET name = $1 WHERE name = $2",
                patch_data.new_name,
                user_name,
            )

            # Update tasks assignees - since assignees is an array,
            # it won't update automatically (there's no foreign key)

            projects = await get_project_list()

            for project in projects:
                project_name = project.name
                query = f"""
                    UPDATE project_{project_name}.tasks SET
                    assignees = array_replace(
                        assignees,
                        '{user_name}',
                        '{patch_data.new_name}'
                    )
                    WHERE '{user_name}' = ANY(assignees)
                """
                await conn.execute(query)

                query = f"""
                    UPDATE project_{project_name}.files SET
                    author = $1 WHERE author = $2
                """

                await conn.execute(query, patch_data.new_name, user_name)

                # activities.data->>'author'

                query = f"""
                    UPDATE project_{project_name}.activities SET
                    data = jsonb_set(
                        data,
                        '{{author}}',
                        $1::jsonb
                    )
                    WHERE data->>'author' = $2
                """

                # TODO: there is also an author record in:
                # activities->data->files[]->author
                # but it is probably not important to update it

                await conn.execute(query, patch_data.new_name, user_name)

                # references

                query = f"""
                    UPDATE project_{project_name}.activity_references SET
                    entity_name = $1 WHERE entity_name = $2 AND entity_type = 'user'
                """

                await conn.execute(query, patch_data.new_name, user_name)

    # Renaming user has many side effects, so we need to log out all Sessions
    # and let the user log in again
    async for session in Session.list(user_name):
        token = session.token
        await Session.delete(token)

    await EventStream.dispatch(
        sender=x_sender,
        user=user.name,
        **event,
    )

    return EmptyResponse()


#
# Sessions
#


class UserSessionModel(OPModel):
    token: str
    is_service: bool
    last_used: float
    client_info: ClientInfo | None = None


class UserSessionsResponseModel(OPModel):
    sessions: list[UserSessionModel]


@router.get("/{user_name}/sessions")
async def get_user_sessions(
    current_user: CurrentUser, user_name: UserName
) -> UserSessionsResponseModel:
    if (not current_user.is_manager) and (current_user.name != user_name):
        raise ForbiddenException("You are not allowed to list other users' sessions")

    return UserSessionsResponseModel(
        sessions=[
            UserSessionModel(
                token=session.token,
                client_info=session.client_info,
                is_service=session.is_service,
                last_used=session.last_used,
            )
            async for session in Session.list(user_name)
        ]
    )


@router.delete("/{user_name}/sessions/{session_id}")
async def delete_user_session(
    current_user: CurrentUser,
    user_name: UserName,
    session_id: str = Path(...),
) -> EmptyResponse:
    session = await Session.check(session_id, None)
    if not session:
        raise NotFoundException("Requested session id does not exist")
    if session.user.name != current_user.name and (not current_user.is_manager):
        raise ForbiddenException(
            "You are not allowed to delete sessions which don't belong to you"
        )
    await Session.delete(session_id)
    return EmptyResponse()


#
# Assign access groups
#


class AccessGroupsOnProject(OPModel):
    project: str = Field(
        ...,
        description="Project name",
    )
    access_groups: list[str] = Field(
        ...,
        description="List of access groups on the project",
    )


class AssignAccessGroupsRequestModel(OPModel):
    access_groups: list[AccessGroupsOnProject] = Field(
        default_factory=list,
        description="List of access groups to assign",
        example=[
            {"project": "project1", "accessGroups": ["artist", "viewer"]},
            {"project": "project2", "accessGroups": ["viewer"]},
        ],
    )


@router.patch("/{user_name}/accessGroups")
async def assign_access_groups(
    patch_data: AssignAccessGroupsRequestModel,
    user: CurrentUser,
    user_name: UserName,
) -> EmptyResponse:
    if not user.is_manager:
        raise ForbiddenException("You are not permitted to assign access groups")

    target_user = await UserEntity.load(user_name)

    ag_set = target_user.data.get("accessGroups", {})
    for rconf in patch_data.access_groups:
        project_name = rconf.project
        access_groups = rconf.access_groups
        if not access_groups:
            if project_name in ag_set:
                del ag_set[project_name]
            continue
        ag_set[project_name] = access_groups

    target_user.data["accessGroups"] = ag_set
    await target_user.save()

    async for session in Session.list(user_name):
        token = session.token
        await Session.update(token, target_user)

    return EmptyResponse()


@router.patch("/{user_name}/frontendPreferences")
async def set_frontend_preferences(
    patch_data: dict[str, Any],
    user: CurrentUser,
    user_name: UserName,
) -> EmptyResponse:
    if (user_name != user.name) and not (user.is_manager):
        # users can only change their own preferences
        # managers can change any preferences
        raise ForbiddenException()

    target_user = await UserEntity.load(user_name)

    preferences = target_user.data.get("frontendPreferences", {})
    preferences.update(patch_data)
    target_user.data["frontendPreferences"] = preferences

    await target_user.save()

    async for session in Session.list(user_name):
        token = session.token
        await Session.update(token, target_user)

    return EmptyResponse()
