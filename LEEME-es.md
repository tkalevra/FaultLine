# FaultLine — soporte en español (rama `es`)

> ## ⚠️ AVISO — VERSIÓN EXPERIMENTAL Y NO OFICIAL
>
> Esta rama (**`es`**) es una versión **experimental** y **en desarrollo** del soporte en español de
> FaultLine. **NO es una release oficial ni está lista para producción.** Se ofrece **"tal cual"**
> (*as is*), **sin garantía alguna** de funcionamiento, corrección o continuidad.
>
> - Las funcionalidades pueden estar **incompletas**, cambiar sin previo aviso o **no funcionar**.
> - La extracción **determinista** (motor *spine*) está **parcialmente** disponible en español: el
>   analizador y la detección de negación ya funcionan, pero las **cadenas de dependencias** del
>   *spine* siguen ajustadas a la sintaxis inglesa y **no se han re-ajustado** para el español.
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
- El analizador de spaCy es **`es_core_news_sm`** por defecto — y ahora se **verifica al arrancar**:
  si `SPACY_MODEL` no coincide con `FAULTLINE_LANGUAGE`, la capa se **desactiva** y registra
  `linguistic_layer.model_language_mismatch` en vez de producir datos incorrectos en silencio.
  (Escape: `FAULTLINE_ALLOW_SPACY_LANG_MISMATCH=true`.)
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

### Lo que TODAVÍA no está en paridad
- Las **cadenas de dependencias** del *spine* están construidas sobre etiquetas del esquema inglés
  (`dobj`, `pobj`, `nsubjpass`, `attr`, `acomp`, `oprd`). En UD el **árbol tiene otra forma**, no
  solo otros nombres, así que no es un simple renombrado. La captura en español será **menor** que
  en inglés hasta que se re-ajusten.
- Las tablas de patrones sembrados (negación, temporales, pistas lingüísticas, alias de relaciones —
  unas 431 filas) **siguen siendo solo en inglés** y aún no tienen columna de idioma.
- El modelo español de spaCy **no emite la etiqueta `DATE`**, que es la única fuente de tramos de
  fecha del motor determinista.

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
Esta rama `es` sigue siendo **experimental**. El analizador ya coincide con el texto y la negación
ya no se pierde, pero **eso no es una garantía de paridad**: las cadenas del *spine* están
construidas y validadas contra el inglés y **no se han re-ajustado** al español. Espera una captura
**inferior** a la del inglés, no equivalente. Para contribuir o reportar problemas, ten presente el
aviso de arriba.

Las correcciones descritas aquí están cubiertas por `tests/test_spanish_language_support.py`
(53 pruebas). Las que dependen de spaCy se **omiten** limpiamente si `es_core_news_sm` no está
instalado, así que la suite sigue pasando en un checkout en inglés.

### Licencias
`es_core_news_sm` se distribuye bajo **GNU GPL 3.0** (por el corpus AnCora), mientras que
`en_core_web_sm` es MIT. FaultLine es **AGPL-3.0-only**, así que la combinación es compatible y no
supone un problema para este proyecto — pero conviene saberlo si redistribuyes la imagen con el
modelo español horneado bajo otros términos. `fastino/gliner2-multi-v1` es Apache-2.0.
