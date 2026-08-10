
# PRM V0 - Property Risk Management

Prototype technique gratuit : une adresse française -> géocodage -> parcelle -> Géorisques -> urbanisme -> feux PRM -> export JSON.

## Ce qui fonctionne dans V0

- Géocodage : service Géoplateforme / BAN
- Parcelle : API Carto IGN / Cadastre
- Risques : API Géorisques (adresse, puis fallback commune)
- Urbanisme : API Carto IGN / GPU
- Carte de localisation
- Feux PRM prudents
- Export JSON

## Ce qui n'est volontairement PAS simulé

- Climat 2030/2050 : à connecter à une source officielle dédiée
- Projets Sitadel proches : étape suivante
- Analyse photo végétation : étape suivante
- PDF client : étape suivante
- Paiement : uniquement après validation de la valeur produit

## Déployer gratuitement sur Streamlit Community Cloud

1. Crée un compte GitHub.
2. Crée un repository, par exemple `prm-v0`.
3. Uploade `app.py` et `requirements.txt`.
4. Crée/connecte ton compte Streamlit Community Cloud avec GitHub.
5. Clique sur `Create app`.
6. Choisis le repository `prm-v0`.
7. Entrypoint : `app.py`.
8. Clique sur `Deploy`.

Tu obtiendras une URL de type `nom-de-ton-app.streamlit.app`.

## Lancer en local (facultatif)

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Sources/API intégrées

- Géocodage : `https://data.geopf.fr/geocodage/search`
- Cadastre : `https://apicarto.ign.fr/api/cadastre/parcelle`
- Urbanisme : `https://apicarto.ign.fr/api/gpu/...`
- Géorisques : `https://georisques.gouv.fr/api/v1/...`

## Règle PRM

V0 est volontairement conservateur :
- présence d'un risque communal != risque prouvé à la maison ;
- aucune donnée = `⚪ à préciser`, jamais vert par défaut ;
- aucune note /100 faussement scientifique ;
- chaque module devra ensuite conserver source, date, portée et confiance.

## Étape suivante

Brancher :
1. Sitadel / autorisations dans un rayon autour de la parcelle ;
2. climat officiel à horizons 2030/2050 ;
3. upload photo + analyse de vulnérabilité végétation ;
4. génération automatique du rapport client.
