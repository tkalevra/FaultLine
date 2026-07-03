# FaultLine — supporto in italiano (ramo `it`)

> ## ⚠️ AVVISO — VERSIONE SPERIMENTALE E NON UFFICIALE
>
> Questo ramo (**`it`**) è una versione **sperimentale** e **in fase di sviluppo** del supporto in lingua
> italiana di FaultLine. **NON è una release ufficiale né pronta per la produzione.** È fornita
> **"così com'è"** (*as is*), **senza alcuna garanzia** di funzionamento, correttezza o continuità.
>
> - Le funzionalità possono essere **incomplete**, cambiare senza preavviso o **non funzionare**.
> - **Non è stata ancora convalidata** end-to-end su dati reali in italiano.
> - L'estrazione **deterministica** (motore *spine*) è disponibile **solo in inglese**; in italiano
>   l'estrazione si affida al percorso basato su **LLM** (*rewrite*), quindi è **meno prevedibile**.
> - **Usare a proprio rischio.** Per la versione **stabile** (in inglese) usare il ramo `master` / `main`.
>
> *(Experimental, unofficial Italian branch — not production-ready, provided as-is with no warranty. Not
> yet validated on real Italian data. Deterministic extraction is English-only; Italian rides the LLM
> rewrite lane and is less predictable. Use at your own risk; for the stable English version use
> `master`/`main`.)*

---

## Che cos'è
FaultLine è una **memoria a grafo della conoscenza** per LLM, per tenant, validata in scrittura: estrae
entità e relazioni dai messaggi dell'utente e le conserva in modo strutturato. Il **cuore della memoria è
indipendente dalla lingua** (identificatori, grafo, valori, date): un fatto memorizzato non è "in inglese".

## Come funziona in italiano (su questo ramo)
- L'**estrazione** passa dal percorso **LLM** (*rewrite*), che comprende l'italiano in modo nativo.
- Il typing delle entità usa un modello **GLiNER multilingue** (`gliner_multi`).
- Le date sono gestite da `dateparser`, che supporta l'italiano.
- Il **motore deterministico** (*spine*) resta in inglese: costruzioni come *"I am 34 years old"* e
  *"ho 34 anni"* hanno una sintassi diversa, quindi le regole inglesi non si applicano all'italiano.

## Stato e dettagli tecnici
Questo ramo `it` è **sperimentale**: l'estrazione in italiano si affida al percorso LLM (*rewrite*),
mentre il motore deterministico (*spine*) resta in inglese. Le funzionalità possono essere incomplete o
cambiare. Per contribuire o segnalare problemi, tenere presente l'avviso qui sopra.
