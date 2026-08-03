# FaultLine — soporte en español (rama `es`)

> ## ⚠️ AVISO — VERSIÓN EXPERIMENTAL Y NO OFICIAL
>
> Esta rama (**`es`**) es una versión **experimental** y **en desarrollo** del soporte en español de
> FaultLine. **NO es una release oficial ni está lista para producción.** Se ofrece **"tal cual"**
> (*as is*), **sin garantía alguna** de funcionamiento, corrección o continuidad.
>
> - Las funcionalidades pueden estar **incompletas**, cambiar sin previo aviso o **no funcionar**.
> - **Todavía no se ha validado** de extremo a extremo con datos reales en español.
> - La extracción **determinista** (motor *spine*) está disponible **solo en inglés**; en español la
>   extracción se apoya en la vía basada en **LLM** (*rewrite*), por lo que es **menos predecible**.
> - **Úsala bajo tu propia responsabilidad.** Para la versión **estable** (en inglés) usa la rama
>   `master` / `main`.
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
- La **extracción** pasa por la vía **LLM** (*rewrite*), que entiende el español de forma nativa.
- El tipado de entidades usa un modelo **GLiNER multilingüe** (`gliner_multi`).
- Las fechas las gestiona `dateparser`, que soporta el español.
- El **motor determinista** (*spine*) sigue siendo inglés: construcciones como *"I am 34 years old"*
  y *"tengo 34 años"* tienen una sintaxis distinta, así que las reglas inglesas no se aplican al
  español.

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
Esta rama `es` es **experimental**: la extracción en español se apoya en la vía LLM (*rewrite*),
mientras que el motor determinista (*spine*) sigue en inglés. El modelo de spaCy pasa a
`es_core_news_sm` para que el analizador coincida con el texto — pero **eso no es una garantía de
paridad**: las cadenas del *spine* están construidas y validadas contra el inglés, y **no se han
medido** en español. Las funcionalidades pueden estar incompletas o cambiar. Para contribuir o
reportar problemas, ten presente el aviso de arriba.
