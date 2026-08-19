# openoutreach/core/admin.py
from django.contrib import admin

from openoutreach.core.models import Campaign, Keyword, QueryNode, SiteConfig
from openoutreach.discovery import describe_filters


@admin.register(SiteConfig)
class SiteConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "ai_model", "llm_api_base")

    def has_add_permission(self, request):
        return not SiteConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "phase")
    filter_horizontal = ("users",)

    @admin.display(description="phase")
    def phase(self, obj):
        """Cold (still part-steering on invented profiles) vs learning (padding retired).

        The anchors change what the engine does — the GP fits on them, acquisition stays
        on exploit, every pass discovers as well as labels — so which phase a campaign is
        in should not need a log dig to answer. The count falls by one per real
        acceptance, so it doubles as the handover's progress bar.
        """
        n = len(obj.anchor_profiles or [])
        return f"cold ({n} anchor{'' if n == 1 else 's'})" if n else "learning"


@admin.register(QueryNode)
class QueryNodeAdmin(admin.ModelAdmin):
    """The discovery walk, node by node — what was searched, how deep, and what it found.

    There is no value column to display: a node's estimate is counted from the label
    store every time it is needed (``select.estimate``), so showing a stored number here
    would only show one that had gone stale.
    """

    list_display = (
        "id", "query", "campaign", "state", "next_offset", "leads_found",
        "lead_yield", "updated_at",
    )
    list_filter = ("state", "campaign")
    readonly_fields = (
        "campaign", "query", "token_key", "parent", "next_offset", "state",
        "leads_found", "lead_yield", "created_at", "updated_at",
    )
    date_hierarchy = "created_at"

    @admin.display(description="query")
    def query(self, obj):
        """The node's keyword set, rendered as the region it searches."""
        return describe_filters(obj.to_filters())

    @admin.display(description="leads")
    def lead_yield(self, obj):
        """First-touch leads this node surfaced."""
        return obj.leads.count()


@admin.register(Keyword)
class KeywordAdmin(admin.ModelAdmin):
    """The vocabulary — every ``(field, token)`` a query node can be built from."""

    list_display = ("__str__", "field", "token", "node_count", "created_at")
    list_filter = ("field",)
    search_fields = ("token",)

    @admin.display(description="nodes")
    def node_count(self, obj):
        """How many query nodes carry this keyword."""
        return obj.nodes.count()
