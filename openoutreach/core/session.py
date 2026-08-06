# openoutreach/core/session.py
"""Browserless run session.

The email-only replacement for the old channel ``AccountSession``: it carries
the operator's identity and campaign context for the daemon and the agents, but
owns no browser — there is nothing to log into, scrape, or reauthenticate. The
operator is just the Django ``User`` running the daemon; ``self_profile`` is
synthesized from that user and the ``SiteConfig`` country rather than scraped.
"""
from __future__ import annotations

import logging
from functools import cached_property

logger = logging.getLogger(__name__)

_sessions: dict[int, "OperatorSession"] = {}


class OperatorSession:
    def __init__(self, user):
        self.django_user = user

        # Active campaign — set by the daemon before each task execution.
        self.campaign = None

    @cached_property
    def campaigns(self):
        """All campaigns this user belongs to (cached)."""
        from openoutreach.core.models import Campaign
        return list(Campaign.objects.filter(users=self.django_user))

    @cached_property
    def self_profile(self) -> dict:
        """The operator's own identity, synthesized (not scraped).

        Name comes from the Django user (the agents read ``first_name`` for the
        seller binding, falling back to the username), country from ``SiteConfig``.
        The contacts store uses ``public_identifier`` (the operator email) as the
        stable operator key.
        """
        from openoutreach.core.models import SiteConfig

        user = self.django_user
        return {
            "public_identifier": user.email or user.username,
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "country_code": SiteConfig.load().country_code or "",
        }

    def __repr__(self) -> str:
        return self.django_user.email or self.django_user.username


def get_active_user():
    """Return the Django ``User`` running the daemon (the onboarded operator)."""
    from django.contrib.auth.models import User

    return User.objects.filter(is_active=True, is_staff=True).order_by("pk").first()


def get_or_create_session(user) -> "OperatorSession":
    pk = user.pk
    if pk not in _sessions:
        _sessions[pk] = OperatorSession(user)
        logger.debug("Created operator session for %s", user)
    return _sessions[pk]
