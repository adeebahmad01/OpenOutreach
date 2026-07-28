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
from openoutreach.core.models import Clause
from openoutreach.discovery import LEAD_SENIORITIES, Seniority, describe_clauses

logger = logging.getLogger(__name__)

# How many synthetic ideal profiles anchor a cold campaign. Several rather than one so
# the positive region is outlined rather than pinned to a single hallucination, but few
# enough that a handful of real labels outweighs them.
ANCHOR_COUNT = 3


class ICPSpec(BaseModel):
    """The LLM's provider-agnostic ICP output — one value per family.

    ``seniority`` is typed to Lead Finder's vocabulary, not ``str``: an unknown level
    returns an empty page rather than an error, wasting a fetch. The other families
    are free text — a value the index doesn't carry is a normal empty page, one fetch
    spent. Each family is a single scalar: the seed is one precise conjunction, and
    minting — not the seed — supplies the alternatives.
    """

    job_title: str = ""
    seniority: Seniority | None = None
    location: str = ""
    headcount_min: int = 1
    headcount_max: int = 10000
    country_code: str = ""


# The ICP's free-value families, paired with the ``ICPSpec`` attr each reads. The
# headcount bounds are absent: each is a single number riding every maximal, not a
# value the seed reads from a scalar field.
_CLAUSE_FAMILIES = (
    ("lead_job_title", "job_title"),
    ("lead_seniority", "seniority"),
    ("lead_location", "location"),
)


def _seed_conjunction(spec: ICPSpec) -> list[tuple[str, str]]:
    """Compose the seed clause set — one clause per family the ICP named.

    Both the seed query and the whole starting pool: with one value per family the
    initial maximal set is this single conjunction. A family the model left empty
    contributes no clause. The headcount bounds are always present and appear in every
    maximal unchanged — a size band is this campaign's ICP, not a knob to search.
    """
    clauses = [
        ("company_headcount_min", str(spec.headcount_min)),
        ("company_headcount_max", str(spec.headcount_max)),
    ]
    for family, attr in _CLAUSE_FAMILIES:
        value = getattr(spec, attr)
        if value:
            clauses.append((family, value))
    return sorted(clauses)


def generate_seed(campaign) -> list[tuple[str, str]]:
    """LLM-generate the campaign's seed query and fold its country onto it.

    The cold start: with no clauses there is nothing to fetch, so this is where the
    pool comes from. Also folds ``country_code`` onto the campaign, which geo-stamps
    every discovered Lead. Returns the seed's clause set, or ``[]`` when the ICP is
    empty.
    """
    from pydantic_ai import Agent

    from openoutreach.core.llm import get_llm_model, run_agent_sync

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

    clauses = _seed_conjunction(spec)
    if not clauses:
        return []

    campaign.clauses.set(Clause.rows_for(clauses))

    country_code = spec.country_code.lower()
    if country_code and campaign.country_code != country_code:
        campaign.country_code = country_code
        campaign.save(update_fields=["country_code"])
    logger.info("[%s] %s: %s", campaign,
                colored("discovery seed", "cyan", attrs=["bold"]),
                colored(describe_clauses(clauses), "cyan"))
    return clauses


# ── anchors: the ICP as synthetic profiles ───────────────────────────


class _AnchorProfiles(BaseModel):
    """The LLM's invented ideal leads, each one line in ``profile_text_for``'s shape."""

    profiles: list[str] = Field(
        default_factory=list,
        description="Lowercase one-line lead profiles: headline, industry, job title, "
                    "company name, seniority, company industry, state, country — space "
                    "separated, no labels.",
    )


def generate_anchors(campaign) -> list[str]:
    """LLM-invent ideal-lead profiles for a campaign. ``[]`` on an outage or empty ICP.

    Best-effort by design: an unanchored campaign still runs, it just spends its cold
    phase without a fitted GP, so failure must not propagate to the caller.
    """
    from pydantic_ai import Agent

    from openoutreach.core.llm import get_llm_model, run_agent_sync

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    prompt = env.get_template("anchor_profiles.j2").render(
        product_docs=campaign.product_docs,
        campaign_target=campaign.campaign_target,
        count=ANCHOR_COUNT,
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


def ensure_anchors(campaign) -> np.ndarray | None:
    """The campaign's anchor embeddings as ``(N, dim)``, generating them on first use.

    Persisted on the campaign so the daemon doesn't re-invent them (and re-anchor the GP
    somewhere slightly different) on every restart. ``None`` when the campaign has no
    ICP text to work from or the LLM call failed — callers treat that as "no anchors",
    never as an error.

    Only ever called while a campaign has no real positive; ``BayesianQualifier`` clears
    both the stored profiles and these embeddings the moment one arrives, so a returning
    value here always means the cold phase is still running.
    """
    from openoutreach.discovery import embed_profile

    if campaign.anchor_embeddings and campaign.anchor_profiles:
        stored = np.frombuffer(bytes(campaign.anchor_embeddings), dtype=np.float32)
        return stored.reshape(len(campaign.anchor_profiles), -1).copy()

    if not (campaign.product_docs or campaign.campaign_target):
        return None

    profiles = generate_anchors(campaign)
    if not profiles:
        return None

    embeddings = np.array([embed_profile(p) for p in profiles], dtype=np.float32)
    campaign.anchor_profiles = profiles
    campaign.anchor_embeddings = embeddings.tobytes()
    campaign.save(update_fields=["anchor_profiles", "anchor_embeddings"])
    logger.info("[%s] %s: %d synthetic ideal profile(s)", campaign,
                colored("anchors", "cyan", attrs=["bold"]), len(profiles))
    return embeddings
