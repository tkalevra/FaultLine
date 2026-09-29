# FaultLine — supporto italiano (ramo `it`)

> **Versione sperimentale e non ufficiale.** Il ramo `it` è in sviluppo, fornito "così com'è",
> senza garanzie. Per la versione stabile (inglese) usa `main`.

## Come funziona in italiano
- Il parser spaCy è `it_core_news_sm`; il database usa la collazione ICU `it-IT-x-icu`
  (fissata all'`initdb`, immutabile dopo).
- Le parole chiuse italiane (possessivi, verbo di denominazione, parentela, ruoli sociali, unità,
  aggettivi di dimensione) stanno nel **database** (migrazione 282), non nel codice, e crescono per
  tenant. Il resto viene dalle etichette UD e dalla morfologia.

### Cosa cattura il motore deterministico (verificato da `tests/test_it_engine_uplift.py`)
- **Denominazione riflessiva** (`chiamarsi`): `mia figlia si chiama Anna` → `(anna, child_of, user)`;
  `il mio cane si chiama Fido` → `(cane, pref_name, fido)` come etichetta preferita;
  `mi chiamo Marco` → `(user, also_known_as, marco)`; `ho una figlia che si chiama Anna` →
  `(figlia, also_known_as, anna)`. Senza il clitico riflessivo (`mia madre chiama Anna`) il verbo
  significa "chiamare qualcuno" e non è una denominazione. Negazione (`non si chiama`) → niente.
- **Possessivo di prima persona**: `it_core_news_sm` non annota `Person=1` su mio/mia/nostro; un
  componente della pipeline lo ripristina dai possessivi della classe `first_person_possessive`,
  così `mia sorella` → `(sorella, sibling_of, user)`, `il mio amico` → `friend_of`, e la domanda
  `Chi è mia figlia?` si ancora all'utente e cerca `parent_of`.
- **Misure**: `la corda è lunga 4 metri` → `length`; `è alta 120 centimetri` → `height`;
  `il film dura 2 ore` → `duration`; `pesa 80 chili` → `weight`; `ho 34 anni` → `age`.
- **Guardia della gerarchia (HARD LINE)**: WordNet è inglese ed è disattivato in italiano; quando non
  ha risposta, la guardia ammette un gradino solo se il nodo è già un tipo nell'ontologia cresciuta
  (ha un proprio `subclass_of` o qualcosa è classificato sotto di lui) e non è tipizzato come
  Persona/Organizzazione/Luogo. Nessuna lettura POS del token isolato: il tagger chiamava NOUN anche
  `marco` o `iphone`. Conseguenza: un nome comune nuovo aspetta che l'ontologia lo collochi.
- **Denominazione — riferimento**: `pref_name` solo con possessivo di prima persona. `Il cane si
  chiama Rex` (definito, senza possessivo) non assegna il nome: senza un modello del referente non si
  sa di chi è il cane. `Mi chiama Luca` (Luca mi chiama) non è una denominazione.
- **Misure — limiti**: un'unità con complemento `di` (`10 anni di esperienza`) non è una misura del
  soggetto; il verbo/aggettivo nomina la dimensione solo con un'unità compatibile (`dura 3 anni` →
  durata, `è lungo 2 ore` → niente); passato/imperfetto e subordinate (`Quando avevo 20 anni`) non
  producono l'età attuale.

### Ancora inerte / disattivato di proposito (nessuna euristica sbagliata)
- `kinship_gender` non è seminato: il lemmatizzatore unisce `nonna`→`nonno`, `figlie`→`figlio`.
- Le catene che leggono struttura Penn (`prep`→`pobj`, `auxpass`, standard `than`): trasferimento,
  participio locativo, nascita, implicativi, esemplificazione. `Vivo a Roma` non produce residenza
  (il modello `sm` tagga `Vivo` in modo errato); `Ho tre figli` non produce il conteggio.
- `il mio nome è X` (frame copulativo) e il conteggio restano sul percorso LLM.
- I voti di negazione della correzione (`analyze_negation_scopes`) restano solo inglesi.
- `compagno/compagna` (partner o compagno) non è seminato: ambiguo.

Prompt e testi MCP restano in inglese (convenzione dei rami di lingua).
