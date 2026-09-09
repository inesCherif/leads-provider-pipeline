"""
M5-S13 — SIRENE liveness for the gîtes/campings population (wrap of m3ag_s13)
=============================================================================
`m3ag_s13_sirene_etat.py` is 100 % sector-neutral: one GET per 14-digit SIRET
on recherche-entreprises.api.gouv.fr, états of the legal unit and of the
établissement, Liquidateur note, "our failure is not evidence" rule, 20 %
not-found abort, append+flush every 50, resume by skipping SIRETs present.

This wrapper only points it at the M5 checkpoint tree: it reads
`operateurs_<dept>.csv` (the m3ag_s2 adapter, comma-delimited — m5_s2
writes exactly that) and writes `sirene_etat_<dept>.csv` next to it.

Output: exports/hebergement/checkpoints/sirene_etat_<dept>.csv

Usage:
    python scripts/m5_s13_sirene_etat.py --departement 63 --limit 20   # pilot
    python scripts/m5_s13_sirene_etat.py --departement 63
    python scripts/m5_s13_sirene_etat.py --departement 03
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import m3ag_s13_sirene_etat as core   # noqa: E402
from m5_lib import CHECK_DIR          # noqa: E402

core.CHECK_DIR = CHECK_DIR            # module global, read at call time by core.main()
core.log = logging.getLogger("m5_s13")

if __name__ == "__main__":
    core.main()
