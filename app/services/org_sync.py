"""Org sync: mirror Bitrix24 departments and users into GLPI.

Fetches the Bitrix24 department tree (``department.get``) and active users
(``user.get``) via a webhook that has the ``user``/``department`` scopes
(``BITRIX24_ORG_WEBHOOK_URL``), then:

- mirrors the department tree into GLPI entities under a configurable root
  entity (``ORG_SYNC_ROOT_ENTITY_ID``), matching existing entities by name;
- creates or updates GLPI users, matching by email (case-insensitive),
  assigning the configured profile (``ORG_SYNC_USER_PROFILE_ID``) and the
  entity of the user's primary Bitrix24 department.

Idempotent: re-running only creates missing entities/users and updates
changed name fields.
"""

import asyncio
import logging

from app.config.settings import settings
from app.services.bitrix import BitrixClient
from app.services.glpi import GLPIClient

logger = logging.getLogger(__name__)


def _norm_name(name: str) -> str:
    """Normalize a department/user name for case-insensitive matching."""
    return " ".join(name.strip().split()).lower()


def _norm_email(email: str) -> str:
    return email.strip().lower()


def _sort_departments(
    departments: list[dict],
) -> list[dict]:
    """Return departments ordered so parents precede their children."""
    by_parent: dict[int | None, list[dict]] = {}
    for d in departments:
        by_parent.setdefault(d["parent_id"], []).append(d)

    ordered: list[dict] = []

    def walk(parent: int | None) -> None:
        for d in sorted(by_parent.get(parent, []), key=lambda x: x["name"]):
            ordered.append(d)
            walk(d["id"])

    walk(None)
    return ordered


def _find_entity_child(
    entities: list[dict], parent_id: int, name: str
) -> dict | None:
    """Find an existing GLPI entity with *name* directly under *parent_id*."""
    needle = _norm_name(name)
    for e in entities:
        if e["parent_id"] == parent_id and _norm_name(e["name"]) == needle:
            return e
    return None


def get_org_sync_status() -> dict:
    """Return configuration status of the org sync feature."""
    return {
        "org_sync_enabled": settings.ORG_SYNC_ENABLED,
        "org_webhook_configured": bool(settings.BITRIX24_ORG_WEBHOOK_URL),
        "root_entity_id": settings.ORG_SYNC_ROOT_ENTITY_ID,
        "user_profile_id": settings.ORG_SYNC_USER_PROFILE_ID,
        "interval_seconds": settings.ORG_SYNC_INTERVAL_SECONDS,
    }


async def sync_org_structure() -> dict:
    """Mirror Bitrix24 departments and users into GLPI.

    Returns:
        Summary dict with counts of created/updated entities and users.
    """
    if not settings.BITRIX24_ORG_WEBHOOK_URL:
        return {"error": "BITRIX24_ORG_WEBHOOK_URL is not configured"}

    bitrix_client = BitrixClient(webhook_url=settings.BITRIX24_ORG_WEBHOOK_URL)
    glpi_client = GLPIClient(
        base_url=settings.GLPI_URL,
        app_token=settings.GLPI_APP_TOKEN.get_secret_value(),
        user_token=settings.GLPI_USER_TOKEN.get_secret_value(),
    )

    summary: dict = {
        "departments_total": 0,
        "departments_created": 0,
        "users_total": 0,
        "users_active": 0,
        "users_created": 0,
        "users_updated": 0,
        "errors": [],
    }

    glpi_session: str | None = None
    try:
        glpi_session = await asyncio.to_thread(glpi_client.init_session)

        # --- Fetch source data ---
        departments = await asyncio.to_thread(bitrix_client.get_departments)
        summary["departments_total"] = len(departments)

        users: list[dict] = []
        start = 0
        while True:
            page = await asyncio.to_thread(bitrix_client.get_users, start=start)
            users.extend(page["users"])
            if not page["next"]:
                break
            start = page["next"]
        summary["users_total"] = len(users)
        summary["users_active"] = sum(1 for u in users if u["active"])

        entities = await asyncio.to_thread(
            glpi_client.get_entities, glpi_session
        )
        user_emails = await asyncio.to_thread(
            glpi_client.get_user_emails, glpi_session
        )

        # --- Mirror department tree into GLPI entities ---
        root_entity_id = settings.ORG_SYNC_ROOT_ENTITY_ID
        dept_to_entity: dict[int, int] = {}

        for dept in _sort_departments(departments):
            # The Bitrix24 root department maps to the configured root entity.
            if dept["parent_id"] is None:
                dept_to_entity[dept["id"]] = root_entity_id
                continue
            parent_entity_id = dept_to_entity.get(
                dept["parent_id"], root_entity_id
            )
            existing = _find_entity_child(
                entities, parent_entity_id, dept["name"]
            )
            if existing is not None:
                dept_to_entity[dept["id"]] = existing["id"]
                continue
            try:
                created = await asyncio.to_thread(
                    glpi_client.create_entity,
                    dept["name"],
                    parent_entity_id,
                    glpi_session,
                )
                new_id = int(created.get("id", 0))
                if new_id:
                    dept_to_entity[dept["id"]] = new_id
                    entities.append(
                        {
                            "id": new_id,
                            "name": dept["name"],
                            "parent_id": parent_entity_id,
                        }
                    )
                    summary["departments_created"] += 1
            except (RuntimeError, ValueError, TypeError) as exc:
                logger.error("Failed to create entity %s: %s", dept, exc)
                summary["errors"].append(str(exc))

        # --- Upsert users (match by email) ---
        email_to_user: dict[str, int] = {}
        for em in user_emails:
            if em["email"]:
                email_to_user.setdefault(_norm_email(em["email"]), em["users_id"])

        profile_id = settings.ORG_SYNC_USER_PROFILE_ID
        for u in users:
            if not u["active"]:
                continue
            email = _norm_email(u["email"])
            entity_id = dept_to_entity.get(
                u["department_ids"][0] if u["department_ids"] else 0,
                root_entity_id,
            )

            try:
                if email and email in email_to_user:
                    existing_id = email_to_user[email]
                    await asyncio.to_thread(
                        glpi_client.update_user,
                        existing_id,
                        realname=u["last_name"],
                        firstname=u["name"],
                        entities_id=entity_id,
                        session_token=glpi_session,
                    )
                    summary["users_updated"] += 1
                else:
                    login = email or f"b24_{u['id']}"
                    created = await asyncio.to_thread(
                        glpi_client.create_user,
                        name=login,
                        realname=u["last_name"],
                        firstname=u["name"],
                        email=email or None,
                        entities_id=entity_id,
                        profiles_id=profile_id,
                        session_token=glpi_session,
                    )
                    new_id = int(created.get("id", 0))
                    if new_id:
                        email_to_user[email] = new_id
                        summary["users_created"] += 1
            except (RuntimeError, ValueError, TypeError) as exc:
                logger.error("Failed to sync user %s: %s", u.get("id"), exc)
                summary["errors"].append(str(exc))

        return summary
    finally:
        bitrix_client.close()
        glpi_client.close()
        if glpi_session is not None:
            try:
                await asyncio.to_thread(glpi_client.kill_session, glpi_session)
            except Exception:
                logger.warning(
                    "Org sync: failed to kill GLPI session", exc_info=True
                )
