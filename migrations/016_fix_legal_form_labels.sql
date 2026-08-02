-- 016_fix_legal_form_labels.sql
-- =============================================================================
-- The client-facing "Forme juridique" column was wrong for 534 businesses and
-- blank-looking (a raw INSEE code) for 10,340 more.
--
-- FOUND 2026-08-02 while closing out the open items in CLAUDE.md. Sam's ask #2
-- was "add activity data for personalization" — so this column is read by a
-- human writing to a farmer, and "6533" or a wrong structure is worse than
-- useless there.
--
-- HOW THE ERRORS WERE PROVEN, not guessed: a French agricultural company is
-- nearly always literally named after its structure ("GAEC DE LA TONNELLERIE",
-- "SCI LES BRUYERES"). Cross-tabbing the stored label against the company name
-- makes each mismatch obvious:
--
--   label "GAEC"           216 rows, only   5 named GAEC   -> 6599 is Société civile
--   label "SARL agricole"  257 rows,      144 named SCI    -> 6540 is SCI
--   label "SARL"            60 rows,       18 named SNC,
--                                            0 named SARL  -> 5202 is SNC
--   label "Groupement agricole"  1 row, "SOCIETE COOPERATIVE CIVILE IMMOBILIERE"
--                                                          -> 6560 is Société civile de moyens
--
-- And the biggest gap: 6533 = GAEC, 8,007 businesses, 6,670 of them literally
-- named "GAEC ...". It was never in the map, so the most common farm structure
-- in France displayed as four digits.
--
-- ⚠ ORDER IS LOAD-BEARING. The wrong labels are corrected FIRST, then the raw
-- codes are translated. Reversed, translating 6533 -> 'GAEC' and then applying
-- 'GAEC' -> 'Société civile' would corrupt all 8,007 rows in one pass.
--
-- The reverse-mapping of the wrong labels is safe because the old map was
-- injective over them: 'GAEC' could only have come from 6599 (6533 was unmapped
-- and stored as the literal string '6533'), 'SARL' only from 5202, and so on.
--
-- Codes still absent from the map stay as raw digits ON PURPOSE. An invented
-- label is a silent lie; a visible code is an obvious gap.
--
-- NOTE FOR LATER: staging.companies stores the LABEL, not the INSEE code, so a
-- mapping error is only fixable by this kind of reverse-engineering. Storing
-- nature_juridique alongside would make it a one-line re-derivation. Worth
-- doing before the next sector.
--
-- REVERSAL: none needed — every value written here is more correct than what it
-- replaced. To re-derive from scratch, add a nature_juridique column and re-run
-- m1_s4_sirene_enrich.py.
-- =============================================================================

BEGIN;

-- ---- step 1: correct the four labels that were simply wrong -----------------
UPDATE staging.companies SET legal_form = 'Société civile'
 WHERE legal_form = 'GAEC';                       -- code 6599

UPDATE staging.companies SET legal_form = 'SCI'
 WHERE legal_form = 'SARL agricole';              -- code 6540

UPDATE staging.companies SET legal_form = 'SNC'
 WHERE legal_form = 'SARL';                       -- code 5202

UPDATE staging.companies SET legal_form = 'Société civile de moyens'
 WHERE legal_form = 'Groupement agricole';        -- code 6560

UPDATE staging.companies SET legal_form = 'Association non déclarée'
 WHERE legal_form = 'Association loi 1901';       -- code 9210

-- ---- step 2: translate the raw INSEE codes ----------------------------------
UPDATE staging.companies c
   SET legal_form = m.label
  FROM (VALUES
        ('2210', 'Société créée de fait'),
        ('5410', 'SARL nationale'),
        ('5458', 'SARL coopérative agricole'),
        ('5460', 'SARL coopérative'),
        ('5499', 'SARL'),
        ('5560', 'SA à conseil d''administration'),
        ('5599', 'SA à conseil d''administration'),
        ('5699', 'SA à directoire'),
        ('6220', 'GIE'),
        ('6316', 'CUMA'),
        ('6317', 'Société coopérative agricole'),
        ('6318', 'Union de coopératives agricoles'),
        ('6532', 'Société civile d''attribution'),
        ('6533', 'GAEC'),
        ('6534', 'GFA'),
        ('6536', 'Groupement forestier'),
        ('9220', 'Association déclarée'),
        ('9260', 'Association de droit local')
       ) AS m(code, label)
 WHERE c.legal_form = m.code;

COMMIT;
