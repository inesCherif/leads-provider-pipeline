"""
Put the two already-delivered départements into the France delivery tree.

03 and 63 were built, gated and sent as V2 on 2026-09-11, so a France run must
not rebuild them as v1 — that would hand Sam a second, different file for the
same département under a different name.

They ARE rebuilt as **v3**, because france_verify found that the V2 files Sam
holds break the principle: dept 03 ships a charcuterie, a pork abattoir and a
vigneron, dept 63 a vignoble and a cidrerie. They were built before the name
test existed. v3 drops those five and keeps the two rows that only LOOK like
violations — A. VIGNERON and CAMILLE VIGNERON, NAF 01.19Z, where Vigneron is
the surname (config/principle_rescue.csv, Ines 2026-09-19).

Gated against V2: 03 is 0 failed; 63 is 0 failed with --email-drop-allow 1,
the one e-mail belonging to CIDRERIE DES VOLCANS and leaving with its row.
63's phones went UP, 899 -> 921, from the refreshed national harvests.

    python scripts/france_copy_v2.py
"""

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import france_run as fr                       # noqa: E402
from france_lib import ALREADY_DONE, region_dir   # noqa: E402

DELIVERED_VERSION = "v3"     # was v2 until the principle name test, 2026-09-19
SUPERSEDED = ("v1", "v2")    # older copies are removed from the delivery tree


def main() -> None:
    status_path = fr.OUT_ROOT / "france_status_delivered.csv"
    rows = {}
    for dept in ALREADY_DONE:
        src = fr.M7_OUT / f"producteurs_{dept}_{DELIVERED_VERSION}.xlsx"
        if not src.exists():
            fr.log.warning(f"[{dept}] {src.name} missing — skipped")
            continue
        dest = fr.OUT_ROOT / region_dir(dept)
        dest.mkdir(parents=True, exist_ok=True)
        # Remove the superseded copies first: two versions of one département
        # side by side is exactly the confusion this script exists to avoid.
        for old in SUPERSEDED:
            for stale in dest.glob(f"producteurs_{dept}_{old}*"):
                stale.unlink()
                fr.log.info(f"[{dept}] removed superseded {stale.name}")
        for suffix in (".xlsx", ".csv", "_sans_siret.csv"):
            p = fr.M7_OUT / f"producteurs_{dept}_{DELIVERED_VERSION}{suffix}"
            if p.exists():
                shutil.copy2(p, dest / p.name)

        saved, fr.VERSION = fr.VERSION, DELIVERED_VERSION
        counts = fr.read_xlsx_counts(dept)
        fr.VERSION = saved

        rows[dept] = {"dept": dept, "region": region_dir(dept), "status": "ok",
                      "failed_step": "",
                      "gate": f"0 failed ({DELIVERED_VERSION}, principle fix on the delivered V2)",
                      "started": "", "finished": "2026-09-19", "minutes": "",
                      **counts}
        fr.log.info(f"[{dept}] {DELIVERED_VERSION} copied to {dest.name}/  "
                    f"rows={counts.get('rows')} tel={counts.get('phones')} "
                    f"mail={counts.get('emails')} joign={counts.get('joignables_total')}")

    if rows:
        fr.save_status(rows, status_path)
        fr.log.info(f"status -> {status_path.name}")


if __name__ == "__main__":
    main()
