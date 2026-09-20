# RUNBOOK — les commandes exactes, dans l'ordre

Tout se lance depuis la racine du dépôt, après l'installation du `README.md` :

```bash
export PYTHONIOENCODING=utf-8
mkdir -p logs exports
```

Avant un nouveau drapeau, lire l'aide du script : `python scripts/<nom>.py --help` (la docstring de
chaque script explique ce qu'il fait, ce qu'il écrit et comment il reprend).

**Sommaire** — [0. Règles de conduite](#0-règles-de-conduite) · [1. Base Supabase](#1-base-supabase-m1--m4) ·
[2. Boulangeries](#2-boulangeries-m2) · [3. Agriculteurs bio](#3-agriculteurs-bio-m3ag) ·
[4. Gîtes et campings](#4-gîtes-et-campings-m5) · [5. Éleveurs](#5-éleveurs-m6) ·
[6. Producteurs](#6-producteurs-m7) · [7. France entière](#7-france-entière) ·
[8. Finir une passe d'enrichissement](#8-finir-une-passe-denrichissement-sur-une-région) ·
[9. Pages Jaunes](#9-pages-jaunes-par-chrome-attaché) · [10. Feuilles d'appels](#10-feuilles-dappels-et-registre-jamais-deux-fois) ·
[11. Ajouter un département ou un secteur](#11-ajouter-un-département-ou-un-secteur) ·
[12. Arrêter, surveiller](#12-arrêter-surveiller) · [13. Incidents et règles](#13-incidents-et-règles)

---

## 0. Règles de conduite

| Règle | Pourquoi |
|---|---|
| **Pilote d'abord** (`--pilot 10` ou `20`), puis ouvrir à la main les lignes écrites | un pilote a affiché 50 % en écrivant des déchets ; seuil de décision : 30 % de lignes utilisables |
| **Plus de ~10 min = détaché**, avec un journal | un terminal fermé tue le travail ; tous les scripts longs journalisent `[n/total]` |
| **Relancer = reprendre** | chaque script garde un fichier « fait » ; tuer un run ne perd rien |
| **Un fichier ne part que si le contrôle dit `0 failed`** | les contrôles relisent le `.xlsx`, pas les journaux |
| **Une seule moisson à la fois sur le Chrome attaché** (port 9222) | deux moissons se disputent le même onglet |
| **Un secteur par jour sur `ddgs`** | le compteur de quota (300/jour) est partagé |
| **`--dry-run` avant toute écriture en base** | tout script qui écrit en base le propose |

Lancer détaché sous Windows (PowerShell) :

```powershell
Start-Process "C:\Program Files\Git\usr\bin\sh.exe" -ArgumentList 'scripts/<lanceur>.sh <args>' `
  -WorkingDirectory (Get-Location).Path -WindowStyle Hidden
```

Si le shell détaché ne trouve pas Python : définir la variable d'environnement `PYTHON_DIR` (le
dossier qui contient `python.exe`). Sous Linux/macOS : `nohup sh scripts/<lanceur>.sh <args> &`.
Pour un script Python seul : `python scripts/<nom>.py > logs/<nom>.log 2>&1 &` — le dossier `logs/`
doit exister, sinon la redirection échoue et le script ne démarre jamais.

---

## 1. Base Supabase (M1 + M4)

Prérequis : `SUPABASE_DB_URL` (session pooler) dans `.env` ; projet **réveillé** (l'offre gratuite
met le projet en pause : le restaurer dans le tableau de bord). Migrations : `migrations/README.md`
(001 → 020, rejouables sauf le bloc RLS de 001).

| # | Script | Commande | Durée | Détaché ? | Écrit | Reprise |
|---|---|---|---|---|---|---|
| 0 | `check_data_quality.py` | `--strict` · `--sector <clé>` | 1–2 min | non | rien | — |
| 1 | `test_normalisers.py` | — | < 1 s | non | rien | — |
| 2 | `m1_s1_inspect_files.py` | — | minutes | non | rien (profil des fichiers) | — |
| 3 | `m1_s3_ingest.py` | `--list` · `--file "<nom>" --dry-run` · `--file "<nom>"` · `--limit N` | 1 min (9 k lignes) à 6 min (50 k) | le gros fichier | `source_files`, `raw.ingest_rows`, `companies`, `sites`, `contacts`, `emails`, `company_sources`, `contact_points`… | dix étapes validées une à une ; un fichier fini s'affiche `[SKIP]` (hash) |
| 4 | `m4_s1_load_boulangerie.py` | `--dry-run` · (rien) · `--version v15` | 2 min | non | mêmes tables + colonnes registre | par hash du livrable |
| 5 | `m4_s2_load_agriculteurs.py` | `--departement 63 --dry-run` · `--departement 63` | 1 min | non | mêmes tables + vivacité SIRENE | par hash |
| 6 | `m1_s4_sirene_enrich.py` | (rien) · `--limit 50 --dry-run` | **heures** | **oui** | `naf_code`, `sirene_etat`, `legal_form`… là où c'est NULL | `sirene_last_checked_at` ; **relancer s'il meurt** |
| 7 | `m1_s5_qualify.py` | `--sector <clé> --force --dry-run` · `--sector <clé> --force` | 5–30 s | non | `qualification_*`, `audit.audit_log` | idempotent |
| 8 | `m1_s5b_dedup.py` | `--dry-run` · (rien) | 1 min | non | `duplicate_of_company_id` (non destructif) | lignes non marquées |
| 9 | `m1_s8_export.py` | `--sector <clé> --dry-run` · `--sector <clé> --format both` | 30 s à 5 min | les deux gros | `exports/<slug>/<slug>_{email,phone}.{xlsx,csv}` | écrase |
| — | `m4_unload_file.py` | `--file "<nom>" --dry-run` · `--file "<nom>" --confirm` | 30 s | non | **SUPPRIME les lignes staging d'UN fichier** (raw intact) | — |

Clés de secteur : `agriculture_livestock`, `boulangerie`, `imprimerie`, `viticulture`, `tourisme`,
`agriculteurs_bio` (définies dans `config/sector_rules.py`, `SECTORS`).

Pièges : `m1_s4 --dry-run` sans `--limit` ne rend jamais la main · `m1_s5_qualify --dry-run` seul
sélectionne 0 ligne quand la version de règle n'a pas changé : utiliser `--force --dry-run`.

### Recette A — ajouter un fichier fournisseur

1. **Le profiler** : tout lire en texte ; compter lignes, identifiants, remplissage e-mail/téléphone, doublons.
2. **Choisir ou créer le secteur** : entrée dans `SECTORS` (`key`, `rule_version`, `source_sectors`,
   préfixes NAF, `include`/`exclude`). Le contrôle refuse un secteur sans jeu de règles.
3. **Écrire la spec** dans `config/ingest_specs.py` : fichier, feuille, secteur, `col_map`,
   `required`, `phone_columns` par priorité, `email_columns`, `attribute_columns`,
   `identifier_columns`, un `pre_clean` si le fichier a besoin d'une réparation.
4. `python scripts/m1_s3_ingest.py --file "<nom>" --dry-run` jusqu'à ce que la distribution affichée corresponde au profil.
5. Pilote `--limit 200`, contrôle, relire 20 lignes contre le fichier.
6. Charger, contrôler, relancer pour voir `[SKIP]`.
7. Qualifier (`--force --dry-run`, lire les échantillons, puis réel), dédupliquer, exporter.

### Recette B — une règle a changé après un chargement

Ne jamais corriger les lignes à la main. Changer la règle dans le chargeur (avec un test unitaire), puis :

```bash
python scripts/m1_s3_ingest.py --file "<nom>" --dry-run
python scripts/m4_unload_file.py --file "<nom>" --dry-run
python scripts/m4_unload_file.py --file "<nom>" --confirm
python scripts/m1_s3_ingest.py --file "<nom>"
python scripts/check_data_quality.py --strict
```

### Recette C — après la fin de l'enrichissement SIRENE

```bash
python scripts/m1_s5_qualify.py --sector boulangerie --force
python scripts/m1_s5_qualify.py --sector tourisme --force
python scripts/check_data_quality.py --strict
python scripts/m1_s8_export.py --sector boulangerie --format both
```

| Symptôme | Cause | Réponse |
|---|---|---|
| tout appel base expire | projet en pause | le restaurer dans le tableau de bord |
| `could not translate host name` | hôte direct (IPv6 seulement) | utiliser la chaîne *session pooler* |
| `COLUMN CONTRACT VIOLATION` | les en-têtes ne correspondent pas à la spec | corriger la spec, pas le fichier |
| `[SKIP]` alors que le chargeur a changé | fichier identique et terminé | Recette B |
| `statement timeout` sur une migration | session orpheline `idle in transaction` | `pg_stat_activity`, `pg_terminate_backend(pid)` |
| `SSL connection has been closed unexpectedly` | coupure du pooler | l'export réessaie seul ; `m1_s4` se relance |
| un `UPDATE` sur `CHAR(n)` ne touche rien | espaces de remplissage | `btrim()` avant de comparer |

---

## 2. Boulangeries (M2)

Fichiers sous `exports/boulangerie/` et `exports/boulangerie/checkpoints/`. Périmètre : NAF 10.71C,
10.71B, 10.71D, un département (pilote : 13).

| # | Script | Rôle | Durée | Détaché ? | Besoin | Reprise |
|---|---|---|---|---|---|---|
| s1 | `m2_s1_acquire.py` | registre → `api_raw.jsonl` | ~2 min | non | — | fichier de progression |
| s2 | `m2_s2_transform.py` | → `etablissements.csv` (1 ligne par site) | secondes | non | s1 | idempotent |
| s5 | `m2_s5_osm.py` | OpenStreetMap (Overpass) | ~1 min | non | — | idempotent |
| s6 | `m2_s6_pagesjaunes.py --attach` | Pages Jaunes | 1–3 h | oui | **Chrome attaché** (§ 9) | `pj_done.txt` |
| s7 | `m2_s7_match.py` | fiches → SIRET (géographie puis nom) | secondes | non | des fiches | fonction pure |
| s8 | `m2_s8_websites.py --backend ddgs` (ou `tavily`) | recherche de sites + extraits | 30–60 min | oui | quota | `sites_done.txt` |
| s21 | `m2_s21_validate_sites.py` | verdict par (SIRET, domaine) | ~5 s/domaine | 1re fois | — | `validate_done.txt` |
| s9 | `m2_s9_emails.py --only-valid` | crawl des sites validés | ~10 s/domaine | si beaucoup | s21 | `emails_done.txt` |
| s10 | `m2_s10_patterns.py` | adresses générées, prouvées par SMTP | ~3 min | non | s21, s2 | `patterns_done.txt` |
| s11 | `m2_s11_verify.py` | vérification SMTP de tout | ~15 min | oui | — | re-sonde les non-verdicts |
| s13 | `m2_s13_places.py` | Google Maps (Serper `/maps`) | ~30 min | oui | crédits Serper | ancres faites |
| s16 | `m2_s16_serp_phones.py --backend ddgs` | téléphones d'extraits (corroboration) | ~35 min / 300 | oui | quota du jour | `serp_done.txt` |
| s18 | `m2_s18_social_emails.py --network facebook` | pages Facebook publiques | ~14 s/page | oui | playwright | `social_done.txt` |
| s23 | `m2_s23_google_panel.py` | fiches Google Business | ~2 h / 850 | oui | **Chrome attaché** | `google_panel_done.txt` |
| s24–s26 | `m2_s24_rdap.py`, `m2_s25_bcontact.py`, `m2_s26_legal.py` | RDAP AFNIC, annuaire métier, mentions légales | minutes | non | — | fichiers faits |
| s14 | `m2_s14_export_v3.py --version vN` | **la fusion** → livrable | ~30 s | non | tout | écrase |
| s19 | `m2_s19_check_v5.py --version vN --baseline vM --strict` | **le contrôle** | ~1 min | non | s14 | — |

Retirés, ne pas lancer : `m2_s3`/`m2_s4` (V1), `m2_s12`, `m2_s15`, `m2_s17` (anciens exports et
contrôles), `m2_s20` (vérification par API : pilote raté), `m2_s22` (pages détail PJ : 425 lues → 1 site).

**La reconstruction standard, après toute nouvelle moisson :**

```bash
python scripts/m2_s7_match.py
python scripts/m2_s21_validate_sites.py
python scripts/m2_s9_emails.py --only-valid
python scripts/m2_s11_verify.py
python scripts/m2_s14_export_v3.py --version v16
python scripts/m2_s19_check_v5.py --version v16 --baseline v15 --strict --site-drop-allow 0 --email-drop-allow 0 --phone-drop-allow 0
```

Quotas : `python scripts/m2lib_search.py --quota` · nouvelle clé : `python scripts/m2lib_search.py --reset-pool serper`
(ou `tavily`) · `--selftest`, `--ping`.

---

## 3. Agriculteurs bio (M3AG)

Source de population : l'API ouverte de l'Agence Bio. Fichiers sous `exports/agriculteurs/`.

```bash
python scripts/m3ag_s1_acquire.py --departements 63 --fresh
python scripts/m3ag_s2_transform.py --departements 63
python scripts/m3ag_s6_osm.py --departement 63
python scripts/m3ag_s10_baf.py --departement 63 --pilot 10      # bienvenue-à-la-ferme ; puis sans --pilot
sh scripts/m3ag_s7_run_until_done.sh 63                          # Pages Jaunes, détaché, Chrome attaché (§ 9)
python scripts/m3ag_s8_match.py --departement 63 --report-rejects
python scripts/m3ag_s3_search.py --departement 63 --backend ddgs # 300/jour ; --backend tavily avec une clé
python scripts/m3ag_s4_crawl.py --departement 63 --pilot 10      # puis sans --pilot ; --redo-mort pour re-tenter
python scripts/m3ag_s11_verify.py                                # SMTP, détaché si > 100 domaines
python scripts/m3ag_s13_sirene_etat.py --departement 63          # SIRET encore actif ? (16–18 % sont fermés)
python scripts/m3ag_s8_match.py --departement 63                 # à refaire : crawl et vérification ont changé les entrées
python scripts/m3ag_s9_export.py --departement 63 --version v3
python scripts/m3ag_s12_check.py --departement 63 --version v3 --baseline v2 --strict
python scripts/m3ag_s14_export_teleop.py --departement 63 --version v3 --out-version v2
```

`m3ag_s5_places.py` (Google Places) exige la facturation Google : jamais utilisé en production.

---

## 4. Gîtes et campings (M5)

Particularité : un **score de vivacité**, un point par famille de preuve datée et indépendante
(offre France Travail ≤ 12 mois · BODACC création/modification/dépôt des comptes ≤ 24 mois ·
DATAtourisme mis à jour ≤ 12 mois · classement Atout France ≤ 5 ans · fiche OSM/PJ ·
immatriculation ≤ 24 mois). Exclusions dures : SIRENE fermé, BODACC radiation / liquidation /
fonds cédé. Onglet « Sûr » = téléphone composable · SIRENE actif · aucun drapeau BODACC · ≥ 2 preuves.

```bash
python scripts/m5_s1_acquire.py --departements 63 --fresh
python scripts/m5_s2_transform.py --departements 63
python scripts/m5_s6_osm.py --departement 63
python scripts/m5_s15_bodacc.py --departements 63 --pilot 20     # puis sans --pilot
python scripts/m5_s16_francetravail.py --departements 63          # FT_CLIENT_ID / FT_CLIENT_SECRET
python scripts/m5_s17_datatourisme.py --download
python scripts/m5_s18_atout.py --download
python scripts/m5_s13_sirene_etat.py --departement 63
python scripts/m5_s19_provider.py                                 # témoin : lignes tourisme de la base (lecture seule)
python scripts/m5_s7_pagesjaunes.py --attach --departement 63     # Chrome attaché (§ 9)
python scripts/m5_s8_match.py --departement 63 --report-rejects
python scripts/m5_s9_export.py --departement 63 --version v2
python scripts/m5_s14_export_teleop.py --departement 63 --version v2 --out-version v2
```

Non construits : crawl, vérification SMTP, recherche, gîtes à la ferme, campings municipaux (contact
voulu = le gestionnaire sur place, jamais la mairie).

---

## 5. Éleveurs (M6)

Population : établissements actifs, NAF 01.41Z à 01.50Z. Fichiers sous `exports/eleveurs/`.
Le rapprocheur lit aussi les moissons M7 (mêmes annuaires, mêmes Pages Jaunes).

```bash
python scripts/m6_s1_acquire.py --departements 63 --fresh --active-only
python scripts/m6_s2_transform.py --departements 63
python scripts/m6_s19_supabase.py --departements 63               # témoins de la base (2 SELECT, lecture seule)
python scripts/m6_s20_agencebio.py --departements 63
sh scripts/m6_s7_run_until_done.sh 63                              # Pages Jaunes, détaché (§ 9)
python scripts/m6_s3_search.py --departement 63 --backend ddgs
python scripts/m6_s4_crawl.py --departement 63 --pilot 10          # puis sans --pilot
python scripts/m6_s11_verify.py --departements 63                  # TOUJOURS avec --departements (voir § 13)
python scripts/m6_s8_match.py --departement 63 --report-rejects
python scripts/m6_s9_export.py --departement 63 --version v2
python scripts/m6_s12_check.py --departement 63 --version v2 --baseline v1 --strict
python scripts/m6_s14_export_teleop.py --departement 63 --version v2 --out-version v2
```

---

## 6. Producteurs (M7)

Population : agriculture hors élevage (29 codes NAF), exclusions de périmètre codées dans
`m7_lib.py` (`EXCLUDED_NAF`, `EXCLUDED_RE`). Fichiers sous `exports/producteurs/`.

**Construire un nouveau département (exemple : 15)**

```bash
python scripts/m7_s1_acquire.py --departements 15 --fresh --active-only   # ~5 min
python scripts/m7_s1_acquire.py --departements 15 --report                # compte par NAF
python scripts/m7_s2_transform.py --departements 15                       # -> checkpoints/operateurs_15.csv
# annuaires : --pilot 10 d'abord, lire les lignes, puis complet
python scripts/m7_s5a_acheteralasource.py --departement 15 --pilot 10
python scripts/m7_s5a_acheteralasource.py --departement 15
python scripts/m7_s5b_producteurdirect.py --departement 15                # sitemaps nationaux : instantané
python scripts/m7_s5c_fermeslocales.py                                    # national, une seule fois
python scripts/m7_s5d_joursdemarche.py --departement 15
python scripts/m7_s5f_bonfromager.py --departement 15                     # page région : vérifier le slug
python scripts/m3ag_s10_baf.py --departement 15                           # bienvenue-à-la-ferme
python scripts/m3ag_s6_osm.py --departement 15
# chercher l'annuaire officiel du département (le meilleur de la 63 n'était sur aucune liste) -> nouveau m7_s5x
sh scripts/m7_s7_run_until_done.sh 15                                     # Pages Jaunes, détaché (§ 9)
python scripts/m7_s19_supabase.py --departements 15                       # témoin base, lecture seule
python scripts/m7_s8_match.py --departement 15 --report-rejects
python scripts/m7_s3_search.py --departement 15 --backend ddgs            # 300/jour
python scripts/m7_s4_crawl.py --departement 15 --pilot 10
python scripts/m7_s4_crawl.py --departement 15 --unmatched                # tous les sites détenus, avec et sans SIRET
python scripts/m7_s18_social.py --departements 15 --pilot 20              # APRÈS le crawl : le vivier vient de lui
python scripts/m7_s18_social.py --departements 15
python scripts/m7_s11_verify.py --departements 15
python scripts/m7_s8_match.py --departement 15                            # à refaire : les entrées ont changé
python scripts/m7_s9_export.py --departement 15 --version v1 --with-eleveurs --eleveurs-version v1
python scripts/m7_s12_check.py --departement 15 --version v1 --strict
python scripts/m7_s14_export_teleop.py --departement 15 --version v1 --out-version v1
```

Avant : ajouter `"15": "cantal-15"` à `PJ_DEPT_SLUG` dans `m3ag_s7_pagesjaunes.py` (le slug que Pages
Jaunes utilise dans ses URL). `m7_s5e_denosfermes63.py` (annuaire du Puy-de-Dôme) a besoin du Chrome
attaché : ses pages de liste servent une preuve de travail JavaScript aux scripts.

**Reconstruire une version V(n+1) après de nouvelles preuves**

```bash
python scripts/m7_s11_verify.py --departements 63
python scripts/m6_s8_match.py --departement 63
python scripts/m6_s9_export.py --departement 63 --version v4
python scripts/m6_s12_check.py --departement 63 --version v4 --baseline v3 --strict
python scripts/m7_s8_match.py --departement 63 --report-rejects
python scripts/m7_s9_export.py --departement 63 --version v3 --with-eleveurs --eleveurs-version v4
python scripts/m7_s12_check.py --departement 63 --version v3 --baseline v2 --strict
```

Le livrable par département contient producteurs + éleveurs, une colonne `Sous-segment`, et une
feuille `Sans SIRET` (fiches d'annuaire non rapprochées, passées au filtre de périmètre large).

---

## 7. France entière

Un Excel par département, rangé par région, sous `exports/france_agriculture/<Région>/`, plus
`france_recap.xlsx`. Chaîne = M7 + M6 par département ; ~11 min par département et par worker
(`per_page` plafonné à 25 par l'API du registre : le seul levier est le parallélisme).

```bash
python scripts/france_lib.py --selftest                    # 96 départements, 13 régions, Corse
python scripts/france_lib.py --list
python scripts/france_run.py --departements 15,32,2B       # un pilote
python scripts/france_run.py --status                      # où en est le run
python scripts/france_run.py --recap                       # reconstruit le récapitulatif
python scripts/france_verify.py --strict                   # relit TOUS les fichiers livrés
```

Run complet, **détaché**, avec des listes de départements **disjointes** (les workers partagent les
dossiers de checkpoints : deux workers sur un même département se disputent le même JSONL) :

```
scripts/france_worker1.sh … france_worker4.sh     les départements, par région
scripts/france_agencebio.sh                       Agence Bio, national
scripts/france_pd_harvest.sh                      producteur.direct, national
scripts/france_finalize.sh                        PAS OPTIONNEL — à lancer quand tout le reste est fini
```

`france_finalize.sh` n'est pas optionnel : les deux sources nationales arrivent dans UN fichier
partagé pendant que les workers construisent déjà ; tout département construit avant l'arrivée de sa
source part sans elle — et l'Agence Bio est au rang 1 pour le téléphone ET l'e-mail. La passe finale
re-rapproche, ré-exporte et re-contrôle tout, sans réseau.

`python scripts/france_review_excluded.py` consolide tous les `principle_excluded_*.csv` en un seul
fichier trié pour relecture ; un faux positif se sauve en copiant son SIRET dans
`config/principle_rescue.csv`, puis en reconstruisant ce département.

Une cellule (département × NAF) qui touche le plafond de 10 000 résultats est journalisée dans
`capped_cells.csv` et le run continue.

---

## 8. Finir une passe d'enrichissement sur une région

Exemple réel : la passe e-mails sur PACA (04 · 05 · 06 · 13 · 83 · 84). État au 2026-09-20 :
**13, 83, 84 reconstruits en v2 et contrôlés ; 04, 05, 06 restent à faire ; Pages Jaunes et le
balayage de recherche n'ont pas tourné.** Dans l'ordre :

```bash
# 1. rapprochement après les moissons, par département restant
python scripts/m6_s8_match.py --departement 04
python scripts/m7_s8_match.py --departement 04
# 2. crawl de tous les sites détenus — détaché, listes DISJOINTES par worker (rendement mesuré ~30 %)
sh scripts/paca_crawl.sh w1 04 05
sh scripts/paca_crawl.sh w2 06
# 3. pages Facebook publiques — À REFAIRE après chaque passe de crawl (80 % mesuré ; vivier = le crawl)
sh scripts/paca_social.sh
# 4. vérification SMTP — JAMAIS sans le drapeau (sinon ~28 000 adresses nationales)
python scripts/m7_s11_verify.py --departements Provence-Alpes-Cote-d-Azur
python scripts/m6_s11_verify.py --departements Provence-Alpes-Cote-d-Azur
# 5. reconstruction + contrôles stricts contre la version précédente, sans réseau
sh scripts/paca_rebuild.sh v2 v1 04 05 06
# 6. publication de ce qui a passé « 0 failed », puis contrôle national et récapitulatif
python scripts/france_publish.py --departements 04,05,06 --version v2 --dry-run
python scripts/france_publish.py --departements 04,05,06 --version v2
python scripts/france_verify.py --strict
python scripts/france_run.py --recap
# 7. téléphones : Pages Jaunes par Chrome attaché (§ 9), puis refaire 1, 5 et 6 en v3
sh scripts/paca_pj.sh
# 8. balayage de recherche avec des clés neuves, moteurs sur des départements DISJOINTS
python scripts/m2lib_search.py --reset-pool serper
python scripts/m7_s3_search.py --departement 13 --backend tavily --targets emailless --priority
python scripts/m7_s3_search.py --departement 84 --backend ddgs --targets emailless --priority
```

Autres lanceurs : `sh scripts/paca_baf.sh 84 04 05 83 13` (bienvenue-à-la-ferme, l'un après l'autre :
fichier partagé) · `sh scripts/paca_run.sh <journal> <script.py> <dept> [<dept>…]` (lanceur générique
pour une étape qui écrit dans UN fichier partagé) · `python scripts/m7_s5g_biopaca06.py` (carte
Agribio 06, API publique, ODbL).

Rendements mesurés, pour décider où mettre le temps : crawl ~30 % des sites détenus → un e-mail ·
Facebook 80 % mais seulement après le crawl · recherche 5–10 % par requête (dernière au rendement,
première à l'échelle) · Pages Jaunes = téléphones, jamais d'e-mails · l'API Google Business Profile
ne lit que les fiches qu'on possède ; lire les autres = Places API (facturation, aucun champ e-mail).

La même séquence vaut pour n'importe quelle région : remplacer la liste des départements, et pour
`--departements` un nom de région tel qu'imprimé par `python scripts/france_lib.py --list`.

---

## 9. Pages Jaunes par Chrome attaché

Le site sert les vrais navigateurs et bloque le reste : les scripts **s'attachent** à un Chrome que
vous avez démarré vous-même, et naviguent par URL.

1. Fermer Chrome complètement. Puis (Win+R) :
   `chrome --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\pj_cdp_profile"`
2. Ouvrir https://www.pagesjaunes.fr dans cette fenêtre, la laisser ouverte.
3. Pilote au premier plan :
   `python scripts/m7_s7_pagesjaunes.py --attach --departement 63 --what travaux-agricoles --dump-html`
4. Run complet, détaché : `scripts/m7_s7_overnight.sh` (producteurs), `scripts/m6_s7_overnight.sh`
   (éleveurs), `scripts/m3ag_s7_run_until_done.sh 03` (bio), `scripts/paca_pj.sh` (une région).
5. Suivre : `tail -3 exports/producteurs/checkpoints/pj_run_63.log`. Sur `CHALLENGE DETECTED`,
   résoudre le défi dans la fenêtre Chrome : le run reprend. Le lanceur s'arrête après deux passes
   sans progrès. Reprise : `pj_done.txt` (≤ 1 page perdue).
6. Repli au niveau commune pour un métier qui annonce plus de 400 fiches :
   `sh scripts/m7_s7_run_until_done.sh 63 pepinieristes --no-dept-level`
7. Ensuite : `m7_s8_match` et `m6_s8_match` sur les départements concernés.

~30 min de Chrome par département. Un métier inconnu n'est pas une 404 mais une recherche en texte
libre : le filtre `PJ_CATEGORY_OK_RE` (dans `m7_s8` / `m6_s8`) écarte les fiches hors sujet.
`m2_s23_google_panel.py` et `m7_s5e_denosfermes63.py` utilisent le même Chrome attaché.
`m2_s18` / `m7_s18` (Facebook) lancent au contraire **leur propre navigateur vide** : aucun compte
personnel n'est jamais impliqué.

---

## 10. Feuilles d'appels et registre « jamais deux fois »

Les `*_s14_export_teleop.py` produisent une feuille réduite (téléphone d'abord, e-mail ensuite,
colonnes en français, onglets **Sûr** / **Probable**). Chaque feuille **exclut tout ce qui a déjà
été remis** : `scripts/maha_lib.py` tient le registre, les copies sont dans `exports/maha_sent/`
(contrôle T16).

```bash
# éleveurs d'abord, puis producteurs en excluant la feuille éleveurs du jour
python scripts/m6_s14_export_teleop.py --departement 63 --version v4 --out-version v4 --also-exclude exports/producteurs/producteurs_63_teleop_v2.xlsx
python scripts/m7_s14_export_teleop.py --departement 63 --version v3 --out-version v3 --also-exclude exports/eleveurs/eleveurs_63_teleop_v4.xlsx
# dès que la réception est confirmée, enregistrer chaque fichier envoyé
python scripts/maha_lib.py --register exports/eleveurs/eleveurs_63_teleop_v4.xlsx
```

Sans le dossier `exports/maha_sent/` d'origine, la prochaine feuille répète des lignes déjà appelées.
`maha_lib` recrée `docs/maha_deliveries.md` (le journal versionné) au premier enregistrement.

---

## 11. Ajouter un département ou un secteur

**Un département** (une heure de travail + le temps de moisson) :
1. l'ajouter à `mN_lib.DEPARTEMENTS` et à `m3ag_s7_pagesjaunes.PJ_DEPT_SLUG` ;
2. `mN_s1_acquire --departements <d> --fresh` puis `--report`, `mN_s2_transform` ; lire le compte par NAF ;
3. chaque annuaire en `--pilot 10`, lire les dix lignes, puis complet (les moissons nationales ne se refont pas) ;
4. **chercher l'annuaire officiel du département** (conseil départemental, chambre d'agriculture,
   office de tourisme). S'il imprime téléphone / e-mail / SIRET : cloner `m7_s5d_joursdemarche.py`
   (le plus petit), ajouter le libellé à `PHONE_SOURCE_FR`, le fichier à `mN_s8.SOURCES`, l'hôte à
   `m3ag_lib.AGRI_AGGREGATORS` ;
5. Pages Jaunes (§ 9), témoin base (`mN_s19`), puis rapprochement → recherche → crawl → vérification
   → export → contrôle → feuille d'appels (§ 6) ; reconstruire aussi le secteur voisin (éleveurs ↔ producteurs).

**Un secteur** (un à deux jours) : copier M7 et changer le périmètre.
1. Décider la population : codes NAF, départements, exclusions ; compter avant de moissonner (`--report`).
2. `mX_lib.py` (copie de `m7_lib.py`) : `SECTOR`, `DEPARTEMENTS`, `NAF_SCOPE`, `EXCLUDED_NAF`, les mots
   vides du secteur, `CATEGORY_RULES`, `PHONE_SOURCE_FR`, `--selftest` avec de vrais cas.
3. `mX_s1_acquire`, `mX_s2_transform` : copies ; seuls l'import de la lib et le garde d'activité changent.
4. Un moissonneur par source. Mesurer d'abord dans un navigateur : index paginé par URL ? fiche rendue
   côté serveur (`requests`) ou par JavaScript (Playwright) ? un sitemap ? que dit `robots.txt` ?
5. `mX_s8_match` (copie ; éditer `SOURCES`), `mX_s3_search` / `mX_s4_crawl` / `mX_s11_verify`
   (enveloppes de 3 lignes avec le `score_fn` du secteur), `mX_s9_export`, `mX_s12_check`, `mX_s14_export_teleop`.
6. Côté base, plus tard : `SECTORS["<slug>"]` avec `source_sectors`, une spec d'ingestion, un chargeur
   copié de `m4_s2_load_agriculteurs.py`, `check_data_quality.py --sector <slug>`.

**Une nouvelle source en cours de route** : mesurer → pilote 10 → moissonneur → `SOURCES` +
`PHONE_SOURCE_FR` + hôte agrégateur → `s8` → lire la ligne d'accord dans le journal de `s9` → seulement
alors décider son rang (≥ ~80 % d'accord avec les sources mesurées = composable ; en dessous = témoin).

**Une règle change** : la coder dans la lib ou l'export, ajouter un contrôle qui **échoue sur l'ancien
fichier** (c'est ce qui prouve le contrôle), reconstruire V(n+1) avec `--baseline v(n)`.

---

## 12. Arrêter, surveiller

```bash
tail -3 logs/<nom>.log                          # chaque script long journalise [n/total]
python scripts/m2lib_search.py --quota          # compteurs de recherche
ps -W | grep python.exe                         # (Git Bash) ce qui tourne encore
taskkill //PID <pid> //F                        # arrêter un processus
```

PowerShell : `Get-Process python | Stop-Process -Force` (un lanceur « run until done » relance sa
moisson après 90 s : arrêter d'abord le `sh.exe`). Les fichiers « fait » rendent le redémarrage gratuit.

---

## 13. Incidents et règles

Chaque ligne est un incident réel de ce projet et la règle (souvent un contrôle) qu'il est devenu.

| Incident | Règle |
|---|---|
| Une faute dans le nom d'une colonne mappée a écrit NULL sur 6 523 lignes, sans erreur | contrat de colonnes à l'ingestion ; contrôle « colonnes mappées non vides » |
| `DISTINCT ON` sans départage total : le même export donnait des octets différents | départage final unique garanti ; `business_id` unique dans la vue |
| Un identifiant faux importait les données d'une autre entreprise | `SIREN = left(SIRET, 9)` contrôlé ; un SIREN retourné est comparé avant écriture |
| Dédoublonnage sur un numéro surtaxé partagé : faux taux de 12,5 % | taux de doublons attendu dans une bande 3–10 % ; garde « même téléphone » sur le passage flou (une enseigne nationale a 15 magasins, 15 téléphones) |
| 685 domaines sans point (`gmailcom`) et 91 domaines soudés (`orange.frnadoo.fr`) — certains étaient des typosquats avec MX attrape-tout : le courrier était **livré à un inconnu** | réparation déterministe à l'ingestion ; contrôles « forme de l'e-mail » et « pas de domaine soudé » |
| Une adresse `invalid` partait quand même (le choix du meilleur e-mail préférait une valide sans exclure une invalide) | migration 015 ; contrôle « aucun `invalid` n'est exporté » |
| Des délais DNS écrits comme `invalid` : 10 478 bonnes adresses marquées mortes | verdict à trois états ; garde qui abandonne sans écrire si > 20 % des domaines paraissent morts. Le pilote de 400 adresses ne l'avait pas vu : le défaut vivait dans l'échelle |
| Une connexion tenue ouverte 3 h 30 pendant une moisson : 3 402 fiches perdues (troisième fois) | connexion neuve par écriture, vidage à chaque page, journaliser `written=` |
| 3 318 `UPDATE` séquentiels : connexion tuée, verrous laissés par une session `idle in transaction` | écritures ensemblistes ; regarder `pg_stat_activity` avant d'accuser une requête |
| Qualifier le secteur B re-classait tout le secteur A (le sélecteur est `rule_version IS DISTINCT FROM`) | portée `source_sectors` obligatoire ; contrôle `rule_version` uniforme par secteur |
| Un fichier réparé deux fois à la main, faux les deux fois | recharger depuis `raw` (`m4_unload_file.py`), jamais de correctif en place |
| `departement=13` renvoyait des sièges du 62 et du 83 ; 5,6 % des sites étaient des holdings | lignes construites depuis `matching_etablissements` + garde d'activité |
| `dirigeants[0]` était souvent le commissaire aux comptes | classement gérant > président > exploitant > DG ; un *Liquidateur* est un signal de fermeture indépendant |
| Une liste de magasins de franchise : 94 e-mails d'autres départements sur un seul SIRET | > 12 adresses sur une page = liste de magasins |
| Un domaine de réseau rapproché de trois boulangeries | domaine réclamé par > 1 SIRET = `reseau`, confiance `faible` |
| `u003emarius@…` : un `>` échappé en JSON soudé à l'adresse | nettoyage des artefacts d'échappement |
| 320 adresses de fournisseurs, annuaires et juristes ramassées sur les sites des boulangeries | `is_third_party_email` : même domaine que le site, ou boîte grand public |
| `04 42 56 68 46` sur les pages de 19 entreprises | garde standard : numéro réclamé par > 2 SIREN = retiré |
| Suites de 10 chiffres dans du JavaScript minifié prises pour des téléphones | un téléphone doit être *annoncé* (`tel:` ou mot-clé à < 60 caractères) |
| Des fiches de réseaux sociaux enregistrées comme « site web » ; une vidéo d'actualité livrée comme la page de 43 boulangeries | une page sociale n'est pas un site ; liens profonds rejetés ; garde « social partagé » |
| Le pilote Google affichait 50 % en lisant les extraits d'annuaires de la page de résultats | lecture limitée au panneau ; source mesurée à 79 % → témoin, pas composable seule |
| Une requête Maps par nom a renvoyé un magasin d'électronique | les résultats nommés passent par le rapprocheur comme les autres |
| Sites livrés sans jamais avoir été vérifiés ; 132 domaines sur plusieurs SIRET ; un dictionnaire et cinq mairies comme « site de boulangerie » | verdict par (SIRET, domaine), colonne `Site confiance`, contrôles H19–H22 |
| Adresse générée sans test de propriété : la boîte d'une mairie ; l'adresse du siège d'une franchise sur neuf SIREN | contrôles H25 (propriété) et H26 (boîte partagée) |
| Pages Jaunes déclarée « fermée » pendant des semaines : vrai seulement d'un navigateur *lancé* ; puis le détecteur a vu le mot `captcha` dans une feuille de style | une mesure sur un montage n'est pas un verdict sur le but ; les cartes de résultats priment sur tout marqueur de blocage |
| Taper dans le formulaire de Pages Jaunes relançait silencieusement la ville précédente | navigation par URL, slug de localité calculé |
| Un slug de métier inconnu = recherche en texte libre : un garage inscrit comme « arboriculteur » | `PJ_CATEGORY_OK_RE`, `NON_PRODUCER_RE`, contrôle H17 |
| La Corse sortait **vide**, en silence (`cp.startswith("2A")` est toujours faux) | `france_lib.in_dept` partout ; sans effet ailleurs (la 63 se réécrit à l'octet près) |
| Un nom de famille (Vigneron, Brasseur) pris pour un métier exclu | `name_hit_is_surname` : la grammaire tranche ; retraits journalisés, sauvetages dans `config/principle_rescue.csv` |
| Les fichiers partagés sont devenus nationaux : une vérification SMTP sans portée sondait 27 767 adresses | `--departements` obligatoire sur `m6_s11` / `m7_s11` |
| Un annuaire servait 20 fiches par département sans pagination | le moissonneur parcourt les pages communes ; chaque ligne est étiquetée par SON code postal |
| Une fiche d'annuaire non rapprochée, sans catégorie, cachait des domaines viticoles | la fiche porte ses productions ; elle n'atteint l'onglet Sans SIRET qu'avec ce texte, passé au filtre large |
| **Une source d'e-mail jamais classée échappait au test de propriété** (la liste des sources faibles était écrite à la main) : trois adresses sur le mauvais établissement | `needs_name(src) = source faible OU absente d'EMAIL_RANK` — *inconnu veut dire faible* ; contrôle H18, prouvé en échouant sur l'ancien fichier. Un fichier construit avant ce correctif se corrige par une reconstruction |
| PowerShell 5.1 `-Encoding utf8` écrit un BOM : un `.sh` détaché sort en silence, sans journal | écrire les lanceurs sans BOM |
| Excel français coupe le CSV sur `;`, écrit un SIRET `4,47E+13`, perd le zéro d'un code postal | le format client est `xlsx`, SIRET et code postal en texte ; caractères de contrôle retirés avant openpyxl |
