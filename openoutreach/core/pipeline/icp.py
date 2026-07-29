# openoutreach/core/pipeline/icp.py
"""ICP generators — the LLM writes the campaign's cold-start priors, in two shapes.

The same two inputs (``product_docs + campaign_target``) are the only prior available
before any lead has been judged, and the engine needs them expressed two ways:

- ``generate_seed`` — the ICP as a **query**: one value per family (a title, a
  seniority, a country, a size band), the single most precise conjunction the model can
  name. That conjunction is the campaign's whole starting **pool**, so the initial
  maximal set is exactly one query — the seed. Breadth is not seeded; it grows from the
  leads that qualify (``mint.py``), which add more values per family and so more
  maximals for the selector to rank.
- ``generate_anchors`` — the ICP as **profiles**: a few invented leads that would be
  ideal fits, written in the shape ``discovery.profile_text_for`` produces. Embedded and
  handed to the GP as synthetic positives (``BayesianQualifier.set_anchors``), they are
  what lets the model fit at all on a campaign whose every real verdict so far is a
  rejection — a single-class label set never produces a posterior, so without them BALD,
  P(f>0.5), and every piece of steering that reads them stay unavailable for the whole
  cold phase. They are dropped the moment a real lead qualifies (see
  ``ensure_anchors``).

Profiles rather than the product text itself because the space they have to land in is
one of *lead* embeddings: marketing prose about the product embeds nowhere near a row of
firmographics, so it would anchor the model in a region no candidate occupies. They are
also embedded **without** query terms (unlike a discovered lead, whose retrieving query
rides its embedding) — an anchor is a claim about what a good lead looks like, not about
which query to run, and folding the seed's keywords in would have discovery score the
seed highly on the strength of our own guess.

One value per family, never headcount as a range to search: the size band is a single
ICP attribute that rides every maximal unchanged. See ``discovery.filters_for``.
"""
from __future__ import annotations

import logging

import jinja2
import numpy as np
from pydantic import BaseModel, Field
from termcolor import colored

from openoutreach.core.conf import PROMPTS_DIR
from openoutreach.discovery import LEAD_SENIORITIES, Seniority

logger = logging.getLogger(__name__)

# How many synthetic ideal profiles anchor a cold campaign. Several rather than one so
# the positive region is outlined rather than pinned to a single hallucination, but few
# enough that a handful of real labels outweighs them.
ANCHOR_COUNT = 3


class ICPSpec(BaseModel):
    """The LLM's provider-agnostic ICP output — the walk's opening **vocabulary**.

    Not a query. The seed used to be "one value per family, the single most precise
    conjunction", which is what the clause model needed; the walk now conjoins tokens
    itself against measured feedback, so what it wants from the LLM is *words worth
    trying*, and as many as the ICP genuinely implies.

    **``domain_keywords`` is the field that makes an ICP an ICP.** The old spec had
    nowhere to put "what the target company actually does" — no field for it — so a
    health-and-wellness campaign seeded on ``content``/``lead``/``united``/``states``
    and every query it could compose selected for *role* while being blind to
    *industry*. The obvious home would be ``lead_industry``, and that field is inert:
    a nonsense value returns the identical count to no filter at all (§8 of the roadmap
    card). But domain words are demonstrably alive in ``lead_job_title``, which matches
    title *and* headline text — ``saas`` counts 3,306, ``startup`` 6,223, ``llm`` 1,214,
    ``agentic`` 1,213, ``stealth`` 932 (§10). So they go there, alongside the role words,
    and the frontier conjoins the two.

    ``seniority`` is typed to Lead Finder's vocabulary, not ``str``: an unknown level
    returns an empty page rather than an error, wasting a fetch. Everything else is free
    text — a token the index doesn't carry is a normal empty page, one fetch spent, and
    the walk retires it.
    """

    role_keywords: list[str] = Field(
        default_factory=list,
        description="Single lowercase words from the job titles the buyer holds — "
                    "'founder', 'head', 'marketing', 'content'. Words, never phrases.",
    )
    domain_keywords: list[str] = Field(
        default_factory=list,
        description="Single lowercase words naming what the target company does or "
                    "sells — 'wellness', 'supplement', 'nutrition', 'saas'. Words, "
                    "never phrases.",
    )
    seniority: Seniority | None = None
    location: str = ""
    headcount_min: int = 1
    headcount_max: int = 10000
    country_code: str = ""


# Which ``ICPSpec`` attrs feed which search axis. Both keyword lists land in
# ``lead_job_title`` — the only free-text axis, and the one that matches headline text as
# well as titles, which is what makes a domain word like ``wellness`` reachable at all.
# Headcount is absent: numbers riding every query, not search terms.
_SEED_FIELDS = (
    ("lead_job_title", "role_keywords"),
    ("lead_job_title", "domain_keywords"),
    ("lead_seniority", "seniority"),
    ("lead_location", "location"),
)


def _seed_keywords(spec: ICPSpec) -> list[tuple[str, str]]:
    """The ICP as ``(field, token)`` keywords — the vocabulary the walk opens with.

    Everything is **split into words**, list or scalar, because a keyword is one word:
    Lead Finder reads ``"Head of Growth"`` as three ANDed tokens, a query narrow enough
    to be empty before the walk has learned anything. Splitting hands the frontier three
    separate one-token nodes and lets *measurement* decide which pair is worth conjoining
    — which is how ``"founder cto"`` (9,027 rows, near-perfect precision) gets found and
    ``"head of growth"`` never gets fired. Stopwords go with them, so ``of`` never becomes
    a search term, and the model's own phrasing survives being sloppy.
    """
    from openoutreach.core.pipeline.vocabulary import tokenize

    keywords = set()
    for field, attr in _SEED_FIELDS:
        value = getattr(spec, attr)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item:
                keywords |= {(field, token) for token in tokenize(str(item))}
    return sorted(keywords)


def generate_seed(campaign) -> list[tuple[str, str]]:
    """LLM-generate the campaign's opening vocabulary and size band.

    The cold start, and the **only** LLM call discovery makes about queries: with no
    qualified leads there are no profiles to count words from, so the ICP text is the one
    available source. Everything after this is counting (``vocabulary.refresh``). Also
    folds ``country_code`` and the headcount band onto the campaign — the band rides every
    query unchanged and is never searched.

    Returns the seed keywords, or ``[]`` when the ICP is empty.
    """
    from pydantic_ai import Agent

    from openoutreach.core.llm import get_llm_model, run_agent_sync
    from openoutreach.core.models import Keyword
    from openoutreach.discovery import describe_node

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    prompt = env.get_template("icp_filters.j2").render(
        product_docs=campaign.product_docs,
        campaign_target=campaign.campaign_target,
        seniorities=LEAD_SENIORITIES,
    )

    agent = Agent(
        get_llm_model(),
        output_type=ICPSpec,
        model_settings={"temperature": 0.3, "timeout": 60},
    )
    spec = run_agent_sync(agent.run(prompt)).output

    keywords = _seed_keywords(spec)
    if not keywords:
        return []

    Keyword.rows_for(keywords)

    updates = []
    country_code = spec.country_code.lower()
    if country_code and campaign.country_code != country_code:
        campaign.country_code = country_code
        updates.append("country_code")
    if (campaign.headcount_min, campaign.headcount_max) != (spec.headcount_min, spec.headcount_max):
        campaign.headcount_min = spec.headcount_min
        campaign.headcount_max = spec.headcount_max
        updates += ["headcount_min", "headcount_max"]
    if updates:
        campaign.save(update_fields=updates)

    logger.info("[%s] %s: %s · headcount %d–%d", campaign,
                colored("discovery seed", "cyan", attrs=["bold"]),
                colored(describe_node(keywords), "cyan"),
                spec.headcount_min, spec.headcount_max)
    return keywords


# ── anchors: the ICP as synthetic profiles ───────────────────────────


class _AnchorProfiles(BaseModel):
    """The LLM's invented ideal leads, each one line in ``profile_text_for``'s shape."""

    profiles: list[str] = Field(
        default_factory=list,
        description="Lowercase one-line lead profiles: headline, industry, job title, "
                    "company name, seniority, company industry, state, country — space "
                    "separated, no labels.",
    )


def generate_anchors(campaign, count: int = ANCHOR_COUNT, existing=()) -> list[str]:
    """LLM-invent ``count`` ideal-lead profiles. ``[]`` on an outage or empty ICP.

    ``existing`` are the profiles already written for this campaign — shown to the model
    so a top-up round widens the positive region instead of restating it.

    Best-effort by design: an unanchored campaign still runs, it just spends its cold
    phase without a fitted GP, so failure must not propagate to the caller.
    """
    from pydantic_ai import Agent

    from openoutreach.core.llm import get_llm_model, run_agent_sync

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    prompt = env.get_template("anchor_profiles.j2").render(
        product_docs=campaign.product_docs,
        campaign_target=campaign.campaign_target,
        count=count,
        existing=list(existing),
    )

    try:
        agent = Agent(
            get_llm_model(),
            output_type=_AnchorProfiles,
            # Warmer than the seed: the seed wants the single most likely conjunction,
            # these want spread across the ideal region.
            model_settings={"temperature": 0.8, "timeout": 60},
        )
        result = run_agent_sync(agent.run(prompt)).output
    except Exception:
        logger.exception("[%s] anchor generation failed — campaign stays unanchored", campaign)
        return []

    return [p.strip().lower() for p in result.profiles if p.strip()]


def stored_anchors(campaign) -> np.ndarray | None:
    """The campaign's persisted anchor embeddings as ``(N, dim)``, or ``None``."""
    if not (campaign.anchor_embeddings and campaign.anchor_profiles):
        return None
    stored = np.frombuffer(bytes(campaign.anchor_embeddings), dtype=np.float32)
    return stored.reshape(len(campaign.anchor_profiles), -1).copy()


def ensure_anchors(campaign, minimum: int = ANCHOR_COUNT) -> np.ndarray | None:
    """The campaign's anchor embeddings as ``(N, dim)``, topped up to ``minimum``.

    Generates on first use and **adds** on later calls, so the synthetic positive class
    can be grown to track the rejections piling up against it (see
    ``pools._rebalance_anchors``). Already-written profiles are shown to the model so a
    top-up widens the ideal region rather than restating it, and the whole accumulated
    set is persisted — the daemon must not re-invent anchors (and re-anchor the GP
    somewhere slightly different) on every restart.

    ``None`` when the campaign has no ICP text to work from, or the LLM call failed and
    nothing is stored — callers treat that as "no anchors", never as an error. A failed
    *top-up* keeps whatever is already there.

    Only ever called while a campaign has no real positive; ``BayesianQualifier`` clears
    both the stored profiles and these embeddings the moment one arrives, so a returning
    value here always means the cold phase is still running.
    """
    from openoutreach.discovery import embed_profile

    profiles = list(campaign.anchor_profiles or [])
    stored = stored_anchors(campaign)
    if len(profiles) >= minimum:
        return stored

    if not (campaign.product_docs or campaign.campaign_target):
        return stored

    fresh = [
        p for p in generate_anchors(campaign, count=minimum - len(profiles), existing=profiles)
        if p not in profiles
    ]
    if not fresh:
        return stored

    embeddings = np.array([embed_profile(p) for p in fresh], dtype=np.float32)
    if stored is not None:
        embeddings = np.vstack([stored, embeddings])

    campaign.anchor_profiles = profiles + fresh
    campaign.anchor_embeddings = embeddings.tobytes()
    campaign.save(update_fields=["anchor_profiles", "anchor_embeddings"])
    logger.info("[%s] %s: +%d synthetic ideal profile(s) (%d total)", campaign,
                colored("anchors", "cyan", attrs=["bold"]), len(fresh), len(embeddings))
    return embeddings
