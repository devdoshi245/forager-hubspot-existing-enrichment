"""
bulk — the backfill/"catch-up" layer for existing HubSpot records.

This package is the ONLY place new code lives. The proven enrichment logic
(Forager client, HubSpot client, Claude scoring, Deepline, orchestration) is a
FROZEN, unmodified snapshot under ../core (see CORE_SNAPSHOT.md).

Those core modules import each other by bare name (``import forager``,
``import hubspot``, ...), exactly as in the original service. So we put the
``core/`` directory on sys.path here, once, and import them by bare name too.
Do NOT import them as ``core.forager`` — that breaks their internal cross-imports.
"""

import os
import sys

_CORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)
