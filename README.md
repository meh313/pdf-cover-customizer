# PDF cover customizer

Script Python qui personnalise **uniquement la premiere page** d'une plaquette
Infoscribe « Nos references clients » / « Our references — client testimonials ».

Il ajoute :

- le **nom du client**, aligne a droite juste sous le titre, dans le bandeau noir ;
- la **date d'execution** (ou toute date personnalisee), en bas a gauche ;
- la **grande annee** dans le losange noir, remplacee par l'annee demandee.

Le reste du document (pages 2 a N, photos, logos, formes) est recopie tel quel,
et le fichier source n'est jamais modifie.

![Couverture generee](docs/preview-cover.png)

![Detail du bandeau](docs/preview-client.png)

---

## Installation

```bash
git clone https://github.com/meh313/pdf-cover-customizer.git
cd pdf-cover-customizer
python -m venv .venv
```

Windows :

```bash
.venv\Scripts\activate
```

Linux / macOS :

```bash
source .venv/bin/activate
```

Puis :

```bash
pip install -r requirements.txt
```

Seule dependance : [PyMuPDF](https://pymupdf.readthedocs.io/). Python 3.9 ou plus.

## Utilisation

Cas le plus simple — nom du client, date du jour, annee deduite de la date :

```bash
python customize_cover.py "2026 ref clients infoscribe.ai.pdf" --client "ArcelorMittal"
```

Le fichier produit est ecrit a cote de l'original :
`2026 ref clients infoscribe.ai - ArcelorMittal.pdf`

Avec une date et une annee explicites, et un chemin de sortie choisi :

```bash
python customize_cover.py brochure.pdf -c "Valeo" -d 30/09/2026 -y 2027 -o out/valeo.pdf
```

Date entierement personnalisee (le texte non reconnu comme une date est imprime
tel quel) :

```bash
python customize_cover.py brochure.pdf -c "Safran Tech" -d "Paris, le 30 septembre 2026"
```

Verifier le rendu sans ouvrir un lecteur PDF :

```bash
python customize_cover.py brochure.pdf -c "Doctolib" --preview page1.png --verbose
```

## Entrees du script

Arguments obligatoires :

- `pdf` — chemin du fichier PDF a modifier ;
- `-c`, `--client` — nom du client imprime sous le titre.

Arguments optionnels :

- `-d`, `--date` — date a afficher. Par defaut : la date du jour. Les formats
  `2026-08-18`, `18/08/2026`, `18-08-2026`, `18.08.2026`, `2026/08/18`,
  `08/18/2026`, `18 August 2026` et `18 Aug 2026` sont reconnus et reformates
  avec `--date-format`. Tout autre texte est imprime tel quel.
- `--date-format` — format `strftime` applique aux dates reconnues.
  Par defaut `%d/%m/%Y`.
- `--date-prefix` — texte place avant la date, par exemple `"Le "`.
- `-y`, `--year` — annee affichee dans le losange. Par defaut : l'annee de la
  date resolue ci-dessus.
- `--keep-year` — ne pas toucher a l'annee de la couverture.
- `-o`, `--output` — chemin du PDF genere.
  Par defaut `"<nom du fichier> - <client>.pdf"`, dans le meme dossier.
- `-f`, `--force` — ecraser le fichier de sortie s'il existe deja.
- `--client-size` — taille de police du nom du client, en points.
  Par defaut : calculee automatiquement pour remplir l'espace disponible.
- `--client-regular` — nom du client en graisse normale plutot qu'en gras.
- `--client-color` — couleur du nom du client, `#F1F1F1` ou `241,241,241`.
  Par defaut : la couleur du titre existant.
- `--date-size`, `--date-bold`, `--date-color` — equivalents pour la date.
  La couleur par defaut s'adapte au fond (bleu Infoscribe sur fond clair).
- `--preview` — genere en plus un PNG de la premiere page pour verification.
- `-v`, `--verbose` — affiche la geometrie detectee.

Le script renvoie le code `0` en cas de succes et `1` avec un message sur
`stderr` en cas d'erreur.

## Utilisation comme module

```python
from customize_cover import customize_cover

path = customize_cover(
    "2026 ref clients infoscribe.ai.pdf",
    client="ArcelorMittal",
    date="30/09/2026",
    year=2027,
    output_path="out/arcelormittal.pdf",
)
print(path)
```

Exemple de traitement en lot :

```python
from pathlib import Path
from customize_cover import customize_cover

clients = ["ArcelorMittal", "Valeo", "Safran Tech", "Doctolib"]
for client in clients:
    customize_cover(
        "2026 ref clients infoscribe.ai.pdf",
        client=client,
        date="30/09/2026",
        output_path=Path("out") / f"{client}.pdf",
        overwrite=True,
    )
```

## Comment ca marche

La geometrie n'est pas codee en dur : elle est relue sur la page a chaque
execution, ce qui permet au script de continuer a fonctionner si le gabarit
bouge un peu, et de traiter indifferemment la version FR et la version EN.

1. **Bandeau du titre** — la plus petite forme pleine de couleur sombre qui
   contient le texte du titre.
2. **Bas du titre** — la deuxieme ligne du titre est une image matricielle dans
   ce gabarit, son bbox ne donne donc pas la vraie hauteur des lettres. Le
   bandeau est rendu en memoire et on cherche la derniere ligne de pixels
   clairs : cela traite de la meme facon la ligne texte et la ligne image.
3. **Nom du client** — aligne a droite sur le bord droit du titre, centre
   verticalement dans l'espace libre entre le bas du titre et le bas du
   bandeau. La taille est calculee a partir de cet espace, puis reduite tant
   que le nom depasse la largeur disponible.
4. **Annee** — le plus grand bloc de texte de la page qui ressemble a une annee.
   Les anciens caracteres sont **supprimes** du flux de contenu (redaction), pas
   simplement recouverts : l'ancienne annee ne ressort donc pas dans une
   extraction de texte. La nouvelle annee est redessinee chiffre par chiffre sur
   la grille d'avance d'origine, ce qui reproduit exactement l'interlettrage
   serre du gabarit (`Tc = -1.92`).
5. **Date** — en bas a gauche, avec une encre choisie selon la luminosite du
   fond a cet endroit.

Les polices Arial du gabarit ne sont pas embarquees dans le PDF ; le script
utilise donc les polices base-14 Helvetica / Helvetica-Bold, metriquement
identiques a Arial, sans avoir besoin d'un fichier de police.

## Limites connues

- Le script est prevu pour ce gabarit de couverture (bandeau sombre + losange
  avec l'annee). Sur un PDF sans ce bandeau il s'arrete avec
  `no cover title found on page 1`.
- Relancer le script **sur un PDF deja personnalise** est refuse
  (`this PDF looks like it was already stamped`) : il faut toujours repartir du
  fichier d'origine.
- Les PDF chiffres doivent etre dechiffres au prealable.
- Le texte est encode en Latin-1 (jeu de caracteres des polices base-14), ce qui
  couvre le francais et l'anglais.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

Les tests s'executent sur les deux plaquettes reelles de `samples/` et
verifient, entre autres, que les pages 2 a N sont rendues au pixel pres a
l'identique, que les images de la page 1 restent octet pour octet identiques,
que l'ancienne annee disparait bien de l'extraction de texte et que la nouvelle
se pose exactement au meme endroit.

---

# English

Python script that customizes **only the first page** of an Infoscribe
references / client testimonials brochure. It writes the client name under the
cover title, the run date (or any custom date) at the bottom left, and replaces
the large year in the black diamond. Pages 2 to N are copied through untouched
and the source file is never modified.

```bash
pip install -r requirements.txt
python customize_cover.py brochure.pdf --client "ArcelorMittal"
python customize_cover.py brochure.pdf -c "Valeo" -d 30/09/2026 -y 2027 -o out/valeo.pdf
python customize_cover.py --help
```

Inputs are the PDF path, the client name, and an optional date (parameterizable:
a recognised date is reformatted with `--date-format`, anything else is printed
verbatim). The year defaults to the year of that date and can be forced with
`--year`. See the French section above for the full option list and for how the
cover geometry is detected.
