import os, json, urllib.request
from pathlib import Path
from dotenv import load_dotenv
import psycopg2

load_dotenv(Path(__file__).parent.parent / ".env")
conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"))
cur = conn.cursor()
cur.execute("SELECT siren, legal_name, trade_name FROM staging.companies WHERE siren IS NOT NULL LIMIT 3")
rows = cur.fetchall()
conn.close()

for siren, legal, trade in rows:
    name = legal or trade
    print(f"Testing SIREN: {siren} ({name})")
    url = f"https://recherche-entreprises.api.gouv.fr/search?q={siren}&page=1&per_page=1"
    r = urllib.request.urlopen(url)
    data = json.loads(r.read())
    res = data.get("results", [])
    if res:
        c = res[0]
        fields = [
            "nom_raison_sociale", "activite_principale", "nature_juridique",
            "tranche_effectif_salarie", "date_creation", "date_fermeture",
            "etat_administratif", "categorie_entreprise",
        ]
        for f in fields:
            print(f"  {f:<30}: {c.get(f)}")
        siege = c.get("siege") or {}
        print(f"  siege.code_postal              : {siege.get('code_postal')}")
        print(f"  siege.activite_principale      : {siege.get('activite_principale')}")
        print(f"  siege.etat_administratif       : {siege.get('etat_administratif')}")
        print(f"  siege.nombre_etablissements    : {c.get('nombre_etablissements')}")
    else:
        print("  NO RESULT from API")
    print()
