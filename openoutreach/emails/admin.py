# openoutreach/emails/admin.py
from django.contrib import admin

from openoutreach.emails.models import Mailbox


@admin.register(Mailbox)
class MailboxAdmin(admin.ModelAdmin):
    list_display = ("from_address", "host", "port", "daily_limit", "sent_today", "paused_today")
    search_fields = ("from_address", "username")
