# Leads pipeline — prospection B2B à partir de sources ouvertes

Pipeline de données en Python qui construit des fichiers de prospection B2B français **à coût
zéro** : il part du registre public des entreprises (SIRENE), centralise des fichiers Excel
dispersés dans une base PostgreSQL (Supabase), enrichit chaque établissement avec un téléphone, un
e-mail, un site web et un dirigeant trouvés dans des sources ouvertes, puis **refuse d'exporter un
fichier qui n'a pas passé ses contrôles**.

Auteure : **Ines Cherif** — conception, code, mesures et documentation (juillet → septembre 2026).

| Où aller | Pour quoi |
|---|---|
| ce fichier | installer, comprendre l'ensemble, premier test |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | **les commandes exactes**, dans l'ordre, pour chaque pipeline : durée, reprise, contrôle |
| [`CLAUDE.md`](CLAUDE.md) | conventions, règles à ne pas assouplir, état du projet — chargé automatiquement par Claude Code |
| [`docs/project_documentation.md`](docs/project_documentation.md) | la base Supabase : schémas, tables, décisions |
| [`docs/enrichment/`](docs/enrichment/) | les sources de contact, leurs quotas, leurs taux mesurés |
| [`migrations/README.md`](migrations/README.md) | l'index des migrations SQL |

## Ce que contient le dépôt

Deux mondes, volontairement séparés :

**1. La base (Supabase / PostgreSQL)** — `scripts/m1_*`, `scripts/m4_*`, `config/`, `migrations/`.
Ingestion de fichiers Excel fournisseurs → normalisation → déduplication → enrichissement SIRENE →
qualification par secteur → export. Schémas `raw` (lignes brutes immuables), `staging` (tables de
travail), `audit` (journal), `public` (vues en lecture, interrogeables en langage naturel via MCP).

**2. Les pipelines sectoriels, sans base** — `scripts/m2_*` … `m7_*`, `france_*`, `paca_*`.
Ils ne lisent et n'écrivent que des **fichiers**, sous `exports/<secteur>/` et
`exports/<secteur>/checkpoints/`. Aucune connexion ouverte : un arrêt brutal ne perd rien, chaque
script reprend là où il s'est arrêté.

| Famille | Secteur | Export · contrôle |
|---|---|---|
| `m1_*`, `m4_*` | base multi-secteurs (agriculture, boulangerie, imprimerie, viticulture, tourisme, agriculteurs bio) | `m1_s8_export.py` · `check_data_quality.py` |
| `m2_*`, `m2lib_*` | boulangeries d'un département (pilote : 13) | `m2_s14_export_v3.py` · `m2_s19_check_v5.py` |
| `m3ag_*` | agriculteurs bio (Agence Bio) | `m3ag_s9_export.py` · `m3ag_s12_check.py` |
| `m5_*` | gîtes et campings, avec score de « preuves d'activité » | `m5_s9_export.py` · `m5_s14_export_teleop.py` |
| `m6_*` | éleveurs | `m6_s9_export.py` · `m6_s12_check.py` |
| `m7_*` | producteurs (agriculture hors élevage) + annuaires de vente directe | `m7_s9_export.py` · `m7_s12_check.py` |
| `france_*` | les 96 départements de métropole, un Excel par département, rangés par région | `france_run.py` · `france_verify.py` |
| `paca_*` | lanceurs détachés pour une passe d'enrichissement sur une région | `paca_rebuild.sh` |

Tous les pipelines sectoriels suivent la même forme :
**acquérir** (registre) → **transformer** (une ligne par établissement) → **moissonner** (annuaires,
OpenStreetMap, Pages Jaunes, recherche web, crawl des sites, pages Facebook publiques) →
**rapprocher** chaque fiche d'un SIRET (adresse, distance, nom) → **vérifier** les e-mails (DNS +
SMTP) → **exporter** en appliquant un classement des sources **fondé sur des taux mesurés** →
**contrôler** le fichier relu depuis le disque → éventuellement une **feuille d'appels** réduite.

## Prérequis

- **Python 3.11**, Git. Sous Windows : **Git Bash** (les lanceurs `.sh` en ont besoin).
- Pour la base : un projet Supabase (offre gratuite) et sa chaîne de connexion *session pooler*.
- Pour Pages Jaunes et quelques annuaires : **Google Chrome**, lancé à la main avec un port de
  débogage (voir le RUNBOOK, « Pages Jaunes »). Le script s'attache à ce Chrome ; il ne lance rien.

## Installation

```bash
git clone <url-du-depot> && cd <dossier>
python -m venv .venv && source .venv/Scripts/activate      # Linux/macOS : source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env                                        # puis remplir — voir ci-dessous
mkdir -p logs exports
export PYTHONIOENCODING=utf-8
```

`.env`, `exports/`, `logs/` et tous les fichiers de données sont ignorés par git : **aucune donnée
de prospect n'est versionnée**, seulement le code et les règles.

### Comptes à créer (tous gratuits, sans carte bancaire)

Les clés sont personnelles : chacun crée les siennes. Rien n'est obligatoire pour démarrer — sans
clé, la recherche web passe par `ddgs` (≈ 300 requêtes/jour).

| Service | Variable | Offre gratuite | Utilisé par |
|---|---|---|---|
| [Supabase](https://supabase.com) | `SUPABASE_DB_URL` | 1 projet, 500 Mo | tout `m1_*` / `m4_*`, `check_data_quality.py`, et en lecture seule `m5_s19`, `m6_s19`, `m7_s19` |
| [Serper](https://serper.dev) | `SERPER_API_KEY` | 2 500 crédits, une fois | `m2lib_search.py` (Google Maps : seul `/maps` renvoie les téléphones, 3 crédits) |
| [Tavily](https://tavily.com) | `TAVILY_API_KEY` | 1 000 recherches/mois | `m2lib_search.py` |
| [France Travail](https://francetravail.io) | `FT_CLIENT_ID`, `FT_CLIENT_SECRET` | gratuit | `m5_s16_francetravail.py` |
| API Recherche d'entreprises, Agence Bio, Overpass (OSM), BODACC, RDAP AFNIC | — | sans clé | acquisitions et moissons |

Les compteurs de quota sont tenus localement (`checkpoints/search_quota.json`) et **arrêtent le
script au plafond gratuit** : on ne peut pas dépasser par accident.

Deux scripts envoient une adresse de contact dans leur `User-Agent` (`m2_s5_osm.py`,
`m3ag_s1_acquire.py`) : y mettre la vôtre, c'est l'usage attendu par ces API ouvertes.

## Premier test (quelques secondes, sans base, sans réseau)

```bash
python scripts/test_normalisers.py
python scripts/france_lib.py --selftest
python scripts/m2lib_contact.py --selftest
python scripts/m2lib_validate.py --selftest
python scripts/m7_lib.py --selftest
python scripts/m3ag_lib.py --selftest
```

Avec une base configurée :

```bash
python scripts/check_data_quality.py            # contrôles de distribution sur la base
python scripts/check_data_quality.py --strict   # les avertissements échouent aussi
```

Un code de sortie 1 veut dire : **on s'arrête**. Chaque contrôle correspond à un incident réel
(voir « Incidents et règles » dans le RUNBOOK).

## Travailler avec Claude Code

Ouvrir le dossier dans Claude Code : `CLAUDE.md` est chargé tout seul et donne les conventions, les
contrôles à lancer et les règles mesurées à ne pas assouplir. Pour interroger ou modifier la base
en langage naturel, copier `.mcp.json.example` en `.mcp.json`, y mettre la référence du projet, et
définir `SUPABASE_ACCESS_TOKEN` comme variable d'environnement **du shell** (Claude Code ne lit pas
`.env`).

## Principes de conception

- **Idempotent et reprenable.** Chaque script a un fichier « fait » ou une colonne de contrôle ;
  le relancer ne refait que ce qui manque.
- **Mesurer avant de classer.** Une source entre dans la colonne « téléphone » seulement si son
  accord avec des sources déjà fiables a été mesuré (≈ 80 % et plus). En dessous, elle reste un
  *témoin* : elle peut corroborer, jamais être composée seule.
- **Relire le fichier, pas les journaux.** Les contrôles ouvrent le `.xlsx` produit et échouent
  sur un défaut ; un contrôle n'est accepté que s'il **échoue sur l'ancien fichier fautif**.
- **Pilote de 10 ou 20 avant tout passage complet**, et lecture des lignes à la main.
- **Respect des sites** : `robots.txt` lu, débit limité, aucune connexion à un compte, pages
  publiques uniquement.

## Licence et données

Le code est publié sous licence MIT (voir `LICENSE`).


Aucune donnée de prospect n'est dans ce dépôt. Les fichiers produits contiennent des données
d'entreprises issues de sources publiques ; leur usage en prospection relève du RGPD et de la
responsabilité de celui qui les exploite. Les adresses et noms de personnes qui servent d'exemples
dans le code et les tests sont inventés.
