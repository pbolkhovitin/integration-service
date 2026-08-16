"""Org sync: mirror Bitrix24 departments and users into GLPI.

Fetches the Bitrix24 department tree (``department.get``) and active users
(``user.get``) via a webhook that has the ``user``/``department`` scopes
(``BITRIX24_ORG_WEBHOOK_URL``), then:

- mirrors the department tree into GLPI entities under a configurable root
  entity (``ORG_SYNC_ROOT_ENTITY_ID``). Entities are matched by the
  ``org_department_map`` (b24_dept_id → glpi_entity_id), so renames and
  re-parenting update the SAME entity; removed departments are deactivated.
- creates/updates GLPI users (full profile: realname/firstname/phone/mobile/
  position/entity/sync_field), matching by ``org_user_map`` (b24_user_id →
  glpi_user_id) first, then by email. The maps are persisted in the
  integration DB and used by ticket creation to resolve users/entities by ID.
"""

import asyncio
import logging

from sqlalchemy import select

from app.config.settings import settings
from app.core.database import async_session_factory
from app.models.org_map import OrgDepartmentMap, OrgUserMap
from app.services.bitrix import BitrixClient
from app.services.glpi import GLPIClient

logger = logging.getLogger(__name__)


def _norm_name(name: str) -> str:
    """Normalize a department/user name for case-insensitive matching."""
    return " ".join(name.strip().split()).lower()


def _norm_email(email: str) -> str:
    return email.strip().lower()


def _sort_departments(departments: list[dict]) -> list[dict]:
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


def _find_entity_child(entities: list[dict], parent_id: int, name: str) -> dict | None:
    """Find an existing GLPI entity with *name* directly under *parent_id*."""
    needle = _norm_name(name)
    for e in entities:
        if e["parent_id"] == parent_id and _norm_name(e["name"]) == needle:
            return e
    return None


async def load_department_map() -> dict[int, int]:
    """Load b24_dept_id → glpi_entity_id from the integration DB."""
    async with async_session_factory() as db:
        result = await db.execute(select(OrgDepartmentMap))
        return {row.b24_dept_id: row.glpi_entity_id for row in result.scalars()}


async def upsert_department_map(b24_dept_id: int, glpi_entity_id: int) -> None:
    async with async_session_factory() as db:
        result = await db.execute(
            select(OrgDepartmentMap).where(
                OrgDepartmentMap.b24_dept_id == b24_dept_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            db.add(OrgDepartmentMap(b24_dept_id=b24_dept_id, glpi_entity_id=glpi_entity_id))
        else:
            row.glpi_entity_id = glpi_entity_id
        await db.commit()


async def load_user_map() -> dict[int, dict]:
    """Load b24_user_id → {glpi_user_id, email} from the integration DB."""
    async with async_session_factory() as db:
        result = await db.execute(select(OrgUserMap))
        return {
            row.b24_user_id: {"glpi_user_id": row.glpi_user_id, "email": row.email}
            for row in result.scalars()
        }


async def upsert_user_map(b24_user_id: int, glpi_user_id: int, email: str) -> None:
    async with async_session_factory() as db:
        result = await db.execute(
            select(OrgUserMap).where(OrgUserMap.b24_user_id == b24_user_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            db.add(OrgUserMap(b24_user_id=b24_user_id, glpi_user_id=glpi_user_id, email=email))
        else:
            row.glpi_user_id = glpi_user_id
            row.email = email
        await db.commit()


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
        "departments_updated": 0,
        "departments_deactivated": 0,
        "users_total": 0,
        "users_active": 0,
        "users_created": 0,
        "users_updated": 0,
        "users_deactivated": 0,
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

        entities = await asyncio.to_thread(glpi_client.get_entities, glpi_session)
        user_emails = await asyncio.to_thread(glpi_client.get_user_emails, glpi_session)

        dept_map = await load_department_map()
        root_entity_id = settings.ORG_SYNC_ROOT_ENTITY_ID
        seen_depts: set[int] = set()

        async def _refresh_session() -> None:
            """Re-init the GLPI session to refresh the rights cache.

            GLPI caches per-session rights; entities created during the
            session are not visible in the cache, so creating deeper
            children fails with 'no rights' until the session is restarted.
            """
            nonlocal glpi_session
            if glpi_session:
                try:
                    await asyncio.to_thread(glpi_client.kill_session, glpi_session)
                except Exception:  # noqa: BLE001
                    pass
            glpi_session = await asyncio.to_thread(glpi_client.init_session)

        # --- Mirror department tree, matching by org_department_map ---
        for dept in _sort_departments(departments):
            seen_depts.add(dept["id"])
            if dept["parent_id"] is None:
                # Bitrix24 root department → configured root entity.
                await upsert_department_map(dept["id"], root_entity_id)
                continue

            parent_entity_id = dept_map.get(dept["parent_id"], root_entity_id)
            entity_id = dept_map.get(dept["id"])
            if entity_id is None:
                # No mapping yet: match by name under parent, else create.
                existing = _find_entity_child(entities, parent_entity_id, dept["name"])
                if existing is not None:
                    entity_id = existing["id"]
                else:
                    try:
                        created = await asyncio.to_thread(
                            glpi_client.create_entity,
                            dept["name"],
                            parent_entity_id,
                            glpi_session,
                        )
                    except (RuntimeError, ValueError, TypeError) as exc:
                        # Likely a rights-cache miss — refresh session, retry once.
                        if "нет прав" in str(exc) or "ERROR_GLPI_ADD" in str(exc):
                            await _refresh_session()
                            try:
                                created = await asyncio.to_thread(
                                    glpi_client.create_entity,
                                    dept["name"],
                                    parent_entity_id,
                                    glpi_session,
                                )
                            except (RuntimeError, ValueError, TypeError) as exc2:
                                logger.error(
                                    "Failed to create entity %s: %s", dept, exc2
                                )
                                summary["errors"].append(str(exc2))
                                continue
                        else:
                            logger.error("Failed to create entity %s: %s", dept, exc)
                            summary["errors"].append(str(exc))
                            continue
                    entity_id = int(created.get("id", 0))
                    if entity_id:
                        entities.append(
                            {
                                "id": entity_id,
                                "name": dept["name"],
                                "parent_id": parent_entity_id,
                            }
                        )
                        summary["departments_created"] += 1

            if entity_id:
                # Update name/parent if changed (rename / re-parent).
                ent = next((e for e in entities if e["id"] == entity_id), None)
                needs_update = ent is None or (
                    _norm_name(ent["name"]) != _norm_name(dept["name"])
                    or ent["parent_id"] != parent_entity_id
                )
                if needs_update:
                    try:
                        await asyncio.to_thread(
                            glpi_client.update_entity,
                            entity_id,
                            name=dept["name"],
                            parent_id=parent_entity_id,
                            session_token=glpi_session,
                        )
                        summary["departments_updated"] += 1
                    except (RuntimeError, ValueError, TypeError) as exc:
                        logger.error("Failed to update entity %s: %s", dept, exc)
                        summary["errors"].append(str(exc))
                await upsert_department_map(dept["id"], entity_id)
                dept_map[dept["id"]] = entity_id

        # Deactivate departments present in the map but removed from Bitrix24.
        for b24_dept_id, entity_id in dept_map.items():
            if b24_dept_id not in seen_depts:
                try:
                    await asyncio.to_thread(
                        glpi_client.update_entity,
                        entity_id,
                        is_active=False,
                        session_token=glpi_session,
                    )
                    summary["departments_deactivated"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to deactivate entity %s: %s", entity_id, exc)

        # --- Upsert users (match by org_user_map, then by email) ---
        user_map = await load_user_map()
        email_to_user: dict[str, int] = {}
        for em in user_emails:
            if em["email"]:
                email_to_user.setdefault(_norm_email(em["email"]), em["users_id"])

        profile_id = settings.ORG_SYNC_USER_PROFILE_ID
        active_b24: set[int] = set()
        for u in users:
            active_b24.add(u["id"])
            if not u["active"]:
                continue
            email = _norm_email(u["email"])
            entity_id = dept_map.get(
                u["department_ids"][0] if u["department_ids"] else 0,
                root_entity_id,
            )
            position = u["work_position"]
            full_name = " ".join(
                p for p in (u["last_name"], u["name"]) if p
            )

            try:
                existing = user_map.get(u["id"])
                if existing is not None:
                    glpi_user_id = existing["glpi_user_id"]
                    await asyncio.to_thread(
                        glpi_client.update_user,
                        glpi_user_id,
                        realname=u["last_name"],
                        firstname=u["name"],
                        entities_id=entity_id,
                        phone=u.get("phone") or None,
                        mobile=u.get("mobile") or None,
                        comment=position or None,
                        sync_field=str(u["id"]),
                        session_token=glpi_session,
                    )
                    summary["users_updated"] += 1
                else:
                    glpi_user_id = email_to_user.get(email) if email else None
                    if glpi_user_id is None:
                        login = email or f"b24_{u['id']}"
                        created = await asyncio.to_thread(
                            glpi_client.create_user,
                            name=login,
                            realname=u["last_name"],
                            firstname=u["name"],
                            email=email or None,
                            entities_id=entity_id,
                            profiles_id=profile_id,
                            phone=u.get("phone") or None,
                            mobile=u.get("mobile") or None,
                            comment=position or None,
                            sync_field=str(u["id"]),
                            session_token=glpi_session,
                        )
                        glpi_user_id = int(created.get("id", 0))
                        if glpi_user_id:
                            summary["users_created"] += 1
                    else:
                        await asyncio.to_thread(
                            glpi_client.update_user,
                            glpi_user_id,
                            realname=u["last_name"],
                            firstname=u["name"],
                            entities_id=entity_id,
                            phone=u.get("phone") or None,
                            mobile=u.get("mobile") or None,
                            comment=position or None,
                            sync_field=str(u["id"]),
                            session_token=glpi_session,
                        )
                        summary["users_updated"] += 1
                if glpi_user_id:
                    await upsert_user_map(u["id"], glpi_user_id, email)
                    user_map[u["id"]] = {
                        "glpi_user_id": glpi_user_id,
                        "email": email,
                    }
            except (RuntimeError, ValueError, TypeError) as exc:
                logger.error("Failed to sync user %s: %s", u.get("id"), exc)
                summary["errors"].append(str(exc))

        # Deactivate GLPI users mapped to b24 users that are gone/inactive.
        for b24_user_id, mapped in user_map.items():
            if b24_user_id not in active_b24:
                try:
                    await asyncio.to_thread(
                        glpi_client.update_user,
                        mapped["glpi_user_id"],
                        is_active=False,
                        session_token=glpi_session,
                    )
                    summary["users_deactivated"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to deactivate user %s: %s", b24_user_id, exc
                    )

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
