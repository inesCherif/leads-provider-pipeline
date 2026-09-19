"""
Put the two already-delivered départements into the France delivery tree.

03 and 63 were built, gated and sent as V2 on 2026-09-11. A France run must
not rebuild them as v1: that would hand Sam a second, different file for the
same département under a different name. So they are copied as they are, and
their measured numbers are read back from the V2 files and added to the
status so the recap totals cover the whole country.

    python scripts/france_copy_v2.py
"""

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import france_run as fr                       # noqa: E402
from france_lib import ALREADY_DONE, region_dir   # noqa: E402

DELIVERED_VERSION = "v2"


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
        for suffix in (".xlsx", ".csv", "_sans_siret.csv"):
            p = fr.M7_OUT / f"producteurs_{dept}_{DELIVERED_VERSION}{suffix}"
            if p.exists():
                shutil.copy2(p, dest / p.name)

        saved, fr.VERSION = fr.VERSION, DELIVERED_VERSION
        counts = fr.read_xlsx_counts(dept)
        fr.VERSION = saved

        rows[dept] = {"dept": dept, "region": region_dir(dept), "status": "ok",
                      "failed_step": "", "gate": "0 failed (V2, delivered 2026-09-11)",
                      "started": "", "finished": "2026-09-11", "minutes": "",
                      **counts}
        fr.log.info(f"[{dept}] V2 copied to {dest.name}/  "
                    f"rows={counts.get('rows')} tel={counts.get('phones')} "
                    f"mail={counts.get('emails')} joign={counts.get('joignables_total')}")

    if rows:
        fr.save_status(rows, status_path)
        fr.log.info(f"status -> {status_path.name}")


if __name__ == "__main__":
    main()
