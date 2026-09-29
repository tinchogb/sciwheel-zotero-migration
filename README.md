# Sciwheel to Zotero 10+ Migration Suite 🚀

![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Package Manager](https://img.shields.io/badge/uv-supported-6f42c1.svg)
![Target](https://img.shields.io/badge/Zotero-10%2B%20Local%20API-red.svg)
![Automation](https://img.shields.io/badge/Playwright-DOM%20Extraction-green.svg)
![License](https://img.shields.io/badge/license-Apache-blue.svg)

Suite de herramientas en Python diseñadas para migrar de forma integral y con **mínima pérdida de información** la biblioteca personal o compartida desde **Sciwheel** hacia **Zotero 10+** mediante la *Local Write API* oficial de Zotero (`http://127.0.0.1:23119/api/`).

---

## 🎯 Propósito

El propósito de este proyecto es resolver los vacíos de fidelidad que dejan las exportaciones tradicionales en formatos estándar (`.bib`, `.ris`), los cuales omiten comentarios contextuales, archivos suplementarios y la posición exacta de las marcas de lectura en PDF.

Los scripts trabajan en conjunto mediante un modelo desacoplado en dos fases:

1. **Extracción Exhaustiva (`sciwheel_extractor.py`):**
   * Descarga la jerarquía completa de proyectos privados y compartidos (anidación ilimitada de carpetas).
   * Obtiene metadatos bibliográficos, PDFs principales y todos los archivos suplementarios/adjuntos.
   * Recupera notas de la biblioteca (*Lean Library*) y **anotaciones de resaltado en PDF**.
   * Inspecciona el DOM de PDF.js mediante **Playwright Chromium** para capturar las coordenadas geométricas ($x, y, w, h$) de cada resaltado, transformándolas del espacio web al espacio nativo de puntos PDF (origen *Bottom-Left*, 72 DPI).
   * Genera un manifiesto intermedio unificado: `hierarchy_and_metadata.json`.

2. **Importación Local e Idempotente (`zotero_importer.py`):**
   * Reconstruye la jerarquía de colecciones en Zotero local.
   * Realiza una **reconciliación aditiva pura**: verifica el estado real de Zotero mediante etiquetas de seguimiento (`sciwheel-id:*`, `sciwheel-note:*`, `sciwheel-att:*`) para evitar duplicaciones o sobreescrituras en ejecuciones repetidas.
   * Sube archivos PDF y adjuntos suplementarios mediante el protocolo oficial de subida en 3 fases (*Local Write API*).
   * Mapea los resaltados detectados a la paleta nativa de 8 colores de Zotero (`#ffd400`, `#ff6666`, `#5fb236`, etc.) y crea anotaciones nativas vinculadas directamente a la página correspondiente del PDF.

---

## 📋 Tabla de Contenidos (ToC)

- [🎯 Propósito](#-propósito)
- [📋 Tabla de Contenidos (ToC)](#-tabla-de-contenidos-toc)
- [💎 El Manifiesto JSON: Corazón de la Migración](#-el-manifiesto-json-corazón-de-la-migración)
- [🧩 Arquitectura y Flujo de Trabajo](#-arquitectura-y-flujo-de-trabajo)
- [⚙️ Requisitos](#️-requisitos)
- [🚀 Instalación](#-instalación)
- [💻 Uso](#-uso)
  - [1. Extractor de Sciwheel (`sciwheel_extractor.py`)](#1-extractor-de-sciwheel-sciwheel_extractorpy)
  - [2. Importador de Zotero (`zotero_importer.py`)](#2-importador-de-zotero-zotero_importerpy)
- [🧪 Ejemplo Completo de Migración](#-ejemplo-completo-de-migración)
- [🛡️ Idempotencia y Tolerancia a Fallos](#️-idempotencia-y-tolerancia-a-fallos)
- [🤝 Contribución](#-contribución)

---

## 💎 El Manifiesto JSON: Corazón de la Migración

El archivo `hierarchy_and_metadata.json` es el punto neurálgico del proyecto. Actúa como un contrato de datos persistente e independiente que resguarda el estado completo de la extracción antes de interactuar con Zotero.

### ¿Por qué es fundamental?
* **Desacoplamiento Absoluto:** Permite ejecutar la extracción remota en un servidor o máquina sin Zotero, e importar los datos posteriormente en otro entorno.
* **Auditoría Offline:** Posibilita inspeccionar, validar y generar reportes analíticos avanzados (`--report`) directamente en disco sin depender de conexiones de red.
* **Preservación de Geometría Espacial ($x,y$):** Almacena las transformaciones de coordenadas y bounding boxes calculados por Playwright para reubicar los resaltados exactamente sobre las palabras correspondientes en el visor PDF de Zotero.
* **Trazabilidad e Idempotencia:** Mantiene hashes MD5 de archivos binarios, firmas únicas de notas (`sciwheel-note:<hash>`) y relaciones de archivos suplementarios para garantizar que ninguna re-ejecución duplique información.

### Estructura del Manifiesto JSON

```json
{
  "manifest_version": 1,
  "extractor_version": "0.0.1",
  "extracted_at": "2026-09-28T22:41:41Z",
  "summary": { ... },
  "failed_items": [ ... ],
  "failed_projects": [ ... ],
  "unmatched_annotations": [ ... ],
  "collections": [ ... ],
  "items": [ ... ]
}
```

* **Control e Historial:** `manifest_version`, `extractor_version` y `extracted_at` (marca ISO 8601 UTC).
* **Colecciones (`collections`):** Lista el árbol de proyectos (`id`, `name`, `parent_id`, `item_ids`), preservando la jerarquía completa.
* **Ítems Bibliográficos (`items`):** Contiene metadatos CSL/Zotero, creadores, etiquetas, rutas locales de archivos binarios, MD5, tamaño en bytes, y el desglose de notas planas y anotaciones de PDF con su correspondiente `annotationPosition` (`pageIndex`, `rects`, `rect`).
* **Resumen Embebido (`summary`):** Estadísticas consolidadas de la extracción (disponibilidad de PDFs, adjuntos y rendimiento del motor Playwright).
* **Anotaciones Huérfanas e Incidencias:** `unmatched_annotations` guarda marcas detectadas en el PDF sin coincidencia exacta 1:1 en la API, mientras que `failed_items` y `failed_projects` registran posibles errores para reintentos focalizados.

---

## 🧩 Arquitectura y Flujo de Trabajo

```text
[ Sciwheel API / Web Viewer ]
            │
            ▼ (requests & Playwright DOM)
 🛠️  sciwheel_extractor.py
            │
            ├─► PDFs/                     (Archivos PDF descargados)
            ├─► Attachments/              (Archivos suplementarios)
            ├─► Projects/                 (Archivos .bib por proyecto)
            └─► hierarchy_and_metadata.json  (Manifiesto Consolidado)
                        │
                        ▼ (reconciliación aditiva)
             🛠️  zotero_importer.py
                        │
                        ▼ (Local Write API)
           [ Zotero 10+ http://127.0.0.1:23119 ]
```

---

## ⚙️ Requisitos

* **Entorno del sistema:**
  * Python 3.10 o superior.
  * Administrador de entornos y paquetes [`uv`](https://github.com/astral-sh/uv) (recomendado) o `pip`.
* **Zotero:**
  * Zotero 10+ ejecutándose localmente en la máquina donde se ejecute el importador.
  * La API Local de Zotero habilitada y accesible en `http://127.0.0.1:23119/api/`.
* **Dependencias de Python:**
  * `requests`, `playwright` (con Chromium), `pypdf`.

---

## 🚀 Instalación

Utilizando `uv` la preparación del entorno se realiza en un solo paso sin gestionar entornos virtuales manualmente:

```bash
# Clonar el repositorio
git clone https://github.com/tu-usuario/sciwheel-zotero-migration.git
cd sciwheel-zotero-migration

# Instalar el navegador Chromium requerido por Playwright
uv run --with playwright python -m playwright install chromium
```

---

## 💻 Uso

### 1. Extractor de Sciwheel (`sciwheel_extractor.py`)

Extrae todos los proyectos, metadatos, adjuntos y geometría de anotaciones.

```bash
# Extracción completa a la carpeta ./Sciwheel_Data
uv run --with requests --with playwright --with pypdf sciwheel_extractor.py \
  -t 'Cookie_o_Bearer_Token_de_Sciwheel' \
  -v 1 \
  -o ./Sciwheel_Data
```

#### Banderas principales:
* `-t, --token`: Cookie de sesión (`sng_preferences=...`) o Bearer token de Sciwheel (**Requerido**).
* `-o, --output-dir`: Directorio de salida (Predeterminado: `~/Downloads/Sciwheel_Data`).
* `-P, --projects`: Filtra la extracción a proyectos o carpetas específicas por nombre o ID.
* `--retry-failed-pdfs`: Reintenta únicamente la descarga de PDFs faltantes en corridas previas.
* `-r, --report FILEPATH`: Genera e imprime un reporte analítico detallado directamente desde un manifiesto JSON en disco sin realizar peticiones de red.
* `--skip-geometry`: Desactiva la captura geométrica vía Playwright DOM.

### 2. Importador de Zotero (`zotero_importer.py`)

Importa el contenido del manifiesto a tu biblioteca local de Zotero.

> ⚠️ **Nota:** Asegúrate de tener **Zotero abierto** antes de ejecutar el importador.

```bash
# 1. Simulación (Dry-Run, por defecto no escribe nada en Zotero)
python3 zotero_importer.py --manifest ./Sciwheel_Data/hierarchy_and_metadata.json -v 1

# 2. Ejecución real
python3 zotero_importer.py --manifest ./Sciwheel_Data/hierarchy_and_metadata.json -x -v 1
```

#### Banderas principales:
* `--manifest`: Ruta al archivo `hierarchy_and_metadata.json` (**Requerido**).
* `-x, --execute`: Aplica los cambios de forma real en Zotero. Si se omite, corre en modo simulación (*dry-run*).
* `--retry-failed`: Recibe un informe `informe_importacion_*.json` previo y acota la ejecución únicamente a los ítems fallidos.
* `--zotero-base-url`: URL base de la API local de Zotero (Predeterminado: `http://127.0.0.1:23119/api/`).
* `--max-attachment-mb`: Límite máximo en MB por adjunto suplementario (Predeterminado: `500`).

---

## 🧪 Ejemplo Completo de Migración

A continuación se ilustra el ciclo completo de migración registrado desde la consola.

### Paso 1: Instalación del motor headless Chromium

```bash
$ time uv run --with playwright python -m playwright install chromium
```

### Paso 2: Extracción e Inspección Geométrica

```bash
$ time uv run --with requests --with playwright --with pypdf sciwheel_extractor.py \
    -t 'sng_preferences=...' \
    -v 2 \
    -o ./Sciwheel_Data
```

### Paso 3: Análisis y Reporte del Manifiesto JSON Extraído

El modo `--report` permite auditar el contenido extraído en disco sin tocar la red:

```bash
$ time uv run --with requests --with playwright --with pypdf sciwheel_extractor.py \
    --report ./Sciwheel_Data/hierarchy_and_metadata.json

================================================================================
               REPORTE ANALÍTICO DETALLADO DE MANIFIESTO JSON
================================================================================
  • Archivo analizado                         : /home/usuario/Downloads/Sciwheel_Data/hierarchy_and_metadata.json
  • Fecha/Hora de extracción                  : 2026-09-28T22:41:41Z
  • Versión del manifiesto / extractor        : v1 / v0.0.1
--------------------------------------------------------------------------------
1. RESUMEN GLOBAL DEL REPOSITORIO
--------------------------------------------------------------------------------
  • Total de proyectos / colecciones          : 15
  • Total de referencias procesadas           : 253
  • Desglose por tipo de documento            : journalArticle: 209, webpage: 14, bookSection: 13, conferencePaper: 9, book: 7, thesis: 1

--------------------------------------------------------------------------------
2. DISPONIBILIDAD DE PDFs Y ALMACENAMIENTO LOCAL
--------------------------------------------------------------------------------
  • Referencias en Sciwheel con PDF           : 243
  • Referencias en Sciwheel sin PDF (Normal)  : 10
  • PDFs disponibles localmente (descargados) : 242
  • PDFs faltantes por error/pendiente        : 1
  • Tamaño total de PDFs locales              : 914.46 MB (0.914 GB)

--------------------------------------------------------------------------------
3. ANÁLISIS DE NOTAS, ANOTACIONES Y GEOMETRÍA ESPACIAL
--------------------------------------------------------------------------------
  • Total de notas/comentarios registrados    : 685
    - Notas de biblioteca (WEB_LEAN_LIBRARY)  : 340
    - Anotaciones de PDF (PDF_NATIVE)         : 345
  • Referencias totales con notas/comentarios : 166
  • PDFs con notas                            : 153
  • PDFs con anotaciones (geometría espacial) : 111
  • PDFs con notas + anotaciones (híbridos)   : 103
  • Anotaciones sin nota asociada (huérfanas) : 64
  • Desglose de captura Playwright DOM        :
    - Evaluados por Playwright                : 3
    - Geometría capturada completa           : 2
    - Geometría capturada parcial            : 0
    - Sin geometría detectada (fallback)     : 1

--------------------------------------------------------------------------------
4. ARCHIVOS SUPLEMENTARIOS Y ADJUNTOS
--------------------------------------------------------------------------------
  • Total de adjuntos suplementarios detectados : 19
  • Adjuntos descargados localmente           : 19
  • Ítems con archivos suplementarios         : 12
  • PDFs con archivos suplementarios          : 12
  • Tamaño total de adjuntos locales          : 108.03 MB

--------------------------------------------------------------------------------
5. REGISTRO DE INCIDENCIAS Y ADVERTENCIAS
--------------------------------------------------------------------------------
  • Ítems fallidos en la extracción           : 0
  • Proyectos fallidos en la extracción       : 0
================================================================================

real    0m0,263s
user    0m0,208s
sys     0m0,056s
```

### Paso 4: Simulación Inicial de Importación (Dry-Run con Zotero Vacío)

Simulación previa para calcular todas las entidades que se escribirán en una base de datos limpia de Zotero:

```bash
$ time python3 zotero_importer.py --manifest ./Sciwheel_Data/hierarchy_and_metadata.json -v 1 --zotero-base-url http://127.0.0.1:23119/api/

[INFO]    📂 Manifest cargado: 253 ítems, 15 colecciones (extraído: 2026-09-28T22:41:41Z)
[INFO]    ⚠ 2 ítems excluidos antes de tocar la red (ver el reporte final para el detalle).
[INFO]    🔎 Consultando estado actual de Zotero (colecciones e ítems ya importados)...
[INFO]    🔎 0 de 15 colecciones del manifest ya existen en Zotero.
[INFO]    🔎 0 ítems ya marcados como importados en Zotero.

================================================================================
REPORTE DE SIMULACIÓN (dry-run) — no se escribió nada en Zotero
================================================================================
  • Manifest                                            : Sciwheel_Data/hierarchy_and_metadata.json
--------------------------------------------------------------------------------
1. COLECCIONES
--------------------------------------------------------------------------------
  • A crear                                                 : 15
  • Ya existen en Zotero                                    : 0
--------------------------------------------------------------------------------
2. ÍTEMS BIBLIOGRÁFICOS
--------------------------------------------------------------------------------
  • Nuevos a crear                                          : 251
  • Con novedades aditivas (tags/colecciones/notas/adjuntos): 0
  • Sin cambios (ya al día)                                 : 0
  • Excluidos antes de tocar la red                         : 2
  • Desglose por tipo de documento                          : journalArticle: 207, webpage: 14, bookSection: 13, conferencePaper: 9, book: 7, thesis: 1
--------------------------------------------------------------------------------
3. NOTAS Y ANOTACIONES
--------------------------------------------------------------------------------
  • Notas planas a crear                                    : 640
  • Anotaciones a crear (con geometría)                     : 345
  • Huérfanas subsumidas, descartadas                       : 72
  • Colores de anotación (mapeados a paleta Zotero)         : amarillo: 52, rojo: 18, azul: 3, verde: 1
--------------------------------------------------------------------------------
4. ADJUNTOS
--------------------------------------------------------------------------------
  • PDFs principales a subir                                : 242
  • Peso total de PDFs principales                          : 914.46 MB
  • Suplementarios a subir                                  : 19
  • Peso total de suplementarios                            : 108.03 MB
--------------------------------------------------------------------------------
5. INCIDENCIAS
--------------------------------------------------------------------------------
  • Archivos del manifest ausentes en disco                 : 0
================================================================================
Corré con -x / --execute para aplicar estos cambios.

real    0m4,120s
user    0m0,480s
sys     0m0,080s
```

### Paso 5: Importación Efectiva (`-x / --execute`)

Creación real de las colecciones, ítems bibliográficos, notas, anotaciones geométricas en PDF y archivos suplementarios en Zotero:

```bash
$ time python3 zotero_importer.py -x --manifest ./Sciwheel_Data/hierarchy_and_metadata.json -v 1 --zotero-base-url http://127.0.0.1:23119/api/
```

### Paso 6: Verificación Posterior de Idempotencia (Dry-Run Post-Importación)

Al re-ejecutar el importador en modo simulación sobre la misma biblioteca de Zotero, las etiquetas de control detectan la totalidad del contenido preexistente, garantizando **cero duplicados**:

```bash
$ time python3 zotero_importer.py --manifest ./Sciwheel_Data/hierarchy_and_metadata.json -v 1 --zotero-base-url http://127.0.0.1:23119/api/

[INFO]    📂 Manifest cargado: 253 ítems, 15 colecciones (extraído: 2026-09-28T22:41:41Z)
[INFO]    ⚠ 2 ítems excluidos antes de tocar la red (ver el reporte final para el detalle).
[INFO]    🔎 Consultando estado actual de Zotero (colecciones e ítems ya importados)...
[INFO]    🔎 15 de 15 colecciones del manifest ya existen en Zotero.
[INFO]    🔎 251 ítems ya marcados como importados en Zotero.

================================================================================
REPORTE DE SIMULACIÓN (dry-run) — no se escribió nada en Zotero
================================================================================
  • Manifest                                            : Sciwheel_Data/hierarchy_and_metadata.json
--------------------------------------------------------------------------------
1. COLECCIONES
--------------------------------------------------------------------------------
  • A crear                                                 : 0
  • Ya existen en Zotero                                    : 15
--------------------------------------------------------------------------------
2. ÍTEMS BIBLIOGRÁFICOS
--------------------------------------------------------------------------------
  • Nuevos a crear                                          : 0
  • Con novedades aditivas (tags/colecciones/notas/adjuntos): 12
  • Sin cambios (ya al día)                                 : 249
  • Excluidos antes de tocar la red                         : 2
  • Desglose por tipo de documento                          : journalArticle: 207, webpage: 14, bookSection: 13, conferencePaper: 9, book: 7, thesis: 1
--------------------------------------------------------------------------------
3. NOTAS Y ANOTACIONES
--------------------------------------------------------------------------------
  • Notas planas a crear                                    : 0
  • Anotaciones a crear (con geometría)                     : 0
  • Huérfanas subsumidas, descartadas                       : 72
  • Colores de anotación (mapeados a paleta Zotero)         : amarillo: 52, rojo: 18, azul: 3, verde: 1
--------------------------------------------------------------------------------
4. ADJUNTOS
--------------------------------------------------------------------------------
  • PDFs principales a subir                                : 0
  • Peso total de PDFs principales                          : 0.00 MB
  • Suplementarios a subir                                  : 19
  • Peso total de suplementarios                            : 108.03 MB
--------------------------------------------------------------------------------
5. INCIDENCIAS
--------------------------------------------------------------------------------
  • Archivos del manifest ausentes en disco                 : 0
================================================================================
Corré con -x / --execute para aplicar estos cambios.

real    0m4,860s
user    0m0,521s
sys     0m0,095s
```

---

## 🛡️ Idempotencia y Tolerancia a Fallos

El sistema está diseñado para fallar de forma segura y permitir reintentos parciales:

1. **Etiquetas de Control (Bookkeeping Tags):**
   El importador inyecta etiquetas automáticas en Zotero (`sciwheel-import`, `sciwheel-id:<ID>`, `sciwheel-note:<HASH>`, `sciwheel-att:<RES_ID>`). Esto permite consultar la API de Zotero antes de realizar cambios y saltar registros previamente procesados.

2. **Reconciliación Aditiva:**
   Si añades nuevas notas o adjuntos en Sciwheel y vuelves a ejecutar la migración, los ítems existentes en Zotero **no serán sobreescritos**; únicamente se les agregarán las novedades encontradas.

3. **Flujo `--retry-failed`:**
   Al finalizar cada importación real, se genera un archivo `informe_importacion_<timestamp>.json`. Si la conexión cae o un archivo superó el peso límite, puedes reintentar procesar únicamente los ítems fallidos:

   ```bash
   python3 zotero_importer.py -x \
     --manifest ./Sciwheel_Data/hierarchy_and_metadata.json \
     --retry-failed ./informe_importacion_20260928T224500Z.json
   ```

---

## 🤝 Contribución

¡Las contribuciones son más que bienvenidas! Si querés proponer mejoras, corregir un error o adaptar los scripts a nuevos esquemas de Sciwheel o Zotero:

1. Hacé un *Fork* del repositorio.
2. Creá una rama con tu nueva funcionalidad (`git checkout -b feature/nueva-funcionalidad`).
3. Realizá tus cambios y confirmá los *commits* (`git commit -m 'Añade nueva funcionalidad'`).
4. Subí la rama (`git push origin feature/nueva-funcionalidad`).
5. Abrí un *Pull Request* detallando las pruebas realizadas.

---
*Desarrollado con asistencia de vibe coding por Martín González Buitrón.*
