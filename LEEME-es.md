# FaultLine — soporte en español (rama `es`)

> ## ⚠️ AVISO — VERSIÓN EXPERIMENTAL Y NO OFICIAL
>
> Esta rama (**`es`**) es una versión **experimental** y **en desarrollo** del soporte en español de
> FaultLine. **NO es una release oficial ni está lista para producción.** Se ofrece **"tal cual"**
> (*as is*), **sin garantía alguna** de funcionamiento, corrección o continuidad.
>
> - Las funcionalidades pueden estar **incompletas**, cambiar sin previo aviso o **no funcionar**.
> - La extracción **determinista** (motor *spine*) está disponible en español: el analizador, la
>   detección de negación, y las **cadenas de dependencias** (posesión/kinship, sentimientos y
>   preferencias, cantidades y medidas, fechas y duraciones, nombres y clasificación) leen el
>   esquema **Universal Dependencies** que el modelo español emite, de la mano del modelo por
>   defecto `es_core_news_md` (verificado por la suite `tests/test_spanish_capture_and_walk.py`).
> - **Úsala bajo tu propia responsabilidad.** Para la versión **estable** (en inglés) usa la rama
>   `master` / `main`.

> ## 🛑 SI YA USASTE ESTA RAMA ANTES DE ESTA CORRECCIÓN, LEE ESTO
>
> Las versiones anteriores de la rama `es` **decían** usar el modelo español de spaCy y un GLiNER
> multilingüe. **No lo hacían**: la configuración por defecto cargaba `en_core_web_sm` (inglés) y
> `gliner2-base-v1` (solo inglés). Consecuencias **medidas** sobre datos reales en español:
>
> - **Las negaciones se perdían.** El esquema de etiquetas inglés (Penn) tiene un arco `neg`; el
>   español usa **Universal Dependencies**, que lo eliminó. Las 44 comprobaciones `dep_ == "neg"`
>   devolvían `False` en español, así que **«No uso el puerto 9004» se almacenaba como afirmación**.
>   Esto no es una laguna, es **corrupción**: el hecho guardado dice lo **contrario** de lo dicho.
> - **Los puertos y atributos no se capturaban.** El analizador inglés interpreta «OpenWebUI usa el
>   puerto 3000» como un único bloque `PROPN` sin verbo, sujeto ni objeto.
> - **Los acentos se destruían** al canonicalizar (`está`→`est`, `años`→`a_os`).
>
> **Los hechos almacenados en español antes de esta corrección no son de fiar.** Recomendación:
> **vuelve a ingerir** el contenido importante. Los slugs antiguos (sin acentos) no coincidirán con
> los nuevos, así que la reingesta también consolida el grafo.
>
> *(Experimental, unofficial Spanish branch — not production-ready, provided as-is with no warranty.
> Not yet validated on real Spanish data. Deterministic extraction is English-only; Spanish rides the
> LLM rewrite lane and is less predictable. Use at your own risk; for the stable English version use
> `master`/`main`.)*

---

## Qué es
FaultLine es una **memoria de grafo de conocimiento** para LLM, por tenant y validada en escritura:
extrae entidades y relaciones de los mensajes del usuario y las conserva de forma estructurada. El
**núcleo de la memoria es independiente del idioma** (identificadores, grafo, valores, fechas): un
hecho almacenado no está "en inglés".

## Cómo funciona en español (en esta rama)
- El analizador de spaCy es **`es_core_news_md`** por defecto (medido: `sm` etiqueta mal los
  verbos en pretérito de 1ª persona al inicio de frase y rompe el parseo de posesivos) — y se
  **verifica al arrancar**: si `SPACY_MODEL` no coincide con `FAULTLINE_LANGUAGE`, la capa se
  **desactiva** y registra `linguistic_layer.model_language_mismatch` en vez de producir datos
  incorrectos en silencio. (Escape: `FAULTLINE_ALLOW_SPACY_LANG_MISMATCH=true`.)
- El tipado de entidades usa **`fastino/gliner2-multi-v1`** (Apache-2.0; en/es/fr/de/it/pt), que se
  **hornea en la imagen** — obligatorio, porque el runtime va con `HF_HUB_OFFLINE=1`.
- La **negación** se detecta con `advmod`/`det` además de `neg`, así que «no», «nunca», «tampoco»,
  «ningún» y «ya no» funcionan. El inglés no cambia.
- `learn_facts` acepta formas en español: *«X es una subclase de Y»*, *«X es una instancia de Y»*,
  *«X es parte de Y»*, y también *«es un tipo de»*, *«es un ejemplo de»*, *«forma parte de»*.
- El **filtro de inyección de prompts** cubre el español (antes se saltaba por completo).
- La **canonicalización respeta los acentos**; ya no fragmenta las palabras.
- Las fechas usan `dateparser` con `languages=["es","en"]` y `DATE_ORDER=DMY` (*3/4/2026* = 3 de
  abril, no 4 de marzo).
- Los pronombres de **primera persona** en español (*yo, mi, mis, nosotros, nuestro*) se resuelven
  al usuario y ya no crean entidades fantasma.
- La **extracción** sigue apoyándose también en la vía **LLM** (*rewrite*), que entiende el español
  de forma nativa.

### Lo que TODAVÍA no está en paridad (residuales honestos)
- Las **cadenas de dependencias** del *spine* leen el esquema **Universal Dependencies** que el modelo
  español emite (posesión/kinship, sentimientos y preferencias, cantidades y medidas, fechas y
  duraciones, nombres y clasificación — verificado por `tests/test_spanish_capture_and_walk.py`),
  así que la captura básica es **equivalente a la inglesa** en esas construcciones. Residuales
  conocidos y documentados: los adjetivos que el modelo etiqueta como NOUN en posición copulativa
  (`Mi coche es rojo`, `Mi coche es el azul`) se dejan **vacíos honestos**, nunca se archivan
  como tipo (una ocupación/persona bajo `ser` — parentesco o nombre — sí se clasifica: `Rex es un
  labrador`, `París es la capital`, `Mi madre es enfermera`). Caso límite del mismo límite: un ser
  nombrado + cualidad nominalizada (`Rex es el negro`) admite instance_of porque la puerta de persona
  no puede separar `un labrador` de `el negro` sin lista de palabras — el inglés emite basura
  equivalente (`(rex, age, one)`), ninguno de los dos es una fabricación limpia; una ocupación etiquetada como ADJ
  (`Mi padre es médico`) o como participio (`Mi hermana es abogada`) cae en `has_state` (sin
  señal gramatical que la separe de un adjetivo descriptivo sin lista de palabras); `Soy ingeniero`
  (pro-drop, sin sujeto) devuelve `[]` en el deriver — paridad con el inglés `I am an engineer` →
  `[]` (la captura real va por el seam de auto-predicación).
- **Verbo en 1ª persona al inicio de frase homónimo de un sustantivo** (`Trabajo en Google`,
  `Corro en el parque`, `Nado en la piscina`, `Como una manzana`): `es_core_news_md` los etiqueta
  como PROPN/SCONJ (la forma explícita `Yo trabajo en Google` sí captura `(user, trabajar_en, google)`
  → alias → `works_for`; `Ana come una manzana` sí captura). Es una limitación del modelo, no de las
  cadenas — sin una lista de verbos (prohibida) no hay señal gramatical que la separe de un nombre
  propio o de la conjunción `como`.
- **Psicoverbos en tercera persona** (`A María le gusta el café` → `(café, gustar, maría)`): es la
  estructura gramatical española literal («el café gusta a María»), no una inversión; forzarla al
  orden inglés (`(maría, like, café)`) sería imponer una cadena inglesa, contra el diseño de la rama.
- Las tablas de patrones sembrados (negación, temporales, pistas lingüísticas, alias de relaciones)
  incluyen ya **semillas en español** (migración 218: meses, días, pistas relativas, clases de
  parentesco/unidades/verbos, alias `vivir_en`→`lives_in`, `trabajar_en`/verbos → `works_for`)
  — crecen por tenant, igual que las inglesas.
- El modelo español de spaCy **no emite la etiqueta `DATE`**: la capa de fechas usa las **pistas
  temporales en la base de datos** (meses/relativos, migración 218) como autoridad, así que las
  fechas con palabra (`el 15 de marzo de 1990`, `hace dos semanas`) se resuelven igual que en inglés.
- **Consulta en inglés en esta rama**: los `natural_language` sembrados son **españoles** (la rama ES
  el idioma, contrato de la rama), así que una consulta inglesa de alcance relacional puede no
  acotar (p. ej. `where do you work?`); la consulta española (`¿dónde trabajas?`) sí. Las
  instalaciones inglesas usan `master`/`main`.

## Idioma y base de datos (léelo antes de instalar)
El idioma se elige **al principio** de `quickstart.py`, antes que cualquier otra cosa, y esa elección
fija la **colación (collation) de PostgreSQL** mediante ICU (`es-ES`).

> ⚠️ **La colación se fija en `initdb` y después es INMUTABLE.** No se puede cambiar sin un
> dump/restore. Por eso hay que decidirlo en la instalación: una instalación en español que aterrice
> sobre una base de datos creada con la colación inglesa por defecto arrastraría un **orden de
> texto incorrecto para siempre**.

Si reutilizas un volumen de Postgres ya existente cuya colación no coincide, el arranque **se detiene
a propósito** y te ofrece las opciones reales (recrear el volumen, dump/restore, o aceptar el desajuste
con `FAULTLINE_ALLOW_DB_LOCALE_MISMATCH=true`). No se modifica ni se borra nada.

Se usa **ICU** y no una locale del sistema porque la imagen de postgres **no incluye locales `es_*`**,
así que `--locale=es_ES.utf8` fallaría directamente.

## Estado y detalles técnicos
Esta rama `es` sigue siendo **experimental** (no es una release oficial), pero la **extracción
determinista** (motor *spine*) ya lee el esquema UD del modelo español en las construcciones
principales — posesión/kinship, sentimientos y preferencias, cantidades y medidas, fechas y
duraciones, nombres y clasificación — verificadas por `tests/test_spanish_capture_and_walk.py`
(paridad de captura contra el motor inglés, construcción por construcción, más el paseo de
consulta «capturar → preguntar en español → devolver»). Los residuales honestos están listados en
«Lo que TODAVÍA no está en paridad» arriba.

Las correcciones descritas aquí están cubiertas por `tests/test_spanish_language_support.py`
y `tests/test_spanish_capture_and_walk.py` (80 pruebas en total con el DSN del banco de
pruebas). Las que dependen de spaCy se **omiten** limpiamente si `es_core_news_md` no está
instalado, así que la suite sigue pasando en un checkout en inglés.

### Licencias
`es_core_news_md` se distribuye bajo **GNU GPL 3.0** (por el corpus AnCora), mientras que
`en_core_web_sm` es MIT. FaultLine es **AGPL-3.0-only**, así que la combinación es compatible y no
supone un problema para este proyecto — pero conviene saberlo si redistribuyes la imagen con el
modelo español horneado bajo otros términos. `fastino/gliner2-multi-v1` es Apache-2.0.
