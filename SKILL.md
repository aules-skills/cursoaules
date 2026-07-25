---
name: cursoaules
description: "Genera un backup .mbz de curso para Aules/Moodle a partir de un PDF de currículum: extrae competencias, criterios de evaluación y contenidos, y crea un curso por temas con el libro de calificaciones organizado por Competencia → Criterio, notas ocultas al alumnado y Tasques numeradas que ya puntúan en criterios concretos. Úsala siempre que el usuario pida crear, montar o generar un curso de Aules, un .mbz, o una plantilla de curso por competencias a partir de un currículum/programación, aunque no diga 'mbz' explícitamente, y también al adaptarlo a otra asignatura."
---

# cursoaules

Genera un archivo `.mbz` (backup de curso Moodle) listo para restaurar en Aules, a partir del PDF de un currículum oficial. El resultado es un curso con secciones por tema y un libro de calificaciones organizado por **competencia específica → criterio de evaluación**, con actividades reales ("Practica") que ya cuentan en criterios concretos.

## Por qué existe esta skill

Configurar esto a mano en la interfaz de Aules (crear cada sección, cada categoría de calificación, cada actividad, cada peso) es lento y repetitivo, y hay que rehacerlo cada curso escolar y para cada asignatura. Esta skill reproduce ese trabajo generando directamente el archivo de backup, que se restaura en segundos. El formato XML interno de un `.mbz` es exigente (cualquier HTML sin escapar o un campo con formato incorrecto hace que la restauración falle silenciosamente en Aules, con errores genéricos como "error escribiendo en la base de datos" que no dicen qué falló). El script `scripts/build_mbz.py` ya encapsula todo eso — reproduce el esquema comprobado que restaura sin errores en Aules (Moodle 4.5.10+) para secciones, etiquetas, foro y libro de calificaciones — así que el trabajo real de esta skill es EXTRAER bien la información del PDF, no pelearse con el formato del backup.

**Estado del esquema de las Tasques (mod_assign) y su rúbrica:** ambos están ya verificados contra un caso real. Jose confirmó que las Tasques se restauran y quedan vinculadas al criterio correcto en el libro de calificaciones, y el esquema de la rúbrica (`gradingform_rubric`) se corrigió y confirmó a partir de una copia de seguridad real de una sola actividad que Jose exportó tras crear una rúbrica a mano en Aules — ver la sección "Rúbrica de corrección" más abajo para los detalles exactos del esquema (nombres de etiquetas, anidamiento) por si hace falta depurarlo de nuevo en el futuro.

## Cómo está organizado el libro de calificaciones

Esto es lo más importante de esta skill y lo que la diferencia de una plantilla genérica de Moodle: **el libro de calificaciones no sigue la estructura de temas/evaluaciones del curso, sino la de las competencias del currículum**:

```
Curso
├── CE1 (peso igual entre todas las CE)
│   ├── Criterio 1.1 (peso igual entre los criterios de su CE)
│   ├── Criterio 1.2
│   ├── ...
├── CE2
│   ├── Criterio 2.1
│   ├── ...
```

Los temas (secciones del curso) siguen organizando el *contenido* — qué se ve y en qué orden —, pero son independientes del libro de calificaciones. Cada tema está asociado a una competencia principal (y opcionalmente a competencias secundarias/transversales), y esa asociación es la que determina en qué criterios cuentan sus actividades.

### Notas pedagógicas ocultas al alumnado

Cada tema lleva, además de su contenido público, una etiqueta (label) que **solo ve el profesorado** (aparece en gris, tachada, en la vista de edición; el alumnado no la ve en absoluto). Ahí va: la evaluación a la que pertenece el tema, la competencia principal con su texto completo, el desglose de TODOS sus criterios uno a uno con su texto literal (no solo el rango "1.1-1.6"), lo mismo para cualquier competencia secundaria/transversal, y el mapeo explícito de qué practica evalúa qué criterios.

### Practiques (Tasca) numeradas y su mapeo a criterios

Por cada tema se generan `num_practiques_per_tema` Tasques públicas (mod_assign), por defecto 3, nombradas `Practica Tema#{n} #1`, `#2`, `#3`... Cada práctica evalúa, por defecto, **un único criterio** de la competencia principal del tema: la práctica #1 el primer criterio, la #2 el segundo, la #3 el tercero, y así sucesivamente, recorriendo la lista de criterios de esa competencia. Si hay más prácticas que criterios, se recorre cíclicamente desde el principio (y si hay más criterios que prácticas, los últimos se quedan sin práctica propia ese tema — normal si `num_practiques_per_tema` es menor que el número de criterios de la CE).

Como cada práctica evalúa un solo criterio, es directamente la categoría oficial (`grade_item`) de ese criterio en el calificador — no hacen falta elementos de calificación manuales ni ningún reparto especial. La rúbrica de cada práctica lleva, en consecuencia, una única fila (la del criterio que evalúa).

Si el usuario pide agrupar varios criterios por práctica (el comportamiento antiguo, por parejas u otro tamaño de grupo), es cuestión de fijar `criteris_per_practica` a un valor mayor que 1 en el JSON — en ese caso vuelve a aparecer el mecanismo de elemento manual para el criterio "sobrante" de cada grupo (el primer criterio del grupo es la nota nativa de la Tasca, los demás se introducen a mano). Pregúntale al usuario antes de asumir esto, porque el valor por defecto ahora es 1 criterio por práctica.

### Rúbrica de corrección en cada Practica (esquema verificado)

Cada Tasca lleva ya configurada una rúbrica de corrección (método de calificación avanzada `rubric` de Moodle), con una fila por cada criterio que evalúa esa práctica (1 o 2, según el reparto de arriba) y **5 niveles de logro fijos** por fila: **Insuficient** (0/4), **Suficient** (1/4), **Be** (2/4), **Notable** (3/4), **Excel·lent** (4/4). El texto de cada nivel incrusta el texto literal del criterio correspondiente, así que no hace falta redactar nada a mano. Moodle convierte la puntuación obtenida a la nota sobre 10 de la Tasca de forma proporcional, sea cual sea el número de criterios o niveles.

Este esquema de 5 niveles (en vez de los 4 de la primera versión) se adoptó porque coincide con la plantilla predefinida de rúbrica que el propio Moodle ofrece (Insuficiente/Suficiente/Bien/Notable/Excelente) — la misma que Jose usó para la prueba real que permitió confirmar el esquema (ver abajo).

**Historial de la depuración (importante si esto se vuelve a romper):** dos intentos previos de generar esta rúbrica fallaban en Aules real — la rúbrica aparecía seleccionada como método pero completamente vacía, sin ningún criterio. Se resolvió pidiéndole a Jose que creara una rúbrica a mano en una Tasca real, y que hiciera una **copia de seguridad de esa única actividad** (Moodle permite hacer backup de un solo módulo, no solo del curso entero). Al inspeccionar el `grading.xml` real de ese backup salieron a la luz los errores exactos de las versiones anteriores:

- Los nombres de los elementos anidados dentro de `plugin_gradingform_rubric_definition` son **`criteria` / `criterion` / `levels` / `level`** — SIN el prefijo `rubric_` que llevaban las versiones anteriores (`rubric_criteria`, etc.). Este era el fallo principal: Moodle interpretaba `<definition>` correctamente (por eso se veía "Rúbrica" como método) pero no reconocía los elementos con el nombre incorrecto y los descartaba en silencio.
- `<instances>` va **dentro de `<definition>`**, como su último hijo — NO como hermano de `<definitions>` dentro de `<area>`.
- `<area>` **no lleva** ningún elemento `<component>`.
- `<definition>` **no lleva** ningún elemento `<copiedfromid>`.
- `<options>` es un JSON con los valores como **cadenas** (`"1"`, no `1`), e incluye la clave `lockzeropoints` además de las que ya se habían identificado.
- `<descriptionformat>` y `<definitionformat>` dentro de `criterion`/`level` son **`0`** (no `1`) cuando el texto es plano.
- Las puntuaciones (`<score>`) se guardan como decimales de 5 cifras (`0.00000`, `1.00000`...), igual que el resto de campos numéricos de calificación del backup.

La lección general: cuando algo del esquema de un sub-plugin de Moodle (grading forms, tipos de módulo, etc.) no se puede verificar por otra vía, pedir al usuario un backup real de una sola actividad/curso con ese elemento ya configurado a mano es mucho más fiable y rápido que seguir adivinando el esquema por conjetura — vale la pena proponerlo pronto en vez de gastar varios ciclos de prueba y error del usuario.

### Recursos (H5P / paquete SCORM)

En el modo por defecto (sin `evaluacio_weights`) no se generan: solo se crean las Practiques (Tasca), y si el usuario quiere Recursos hay que añadirlos a mano en Aules (por ejemplo con las skills `h5p`, `ahorcado` o `pasapalabra`). En el modo `evaluacio_weights` (ver abajo), en cambio, **sí se generan 6 Recursos reales por tema automáticamente** — ver la sección siguiente.

## Modo alternativo: libro de calificaciones anidado por CE con Activitats/Recursos/Examen

Por defecto (sin el campo `evaluacio_weights` en el JSON) el libro de calificaciones es el descrito arriba: plano, Curs → CE → Criteri, sin que las evaluaciones pesen nada. Si el usuario pide explícitamente pesos por evaluación (p. ej. "60% actividades, 30% recursos, 10% examen"), añade `evaluacio_weights` al JSON y el script cambia a una estructura anidada por CE dentro de cada avaluació:

```
Curs
├── 1a Avaluacio (pes igual entre avaluacions)
│   ├── CE1 (pes igual entre les CE principals d'esta avaluacio)
│   │   ├── Activitats (p.ex. 60%)
│   │   │   ├── Criteri 1.1 — Practica(s) d'eixe criteri
│   │   │   ├── ...
│   │   ├── Recursos (p.ex. 30%)
│   │   │   ├── Criteri 1.1 — Recurs(os) H5P/SCORM que avaluen eixe criteri
│   │   │   ├── ...
│   │   └── Examen (p.ex. 10%, gradepass=5)
│   ├── CE2
│   │   └── (mateixa estructura)
│   ├── CE5 (transversal — mai és la CE principal de cap tema)
│   │   └── Examen (única branca: no hi ha Activitats ni Recursos perquè cap practica/recurs treballa una CE transversal com a principal)
├── 2a Avaluacio
│   └── ...
```

Nota clau: **Recursos té la mateixa forma que Activitats** — dins seu hi ha una subcategoria per cada criteri de la CE, no un calaix únic. Cada Recurs avalua un criteri concret, exactament igual que cada Practica.

Este modo cambia varias coses a la vegada respecte al modo per defecte:

0. **Mode preferit — content-driven amb `criterios_tema` + `tipo` (recomanat sempre que es puga extraure del currículum).** En comptes de decidir per rotació quin criteri toca cada Practica/Recurs, cada tema pot indicar EXPLÍCITAMENT al JSON, camp `criterios_tema`, la llista exacta de `{"ce": codi, "crit": codi_criteri}` que treballa eixa unitat concreta — molts currículums de FP (Resultats d'Aprenentatge) ja donen esta llista literalment unitat per unitat, i fins i tot en currículums LOMLOE per competències sovint es pot inferir de quins criteris parla cada bloc de sabers bàsics. Junt amb això, cada criteri de `competencias[].criterios[]` porta un camp `tipo`: `"accion"` (procediment físic/demostrable: muntar, connectar, instal·lar, comprovar, realitzar...) o `"concepto"` (identificar, descriure, reconèixer...).

   Amb estos dos camps, CADA TEMA genera sempre **4 Practiques i 6 Recursos** (un de cada tipus de joc — mateixos comptes fixos que sense `criterios_tema`, vore punts 1-2), pero ara el criteri que avalua cada una NO ix per rotació entre temes: cicla nomes pels criteris PROPIS d'eixe tema, preferint el tipus pedagògic correcte — les 4 Practiques ciclen pels criteris "accion" del tema (repetint-los si en té menys de 4), i els 6 Recursos ciclen pels criteris "concepto" del tema (repetint-los si en té menys de 6). Si un tema no té cap criteri d'un dels dos tipus (p. ex. una unitat purament teòrica sense cap criteri "accion"), cicla en el seu lloc per TOTS els criteris del tema, per a que cap tema es quede amb Practiques o Recursos "buits" — sempre isquen 4 i 6 encara que el tema siga molt prim en criteris (en eixe cas, alguns es repetiran mes d'una vegada, reforçant-los). Com el currículum pot repetir el mateix criteri en dos unitats diferents (és habitual, no un error), eixe criteri simplement rep activitats en cada tema que el treballa, i Moodle reparteix el pes a parts iguals entre elles dins de la mateixa subcategoria.

   Este mode tambe permet que una CE SECUNDÀRIA d'un tema (`competencies_extra`) tinga Practiques/Recursos propis si el currículum li assigna criteris concrets en eixa unitat (no nomes una branca d'Examen com fins ara) — la skill crea automàticament la categoria "Activitats"/"Recursos" d'eixa CE encara que no siga principal de cap tema en eixa avaluació. Quan un tema NO porta `criterios_tema` (o el currículum no es presta a esta extraccio), la skill cau al mode per rotació descrit als punts 1-2 següents, íntegrament compatible amb cursos ja existents.

1. **Sense `criterios_tema` (fallback) — un nombre FIX de Practiques per tema (per defecte 4), cadascuna avaluant UN sol criteri** (`criteris_per_practica` queda forçat a 1; `num_practiques_per_tema` per defecte 4, configurable). Ja NO depén del nombre de criteris de la CE del tema — abans (fins la v18) cada tema generava tantes Practiques com criteris tinguera la seua CE, pero aixo feia que temes amb CE de poques o moltes criteris tingueren un nombre de Practiques molt desigual. Ara el nombre és sempre el mateix, i quin criteri toca cada Practica es decideix per un índex rotatori: la Practica `k` d'un tema avalua el criteri `(k - 1 + tema_offset) % n_criteris`, on `tema_offset` és la posició del tema entre els que comparteixen la mateixa CE principal (0 pel primer, 1 pel segon...). Aixo és EXACTAMENT el mateix mecanisme que ja fan servir els Recursos (vore més avall) — quan diversos temes comparteixen una CE amb mes criteris que Practiques per tema, la rotació fa que entre tots acaben cobrint mes criteris que si tots repetiren sempre els mateixos primers. Cada Practica és directament la categoria oficial del seu criteri, dins de la subcategoria "Activitats" d'eixa CE.
2. **Sense `criterios_tema` (fallback) — 6 Recursos reals per tema**, generats automàticament com a activitats H5P/SCORM de veritat (no elements manuals). Els 6 jocs, sempre en el mateix ordre:

   | # | Joc | Tipus Moodle | Skill que el genera |
   |---|---|---|---|
   | 1 | Pasapalabra | SCORM | `pasapalabra` |
   | 2 | Ahorcat | SCORM | `ahorcado` |
   | 3 | Crucigrama | H5P (`H5P.Crossword`) | `h5p` |
   | 4 | Sopa de lletres | H5P | `h5p` |
   | 5 | Omplir buits | H5P (`H5P.AdvancedBlanks`) | `h5p` |
   | 6 | Arrossegar paraules | H5P (`H5P.DragText`) | `h5p` |

   **Cada Recurs avalua un criteri concret** dins de la subcategoria "Recursos" de la CE del seu tema — igual que cada Practica avalua un criteri concret dins d'"Activitats". Els 6 jocs es reparteixen cíclicament entre els criteris de la CE, en l'ordre de la taula: si la CE té 6 criteris, cada joc va a un criteri distint (1 a 1); si en té menys de 6 (p. ex. 5), l'últim joc "dona la volta" i el primer criteri rep 2 jocs.

   **Si una CE té MÉS de 6 criteris** (p. ex. 9), un sol tema no pot cobrir'ls tots amb només 6 jocs — el punt de partida del recorregut cíclic "rota" segons la posició del tema entre els que comparteixen eixa CE (el primer tema comença pel criteri 1, el segon pel 2, etc.), així que **si prou temes comparteixen eixa CE, entre tots acaben cobrint tots els criteris** (verificat: amb 5 temes i 9 criteris es cobreixen els 9). Però si nomes 1 o 2 temes tenen eixa CE com a principal i té molts criteris, alguns es queden sense cap Recurs propi (només Activitats i Examen els avaluen) — és una limitació estructural de tindre només 6 jocs fixos, no un error; avisa d'açò explícitament si detectes el cas en construir un curs nou.

   Cada Recurs pesa dins del seu criteri exactament com pesa una Practica dins del seu — i el pes global de Recursos (30% o el que s'indique) es reparteix igual entre tots els criteris de la CE. **Si diverses temes comparteixen la mateixa CE principal, els recursos de cada criteri es reparteixen automàticament entre tots eixos temes** — no cal fer res especial: com tots cauen dins de la mateixa subcategoria "Recursos → Criteri X" i Moodle pondera per defecte a parts iguals entre els elements d'una categoria, si 2 temes tenen CE2 com a principal, els seus 2 recursos per criteri es reparteixen automàticament el pes a parts iguals — és exactament l'efecte "es dividirà per dos" que va demanar Jose, sense codi especial.

   **Molt important — el contingut és un placeholder, no el definitiu.** Cada Recurs generat usa un fitxer d'exemple real de la carpeta `scripts/assets_recursos/` (el mateix per a tots els temes que usen eixe joc), NOMÉS per a muntar l'estructura del curs. El professorat ha de substituir cada fitxer pel contingut real de cada tema després de restaurar — a Aules: entra a l'activitat → Editar ajustos → "Substitueix amb el fitxer" (o torna a generar-la amb les skills `h5p`/`ahorcado`/`pasapalabra` i puja eixe fitxer nou). **Avís especial:** no existia cap exemple real de "sopa de lletres" a la carpeta de Jose, així que eixe slot fa servir un segon crucigrama (`H5P.Crossword`) com a placeholder — cal substituir-lo per una sopa de lletres real abans d'usar-lo amb alumnat.

   **Recursos EXTRA per a un tema concret (`recursos_extra`).** A banda dels 6 jocs cíclics de la taula (lligats sempre a la CE del tema), un tema pot demanar Recursos addicionals propis via el camp `recursos_extra` (llista de claus). Per ara nomes existeix `"binarygame"` (Cisco Binary Game, SCORM — practica conversió binari↔decimal contra el rellotge; no necessita cap PDF ni contingut d'origen, sempre és el mateix joc, així que no cal invocar cap skill per a personalitzar-lo). Útil quan un tema treballa una habilitat molt concreta que no té sentit escampar a la resta de temes de la mateixa CE (per exemple, el tema de sistemes numèrics d'un mòdul de muntatge i manteniment). Estos Recursos extra també avaluen un criteri concret (seguint la mateixa rotació que la resta), no son un afegit sense pes — simplement no couten en el cicle fix dels 6 jocs ni es repeteixen a altres temes.

3. **Una secció "Examen" nova al final de cada avaluació**, amb una única Tasca pública ("Examen 1a Avaluacio"...) que avalua TOTES les CE que apareixen en eixa avaluació — incloses les transversals (`competencies_extra`). Com una Tasca només pot tindre un `grade_item` real, la primera CE de la llista és la nota nativa de la Tasca i les altres són elements manuals (mateix mecanisme que el repartiment de criteris, aplicat ací a nivell de CE). L'examen no porta rúbrica generada — és una nota holística que el professorat reparteix a mà entre les seues CE.
4. **Pesos explícits (per defecte 60/30/10, o els que s'indiquen) entre Activitats/Recursos/Examen de cada CE**, i pes igual entre CE dins de cada avaluació, i pes igual entre avaluacions entre elles.

Format del camp (en el JSON d'entrada, junt a `competencias`/`temas`):

```json
"evaluacio_weights": { "activitats": 60, "recursos": 30, "examen": 10 }
```

Els tres valors són percentatges i es normalitzen automàticament si no sumen 100. Si l'usuari demana un altre repartiment, és només canviar estos tres números — no cal tocar l'script.

**Nota mínima de 5 en l'Examen per a aprovar.** Es marca amb `gradepass=5` en l'element de qualificació de la categoria "Examen" de CADA CE (principals i transversals), el camp estàndard de Moodle per a "nota per a aprovar" (indicador visual aprovat/suspès en el qualificador). **Important:** la mitjana ponderada de Moodle NO aplica este requisit de forma automàtica — Moodle seguirà calculant la mitjana encara que l'Examen estiga per davall de 5. Si cal suspendre l'avaluació sencera per no arribar a 5 en l'examen, el professorat ha de revisar eixe cas i ajustar a mà la nota final (amb la funció "invalidar" del qualificador d'Aules). Això queda explicat en la nota oculta del professorat de cada secció "Examen".

**Estat de verificació d'este mode:** tot l'esquema — categories anidades per CE, Tasques/rúbriques, i ara també `mod_h5pactivity` i `mod_scorm` (estructura de `module.xml`, `h5pactivity.xml`/`scorm.xml`, `grades.xml`, `inforef.xml` i emmagatzematge de fitxers binaris amb `files.xml`) — s'ha verificat contra una còpia de seguretat real d'un curs complet que Jose va exportar, amb 6 activitats H5P i 4 SCORM ja restaurades i funcionant. És la primera vegada que esta skill genera activitats amb contingut binari real (no només Tasques de text), així que encara es recomana provar-ho en un curs de prova/sandbox abans d'usar-ho amb alumnat real, per si algun detall canvia entre versions de Moodle.

## Flujo de trabajo

### 1. Localiza el PDF del currículum

El usuario suele dejarlo en una carpeta relacionada con la asignatura (p. ej. una carpeta con el nombre de la materia). Si no lo indica, pregúntale dónde está o pídeselo.

### 2. Extrae del PDF la información curricular

Lee el PDF (texto, no hace falta OCR salvo que esté escaneado) y localiza estas tres piezas, que en los currículums LOMLOE (estatales y autonómicos) suelen aparecer en este orden:

- **Competencias específicas**: normalmente numeradas (CE1, CE2...), cada una con su texto literal completo (una frase). Transcribe este texto tal cual aparece en el PDF, sin parafrasear — va a `competencias[].texto` en el JSON de entrada, y también aparece literalmente en las notas ocultas del profesorado.
- **Criterios de evaluación**: casi siempre organizados por competencia específica y por curso (si la materia se imparte en varios cursos, quédate solo con los criterios del curso que pide el usuario). Cada criterio individual (1.1, 1.2, 1.3...) tiene también su propio texto literal completo, no solo un rango — transcríbelo entero para cada uno. Esto va a `competencias[].criterios[]` en el JSON.
- **Saberes básicos / contenidos**: organizados en bloques (Bloque 1, Bloque 2...), y cada bloque desglosado en subapartados con su propio título (p. ej. dentro de "Bloque 1: Dispositivos digitales..." puede haber subapartados como "Arquitectura de ordenadores", "Sistemas operativos"...). **Cada uno de estos subapartados se convierte en un tema** de tu curso — es la unidad natural que ya trae el propio currículum, no hace falta inventar nada.

Ten cuidado con artefactos típicos de extracción de texto de PDF (espacios de más por guiones de partición de línea, como "se gura" en vez de "segura", o "utilitzar -los" en vez de "utilitzar-los") — revísalos y corrígelos antes de transcribir el texto oficial, porque ese texto se muestra literalmente al profesorado.

Normalmente cada bloque de saberes básicos está vinculado 1 a 1 con una competencia específica (bloque 1 con CE1, bloque 2 con CE2, etc.), y puede haber una última competencia "transversal" que no tiene bloque propio porque moviliza las demás — en ese caso, añádela a TODOS los temas como competencia secundaria (mira `competencies_extra` en el esquema).

Si el PDF no sigue esta estructura típica (algunas materias/currículums la organizan distinto), usa tu criterio: el objetivo final es que cada tema quede asociado a al menos una competencia específica con sus criterios de evaluación (con texto completo), y que los temas queden agrupados en bloques con sentido pedagógico.

**Si vas a usar `evaluacio_weights` (ver "Modo alternativo" más abajo), comprueba si el propio documento indica, unidad por unidad, qué criterios de evaluación concretos trabaja cada una** (muy común en programaciones de FP: cada "Unidad" suele listar explícitamente sus "Criterios de Evaluación" del RA, a veces de más de un RA a la vez si la unidad es de contenido mixto). Si es así, extráelo tal cual y rellena `criterios_tema` en cada tema — es más preciso que dejar que la skill reparta por rotación, y evita que una CE secundaria de una unidad se quede sin su propia actividad. Aprovecha también para clasificar cada criterio como `"accion"` o `"concepto"` (campo `tipo`, ver más abajo) fijándote en su verbo principal: "se han descrito/identificado/reconocido..." → `concepto`; "se han montado/conectado/instalado/comprobado/realizado..." → `accion`. No hace falta preguntarle al usuario esto — es una lectura directa del texto del criterio, no una decisión editorial.

Fíjate también en si el currículum indica que la materia NO se imparte en todos los cursos que el usuario ha mencionado (algunos currículums LOMLOE solo cubren, por ejemplo, 1r i 3r ESO y se saltan 2n, o distinguen contenidos/criterios distintos por curso dentro del mismo documento). Si detectas esto, avisa al usuario explícitamente antes de entregar el curso — es un matiz curricular real que puede importarle para decidir cómo organizar los grupos, y no algo que debas resolver en silencio fusionando cursos sin más.

### 3. Reparte los temas en evaluaciones (organizativo, no afecta al libro de calificaciones)

Por defecto, esta skill usa **3 evaluaciones** (`num_evaluacions`/`eval_names`) para etiquetar de forma descriptiva a qué momento del curso pertenece cada tema (aparece en la nota oculta del profesorado, p. ej. "1a Avaluacio"). Esto es puramente informativo: **no determina pesos ni categorías del libro de calificaciones**, que depende solo de `competencias`. Reparte los bloques de contenidos entre las 3 evaluaciones de la forma más equilibrada posible en número de temas, manteniendo cada bloque completo dentro de una sola evaluación. No preguntes al usuario por esto salvo que pida explícitamente cambiarlo.

### 4. Construye el JSON de entrada

Mira `scripts/schema_example.json` para el formato exacto. Los campos clave:

- `course_shortname` / `course_fullname`: nombre corto y largo del curso (pregunta al usuario si no está claro qué curso/grupo es).
- `wwwroot`, `moodle_version`, `moodle_release`, `site_identifier_hash`: por defecto ya apuntan a Aules (GVA) con la versión comprobada (Moodle 4.5.10+). Solo cámbialos si el usuario dice que su Moodle es de otro sitio o versión distinta — en ese caso pregúntale la versión antes de asumir nada.
- `competencias`: lista de TODAS las competencias específicas del currículum, cada una con `codigo`, `texto` (literal) y `criterios` (lista de `{codigo, texto}`, también literal, uno por criterio). Esto alimenta directamente el libro de calificaciones (una categoría por CE, una subcategoría por criterio) y las notas ocultas del profesorado. Inclúyelas TODAS aunque algún tema no las use directamente como principal — pueden aparecer como `competencies_extra`.
- `num_practiques_per_tema` (opcional, por defecto **4** en modo `evaluacio_weights`, 3 en modo clásico) y `criteris_per_practica` (opcional, por defecto 1): controlan cuántas Tasques se generan por tema y cuántos criterios evalúa cada una (por defecto, uno solo). En modo `evaluacio_weights` el número de Practiques ya NO depende de cuántos criterios tenga la CE del tema — es siempre el mismo valor fijo, y qué criterio evalúa cada Practica se decide por rotación (ver punto 1 de la sección "Modo alternativo").
- `recursos_extra` (opcional, campo del tema, no del curso): lista de claves de juegos EXTRA para ese tema en concreto, además de los Recursos normales de su CE. Por ahora solo existe la clave `"binarygame"` (Cisco Binary Game, paquete SCORM que practica conversión binario↔decimal contra el reloj — no necesita PDF ni contenido propio, siempre es el mismo juego). Úsalo quan un tema treballe una habilitat molt concreta que no té sentit repetir a la resta de temes de la mateixa CE — el cas típic és el tema de sistemes numèrics/binari d'un mòdul, encara que la seua CE tinga altres temes que no tracten binari. Exemple: `"recursos_extra": ["binarygame"]` al tema corresponent. Estos Recursos extra també avaluen un criteri concret (seguint la mateixa rotació que la resta de Recursos d'eixe tema), no nomes son un afegit sense pes al qualificador.
- `evaluacio_weights` (opcional, ausente por defecto): si el usuario pide pesos por evaluación/CE (Activitats/Recursos/Examen; por defecto se recomienda 60/30/10), añade este campo — ver la sección "Modo alternativo" más arriba. Su sola presencia activa ese modo: desactiva `num_practiques_per_tema`/`criteris_per_practica` (pasan a calcularse automáticamente, una práctica por criterio) y genera automáticamente 6 Recursos H5P/SCORM reales por tema (usando los ficheros de ejemplo de `scripts/assets_recursos/` como contenido placeholder).
- `temas`: lista de temas, cada uno con `n` (número secuencial empezando en 1), `name`, `bloc` (texto libre descriptivo), `eval` (a qué evaluación pertenece — con `evaluacio_weights` esto SÍ determina la sección/categoría de esa avaluación, ya no es solo informativo), `competencia` (`codi`, `text` y `criteris` como rango descriptivo, p. ej. "1.1-1.6" — el texto completo de cada criterio se saca de `competencias`, no hace falta repetirlo aquí), y opcionalmente `competencies_extra` para competencias transversales/secundarias con la misma forma.
- `criterios_tema` (opcional, campo del tema, SOLO en modo `evaluacio_weights` — es el mecanismo PREFERIDO cuando el currículum lo permite): lista exacta de `{"ce": codi, "crit": codi_criterio}` que trabaja esa unidad concreta, tal como venga literalmente en el documento curricular (muy habitual en FP, donde cada Resultado de Aprendizaje ya lista sus Criterios de Evaluación unidad por unidad; también aplicable en currículums por competencias si el bloque de saberes básicos de cada tema se puede mapear a criterios concretos). Cuando un tema lo lleva, sigue generando siempre 4 Practicas y 6 Recursos (los mismos conteos fijos que sin `criterios_tema`), pero decide qué criterio evalúa cada una ciclando SOLO por los criterios propios de ese tema en vez de por rotación entre temas: las 4 Practicas ciclan por sus criterios `"accion"` (repitiéndolos si hay menos de 4) y los 6 Recursos ciclan por sus criterios `"concepto"` (repitiéndolos si hay menos de 6) — ver campo `tipo` más abajo y el punto 0 de "Modo alternativo". Si el tema no tiene ningún criterio de uno de los dos tipos, cicla en su lugar por todos sus criterios, para no dejar Practicas o Recursos sin criterio. Un mismo criterio puede aparecer en `criterios_tema` de dos temas distintos si el currículum lo repite — no lo evites, es correcto y Moodle reparte el peso a partes iguales. Si un tema no lo lleva, se usa el reparto por rotación (fallback, sin cambios respecto a versiones anteriores).
- `tipo` (opcional, campo de cada criterio dentro de `competencias[].criterios[]`, solo relevante junto a `criterios_tema`): `"accion"` (procedimiento físico/demostrable — montar, conectar, instalar, comprobar, realizar...) o `"concepto"` (identificar, describir, reconocer...). Decide si ese criterio, quando aparece en el `criterios_tema` de un tema, se evalúa con una Practica rubricada o con un Recurso H5P/SCORM. Si no se indica, se trata como `"accion"` (comportamiento previo a esta distinción). Para clasificarlo, fíjate en el verbo principal del criterio, no en el tema que lo contiene.

Guarda este JSON en un archivo temporal (p. ej. `curso_data.json`).

### 5. Genera y valida el .mbz

```bash
python3 scripts/build_mbz.py curso_data.json salida.mbz
```

El script ya valida el resultado automáticamente al terminar (bien formado como XML, sin HTML mal escapado en resúmenes/etiquetas, todas las etiquetas de notas del profesorado ocultas, nombres de Tasca sin duplicar, categorías del libro de calificaciones sin huérfanas, `grade_items` de las Tasques apuntando a un criterio concreto y válido, y cada Tasca con su rúbrica de 5 niveles bien formada; en modo `evaluacio_weights` también valida que los Recursos H5P/SCORM tengan categoría válida, fitxers referenciats i blobs presents) e imprime "Validacion: TODO CORRECTO." si todo va bien. **Si el script termina con "¡ATENCION!" o código de salida distinto de 0, no entregues el archivo** — revisa el JSON de entrada (normalmente un campo de texto con `<` o `>` sin pasar por el propio script, un `eval` de un tema que no coincide con ningún índice de `eval_names`, o una `competencia.codi` de un tema que no existe en la lista `competencias`).

### 6. Entrega el archivo

Comparte el `.mbz` con el usuario y explícale brevemente qué construiste (número de temas, competencias y criterios cubiertos, cuántas Practiques por tema, cómo se reparten los criterios y que cada una lleva ya su rúbrica de 5 niveles) y cómo restaurarlo: en Aules, dentro de un curso vacío nuevo, Ajustes > Restaurar, subir el `.mbz` y seguir el asistente. Si NO has usado el modo `evaluacio_weights`, recuérdale que los Recursos (H5P/SCORM) no se generan aquí y hay que añadirlos a mano después. Si SÍ lo has usado, avisa explícitamente de que: (a) cada Recurs generado usa un fichero de ejemplo como placeholder y hay que sustituirlo por el contenido real de cada tema ("Substitueix amb el fitxer" en Aules), (b) si no había ejemplo real de "sopa de letras" el slot correspondiente usa un crucigrama como placeholder temporal, y (c) aunque el esquema H5P/SCORM ya está verificado contra una copia de seguridad real, es la primera vez que esta skill genera contenido binario y conviene probarlo en un curso de prueba antes de usarlo con alumnado.

## Notas importantes sobre el formato .mbz (por si algo falla)

Si tienes que tocar `build_mbz.py` porque algo no cuadra, ten en cuenta estas lecciones aprendidas al construirlo (para no volver a romperlas):

1. **Nunca metas HTML sin escapar dentro de un campo de texto XML** (como `<summary>` o `<intro>`). Si quieres que un campo muestre `<p>texto</p>` en Aules, el XML debe contener literalmente `&lt;p&gt;texto&lt;/p&gt;`, no las etiquetas reales — si no, Moodle las interpreta como sub-elementos XML en vez de como texto, y la restauración falla con un genérico "error escribiendo en la base de datos" sin más detalle. La función `xml_text_escape()` del script ya se encarga de esto; si añades campos de texto nuevos, pásalos siempre por ella.
2. **El campo `backup_id` en `moodle_backup.xml` debe ser un hash hexadecimal de exactamente 32 caracteres** (se genera con `uuid.uuid4().hex`, que ya tiene la longitud correcta). Un valor de longitud distinta también rompe la restauración de forma temprana y silenciosa.
3. **Ocultar una actividad al alumnado es `visible=0` (y `visibleold=0`) en su `module.xml`**, no un campo dentro del propio módulo. Es lo que usa esta skill para las notas del profesorado (label).
4. **Un elemento de calificación manual (`itemtype=manual`) necesita `itemname` relleno** (no `$@NULL@$`) porque, a diferencia de un item de actividad o de categoría, no hay ningún objeto de Moodle del que Moodle pueda derivar el nombre a mostrar — si se deja `$@NULL@$`, el elemento aparece sin nombre en el calificador.
5. **IDs únicos**: el script asigna todos los ids (secciones, actividades, categorías, grade_items...) con un contador secuencial único (`next_id()`) en vez de rangos fijos, para evitar cualquier colisión sea cual sea el tamaño del currículum (número de temas, competencias o criterios).

En general, cualquier cambio al esquema XML es de alto riesgo porque Aules no da mensajes de error útiles cuando algo falla — antes de dar por buena una modificación al script, ejecuta `validate()` (ya integrado al final de `build_mbz.py`) y, si es un cambio importante, pide al usuario que lo pruebe restaurando en un curso de prueba antes de confiar en él para un curso real.
