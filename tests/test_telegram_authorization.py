from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select
from telegram_fakes import TEST_ACCESS_POLICY, FakeTelegramGateway

from receipt_bot.domain.time import utc_now
from receipt_bot.persistence.models import (
    AuditEvent,
    GroupMembership,
    InteractionSession,
)
from receipt_bot.persistence.repositories import GroupRepository, UserRepository
from receipt_bot.telegram.services import (
    AccessDenied,
    AuthorizationService,
    InteractionService,
    SettingsService,
)


def seed_group_and_users(database, chat_id=-1001):
    with database.session() as session:
        group = GroupRepository(session).add(chat_id, "گروه آزمون")
        group.settings = TEST_ACCESS_POLICY.authorized_settings(group.settings, "user_10", 10)
        admin = UserRepository(session).add(10, "مدیر")
        reviewer = UserRepository(session).add(20, "حسابدار")
        outsider = UserRepository(session).add(30, "غریبه")
        return group, admin, reviewer, outsider


def test_current_admin_required_and_settings_changes_are_audited(migrated_database) -> None:
    group, admin, _, outsider = seed_group_and_users(migrated_database)
    gateway = FakeTelegramGateway()
    gateway.set_member(group.telegram_chat_id, admin.telegram_user_id, "administrator")
    gateway.set_member(group.telegram_chat_id, outsider.telegram_user_id, "member")
    authorization = AuthorizationService(migrated_database, gateway, TEST_ACCESS_POLICY)
    settings = SettingsService(migrated_database, authorization)

    updated = asyncio.run(
        settings.set_retention_days(
            telegram_chat_id=group.telegram_chat_id,
            telegram_user_id=admin.telegram_user_id,
            days=365,
        )
    )
    assert updated.retention_days == 365
    with pytest.raises(AccessDenied):
        asyncio.run(
            settings.set_retention_days(
                telegram_chat_id=group.telegram_chat_id,
                telegram_user_id=outsider.telegram_user_id,
                days=90,
            )
        )
    with migrated_database.session() as session:
        events = list(session.scalars(select(AuditEvent).where(AuditEvent.group_id == group.id)))
        assert [(event.action, event.metadata_json) for event in events] == [
            ("group.retention_changed", {"before_days": 183, "after_days": 365})
        ]


def test_accountant_grant_revoke_and_group_isolation(migrated_database) -> None:
    group_one, admin, reviewer, _ = seed_group_and_users(migrated_database, -2001)
    with migrated_database.session() as session:
        group_two = GroupRepository(session).add(-2002, "گروه دوم")
    gateway = FakeTelegramGateway()
    gateway.set_member(-2001, admin.telegram_user_id, "creator")
    gateway.set_member(-2001, reviewer.telegram_user_id, "member")
    gateway.set_member(-2002, reviewer.telegram_user_id, "member")
    authorization = AuthorizationService(migrated_database, gateway, TEST_ACCESS_POLICY)
    settings = SettingsService(migrated_database, authorization)

    asyncio.run(
        settings.set_accountant(
            telegram_chat_id=-2001,
            actor_telegram_user_id=admin.telegram_user_id,
            target_telegram_user_id=reviewer.telegram_user_id,
            active=True,
        )
    )
    asyncio.run(authorization.require_reviewer(group_one, reviewer))
    with pytest.raises(AccessDenied):
        asyncio.run(authorization.require_reviewer(group_two, reviewer))

    asyncio.run(
        settings.set_accountant(
            telegram_chat_id=-2001,
            actor_telegram_user_id=admin.telegram_user_id,
            target_telegram_user_id=reviewer.telegram_user_id,
            active=False,
        )
    )
    with pytest.raises(AccessDenied):
        asyncio.run(authorization.require_reviewer(group_one, reviewer))
    with migrated_database.session() as session:
        membership = session.get(GroupMembership, (group_one.id, reviewer.id))
        assert membership.active is False
        assert [
            event.action
            for event in session.scalars(
                select(AuditEvent).where(AuditEvent.group_id == group_one.id).order_by(AuditEvent.id)
            )
        ] == ["accountant.granted", "accountant.revoked"]


def test_interactions_are_opaque_bound_expiring_and_single_use(migrated_database) -> None:
    group, admin, _, outsider = seed_group_and_users(migrated_database, -3001)
    with migrated_database.session() as session:
        other_group = GroupRepository(session).add(-3002, "گروه دیگر")
    service = InteractionService(migrated_database, ttl_minutes=15)
    interaction = service.create(
        group_id=group.id,
        user_id=admin.id,
        workflow="settings",
        state={"page": "root"},
    )
    callback = service.callback_data(interaction.token, "cancel")
    assert len(callback.encode("utf-8")) <= 64
    assert interaction.token != str(group.id)
    assert service.resolve(interaction.token, user_id=outsider.id, group_id=group.id) is None
    assert service.resolve(interaction.token, user_id=admin.id, group_id=other_group.id) is None

    with migrated_database.session() as session:
        stored = session.get(InteractionSession, interaction.token)
        stored.expires_at = utc_now() - timedelta(seconds=1)
    assert service.resolve(interaction.token, user_id=admin.id, group_id=group.id) is None

    fresh = service.create(group_id=group.id, user_id=admin.id, workflow="settings")
    assert service.consume(fresh.token, user_id=admin.id, group_id=group.id) is True
    assert service.consume(fresh.token, user_id=admin.id, group_id=group.id) is False
