# RÈGLES ABSOLUES — À LIRE AVANT TOUT CODE

## ARCHITECTURE
- Ne jamais modifier app.py directement
- Toujours passer par patches/ + merge_patches.py

- Ne jamais toucher schema.sql — migrations dans init_db() uniquement
- Ne jamais réécrire une fonction existante, seulement l'étendre

## PATCHES
- Créer patches/nom_feature.py avec uniquement les ajouts
- Lancer : python merge_patches.py patches/nom_feature.py
- Backup automatique dans patches/_backups/ avant merge
- Un patch = une feature cohérente, jamais un patch fourre-tout

## MIGRATIONS — RÈGLES PRÉCISES
- Insérer APRÈS la dernière entrée existante, AVANT le ] fermant
- La dernière migration existante est : CREATE TABLE IF NOT EXISTS meuble_zones
- Toujours CREATE TABLE IF NOT EXISTS (idempotent)
- ALTER TABLE sans CHECK() — SQLite ne supporte pas ADD COLUMN WITH CHECK
- Vérification après migration :
    sqlite3 /app/data/mobilier.db "PRAGMA foreign_key_check"
    → Aucune sortie = OK
    
## FONCTIONS INTOUCHABLES
- `_calc_prix_matieres_projet()` — signature immuable
- `generer_pieces_meuble()` — ne pas modifier
- Le canvas 4-view splitter de ensemble_assembler.html — jamais touché

## BASE DE DONNÉES
- Toutes les valeurs viennent de `parametres_globaux`, jamais hardcodées
- Migrations : ajout uniquement à la liste `migrations = [...]` dans init_db()
- Ordre d'insertion des migrations est critique

## DÉPLOIEMENT
- DB Railway : /app/data/mobilier.db (volume persistant)
- DB locale : C:\Mobilier\00_AqGK\mobilier.db
- Push git = redéploiement Railway automatique

## VÉRIFICATIONS OBLIGATOIRES AVANT TOUT PUSH
1. python -c "import app; print('OK')"
2. grep -n "def <nouvelle_route>" app.py | wc -l  → doit être 1
3. Vérifier que les migrations n'ont pas de doublons
4. Vérifier que app.py se lance sans erreur

## STYLE & TEMPLATES
- Inter font, #0f1f35 primary — charte V5
- Toutes les valeurs texte UI viennent de parametres_globaux (nom_societe, etc.)
- Inclure _sidebar_projet.html via {% include %}