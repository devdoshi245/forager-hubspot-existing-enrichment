"""
bulk.estimate
-------------
DRY-RUN. Spends ZERO credits. Counts what's in HubSpot and how much of it is not
yet enriched, then projects the cost of a full backfill. This is the number you
take to the client for budget sign-off BEFORE anything is ever executed.

What it can and can't know:
  * Forager reveal cost per contact is an ESTIMATE (~25 credits — the same figure
    the core service logs). Forager bills the real amount; treat this as a ceiling-ish
    planning number, not an invoice.
  * Companies cost ~0 Forager credits (the Forager org lookup is ~free); their real
    cost is Anthropic/Claude usage (ICP scoring + the logo web-search step), which is
    billed on the Claude account, not in Forager credits. We report the call count so
    you can price it against your Claude plan.
"""

import enrichment  # from core/ — gives us the same per-reveal credit figure the service uses

from bulk import hubspot_list

# Per-contact reveal cost, in Forager credits. Sourced from the core service so this
# stays in lock-step with what the live enrichment logs/estimates use.
CREDITS_PER_CONTACT = getattr(enrichment, "EST_CREDITS_PER_REVEAL", 25)


def estimate_companies() -> dict:
    counts = hubspot_list.count_companies()
    remaining = counts["remaining"]
    return {
        "object": "companies",
        **counts,
        "forager_credits_estimate": 0,  # company lookup is ~free in Forager
        "claude_scoring_runs": remaining,  # ICP + logo web-search per company (Anthropic cost)
        "note": "Companies cost ~0 Forager credits; cost is Claude/Anthropic usage "
                "(2 LLM steps each: ICP scoring + logo web-search) billed on the Claude account.",
    }


def estimate_contacts() -> dict:
    counts = hubspot_list.count_contacts()
    remaining = counts["remaining"]
    return {
        "object": "contacts",
        **counts,
        "credits_per_contact": CREDITS_PER_CONTACT,
        "forager_credits_estimate": remaining * CREDITS_PER_CONTACT,
        "note": f"~{CREDITS_PER_CONTACT} Forager credits per reveal (email+phone). "
                "Estimate only — Forager bills the real amount.",
    }


def estimate_all() -> dict:
    return {"companies": estimate_companies(), "contacts": estimate_contacts()}


def format_summary(est: dict) -> str:
    """Plain-English block you can paste to the client / Shirish."""
    c = est["companies"]
    p = est["contacts"]
    lines = [
        "=== BACKFILL ESTIMATE (dry-run — no credits spent) ===",
        "",
        "COMPANIES",
        f"  Total in HubSpot ......... {c['total']:,}",
        f"  Already enriched ......... {c['enriched']:,}",
        f"  Still to enrich .......... {c['remaining']:,}",
        f"  Forager credits .......... ~0 (company lookup is ~free)",
        f"  Claude scoring runs ...... {c['claude_scoring_runs']:,}  (Anthropic cost, not Forager)",
        "",
        "CONTACTS",
        f"  Total in HubSpot ......... {p['total']:,}",
        f"  Already enriched ......... {p['enriched']:,}",
        f"  Still to enrich .......... {p['remaining']:,}",
        f"  Forager credits/contact .. ~{p['credits_per_contact']}",
        f"  Forager credits TOTAL .... ~{p['forager_credits_estimate']:,}",
        "",
        "Headline number for the client: "
        f"~{p['forager_credits_estimate']:,} Forager credits to reveal all remaining contacts, "
        f"plus {c['claude_scoring_runs']:,} Claude scoring runs for companies.",
        "Nothing has been spent. Execution requires the explicit --execute flag.",
    ]
    return "\n".join(lines)
