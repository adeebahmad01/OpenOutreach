from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class DealState(models.TextChoices):
    """OpenOutreach-owned funnel state for a Deal — the email-only pipeline.

    A lead is discovered and qualified without an email in hand (Lead Finder
    returns firmographics, not addresses), so the funnel first *finds* the email
    and then *talks*. The paid lookup is a two-step async handshake: ``buy_address``
    fires the provider job and parks the deal at FINDING_EMAIL, and ``check_lookup``
    polls it. The job handle (``lookup_request_id``), the poll backoff
    (``lookup_attempt`` + ``not_before``) and every other schedule this deal is
    subject to live **on this row** — the state is the queue, so a deal carries its
    own timing and nothing external gates it:

        QUALIFIED ─(GP rank gate)─▶ READY_TO_FIND_EMAIL ─(buy_address)─▶
            FINDING_EMAIL ─(check_lookup on lookup_request_id)─▶
                hit:  READY_TO_EMAIL ─(first email)─▶ EMAILED ⟲ (agent answers replies)
                                                       ─▶ COMPLETED / FAILED
                                                       ─▶ UNSUBSCRIBED (opt-out)
                miss: NO_EMAIL_BETTERCONTACT (provider found no address — a distinct
                      terminal, not a failure; still a fit positive → ML label=1)

    - **READY_TO_FIND_EMAIL** — passed the GP confidence gate; queued for the
      *paid* provider lookup (one credit per verified hit). The GP gate rations
      spend to leads the model is confident about, and the submit leg only fires
      when there's mailbox send-headroom for the result today (no email is
      resolved that can't be sent).
    - **FINDING_EMAIL** — a provider job is in flight; the deal is excluded from
      the candidate pool (so the next submit slot can't re-select it and
      double-charge) while the ``collect_email`` leg polls to termination. A free
      hub-cache hit skips this state entirely (READY_TO_FIND_EMAIL →
      READY_TO_EMAIL directly, no submit).
    - **READY_TO_EMAIL** — an address exists; waiting for the first email. A cheap,
      *ungated* FIFO send-queue paced only by the per-box daily cap and spacing.
    - **EMAILED** — the first email has been sent. **Nobody is ever chased.** The
      deal becomes actionable again only when the recipient replies: the per-mailbox
      mail pass (``emails/mail_pass.py``) mirrors inbound mail into the log, and a
      deal whose thread's newest inbound turn is newer than its newest outgoing one
      is the queue the outreach agent serves. No reply means no further email, ever — so EMAILED is
      where most deals come to rest, and resting there costs nothing because nothing
      iterates them.

    A lookup *miss* is terminal (NO_EMAIL_BETTERCONTACT — the provider resolved no
    address). It is a *distinct* terminal from FAILED so downstream work can build
    on it (e.g. retry once another enrichment provider exists). The lead was an LLM
    fit positive, so the ML labeler keeps it as label=1 — only reachability failed,
    not fit. A *couldn't-run*
    (no key / API down at submit) leaves the deal at READY_TO_FIND_EMAIL to
    retry. A job that has not terminated is *never* abandoned: ``check_lookup``
    doubles ``not_before`` on the same request_id and the deal simply waits
    here, because a timeout is evidence about the provider, not about whether this
    person has a findable address. (The deadline that used to revert
    FINDING_EMAIL → READY_TO_FIND_EMAIL made an outage worse — the deal returned
    to the pool and bought a *second* job for the same lead.) That wait is now
    strictly this deal's own: ``not_before`` gates one row, where the retired task
    queue's backoff doubled as the daemon's sleep horizon and once stalled the whole
    install for 34 hours. The retired connect leg (READY_TO_CONNECT/PENDING/
    CONNECTED) was removed with the channel.

    UNSUBSCRIBED is the sibling of NO_EMAIL_BETTERCONTACT on the other side of the
    send: a fit positive whose *reachability* ended, not a verdict on the offer.
    The recipient asked to be left alone — by a mail client's unsubscribe button
    (the ``+unsub`` alias, found by the mail pass) or in words the agent read —
    so ``outcome`` stays blank exactly as it does on an enrichment miss, the ML
    labeler keeps the lead at label=1, and the *enforcement* lives on
    ``Lead.disqualified`` (permanent, account-level, cross-campaign) rather than
    here, where it would only bind one campaign.

    NOTE: when adding a state, also add it to ``_STATE_LOG_STYLE`` in
    ``core/db/deals.py`` — an unmapped state logs as a red "ERROR" label.
    """
    QUALIFIED = "Qualified"
    READY_TO_FIND_EMAIL = "Ready to Find Email"
    FINDING_EMAIL = "Finding Email"
    READY_TO_EMAIL = "Ready to Email"
    EMAILED = "Emailed"
    NO_EMAIL_BETTERCONTACT = "No Email (BetterContact)"
    UNSUBSCRIBED = "Unsubscribed"
    COMPLETED = "Completed"
    FAILED = "Failed"


class Outcome(models.TextChoices):
    CONVERTED = "converted"
    NOT_INTERESTED = "not_interested"
    WRONG_FIT = "wrong_fit"
    NO_BUDGET = "no_budget"
    HAS_SOLUTION = "has_solution"
    BAD_TIMING = "bad_timing"
    UNRESPONSIVE = "unresponsive"
    UNKNOWN = "unknown"


class Deal(models.Model):
    class Meta:
        verbose_name = _("Deal")
        verbose_name_plural = _("Deals")
        constraints = [
            models.UniqueConstraint(fields=["lead", "campaign"], name="unique_deal_per_campaign"),
        ]
        # The cycle's one query: every step selects on state, and the two that wait
        # narrow it by ``not_before``.
        indexes = [
            models.Index(fields=["state", "not_before"], name="deal_state_not_before_idx"),
        ]

    lead = models.ForeignKey("Lead", on_delete=models.CASCADE)
    campaign = models.ForeignKey(
        "core.Campaign", on_delete=models.CASCADE, related_name="deals",
    )
    state = models.CharField(
        max_length=32,
        choices=DealState.choices,
        default=DealState.QUALIFIED,
    )
    outcome = models.CharField(
        max_length=20,
        choices=Outcome.choices,
        blank=True,
        default="",
    )
    reason = models.TextField(blank=True, default="")
    # Email channel. The mailbox that sent the opener, bound to the deal: it's the
    # reply anchor and the sticky thread box for the agentic follow-up loop. (The
    # per-box cap counts the transport log, not this field — see Mailbox.sent_today.)
    mailbox = models.ForeignKey(
        "emails.Mailbox", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="deals",
    )
    # The opener's subject, generated once by the agent; the follow-up loop reuses
    # it as "Re: …" on every threaded reply.
    email_subject = models.CharField(max_length=300, blank=True, default="")
    # When the opener was sent — the audit timestamp (the per-box daily cap counts
    # outgoing messages in the log, not this field; see Mailbox.sent_today). Null
    # until sent.
    email_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # The conversation, in the mail log (``emails/models/maillog.py``). A deal has
    # one thread; a thread can exist without a deal, because someone cold-mailing
    # the box is a conversation too and not a special case.
    #
    # This replaces an ``email_message_id`` holding the *opener's* Message-ID, and
    # the root-matching it forced: a reply carrying only ``In-Reply-To`` points at
    # the newest message in the chain, not at the root, so it matched nothing and
    # was dropped. Threading is now a graph over every id in the log, and the deal
    # simply points at the thread it produced. Null until the opener is sent.
    thread = models.ForeignKey(
        "emails.Thread", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="deals",
    )
    # "Do not touch this deal before this time" — the only schedule a deal carries,
    # and it gates *this row alone*. Null means always eligible. Written by the two
    # steps that need to wait: the lookup poll's backoff (``check_lookup``) and a
    # deferred address purchase when there is no room to email the result today.
    # There is deliberately no follow-up clock here: nobody is chased, so the only
    # thing that re-opens an EMAILED deal is an inbound message.
    not_before = models.DateTimeField(null=True, blank=True, db_index=True)
    # The in-flight paid lookup: the provider's job handle and how many times it has
    # been polled (the backoff exponent). Both live here rather than in an external
    # row, so a restart resumes the job from the deal itself and a stalled provider
    # can never hold up anything but this lead.
    lookup_request_id = models.CharField(max_length=64, blank=True, default="")
    lookup_attempt = models.PositiveSmallIntegerField(default=0)
    profile_summary = models.JSONField(null=True, blank=True, default=None)
    chat_summary = models.JSONField(null=True, blank=True, default=None)
    creation_date = models.DateTimeField(default=timezone.now)
    update_date = models.DateTimeField(auto_now=True)

    def __str__(self):
        lead_str = str(self.lead) if self.lead_id else "?"
        return f"{lead_str} [{self.state}]"
