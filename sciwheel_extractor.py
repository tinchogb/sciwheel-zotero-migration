#!/usr/bin/env python3
"""
Sciwheel Data Extractor & Preparer (sciwheel_extractor.py)

Author: Martín González Buitrón
Development Note: Realizado mediante vibe coding.

Propósito:
    Extrae recursivamente metadatos, notas, estructura de carpetas, archivos PDF,
    archivos suplementarios/adjuntos y coordenadas geométricas de visualización
    desde Sciwheel (vía Playwright DOM), normalizándolas al espacio de puntos PDF
    (origen Bottom-Left) para generar un manifiesto .json consolidado compatible
    con el lector nativo de Zotero 10+.

    Soporta etiquetado dinámico de origen de anotaciones ('source_type': 'PDF_NATIVE' 
    o 'WEB_LEAN_LIBRARY'), deduplicación inteligente para ítems híbridos, filtrado 
    por proyecto vía '--projects', extracción geométrica incremental por paso de scroll,
    reintento focalizado de PDFs fallidos vía '--retry-failed-pdfs', normalización
    de fechas al estándar ISO 8601 (YYYY-MM-DD, YYYY-MM, YYYY) para compatibilidad CSL/Zotero,
    y generación de reportes analíticos independientes a partir de un archivo JSON vía '-r / --report'.
"""

import os
import re
import sys
import json
import zipfile
import io
import logging
import argparse
import time
import hashlib
import urllib.parse
import requests
from pathlib import Path
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import List, Dict, Set, Tuple, Optional, Any, Union

# Importaciones defensivas
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    import pypdf
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

UUID_REGEX = re.compile(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', re.IGNORECASE)
SCIWHEEL_FUTURE_EPOCH: float = 1790177360.0

SCIWHEEL_COLOR_MAP: Dict[str, str] = {
    'y': '#fff59d', 'g': '#a5d6a7', 'b': '#90caf9',
    'p': '#f48fb1', 'o': '#ffcc80', 'c': '#80deea',
    'r': '#ef9a9a', 'a': '#ff8a80',
}

SCI_TO_ZOTERO_TYPE_MAP: Dict[str, str] = {
    "journal-article": "journalArticle", "article-journal": "journalArticle",
    "journalarticle": "journalArticle", "article": "journalArticle",
    "book": "book", "monograph": "book", "book-chapter": "bookSection",
    "chapter": "bookSection", "paper-conference": "conferencePaper",
    "proceedings-article": "conferencePaper", "conference": "conferencePaper",
    "thesis": "thesis", "phdthesis": "thesis", "mastersthesis": "thesis",
    "preprint": "preprint", "report": "report", "techreport": "report",
    "webpage": "webpage", "website": "webpage", "patent": "patent",
    "dataset": "dataset"
}

ZOTERO_VALID_CREATORS: Dict[str, Set[str]] = {
    "journalArticle": {"author", "contributor", "translatedBy"},
    "book": {"author", "editor", "translator", "contributor", "seriesEditor"},
    "bookSection": {"author", "editor", "translator", "contributor", "bookAuthor"},
    "conferencePaper": {"author", "editor", "translator", "contributor"},
    "thesis": {"author", "contributor"},
    "preprint": {"author", "contributor"},
    "report": {"author", "editor", "translator", "contributor"},
    "webpage": {"author", "contributor"},
    "patent": {"inventor", "contributor"},
    "dataset": {"author", "contributor"}
}

JS_EXTRACTOR = """
() => {
    let anotacionesCazadas = [];
    let paginas = document.querySelectorAll(".page");
    
    paginas.forEach(pagina => {
        let nPag = parseInt(pagina.getAttribute("data-page-number") || "1");
        let canvasEl = pagina.querySelector("canvas");
        let pR = canvasEl ? canvasEl.getBoundingClientRect() : pagina.getBoundingClientRect();
        let pW = pR.width || 612;
        let pH = pR.height || 792;
        
        let marcas = pagina.querySelectorAll("div[style*='position: absolute'], span[style*='background'], [class*='sci'], [id*='sci']");
        
        marcas.forEach((marca, index) => {
            if (marca.className.includes("annotLink") || marca.querySelector("a")) return;
            
            let r = marca.getBoundingClientRect();
            let bg = window.getComputedStyle(marca).backgroundColor;
            
            if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent" && bg !== "rgb(255, 255, 255)") {
                let x1 = r.left - pR.left;
                let y1 = r.top - pR.top;
                let x2 = r.right - pR.left;
                let y2 = r.bottom - pR.top;
                
                if ((x2 - x1) > 0 && (y2 - y1) > 0) {
                    anotacionesCazadas.push({
                        pagina: nPag,
                        paginaWidth: pW,
                        paginaHeight: pH,
                        texto: (marca.innerText || marca.textContent || "").trim(),
                        bbox: [x1, y1, x2, y2],
                        color: bg
                    });
                }
            }
        });
    });

    if (anotacionesCazadas.length === 0) {
        document.querySelectorAll(".textLayer span, mark").forEach(span => {
            let bg = window.getComputedStyle(span).backgroundColor;
            if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "rgb(255, 255, 255)" && bg !== "transparent") {
                let paginaPadre = span.closest(".page");
                let nPag = paginaPadre ? parseInt(paginaPadre.getAttribute("data-page-number") || "1") : 1;
                let canvasPadre = paginaPadre ? paginaPadre.querySelector("canvas") : null;
                let pR = canvasPadre
                    ? canvasPadre.getBoundingClientRect()
                    : (paginaPadre ? paginaPadre.getBoundingClientRect() : { left: 0, top: 0, width: 612, height: 792 });
                let r = span.getBoundingClientRect();
                
                anotacionesCazadas.push({
                    pagina: nPag,
                    paginaWidth: pR.width || 612,
                    paginaHeight: pR.height || 792,
                    texto: (span.innerText || span.textContent || "").trim(),
                    bbox: [
                        r.left - pR.left,
                        r.top - pR.top,
                        r.right - pR.left,
                        r.bottom - pR.top
                    ],
                    color: bg
                });
            }
        });
    }

    return anotacionesCazadas;
}
"""


def generate_standalone_report(filepath: Union[str, Path]) -> None:
    """Genera e imprime un reporte analítico exhaustivo y fiel a partir de un archivo JSON manifiesto."""
    json_path = Path(filepath).expanduser().resolve()

    if not json_path.exists():
        print(f"\n❌ Error: No se encontró el archivo manifiesto JSON en: {json_path}\n")
        sys.exit(1)

    if not json_path.is_file():
        print(f"\n❌ Error: La ruta especificada no es un archivo válido: {json_path}\n")
        sys.exit(1)

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"\n❌ Error al leer o interpretar el archivo JSON ({json_path}): {e}\n")
        sys.exit(1)

    # Controles de validación estructural del JSON
    if not isinstance(data, dict):
        print(f"\n❌ Error: El archivo '{json_path.name}' no contiene una estructura JSON de tipo manifiesto/diccionario.\n")
        sys.exit(1)

    expected_keys = {"items", "collections", "manifest_version", "summary", "extractor_version"}
    if not any(k in data for k in expected_keys):
        print(f"\n❌ Error: El archivo '{json_path.name}' no parece ser un manifiesto generado por Sciwheel Extractor.")
        print("    No se detectaron las claves estructurales características ('items', 'collections', 'manifest_version', etc.).\n")
        sys.exit(1)

    print("\n================================================================================")
    print("               REPORTE ANALÍTICO DETALLADO DE MANIFIESTO JSON                   ")
    print("================================================================================")
    print(f"  • Archivo analizado                         : {json_path}")

    m_ver = data.get("manifest_version")
    e_ver = data.get("extractor_version")
    ext_at = data.get("extracted_at")

    print(f"  • Fecha/Hora de extracción                  : {ext_at if ext_at else '⚠️ No disponible'}")
    print(f"  • Versión del manifiesto / extractor        : v{m_ver if m_ver else '?'} / v{e_ver if e_ver else '?'}")

    # 1. RESUMEN GLOBAL DEL REPOSITORIO
    print("--------------------------------------------------------------------------------")
    print("1. RESUMEN GLOBAL DEL REPOSITORIO")
    print("--------------------------------------------------------------------------------")

    collections = data.get("collections")
    if collections is None or not isinstance(collections, list):
        print("  • Total de proyectos / colecciones          : ⚠️ Campo 'collections' ausente o no válido en el JSON")
    else:
        real_collections = [c for c in collections if isinstance(c, dict) and c.get("id") not in ("cat_private", "cat_shared")]
        print(f"  • Total de proyectos / colecciones          : {len(real_collections)}")

    items = data.get("items")
    items_available = isinstance(items, list)

    if not items_available:
        print("  • Total de referencias procesadas           : ⚠️ Campo 'items' ausente o no válido en el JSON")
        print("  • Desglose por tipo de documento            : ⚠️ No disponible")
    else:
        total_items = len(items)
        print(f"  • Total de referencias procesadas           : {total_items}")

        item_types_count: Dict[str, int] = {}
        for item in items:
            if isinstance(item, dict):
                itype = item.get("itemType", "journalArticle")
                item_types_count[itype] = item_types_count.get(itype, 0) + 1

        if item_types_count:
            types_formatted = ", ".join([f"{k}: {v}" for k, v in sorted(item_types_count.items(), key=lambda x: x[1], reverse=True)])
            print(f"  • Desglose por tipo de documento            : {types_formatted}")

    # 2. DISPONIBILIDAD DE PDFs Y ALMACENAMIENTO LOCAL
    print("\n--------------------------------------------------------------------------------")
    print("2. DISPONIBILIDAD DE PDFs Y ALMACENAMIENTO LOCAL")
    print("--------------------------------------------------------------------------------")

    if not items_available:
        print("  ⚠️ La información de PDFs no se puede calcular por ausencia del campo 'items'.")
    else:
        sciwheel_has_pdf_count = 0
        sciwheel_no_pdf_count = 0
        local_pdf_count = 0
        missing_pdf_count = 0
        total_pdf_bytes = 0

        for item in items:
            if not isinstance(item, dict):
                continue
            has_pdf_remote = item.get("sciwheel_has_pdf", False)
            if has_pdf_remote:
                sciwheel_has_pdf_count += 1
            else:
                sciwheel_no_pdf_count += 1

            pdf_meta = item.get("pdf")
            if pdf_meta and isinstance(pdf_meta, dict) and pdf_meta.get("path"):
                local_pdf_count += 1
                total_pdf_bytes += (pdf_meta.get("filesize") or 0)
            elif has_pdf_remote:
                missing_pdf_count += 1

        total_pdf_mb = total_pdf_bytes / (1024 * 1024)
        total_pdf_gb = total_pdf_bytes / (1024 * 1024 * 1024)

        print(f"  • Referencias en Sciwheel con PDF           : {sciwheel_has_pdf_count}")
        print(f"  • Referencias en Sciwheel sin PDF (Normal)  : {sciwheel_no_pdf_count}")
        print(f"  • PDFs disponibles localmente (descargados) : {local_pdf_count}")
        print(f"  • PDFs faltantes por error/pendiente        : {missing_pdf_count}")
        print(f"  • Tamaño total de PDFs locales              : {total_pdf_mb:.2f} MB ({total_pdf_gb:.3f} GB)")

    # 3. ANÁLISIS DE NOTAS, ANOTACIONES Y GEOMETRÍA ESPACIAL
    print("\n--------------------------------------------------------------------------------")
    print("3. ANÁLISIS DE NOTAS, ANOTACIONES Y GEOMETRÍA ESPACIAL")
    print("--------------------------------------------------------------------------------")

    if not items_available:
        print("  ⚠️️ La información de notas no se puede calcular por ausencia del campo 'items'.")
    else:
        total_notes_count = 0
        native_pdf_notes_count = 0
        web_lean_notes_count = 0

        items_with_notes = 0
        pdfs_with_notes = 0
        pdfs_with_annotations = 0
        pdfs_with_both = 0

        for item in items:
            if not isinstance(item, dict):
                continue
            pdf_meta = item.get("pdf")
            has_local_pdf = bool(pdf_meta and isinstance(pdf_meta, dict) and pdf_meta.get("path"))

            notes = item.get("notes", [])
            if not isinstance(notes, list):
                notes = []

            total_notes_count += len(notes)

            has_concept_notes = False
            has_spatial_annotations = False

            for n in notes:
                if not isinstance(n, dict):
                    continue
                stype = n.get("source_type")
                has_pos = "annotationPosition" in n
                if has_pos or stype == "PDF_NATIVE":
                    native_pdf_notes_count += 1
                    has_spatial_annotations = True
                else:
                    web_lean_notes_count += 1
                    has_concept_notes = True

            if len(notes) > 0:
                items_with_notes += 1

            if has_local_pdf:
                if len(notes) > 0:
                    pdfs_with_notes += 1
                if has_spatial_annotations:
                    pdfs_with_annotations += 1
                if has_concept_notes and has_spatial_annotations:
                    pdfs_with_both += 1

        print(f"  • Total de notas/comentarios registrados    : {total_notes_count}")
        print(f"    - Notas de biblioteca (WEB_LEAN_LIBRARY)  : {web_lean_notes_count}")
        print(f"    - Anotaciones de PDF (PDF_NATIVE)         : {native_pdf_notes_count}")
        print(f"  • Referencias totales con notas/comentarios : {items_with_notes}")
        print(f"  • PDFs con notas                            : {pdfs_with_notes}")
        print(f"  • PDFs con anotaciones (geometría espacial) : {pdfs_with_annotations}")
        print(f"  • PDFs con notas + anotaciones (híbridos)   : {pdfs_with_both}")

    unmatched_annotations = data.get("unmatched_annotations")
    if unmatched_annotations is None or not isinstance(unmatched_annotations, list):
        print("  • Anotaciones sin nota asociada (huérfanas) : ⚠️ Campo 'unmatched_annotations' ausente en el JSON")
    else:
        print(f"  • Anotaciones sin nota asociada (huérfanas) : {len(unmatched_annotations)}")

    summary_embedded = data.get("summary")
    if summary_embedded and isinstance(summary_embedded, dict):
        g_stats = summary_embedded.get("geometry_stats")
        if isinstance(g_stats, dict):
            print(f"  • Desglose de captura Playwright DOM        :")
            print(f"    - Evaluados por Playwright                : {g_stats.get('targeted', 0)}")
            print(f"    - Geometría capturada completa           : {g_stats.get('captured_full', 0)}")
            print(f"    - Geometría capturada parcial            : {g_stats.get('captured_partial', 0)}")
            print(f"    - Sin geometría detectada (fallback)     : {g_stats.get('failed_fallback', 0)}")
        else:
            print("  • Desglose de captura Playwright DOM        : ⚠️ Campo 'geometry_stats' ausente en el resumen embebido")
    else:
        print("  • Desglose de captura Playwright DOM        : ⚠️ Resumen embebido ('summary') ausente en el JSON")

    # 4. ARCHIVOS SUPLEMENTARIOS Y ADJUNTOS
    print("\n--------------------------------------------------------------------------------")
    print("4. ARCHIVOS SUPLEMENTARIOS Y ADJUNTOS")
    print("--------------------------------------------------------------------------------")

    if not items_available:
        print("  ⚠️ La información de adjuntos no se puede calcular por ausencia del campo 'items'.")
    else:
        total_attachments = 0
        local_attachments = 0
        total_attachment_bytes = 0
        items_with_attachments = 0
        pdfs_with_attachments = 0

        for item in items:
            if not isinstance(item, dict):
                continue
            pdf_meta = item.get("pdf")
            has_local_pdf = bool(pdf_meta and isinstance(pdf_meta, dict) and pdf_meta.get("path"))

            attachments = item.get("attachments", [])
            if not isinstance(attachments, list):
                attachments = []

            total_attachments += len(attachments)

            for att in attachments:
                if isinstance(att, dict) and att.get("path"):
                    local_attachments += 1
                    total_attachment_bytes += (att.get("filesize") or 0)

            if len(attachments) > 0:
                items_with_attachments += 1
                if has_local_pdf:
                    pdfs_with_attachments += 1

        total_att_mb = total_attachment_bytes / (1024 * 1024)

        print(f"  • Total de adjuntos suplementarios detectados : {total_attachments}")
        print(f"  • Adjuntos descargados localmente           : {local_attachments}")
        print(f"  • Ítems con archivos suplementarios         : {items_with_attachments}")
        print(f"  • PDFs con archivos suplementarios          : {pdfs_with_attachments}")
        print(f"  • Tamaño total de adjuntos locales          : {total_att_mb:.2f} MB")

    # 5. REGISTRO DE INCIDENCIAS Y ADVERTENCIAS
    print("\n--------------------------------------------------------------------------------")
    print("5. REGISTRO DE INCIDENCIAS Y ADVERTENCIAS")
    print("--------------------------------------------------------------------------------")

    failed_items = data.get("failed_items")
    if failed_items is None or not isinstance(failed_items, list):
        print("  • Ítems fallidos en la extracción           : ⚠️ Campo 'failed_items' ausente en el JSON")
    else:
        print(f"  • Ítems fallidos en la extracción           : {len(failed_items)}")

    failed_projects = data.get("failed_projects")
    if failed_projects is None or not isinstance(failed_projects, list):
        print("  • Proyectos fallidos en la extracción       : ⚠️ Campo 'failed_projects' ausente en el JSON")
    else:
        print(f"  • Proyectos fallidos en la extracción       : {len(failed_projects)}")

    print("================================================================================\n")


def get_pdf_page_dimensions(pdf_path: Optional[Union[str, Path]], page_index: int) -> Tuple[float, float]:
    """Obtiene las dimensiones reales del MediaBox de la página del PDF en puntos tipográficos (72 DPI)."""
    if pdf_path and Path(pdf_path).exists():
        if PYPDF_AVAILABLE:
            try:
                reader = pypdf.PdfReader(str(pdf_path))
                if page_index < len(reader.pages):
                    page = reader.pages[page_index]
                    box = page.mediabox
                    return float(box.width), float(box.height)
            except Exception:
                pass
        try:
            with open(pdf_path, "rb") as f:
                content = f.read(200000)
                match = re.search(rb'/MediaBox\s*\[\s*0\s+0\s+([0-9\.]+)\s+([0-9\.]+)\s*\]', content)
                if match:
                    return float(match.group(1)), float(match.group(2))
        except Exception:
            pass
    return 612.0, 792.0


def transform_sciwheel_to_zotero_rects(
    raw_rects: List[List[float]],
    page_pdf_w: float,
    page_pdf_h: float,
    canvas_w: float,
    canvas_h: float
) -> Tuple[List[List[float]], List[float]]:
    scale_x = (page_pdf_w / canvas_w) if canvas_w > 0 else 1.0
    scale_y = (page_pdf_h / canvas_h) if canvas_h > 0 else 1.0

    zotero_rects = []
    all_x, all_y = [], []

    for r in raw_rects:
        left, top, right, bottom = r
        x1 = round(left * scale_x, 2)
        x2 = round(right * scale_x, 2)
        y1 = round((canvas_h - bottom) * scale_y, 2)
        y2 = round((canvas_h - top) * scale_y, 2)

        xmin, xmax = min(x1, x2), max(x1, x2)
        ymin, ymax = min(y1, y2), max(y1, y2)

        zotero_rects.append([xmin, ymin, xmax, ymax])
        all_x.extend([xmin, xmax])
        all_y.extend([ymin, ymax])

    bounding_box = [min(all_x), min(all_y), max(all_x), max(all_y)] if all_x and all_y else [0, 0, 0, 0]
    return zotero_rects, bounding_box


def map_sciwheel_type_to_zotero(raw_type: Optional[str]) -> Tuple[str, bool]:
    if not raw_type:
        return "journalArticle", True
    clean = str(raw_type).strip().lower().replace("_", "-").replace(" ", "-")
    if clean in SCI_TO_ZOTERO_TYPE_MAP:
        return SCI_TO_ZOTERO_TYPE_MAP[clean], False
    return "journalArticle", True


def extract_creators_from_reference(ref_data: Dict[str, Any], zotero_type: str = "journalArticle") -> List[Dict[str, str]]:
    creators: List[Dict[str, str]] = []
    authors_list = ref_data.get("authors") or ref_data.get("author") or ref_data.get("creators") or []
    valid_roles = ZOTERO_VALID_CREATORS.get(zotero_type, {"author", "contributor"})

    if isinstance(authors_list, list):
        for a in authors_list:
            if isinstance(a, dict):
                first = a.get("firstName") or a.get("given") or a.get("firstNames") or ""
                last = a.get("lastName") or a.get("family") or a.get("surname") or ""
                single_name = a.get("name") or a.get("literal") or ""
                role = a.get("creatorType") or a.get("role") or "author"

                if zotero_type == "patent":
                    role = "inventor"
                elif role not in valid_roles:
                    role = "author" if "author" in valid_roles else list(valid_roles)[0]

                if first.strip() or last.strip():
                    creators.append({"creatorType": role, "firstName": first.strip(), "lastName": last.strip()})
                elif single_name.strip():
                    creators.append({"creatorType": role, "name": single_name.strip()})
            elif isinstance(a, str) and a.strip():
                role = "inventor" if zotero_type == "patent" else "author"
                creators.append({"creatorType": role, "name": a.strip()})
    return creators


def _format_date_parts(parts: List[Any]) -> str:
    """Formatea una lista de componentes de fecha [YYYY, MM, DD] a la norma ISO 8601."""
    try:
        clean_parts = []
        for p in parts:
            if isinstance(p, (int, float)):
                clean_parts.append(int(p))
            elif isinstance(p, str) and p.strip().isdigit():
                clean_parts.append(int(p.strip()))

        if not clean_parts:
            return ""

        year = clean_parts[0]
        if year < 100:
            year += 2000
        elif year < 1000 or year > 2100:
            return ""

        res = f"{year:04d}"
        if len(clean_parts) >= 2:
            month = clean_parts[1]
            if 1 <= month <= 12:
                res += f"-{month:02d}"
                if len(clean_parts) >= 3:
                    day = clean_parts[2]
                    if 1 <= day <= 31:
                        res += f"-{day:02d}"
        return res
    except Exception:
        return ""


def parse_iso_date(raw_date: Any) -> str:
    """
    Normaliza datos de fecha polimórficos provenientes de Sciwheel / CSL-JSON
    al estándar internacional ISO 8601 (YYYY-MM-DD, YYYY-MM, o YYYY).
    """
    if not raw_date:
        return ""

    # 1. Si es un diccionario CSL-JSON p.ej: {'date-parts': [[2010, 8, 23]]} o {year: 2010, month: 8}
    if isinstance(raw_date, dict):
        date_parts = raw_date.get("date-parts") or raw_date.get("date_parts")
        if date_parts and isinstance(date_parts, list):
            if len(date_parts) > 0 and isinstance(date_parts[0], list):
                parts = date_parts[0]
            else:
                parts = date_parts
            return _format_date_parts(parts)

        y = raw_date.get("year") or raw_date.get("y")
        m = raw_date.get("month") or raw_date.get("m")
        d = raw_date.get("day") or raw_date.get("d")
        if y:
            parts = [y]
            if m:
                parts.append(m)
                if d:
                    parts.append(d)
            return _format_date_parts(parts)

    # 2. Si es una lista directa de componentes de fecha p.ej: [2010, 8, 23]
    if isinstance(raw_date, list):
        if len(raw_date) > 0 and isinstance(raw_date[0], list):
            return _format_date_parts(raw_date[0])
        return _format_date_parts(raw_date)

    # 3. Si es un número (Año simple ej. 2010)
    if isinstance(raw_date, (int, float)):
        val = int(raw_date)
        if 1000 <= val <= 2100:
            return str(val)

    # 4. Si es una cadena de texto
    date_str = str(raw_date).strip()
    if not date_str:
        return ""

    # Si la cadena contiene la estructura JSON / dict CSL serializada en texto (ej: "{'date-parts': [[2010, 8, 23]]}")
    if "date-parts" in date_str or "date_parts" in date_str:
        try:
            json_friendly = date_str.replace("'", '"')
            parsed_dict = json.loads(json_friendly)
            return parse_iso_date(parsed_dict)
        except Exception:
            nums = re.findall(r'\b\d+\b', date_str)
            if nums:
                return _format_date_parts(nums)

    # Extraer patrón ISO YYYY-MM-DD, YYYY-MM o YYYY usando Expresiones Regulares
    iso_match = re.search(r'(\b\d{4}\b)(?:[-/.](\d{1,2}))?(?:[-/.](\d{1,2}))?', date_str)
    if iso_match:
        y, m, d = iso_match.groups()
        parts = [int(y)]
        if m:
            parts.append(int(m))
            if d:
                parts.append(int(d))
        return _format_date_parts(parts)

    return ""


def extract_zotero_fields(ref_data: Dict[str, Any]) -> Dict[str, str]:
    fields: Dict[str, str] = {}

    # Búsqueda polimórfica de la fecha en CSL-JSON o API de Sciwheel
    raw_date = (
        ref_data.get("issued") or
        ref_data.get("publishedDate") or
        ref_data.get("publishedYear") or
        ref_data.get("published-date") or
        ref_data.get("date")
    )
    normalized_date = parse_iso_date(raw_date)

    mapping = {
        "publicationTitle": ref_data.get("journalName") or ref_data.get("journalAbbreviation") or ref_data.get("container-title"),
        "bookTitle": ref_data.get("bookTitle"),
        "proceedingsTitle": ref_data.get("proceedingsTitle"),
        "volume": ref_data.get("volume"),
        "issue": ref_data.get("issue") or ref_data.get("number"),
        "pages": ref_data.get("pagination") or ref_data.get("page") or ref_data.get("pages"),
        "date": normalized_date,
        "DOI": ref_data.get("DOI") or ref_data.get("doi"),
        "abstractNote": ref_data.get("abstract") or ref_data.get("abstractText"),
        "publisher": ref_data.get("publisher"),
        "place": ref_data.get("place") or ref_data.get("publisher-place") or ref_data.get("address"),
        "edition": ref_data.get("edition"),
        "ISBN": ref_data.get("ISBN") or ref_data.get("isbn"),
        "ISSN": ref_data.get("ISSN") or ref_data.get("issn"),
        "url": ref_data.get("url") or ref_data.get("URL"),
        "university": ref_data.get("university") or ref_data.get("school")
    }

    for z_key, val in mapping.items():
        if val is not None:
            str_val = str(val).strip()
            if str_val:
                fields[z_key] = str_val
    return fields


def calculate_pdf_metadata(pdf_path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if not pdf_path or not pdf_path.exists():
        return None
    try:
        stat = pdf_path.stat()
        md5_hash = hashlib.md5()
        with open(pdf_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                md5_hash.update(chunk)

        mtime_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        return {
            "path": str(pdf_path.resolve()),
            "md5": md5_hash.hexdigest(),
            "filesize": stat.st_size,
            "mtime": mtime_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
    except Exception:
        return None


def build_note_fingerprint(note_text: Optional[str]) -> str:
    if not note_text:
        return ""
    return re.sub(r'\s+', ' ', str(note_text)).strip().lower()


def build_note_hash(note_html: str) -> str:
    fp = build_note_fingerprint(note_html)
    md5_val = hashlib.md5(fp.encode("utf-8")).hexdigest()
    return f"sciwheel-note:{md5_val}"


def parse_sciwheel_color_to_hex(color_code: Optional[str]) -> str:
    if not color_code:
        return "#fff59d"
    c_clean = str(color_code).strip().lower()
    if c_clean in SCIWHEEL_COLOR_MAP:
        return SCIWHEEL_COLOR_MAP[c_clean]
    rgba_match = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", c_clean)
    if rgba_match:
        r, g, b = (int(x) for x in rgba_match.groups())
        return f"#{r:02x}{g:02x}{b:02x}"
    if c_clean.startswith("#"):
        return c_clean
    if len(c_clean) in (3, 6) and all(ch in '0123456789abcdef' for ch in c_clean):
        return f"#{c_clean}"
    return "#fff59d"


def convert_sciwheel_timestamp(ts: Optional[Union[float, str, int]]) -> Tuple[Optional[str], Optional[str]]:
    if not ts:
        return None, None
    try:
        ts_val = float(ts)
        if ts_val > 1e11:
            unix_ts = ts_val / 1000.0
        elif ts_val > 1e9:
            unix_ts = ts_val
        else:
            unix_ts = SCIWHEEL_FUTURE_EPOCH - ts_val

        dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return None, None


def sanitize_filename(name: str) -> str:
    unquoted = urllib.parse.unquote(str(name))
    cleaned = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', unquoted)
    return cleaned[:180]


def normalize_project_name(text: Optional[str]) -> str:
    if not text:
        return ""
    clean = str(text).replace(r'\_', '_').replace('\\', '').strip().lower()
    return re.sub(r'[^a-z0-9]', '', clean)


def normalize_path(path_str: Optional[str]) -> str:
    if not path_str:
        return ""
    clean = str(path_str).replace(r'\_', '_').replace('\\', '').strip().lower()
    parts = [re.sub(r'[^a-z0-9]', '', p) for p in clean.split('/') if p.strip()]
    return "/".join(parts)


def clean_pdfjs_text(text: str) -> str:
    if not text:
        return ""
    texto_limpio = re.sub(r'\b(fl|fi|ff)\s+', r'\1', str(text))
    return re.sub(r'\s+', ' ', texto_limpio).strip()


def normalize_for_matching(text: str) -> str:
    if not text:
        return ""
    text_clean = re.sub(r'<[^>]+>', ' ', str(text))
    text_clean = text_clean.lower()
    text_clean = text_clean.replace("\u00ad", "")
    text_clean = re.sub(r"[\u2018\u2019]", "'", text_clean)
    text_clean = re.sub(r"[\u201c\u201d]", '"', text_clean)
    text_clean = re.sub(r"\s+", "", text_clean)
    return text_clean


def match_annotation_to_note(
    annotation_text: str, 
    candidate_notes: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], float, List[Dict[str, Any]]]:
    """
    Compara el texto de una anotación con las notas candidatas.
    Devuelve:
      - matched_note: nota seleccionada si ratio >= 0.90 o coincidencia directa, o None.
      - best_ratio: ratio máximo alcanzado (float).
      - best_candidate_notes: lista de notas candidatas que alcanzaron best_ratio.
    """
    target = normalize_for_matching(annotation_text)
    if not target:
        return None, 0.0, []

    best_notes: List[Dict[str, Any]] = []
    best_ratio = 0.0

    for note in candidate_notes:
        candidate_raw = note.get("raw_quote") or note.get("html") or ""
        candidate = normalize_for_matching(candidate_raw)
        if not candidate:
            continue

        if candidate == target or target in candidate or candidate in target:
            return note, 1.0, [note]

        ratio = SequenceMatcher(None, candidate, target).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_notes = [note]
        elif ratio == best_ratio and ratio > 0.0:
            best_notes.append(note)

    if best_notes and best_ratio >= 0.90:
        return best_notes[0], best_ratio, best_notes

    return None, best_ratio, best_notes


def parse_cookie_string_for_playwright(cookie_raw: str) -> List[Dict[str, str]]:
    cookies = []
    if "=" not in cookie_raw:
        cookies.append({
            "name": "JSESSIONID",
            "value": cookie_raw.strip(),
            "url": "https://sciwheel.com"
        })
        return cookies

    for item in cookie_raw.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "url": "https://sciwheel.com"
        })
    return cookies


def deduplicar_fragmentos(fragmentos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not fragmentos:
        return []
    
    vistos = set()
    dedup = []
    for f in fragmentos:
        pag = f.get("pagina", 1)
        txt_norm = normalize_for_matching(f.get("texto", ""))
        bbox = f.get("bbox", [0, 0, 0, 0])
        bbox_key = tuple(round(float(x), 1) for x in bbox)
        color = str(f.get("color", "")).strip().lower()

        clave = (pag, txt_norm, bbox_key, color)
        if clave not in vistos:
            vistos.add(clave)
            dedup.append(f)
            
    return dedup


def agrupar_fragmentos_dom(fragmentos: List[Dict[str, Any]], max_gap_y: int = 25) -> List[Dict[str, Any]]:
    if not fragmentos:
        return []
    
    anotaciones = []
    actual = None

    for f in fragmentos:
        if actual is None:
            actual = {
                "pagina": f["pagina"],
                "paginaWidth": f.get("paginaWidth", 612),
                "paginaHeight": f.get("paginaHeight", 792),
                "color": f["color"],
                "texto": f["texto"],
                "rects": [f["bbox"]]
            }
            continue

        mismo_bloque = (
            f["pagina"] == actual["pagina"] and
            f["color"] == actual["color"] and
            abs(f["bbox"][1] - actual["rects"][-1][3]) <= max_gap_y
        )

        if mismo_bloque:
            if f["texto"]:
                actual["texto"] += (" " if actual["texto"] else "") + f["texto"]
            actual["rects"].append(f["bbox"])
        else:
            actual["texto"] = clean_pdfjs_text(actual["texto"])
            anotaciones.append(actual)
            actual = {
                "pagina": f["pagina"],
                "paginaWidth": f.get("paginaWidth", 612),
                "paginaHeight": f.get("paginaHeight", 792),
                "color": f["color"],
                "texto": f["texto"],
                "rects": [f["bbox"]]
            }

    if actual:
        actual["texto"] = clean_pdfjs_text(actual["texto"])
        anotaciones.append(actual)

    return anotaciones


def extract_tags_from_reference(ref_data: Dict[str, Any]) -> Set[str]:
    tags: Set[str] = set()
    sources = [
        ref_data.get("tags"), ref_data.get("tagsList"),
        ref_data.get("projectTags"), ref_data.get("collectionTags"),
        ref_data.get("keywords"),
        ref_data.get("custom", {}).get("tags") if isinstance(ref_data.get("custom"), dict) else None
    ]

    for val in sources:
        if not val:
            continue
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    tag_name = item.get("name") or item.get("tag") or item.get("tagName")
                    if tag_name:
                        tags.add(str(tag_name).strip())
                elif isinstance(item, str):
                    tags.add(item.strip())
        elif isinstance(val, str):
            for t in val.replace(";", ",").split(","):
                if t.strip():
                    tags.add(t.strip())
    return tags


def configure_logger(verbose_level: int) -> logging.Logger:
    logger = logging.getLogger("SciwheelExtractor")
    logger.setLevel(logging.DEBUG)
    if logger.hasHandlers():
        logger.handlers.clear()

    ch = logging.StreamHandler(sys.stdout)
    if verbose_level == 0:
        ch.setLevel(logging.WARNING)
        formatter = logging.Formatter('%(message)s')
    elif verbose_level == 1:
        ch.setLevel(logging.INFO)
        formatter = logging.Formatter('[INFO] %(message)s')
    else:
        ch.setLevel(logging.DEBUG)
        formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(name)s: %(message)s')

    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger


def parse_cli_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extractor de datos de Sciwheel: Descarga metadatos, PDFs, adjuntos y coordenadas geométricas de anotaciones para Zotero 10+."
    )
    parser.add_argument("-t", "--token", type=str, default=None, help="Cookie de sesión o Bearer token de Sciwheel.")
    parser.add_argument("-P", "--projects", nargs="+", type=str, default=None, help="Filtro de proyectos por nombre, ID o ruta.")
    parser.add_argument("--poc", action="store_true", help="Modo PoC (limitado a 2 proyectos).")
    parser.add_argument("--skip-bib-backup", action="store_true", help="Omite la descarga de archivos .bib.")
    parser.add_argument("--retry-failed-pdfs", action="store_true", help="Reintenta únicamente la descarga de PDFs para referencias con 'sciwheel_has_pdf': true cuyo campo 'pdf' sea null, omitiendo backup de .bib.")
    parser.add_argument("-r", "--report", type=str, default=None, metavar="FILEPATH", help="Genera e imprime un reporte analítico detallado directamente a partir del archivo JSON especificado, sin realizar peticiones de red.")
    parser.add_argument("-o", "--output-dir", type=str, default="~/Downloads/Sciwheel_Data", help="Directorio de salida.")
    parser.add_argument("--json-output", type=str, default="hierarchy_and_metadata.json", help="Nombre del JSON de salida.")
    
    parser.add_argument("--skip-geometry", action="store_true", help="Desactiva la captura de coordenadas geométricas.")
    parser.add_argument("--headed", action="store_true", help="Ejecuta Playwright en modo visible para depuración.")
    parser.add_argument("--playwright-timeout", type=int, default=25, help="Timeout en segundos por documento.")

    parser.add_argument("--pdf-chunk-size", type=int, default=15, help="Lote para descargas de PDFs.")
    parser.add_argument("--bib-chunk-size", type=int, default=100, help="Lote para descargas de BibTeX.")
    parser.add_argument("--max-retries", type=int, default=3, help="Reintentos por solicitud de red.")
    parser.add_argument("-p", "--page-size", type=int, default=50, help="Tamaño de página API.")
    parser.add_argument("-v", "--verbose", type=int, default=1, choices=[0, 1, 2], help="Nivel de verbosidad.")
    parser.add_argument("--timeout-api", type=int, default=20, help="Timeout para API REST.")
    parser.add_argument("--timeout-export", type=int, default=180, help="Timeout para exportaciones.")
    parser.add_argument("--user-agent", type=str, default="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/151.0.0.0", help="User-Agent HTTP.")
    parser.add_argument("--base-url", type=str, default="https://sciwheel.com/work/api", help="URL base de la API REST.")
    parser.add_argument("--csl-url", type=str, default="https://sciwheel.com/api/v1/csl/references", help="URL base CSL.")

    args = parser.parse_args()

    if args.report:
        if args.token or args.projects or args.retry_failed_pdfs or args.poc:
            parser.error("El flag '-r / --report' es independiente y no debe combinarse con '--token', '--projects', '--retry-failed-pdfs' ni '--poc'.")
    else:
        if not args.token:
            parser.error("El argumento '-t / --token' es obligatorio salvo que se especifique el flag '-r / --report'.")

    args.output_dir = Path(args.output_dir).expanduser().resolve()
    return args


class SciwheelClient:
    def __init__(self, args: argparse.Namespace, logger: logging.Logger) -> None:
        self.args = args
        self.logger = logger
        self.session = requests.Session()
        
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": args.user_agent,
            "Referer": "https://sciwheel.com/work/"
        }
        
        auth_str = (args.token or "").strip()
        if "=" in auth_str or ";" in auth_str:
            headers["Cookie"] = auth_str
        elif auth_str:
            headers["Authorization"] = auth_str if auth_str.startswith("Bearer ") else f"Bearer {auth_str}"
            
        self.session.headers.update(headers)

    def execute_http_request(self, method: str, url: str, max_retries: int = 3, **kwargs: Any) -> Optional[requests.Response]:
        for attempt in range(1, max_retries + 1):
            try:
                res = self.session.request(method, url, **kwargs)
                if res.status_code in (401, 403):
                    self.logger.error("Error 401/403: Autenticación rechazada. Cookie/Token vencido.")
                    sys.exit(1)
                if res.status_code == 200:
                    return res
            except requests.RequestException as e:
                self.logger.warning(f"\t ⚠️ Intento {attempt}/{max_retries} fallido para {url}: {e}")
                if attempt == max_retries: raise
                time.sleep(2 * attempt)
        return None

    def get_owned_projects(self) -> List[Dict[str, Any]]:
        res = self.execute_http_request("GET", f"{self.args.base_url}/collection/ownedList", timeout=self.args.timeout_api)
        return res.json() if res else []

    def get_shared_projects(self) -> List[Dict[str, Any]]:
        res = self.execute_http_request("GET", f"{self.args.base_url}/collection/sharedList", timeout=self.args.timeout_api)
        return res.json() if res else []

    def get_collection_references(self, project_id: int) -> List[Dict[str, Any]]:
        all_refs: List[Dict[str, Any]] = []
        page = 1
        while True:
            payload = {
                "addedByMe": None, "clinicalTrial": None, "collectionId": int(project_id),
                "fieldCriteria": [], "review": None, "sortBy": "addedDate", "sortOrder": "desc",
                "systematicReview": None, "tagIds": None, "withNotes": None, "withPdf": None,
                "withMissingCitationData": None, "withoutTags": None, "query": None,
                "paginator": {"currentPage": page, "pageSize": self.args.page_size}
            }
            res = self.execute_http_request("POST", self.args.csl_url, json=payload, timeout=self.args.timeout_api)
            if not res: break
            data = res.json()
            refs = data.get("references", [])
            if not refs: break
            all_refs.extend(refs)
            if len(refs) < self.args.page_size: break
            page += 1
        return all_refs

    def get_item_details(self, item_id: Union[int, str]) -> Optional[Dict[str, Any]]:
        url = f"{self.args.base_url}/items/{item_id}"
        res = self.execute_http_request("GET", url, timeout=self.args.timeout_api)
        if not res: return None
        try:
            return res.json()
        except Exception:
            return None

    def get_item_collection_paths(self, item_id: Union[int, str]) -> List[str]:
        url = f"{self.args.base_url}/item/{item_id}/collections"
        res = self.execute_http_request("GET", url, timeout=self.args.timeout_api)
        if not res: return []
        try:
            payload = res.json()
            paths: List[str] = []
            for col in payload.get('privateCollections', []):
                fp = col.get('fullPath')
                if fp: paths.append(f"Private_Projects/{fp.strip('/')}")
            for col in payload.get('sharedCollections', []):
                fp = col.get('fullPath')
                if fp: paths.append(f"Shared_Projects/{fp.strip('/')}")
            return paths
        except Exception: return []

    def get_project_comments(self, project_id: int) -> List[Dict[str, Any]]:
        comments: List[Dict[str, Any]] = []
        page = 1
        seen_ids: Set[Any] = set()
        while True:
            url = f"{self.args.base_url}/collection/{project_id}/comments?page={page}&query=&resultsPerPage={self.args.page_size}&useAuthorsFromSolr=true"
            res = self.execute_http_request("GET", url, timeout=self.args.timeout_api)
            if not res: break
            data = res.json()
            items = data if isinstance(data, list) else data.get("displayedItems", [])
            if not items: break
            added_in_page = 0
            for item in items:
                if isinstance(item, dict):
                    c_id = item.get("id")
                    if c_id and c_id not in seen_ids:
                        seen_ids.add(c_id)
                        comments.append(item)
                        added_in_page += 1
            if added_in_page == 0 or len(items) < self.args.page_size: break
            page += 1
        return comments

    def get_item_comments(self, item_id: Union[int, str]) -> List[Dict[str, Any]]:
        url = f"{self.args.base_url}/items/{item_id}/comments"
        res = self.execute_http_request("GET", url, timeout=self.args.timeout_api)
        if not res: return []
        try:
            data = res.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def download_resource_file(self, sci_id: str, resource_id: Union[int, str], target_dir: Path, attachment_meta: Dict[str, Any]) -> Tuple[bool, Optional[Path]]:
        candidate_urls = [
            f"{self.args.base_url}/items/{sci_id}/resources/{resource_id}",
            f"{self.args.base_url}/item/{sci_id}/resources/{resource_id}/download",
            f"{self.args.base_url}/resources/{resource_id}/download"
        ]

        for url in candidate_urls:
            for attempt in range(1, self.args.max_retries + 1):
                try:
                    with self.session.get(url, stream=True, allow_redirects=True, timeout=self.args.timeout_export) as res:
                        if res.status_code == 200:
                            current_fname = attachment_meta.get("filename")
                            if not current_fname or current_fname.startswith("attachment_"):
                                cd = res.headers.get("Content-Disposition", "")
                                cd_match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)["\']?', cd, re.IGNORECASE)
                                if cd_match:
                                    current_fname = cd_match.group(1)
                                else:
                                    url_path_name = Path(urllib.parse.urlparse(res.url).path).name
                                    if url_path_name and "." in url_path_name:
                                        current_fname = url_path_name

                            if not current_fname:
                                current_fname = f"attachment_{resource_id}"

                            attachment_meta["filename"] = current_fname
                            attachment_meta["title"] = attachment_meta.get("title") or current_fname

                            clean_fname = sanitize_filename(current_fname)
                            target_filename = f"{resource_id}_{clean_fname}"
                            target_path = target_dir / target_filename

                            target_dir.mkdir(parents=True, exist_ok=True)
                            with open(target_path, "wb") as f:
                                for chunk in res.iter_content(chunk_size=65536):
                                    if chunk:
                                        f.write(chunk)

                            return True, target_path
                        elif res.status_code in (401, 403):
                            self.logger.error("Error 401/403: Autenticación rechazada descargando adjunto.")
                            sys.exit(1)
                except requests.RequestException as e:
                    self.logger.warning(f"\t ⚠️ Intento {attempt}/{self.args.max_retries} fallido para adjunto {resource_id} vía {url}: {e}")
                time.sleep(1.5 * attempt)
        return False, None

    def download_pdf_direct_resource(self, sci_id: str, pdf_resource_id: Union[int, str], target_dir: Path) -> Tuple[bool, Optional[Path]]:
        """Fallback directo al endpoint de recurso individual para recuperar el PDF sin usar /export."""
        candidate_urls = [
            f"{self.args.base_url}/items/{sci_id}/resources/{pdf_resource_id}",
            f"{self.args.base_url}/item/{sci_id}/resources/{pdf_resource_id}/download",
            f"{self.args.base_url}/resources/{pdf_resource_id}/download"
        ]
        for url in candidate_urls:
            for attempt in range(1, self.args.max_retries + 1):
                try:
                    res = self.session.get(url, timeout=self.args.timeout_export)
                    if res.status_code == 200 and res.content:
                        if res.content.startswith(b"%PDF") or "application/pdf" in res.headers.get("Content-Type", "").lower():
                            target_dir.mkdir(parents=True, exist_ok=True)
                            target_path = target_dir / f"{sci_id}.pdf"
                            with open(target_path, "wb") as f:
                                f.write(res.content)
                            return True, target_path
                except requests.RequestException as e:
                    self.logger.warning(f"\t ⚠️ Falló descarga directa de PDF {pdf_resource_id} para ítem {sci_id}: {e}")
                time.sleep(1.5 * attempt)
        return False, None

    def _download_export_batch(self, item_ids: List[int], export_type: str, batch_label: str) -> Optional[bytes]:
        url = f"{self.args.base_url}/export"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        payload = {
            "itemIds": ",".join(map(str, item_ids)),
            "exportType": export_type,
            "fileName": f"export_{sanitize_filename(batch_label)}",
            "excludeMissingCitationData": "false",
            "exportJournalAbbr": "false"
        }
        for attempt in range(1, self.args.max_retries + 1):
            try:
                res = self.session.post(url, headers=headers, data=payload, timeout=self.args.timeout_export)
                if res.status_code == 200 and res.content:
                    if export_type == "PDF":
                        if res.content.startswith(b"%PDF"):
                            return res.content
                        try:
                            with zipfile.ZipFile(io.BytesIO(res.content)) as z: _ = z.namelist()
                            return res.content
                        except zipfile.BadZipFile:
                            self.logger.warning(f"\t ⚠️ Intento {attempt}/{self.args.max_retries} fallido: la respuesta no es un ZIP ni un PDF válido para {batch_label}.")
                    else:
                        return res.content
            except requests.RequestException as e:
                self.logger.warning(f"\t ⚠️ Intento {attempt}/{self.args.max_retries} fallido para {batch_label}: {e}")
            time.sleep(2 * attempt)
        return None

    def download_data_in_batches(self, item_ids: List[int], export_type: str, chunk_size: int) -> List[Tuple[List[int], bytes]]:
        if not item_ids: return []
        chunks_data: List[Tuple[List[int], bytes]] = []
        total_chunks = (len(item_ids) + chunk_size - 1) // chunk_size

        for i in range(0, len(item_ids), chunk_size):
            chunk = item_ids[i:i + chunk_size]
            chunk_num = (i // chunk_size) + 1
            batch_label = f"batch_{chunk_num}"

            content = self._download_export_batch(chunk, export_type, batch_label)
            if content:
                self.logger.debug(f"\t   🪲 [HTTP 200] /export ({export_type}) Lote {chunk_num}/{total_chunks} ({len(chunk)} ítems) - {len(content)} bytes.")
                chunks_data.append((chunk, content))
            else:
                if export_type == "PDF" and len(chunk) > 1:
                    self.logger.debug(f"\t  🔄 Lote {chunk_num} falló. Descomponiendo en descargas individuales...")
                    for sub_id in chunk:
                        sub_content = self._download_export_batch([sub_id], export_type, f"item_{sub_id}")
                        if sub_content: chunks_data.append(([sub_id], sub_content))
                        else: self.logger.error(f"\t   ❌ No se pudo descargar el PDF para el ítem {sub_id}.")
                else:
                    self.logger.error(f"\t   ❌ No se pudo descargar el lote {chunk_num} tras {self.args.max_retries} reintentos.")
        return chunks_data


class SciwheelDataExtractor:
    def __init__(self, args: argparse.Namespace, logger: logging.Logger) -> None:
        self.args = args
        self.logger = logger
        self.client = SciwheelClient(args, logger)
        
        self.base_dir = args.output_dir
        self.pdfs_dir = self.base_dir / "PDFs"
        self.attachments_dir = self.base_dir / "Attachments"
        self.projects_dir = self.base_dir / "Projects"
        self.private_dir = self.projects_dir / "Private_Projects"
        self.shared_dir = self.projects_dir / "Shared_Projects"

        self.collections: Dict[str, Dict[str, Any]] = {}
        self.global_references: Dict[str, Dict[str, Any]] = {}
        
        self.all_projects_paths_by_id: Dict[str, str] = {}
        self.all_projects_paths_by_full_path: Dict[str, str] = {}
        self.all_projects_paths_by_leaf_name: Dict[str, str] = {}
        self.ambiguous_leaf_names: Set[str] = set()
        self.all_known_project_paths: Set[str] = set()

        self.failed_items: List[Dict[str, Any]] = []
        self.failed_projects: List[Dict[str, Any]] = []
        
        self._unmatched_annotations_map: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
        self.unmatched_annotations: List[Dict[str, Any]] = []
        
        self.geometry_stats = {
            "targeted": 0,
            "captured_full": 0,
            "captured_partial": 0,
            "failed_fallback": 0
        }

        self.attachment_stats = {
            "found": 0,
            "success": 0,
            "failed": 0
        }

    def register_unmatched_annotation(self, entry: Dict[str, Any]) -> None:
        """Registra o actualiza una anotación sin nota coincidente evitando duplicados."""
        if not isinstance(entry, dict):
            return
        sci_id = str(entry.get("sciwheel_id", ""))
        page = int(entry.get("page", 1))
        txt_norm = normalize_for_matching(entry.get("text_detected", ""))
        key = (sci_id, page, txt_norm)

        if key in self._unmatched_annotations_map:
            existing = self._unmatched_annotations_map[key]
            if entry.get("notes_best_match") and not existing.get("notes_best_match"):
                self._unmatched_annotations_map[key] = entry
            elif entry.get("best_ratio", 0) >= existing.get("best_ratio", 0):
                self._unmatched_annotations_map[key] = entry
        else:
            self._unmatched_annotations_map[key] = entry

        self.unmatched_annotations = list(self._unmatched_annotations_map.values())

    def load_state_from_existing_json(self) -> None:
        json_path = self.base_dir / self.args.json_output
        if not json_path.exists(): return
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for col in data.get("collections", []):
                col_id = col.get("id") or col.get("key")
                if col_id and col_id not in ("cat_private", "cat_shared"):
                    parent_id = col.get("parent_id") if "parent_id" in col else col.get("parentCollection")
                    self.collections[col_id] = {
                        "key": col_id, "name": col.get("name", ""),
                        "parentCollection": parent_id,
                        "items": set(col.get("item_ids") or col.get("items") or [])
                    }

            for item in data.get("items", []):
                sci_id = str(item.get("sciwheel_id")) if item.get("sciwheel_id") else None
                if sci_id:
                    tags_set = {t["tag"] for t in item.get("tags", []) if isinstance(t, dict) and "tag" in t}
                    collection_ids_set = set(item.get("collection_ids", []))

                    notes_list = []
                    seen_notes = set()
                    has_geometry = False

                    for n in item.get("notes", []):
                        if isinstance(n, dict):
                            html_val = n.get("html") or n.get("note") or ""
                            n_hash = n.get("hash") or build_note_hash(html_val)
                            fp = build_note_fingerprint(html_val)
                            
                            source_type = n.get("source_type")
                            if not source_type:
                                source_type = "PDF_NATIVE" if "annotationPosition" in n else "WEB_LEAN_LIBRARY"
                            n["source_type"] = source_type

                            if fp and fp not in seen_notes:
                                seen_notes.add(fp)
                                notes_list.append(n)
                            if "annotationPosition" in n:
                                has_geometry = True

                    pdf_path = item.get("pdf_path") or (item.get("pdf", {}).get("path") if isinstance(item.get("pdf"), dict) else None)
                    if pdf_path and not Path(pdf_path).exists(): pdf_path = None

                    sciwheel_has_pdf = item.get("sciwheel_has_pdf", False)
                    if "sciwheel_has_pdf" not in item:
                        sciwheel_has_pdf = bool(pdf_path or item.get("sciwheel_uuid"))

                    already_processed = bool(pdf_path and (has_geometry or len(notes_list) == 0))

                    attachments_list = []
                    for att in item.get("attachments", []):
                        if isinstance(att, dict):
                            att_path = att.get("path")
                            if att_path and not Path(att_path).exists():
                                att["path"] = None
                            attachments_list.append(att)

                    # Sanear campos de metadatos preexistentes en JSON acumulador
                    fields_dict = item.get("fields", {})
                    if isinstance(fields_dict, dict) and "date" in fields_dict:
                        raw_date_val = fields_dict.get("date")
                        norm_date = parse_iso_date(raw_date_val)
                        if norm_date:
                            fields_dict["date"] = norm_date
                        elif raw_date_val and ("date-parts" in str(raw_date_val) or "{" in str(raw_date_val)):
                            del fields_dict["date"]

                    self.global_references[sci_id] = {
                        "sciwheel_id": sci_id,
                        "sciwheel_uuid": item.get("sciwheel_uuid"),
                        "pdf_resource_id": item.get("pdf_resource_id"),
                        "sciwheel_has_pdf": sciwheel_has_pdf,
                        "pdf_path": pdf_path,
                        "itemType": item.get("itemType", "journalArticle"),
                        "title": item.get("title", ""),
                        "creators": item.get("creators", []),
                        "fields": fields_dict,
                        "collection_ids": collection_ids_set,
                        "tags": tags_set,
                        "notes": notes_list,
                        "attachments": attachments_list,
                        "_seen_notes": seen_notes,
                        "_already_processed": already_processed,
                        "_geometry_extracted": has_geometry,
                        "_collections_fetched": True,
                        "_is_fallback_type": item.get("_is_fallback_type", False)
                    }

            for un_ann in data.get("unmatched_annotations", []):
                if isinstance(un_ann, dict):
                    self.register_unmatched_annotation(un_ann)

            for fi in data.get("failed_items", []):
                if isinstance(fi, dict) and fi not in self.failed_items:
                    self.failed_items.append(fi)

            for fp in data.get("failed_projects", []):
                if isinstance(fp, dict) and fp not in self.failed_projects:
                    self.failed_projects.append(fp)

            self.logger.info(f"\t 📂 JSON acumulador preexistente detectado: Cargadas {len(self.collections)} colecciones, {len(self.global_references)} referencias y {len(self.unmatched_annotations)} anotaciones no matcheadas previas.")
        except Exception as e:
            self.logger.warning(f"\t ⚠️ Error leyendo JSON previo: {e}")

    def _flatten_project_tree(self, project_list: List[Dict[str, Any]], category: str, parent_key: Optional[str] = None) -> List[Dict[str, Any]]:
        flat: List[Dict[str, Any]] = []
        if not isinstance(project_list, list): return flat
        for proj in project_list:
            if not isinstance(proj, dict): continue
            p_id = proj.get('id')
            if not p_id: continue

            p_key = f"proj_{p_id}"
            p_parent_id = proj.get('parentId') or proj.get('parentCollectionId') or proj.get('parent_id')
            resolved_parent_key = parent_key or (f"proj_{p_parent_id}" if p_parent_id else ("cat_private" if category == "Private_Projects" else "cat_shared"))

            flat.append({
                "id": p_id, "name": proj.get('name', f"Project_{p_id}"),
                "key": p_key, "parentKey": resolved_parent_key,
                "category": category, "api_full_path": proj.get('fullPath')
            })
            sub_cols = proj.get('subCollections') or proj.get('subcollections') or proj.get('children') or proj.get('collections') or []
            if sub_cols:
                flat.extend(self._flatten_project_tree(sub_cols, category, parent_key=p_key))
        return flat

    def filter_projects(self, flat_projects: List[Dict[str, Any]], all_projects_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self.args.projects:
            return flat_projects

        target_queries = [str(q).strip() for q in self.args.projects if str(q).strip()]
        if not target_queries:
            return flat_projects

        matched_keys: Set[str] = set()

        for proj in flat_projects:
            p_id_str = str(proj['id'])
            p_key = proj['key']
            p_name = proj['name']
            p_full_path = self.build_project_full_path(p_key, all_projects_map)
            
            p_norm_path = normalize_path(p_full_path)
            p_norm_name = normalize_project_name(p_name)
            p_clean_name = p_name.strip().lower()

            for q in target_queries:
                q_clean = q.lower()
                q_norm_path = normalize_path(q)
                q_norm_name = normalize_project_name(q)

                if p_id_str == q or p_key.lower() == q_clean:
                    matched_keys.add(p_key)
                    break
                if p_clean_name == q_clean or (q_norm_name and p_norm_name == q_norm_name):
                    matched_keys.add(p_key)
                    break
                if (q_norm_path and p_norm_path == q_norm_path) or p_full_path.lower().endswith(q_clean):
                    matched_keys.add(p_key)
                    break

        if not matched_keys:
            self.logger.warning(f"\t ⚠️ Advertencia: No se encontraron proyectos que coincidan con los filtros: {self.args.projects}")
            return []

        final_keys: Set[str] = set(matched_keys)
        parent_map = {p['key']: p.get('parentKey') for p in flat_projects}

        def is_descendant_of_matched(p_key: str) -> bool:
            curr = parent_map.get(p_key)
            while curr and curr not in ("cat_private", "cat_shared"):
                if curr in matched_keys:
                    return True
                curr = parent_map.get(curr)
            return False

        for proj in flat_projects:
            pk = proj['key']
            if pk not in final_keys and is_descendant_of_matched(pk):
                final_keys.add(pk)

        filtered = [p for p in flat_projects if p['key'] in final_keys]
        self.logger.info(f"\t 🎯 Filtro '--projects' activo: Seleccionados {len(filtered)} de {len(flat_projects)} proyectos.")
        for p in filtered:
            self.logger.debug(f"\t    • Incluido: '{p['name']}' (ID: {p['id']}) -> {self.build_project_full_path(p['key'], all_projects_map)}")

        return filtered

    def build_project_hierarchy(self) -> List[Dict[str, Any]]:
        self.logger.info(f"\t ⏳ Obteniendo árbol jerárquico de carpetas...")
        raw_owned = self.client.get_owned_projects()
        raw_shared = self.client.get_shared_projects()

        full_flat_owned = self._flatten_project_tree(raw_owned, "Private_Projects", parent_key="cat_private")
        full_flat_shared = self._flatten_project_tree(raw_shared, "Shared_Projects", parent_key="cat_shared")
        all_projects_unfiltered = full_flat_owned + full_flat_shared

        projects_map_unfiltered = {p['key']: p for p in all_projects_unfiltered}
        for p in all_projects_unfiltered:
            full_path = self.build_project_full_path(p['key'], projects_map_unfiltered)
            str_id = str(p['id'])
            self.all_projects_paths_by_id[str_id] = full_path
            self.all_projects_paths_by_id[p['key']] = full_path

            norm_fp = normalize_path(full_path)
            if norm_fp: self.all_projects_paths_by_full_path[norm_fp] = full_path

            norm_leaf = normalize_project_name(p.get('name', ''))
            if norm_leaf:
                if norm_leaf not in self.ambiguous_leaf_names:
                    if norm_leaf in self.all_projects_paths_by_leaf_name:
                        if self.all_projects_paths_by_leaf_name[norm_leaf] != full_path:
                            del self.all_projects_paths_by_leaf_name[norm_leaf]
                            self.ambiguous_leaf_names.add(norm_leaf)
                    else:
                        self.all_projects_paths_by_leaf_name[norm_leaf] = full_path

        if self.args.poc and not self.args.projects:
            raw_owned, raw_shared = raw_owned[:2], raw_shared[:2]

        all_flat = self._flatten_project_tree(raw_owned, "Private_Projects", parent_key="cat_private") + \
                   self._flatten_project_tree(raw_shared, "Shared_Projects", parent_key="cat_shared")

        if self.args.projects:
            return self.filter_projects(all_flat, projects_map_unfiltered)

        return all_flat

    def find_root_project_key(self, p_key: str, projects_map: Dict[str, Dict[str, Any]]) -> str:
        curr_key = p_key
        while curr_key in projects_map and projects_map[curr_key]['parentKey'] not in ("cat_private", "cat_shared", None, False):
            curr_key = projects_map[curr_key]['parentKey']
        return curr_key

    def resolve_project_category_directory(self, p_key: str, projects_map: Dict[str, Dict[str, Any]]) -> Path:
        root_key = self.find_root_project_key(p_key, projects_map)
        root_proj = projects_map.get(root_key, projects_map[p_key])
        return self.private_dir if root_proj.get('category') == "Private_Projects" else self.shared_dir

    def build_project_full_path(self, p_key: str, projects_map: Dict[str, Dict[str, Any]]) -> str:
        proj = projects_map.get(p_key)
        if not proj: return ""
        category = proj.get('category', 'Private_Projects')
        api_fp = proj.get('api_full_path')
        if api_fp: return f"{category}/{api_fp.strip('/')}"
        
        path_parts = []
        curr_key = p_key
        while curr_key in projects_map:
            p_item = projects_map[curr_key]
            path_parts.append(p_item['name'])
            parent_key = p_item['parentKey']
            if parent_key in ("cat_private", "cat_shared", None, False):
                path_parts.append(category)
                break
            curr_key = parent_key
        path_parts.reverse()
        return "/".join(path_parts)

    def extract_reference_project_paths(self, ref_data: Dict[str, Any]) -> Set[str]:
        paths: Set[str] = set()
        def resolve_path_string(val: str) -> Optional[str]:
            norm_fp = normalize_path(val)
            if norm_fp in self.all_projects_paths_by_full_path:
                return self.all_projects_paths_by_full_path[norm_fp]
            norm_leaf = normalize_project_name(val)
            if norm_leaf in self.all_projects_paths_by_leaf_name:
                return self.all_projects_paths_by_leaf_name[norm_leaf]
            return None

        for user in (ref_data.get("users") or []):
            if isinstance(user, dict):
                for col in (user.get("collections") or []):
                    if isinstance(col, dict):
                        col_id, col_name, full_p = col.get("id"), col.get("name"), col.get("fullPath")
                        if col_id and str(col_id) in self.all_projects_paths_by_id:
                            paths.add(self.all_projects_paths_by_id[str(col_id)])
                        elif full_p and resolve_path_string(full_p): paths.add(resolve_path_string(full_p))
                        elif col_name and resolve_path_string(col_name): paths.add(resolve_path_string(col_name))
        return paths

    def _append_comment_to_item(self, sci_id: str, comm: Dict[str, Any], default_source: str = "WEB_LEAN_LIBRARY") -> None:
        quote, text = comm.get("quote", ""), comm.get("text", "")
        if not (quote or text): return

        created_ts = comm.get("created")
        iso_date, display_date = convert_sciwheel_timestamp(created_ts)

        author_data = comm.get("author") or {}
        author_name = ""
        if isinstance(author_data, dict):
            fn = author_data.get("firstName", "").strip()
            ln = author_data.get("lastName", "").strip()
            if fn or ln: author_name = f"{fn} {ln}".strip()

        header_items = []
        if author_name: header_items.append(f"<strong>{author_name}</strong>")
        if display_date: header_items.append(f"<span>({display_date})</span>")

        header_html = f"<p style='font-size: 0.9em; color: #555; margin-bottom: 4px;'>{' - '.join(header_items)}</p>" if header_items else ""

        body_html = ""
        if quote: body_html += f"<blockquote>{quote}</blockquote>"
        if text: body_html += f"<p>{text}</p>"

        inner_content = f"{header_html}{body_html}"
        hex_color = parse_sciwheel_color_to_hex(comm.get("color"))
        full_note_html = f'<div style="border-left: 4px solid {hex_color}; padding-left: 8px; margin-bottom: 8px;">{inner_content}</div>' if hex_color else inner_content

        fp = build_note_fingerprint(full_note_html)
        if fp and fp not in self.global_references[sci_id]["_seen_notes"]:
            self.global_references[sci_id]["_seen_notes"].add(fp)
            self.global_references[sci_id]["notes"].append({
                "hash": build_note_hash(full_note_html),
                "html": full_note_html,
                "dateAdded": iso_date,
                "annotationColor": hex_color,
                "raw_quote": clean_pdfjs_text(quote),
                "raw_text": text,
                "source_type": default_source
            })

    def _extract_attachments_metadata(self, ref: Dict[str, Any], pdf_resource_id: Optional[Union[int, str]]) -> List[Dict[str, Any]]:
        custom_data = ref.get("custom") if isinstance(ref.get("custom"), dict) else {}
        
        candidates = []
        for src in [
            ref.get("supplementaryDataResources"),
            ref.get("attachments"),
            ref.get("supplementaryFiles"),
            ref.get("supplementary"),
            ref.get("resources"),
            custom_data.get("attachments"),
            custom_data.get("supplementaryFiles")
        ]:
            if isinstance(src, list):
                candidates.extend(src)

        parsed_attachments: List[Dict[str, Any]] = []
        seen_res_ids: Set[str] = set()
        str_pdf_res_id = str(pdf_resource_id) if pdf_resource_id else None

        for item in candidates:
            if not isinstance(item, dict):
                continue

            res_id = item.get("id") or item.get("resourceId") or item.get("attachmentId")
            if not res_id:
                continue

            str_res_id = str(res_id)
            if str_res_id in seen_res_ids or (str_pdf_res_id and str_res_id == str_pdf_res_id):
                continue

            filename = item.get("filename") or item.get("fileName") or item.get("name") or item.get("title") or f"attachment_{str_res_id}"
            title = item.get("title") or item.get("name") or filename
            content_type = item.get("contentType") or item.get("mimeType") or item.get("type") or ""

            if item.get("isMainPdf") or item.get("mainPdf") or (content_type == "application/pdf" and "main" in str(title).lower()):
                continue

            seen_res_ids.add(str_res_id)
            parsed_attachments.append({
                "resource_id": res_id,
                "title": str(title).strip() if title else f"attachment_{str_res_id}",
                "filename": str(filename).strip() if filename else f"attachment_{str_res_id}",
                "contentType": str(content_type).strip(),
                "path": None,
                "md5": None,
                "filesize": None,
                "mtime": None
            })

        return parsed_attachments

    def _process_pdf_download_content(self, batch_items: List[int], content_bytes: bytes) -> Dict[str, str]:
        """Procesa polimórficamente los bytes descargados (PDF directo o paquete ZIP)."""
        saved_paths: Dict[str, str] = {}
        if not content_bytes:
            return saved_paths

        if content_bytes.startswith(b"%PDF"):
            if len(batch_items) == 1:
                sci_id = str(batch_items[0])
                item_ref = self.global_references.get(sci_id, {})
                uuid_str = item_ref.get("sciwheel_uuid")
                target_name = f"{uuid_str.lower()}.pdf" if uuid_str else f"{sci_id}.pdf"
                target_path = self.pdfs_dir / target_name
                self.pdfs_dir.mkdir(parents=True, exist_ok=True)
                with open(target_path, "wb") as f:
                    f.write(content_bytes)
                abs_path = str(target_path.resolve())
                saved_paths[sci_id] = abs_path
                item_ref["pdf_path"] = abs_path
            else:
                self.logger.warning("\t ⚠️ Se recibió un PDF directo pero el lote contenía múltiples ítems.")
        else:
            try:
                with zipfile.ZipFile(io.BytesIO(content_bytes)) as z:
                    for filename in z.namelist():
                        if filename.lower().endswith(".pdf"):
                            base_stem = sanitize_filename(Path(filename).stem)
                            u_match = UUID_REGEX.search(base_stem)
                            extracted_uuid = u_match.group(1).lower() if u_match else None

                            target_path = self.pdfs_dir / (f"{extracted_uuid}.pdf" if extracted_uuid else f"{base_stem}.pdf")
                            self.pdfs_dir.mkdir(parents=True, exist_ok=True)
                            if not target_path.exists():
                                with open(target_path, "wb") as f:
                                    f.write(z.read(filename))

                            abs_pdf_path = str(target_path.resolve())
                            for item_id in batch_items:
                                sci_id = str(item_id)
                                item_ref = self.global_references.get(sci_id, {})
                                if not extracted_uuid or item_ref.get("sciwheel_uuid") == extracted_uuid:
                                    item_ref["pdf_path"] = abs_pdf_path
                                    saved_paths[sci_id] = abs_pdf_path
            except zipfile.BadZipFile:
                self.logger.error("\t ❌ El contenido devuelto no es un PDF ni un paquete ZIP válido.")

        return saved_paths

    def extract_project_metadata_and_notes(self, proj: Dict[str, Any], projects_map: Dict[str, Dict[str, Any]]) -> List[int]:
        p_id, p_name, p_key, parent_key = proj['id'], proj['name'], proj['key'], proj['parentKey']
        proj_full_path = self.build_project_full_path(p_key, projects_map)
        self.logger.info(f"\t ⏳ Procesando estructura: '{p_name}' (ID: {p_id}) -> {proj_full_path}")

        if p_key not in self.collections:
            self.collections[p_key] = {"key": p_key, "name": p_name, "parentCollection": parent_key, "items": set()}

        references = self.client.get_collection_references(p_id)
        item_ids: List[int] = []
        items_needing_comments: List[str] = []

        for ref in references:
            try:
                item_id = ref.get("id")
                if not item_id: continue
                sci_id = str(item_id)
                item_ids.append(item_id)
                self.collections[p_key]["items"].add(sci_id)

                if "supplementaryDataResources" not in ref:
                    details = self.client.get_item_details(sci_id)
                    if details and isinstance(details, dict):
                        if "supplementaryDataResources" in details:
                            ref["supplementaryDataResources"] = details["supplementaryDataResources"]
                        if "pdfResource" in details and isinstance(details["pdfResource"], dict):
                            ref["pdfResource"] = details["pdfResource"]

                custom_data = ref.get("custom") if isinstance(ref.get("custom"), dict) else {}
                pdf_info = custom_data.get("pdf") or ref.get("pdf") or ref.get("pdfResource") or {}
                
                pdf_resource_id = pdf_info.get("id") or pdf_info.get("resourceId") or ref.get("resourceId")

                file_path = pdf_info.get("file-path", "") if isinstance(pdf_info, dict) else ""
                uuid_match = UUID_REGEX.search(file_path) if file_path else None
                extracted_uuid = uuid_match.group(1).lower() if uuid_match else None
                has_pdf_remote = ref.get("hasPdf", False) or bool(file_path) or bool(pdf_info)

                zotero_type, is_fallback = map_sciwheel_type_to_zotero(ref.get("type"))
                creators = extract_creators_from_reference(ref, zotero_type=zotero_type)
                zotero_fields = extract_zotero_fields(ref)

                attachments = self._extract_attachments_metadata(ref, pdf_resource_id)

                if sci_id not in self.global_references:
                    self.global_references[sci_id] = {
                        "sciwheel_id": sci_id, "sciwheel_uuid": extracted_uuid,
                        "pdf_resource_id": pdf_resource_id,
                        "sciwheel_has_pdf": has_pdf_remote, "pdf_path": None,
                        "itemType": zotero_type, "title": ref.get("title", ""),
                        "creators": creators, "fields": zotero_fields,
                        "collection_ids": {p_key}, "tags": set(),
                        "notes": [], "attachments": attachments,
                        "_seen_notes": set(), "_already_processed": False,
                        "_geometry_extracted": False, "_collections_fetched": False,
                        "_is_fallback_type": is_fallback
                    }
                    items_needing_comments.append(sci_id)
                else:
                    self.global_references[sci_id]["collection_ids"].add(p_key)
                    if has_pdf_remote: self.global_references[sci_id]["sciwheel_has_pdf"] = True
                    if extracted_uuid and not self.global_references[sci_id].get("sciwheel_uuid"):
                        self.global_references[sci_id]["sciwheel_uuid"] = extracted_uuid
                    if pdf_resource_id and not self.global_references[sci_id].get("pdf_resource_id"):
                        self.global_references[sci_id]["pdf_resource_id"] = pdf_resource_id

                    if creators and not self.global_references[sci_id].get("creators"):
                        self.global_references[sci_id]["creators"] = creators

                    if zotero_fields: self.global_references[sci_id]["fields"].update(zotero_fields)

                    existing_att_ids = {str(a.get("resource_id")) for a in self.global_references[sci_id].get("attachments", [])}
                    for att in attachments:
                        if str(att.get("resource_id")) not in existing_att_ids:
                            self.global_references[sci_id].setdefault("attachments", []).append(att)

                    if not self.global_references[sci_id].get("_already_processed", False):
                        items_needing_comments.append(sci_id)

                extracted_tags = extract_tags_from_reference(ref)
                ref_project_paths = self.extract_reference_project_paths(ref)
                ref_project_paths.add(proj_full_path)

                if not self.global_references[sci_id].get("_collections_fetched", False):
                    har_item_cols = self.client.get_item_collection_paths(sci_id)
                    self.global_references[sci_id]["tags"].update(har_item_cols)
                    self.global_references[sci_id]["_collections_fetched"] = True

                self.global_references[sci_id]["tags"].update(extracted_tags)
                self.global_references[sci_id]["tags"].update(ref_project_paths)
            except Exception as e:
                self.logger.error(f"\t ❌ Error procesando referencia ID {ref.get('id')} en proyecto '{p_name}': {e}")
                self.failed_items.append({"sciwheel_id": str(ref.get("id")), "project_id": p_id, "error": str(e)})

        if items_needing_comments:
            try:
                comments = self.client.get_project_comments(p_id)
                for comm in comments:
                    lib_item = comm.get("libraryItem", {})
                    item_id = lib_item.get("id")
                    if not item_id: continue

                    sci_id = str(item_id)
                    if sci_id in self.global_references:
                        if self.global_references[sci_id].get("_already_processed", False) and self.global_references[sci_id].get("_geometry_extracted", False):
                            continue
                        self._append_comment_to_item(sci_id, comm, default_source="WEB_LEAN_LIBRARY")
            except Exception as e:
                self.logger.warning(f"\t ⚠️ No se pudieron obtener notas para el proyecto '{p_name}': {e}")

            for sci_id in items_needing_comments:
                try:
                    item_comms = self.client.get_item_comments(sci_id)
                    for icomm in item_comms:
                        self._append_comment_to_item(sci_id, icomm, default_source="WEB_LEAN_LIBRARY")
                except Exception:
                    pass

        return item_ids

    def export_project_assets_and_pdfs(self, root_proj: Dict[str, Any], aggregated_item_ids: List[int], projects_map: Dict[str, Dict[str, Any]]) -> None:
        if not aggregated_item_ids: return
        p_name, p_key = root_proj['name'], root_proj['key']
        cat_dir = self.resolve_project_category_directory(p_key, projects_map)

        self.logger.info(f"\t 📦 Exportando recursos consolidados para el proyecto raíz '{p_name}' ({len(aggregated_item_ids)} ítems totales)...")

        if not self.args.skip_bib_backup:
            bib_chunks = self.client.download_data_in_batches(aggregated_item_ids, "BIBTEX", chunk_size=self.args.bib_chunk_size)
            if bib_chunks:
                cat_dir.mkdir(parents=True, exist_ok=True)
                with open(cat_dir / f"{sanitize_filename(p_name)}.bib", "wb") as f:
                    for _, chunk in bib_chunks: f.write(chunk); f.write(b"\n\n")

        pdf_items_to_download = []
        for item_id in aggregated_item_ids:
            sci_id = str(item_id)
            item_ref = self.global_references.get(sci_id, {})
            has_pdf = item_ref.get("sciwheel_has_pdf", False)
            current_pdf_path = item_ref.get("pdf_path")
            sciwheel_uuid = item_ref.get("sciwheel_uuid")

            pdf_exists_locally = False
            if current_pdf_path and Path(current_pdf_path).exists():
                pdf_exists_locally = True
            elif sciwheel_uuid:
                uuid_path = self.pdfs_dir / f"{sciwheel_uuid.lower()}.pdf"
                if uuid_path.exists():
                    pdf_exists_locally = True
                    item_ref["pdf_path"] = str(uuid_path.resolve())

            if has_pdf and not pdf_exists_locally:
                pdf_items_to_download.append(item_id)

        if pdf_items_to_download:
            self.logger.debug(f"\t ⏳ Solicitando {len(pdf_items_to_download)} PDF(s) pendientes a Sciwheel (lotes de {self.args.pdf_chunk_size})...")
            pdf_zip_chunks = self.client.download_data_in_batches(pdf_items_to_download, "PDF", chunk_size=self.args.pdf_chunk_size)
            extracted_count = 0

            for batch_items, chunk_bytes in pdf_zip_chunks:
                saved = self._process_pdf_download_content(batch_items, chunk_bytes)
                extracted_count += len(saved)

                for sub_id in batch_items:
                    str_sub = str(sub_id)
                    item_ref = self.global_references.get(str_sub, {})
                    if str_sub not in saved and not (item_ref.get("pdf_path") and Path(item_ref["pdf_path"]).exists()):
                        pdf_res_id = item_ref.get("pdf_resource_id")
                        if pdf_res_id:
                            ok, d_path = self.client.download_pdf_direct_resource(str_sub, pdf_res_id, self.pdfs_dir)
                            if ok and d_path:
                                item_ref["pdf_path"] = str(d_path.resolve())
                                extracted_count += 1

            self.logger.debug(f"\t 📥 {extracted_count} PDFs nuevos guardados en la carpeta PDFs/")

        self.download_supplementary_attachments(aggregated_item_ids)

    def download_supplementary_attachments(self, item_ids: List[int]) -> None:
        self.attachments_dir.mkdir(parents=True, exist_ok=True)

        for item_id in item_ids:
            sci_id = str(item_id)
            item_ref = self.global_references.get(sci_id, {})
            attachments = item_ref.get("attachments", [])

            for att in attachments:
                res_id = att.get("resource_id")
                if not res_id:
                    continue

                self.attachment_stats["found"] += 1

                existing_matches = list(self.attachments_dir.glob(f"{res_id}_*"))
                if existing_matches and existing_matches[0].exists():
                    target_path = existing_matches[0]
                    meta = calculate_pdf_metadata(target_path)
                    if meta:
                        att["filename"] = target_path.name.replace(f"{res_id}_", "", 1)
                        att["path"] = meta["path"]
                        att["md5"] = meta["md5"]
                        att["filesize"] = meta["filesize"]
                        att["mtime"] = meta["mtime"]
                        self.attachment_stats["success"] += 1
                        continue

                self.logger.debug(f"\t 📎 Descargando adjunto suplementario res_id: {res_id} para ítem {sci_id}...")
                success, target_path = self.client.download_resource_file(sci_id, res_id, self.attachments_dir, att)

                if success and target_path and target_path.exists():
                    meta = calculate_pdf_metadata(target_path)
                    if meta:
                        att["path"] = meta["path"]
                        att["md5"] = meta["md5"]
                        att["filesize"] = meta["filesize"]
                        att["mtime"] = meta["mtime"]
                    self.attachment_stats["success"] += 1
                else:
                    self.logger.error(f"\t ❌ Falló la descarga del adjunto res_id {res_id} para el ítem {sci_id}.")
                    self.attachment_stats["failed"] += 1

    def enrich_notes_with_spatial_geometry(self, target_sci_ids: Optional[Set[str]] = None) -> None:
        if self.args.skip_geometry:
            self.logger.info("\t ⏩ Captura de geometría espacial desactivada vía --skip-geometry.")
            return

        if not PLAYWRIGHT_AVAILABLE:
            self.logger.warning("\t ⚠️ Playwright no está instalado en este entorno python. Se omite la geometría espacial.")
            return

        active_collection_keys = set(self.collections.keys())

        target_items = []
        for item in self.global_references.values():
            sci_id = str(item["sciwheel_id"])
            if target_sci_ids is not None and sci_id not in target_sci_ids:
                continue

            pdf_path = item.get("pdf_path")
            has_pdf = item.get("sciwheel_has_pdf", False) and pdf_path and Path(pdf_path).exists()
            has_notes = len(item.get("notes", [])) > 0
            already_extracted = item.get("_geometry_extracted", False)

            belongs_to_active = not active_collection_keys or any(cid in active_collection_keys for cid in item.get("collection_ids", []))

            if has_pdf and has_notes and not already_extracted and belongs_to_active:
                target_items.append(item)

        if not target_items:
            self.logger.info("\t ℹ️ No hay ítems pendientes de captura geométrica en Playwright (todos al día o sin notas).")
            return

        self.geometry_stats["targeted"] = len(target_items)
        self.logger.info(f"\t ⚡ Iniciando navegador Playwright para extraer geometría en {len(target_items)} ítems con notas...")

        cookies_list = parse_cookie_string_for_playwright(self.args.token or "")

        scroll_step_js = """
        (pos) => {
            const el = document.querySelector("#viewerContainer") || document.querySelector(".pdfViewer") || document.querySelector("#viewer");
            const container = (el && el.scrollHeight > el.clientHeight) ? el : (document.documentElement || document.body);
            if (container === document.documentElement || container === document.body) {
                window.scrollTo(0, pos);
            } else {
                container.scrollTop = pos;
            }
            return container.scrollHeight;
        }
        """

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not self.args.headed)
            context = browser.new_context(viewport={"width": 1400, "height": 1000})
            context.add_cookies(cookies_list)
            page = context.new_page()

            for idx, item in enumerate(target_items, start=1):
                sci_id = item["sciwheel_id"]
                resource_id = item.get("pdf_resource_id")
                pdf_path = item.get("pdf_path")

                if resource_id:
                    url_target = f"https://sciwheel.com/work/item/{sci_id}/resources/{resource_id}/pdf"
                else:
                    url_target = f"https://sciwheel.com/work/item/{sci_id}"

                self.logger.debug(f"\t 🔍 [{idx}/{len(target_items)}] Navegando a {url_target} para ítem ID: {sci_id}")

                try:
                    page.goto(url_target, wait_until="networkidle", timeout=self.args.playwright_timeout * 1000)

                    if "login" in page.url:
                        self.logger.error("❌ ERROR: Redirigido a Login. Token/Cookie vencido en Playwright.")
                        browser.close()
                        sys.exit(1)

                    if "/pdf" not in page.url:
                        pdf_link_element = page.query_selector("a[href*='/resources/'][href*='/pdf']")
                        if pdf_link_element:
                            pdf_href = pdf_link_element.get_attribute("href")
                            if pdf_href:
                                if not pdf_href.startswith("http"):
                                    pdf_href = f"https://sciwheel.com{pdf_href}"
                                self.logger.debug(f"\t 🔗 Enlace al PDF detectado en el DOM: {pdf_href}. Redirigiendo...")
                                page.goto(pdf_href, wait_until="networkidle", timeout=self.args.playwright_timeout * 1000)
                        else:
                            self.logger.warning(f"\t ⚠️️ No se encontró botón/enlace 'PDF' en la vista del ítem {sci_id}.")

                    page.wait_for_selector(".page", timeout=15000)

                    all_fragments = []
                    current_pos = 0
                    step = int(page.evaluate("window.innerHeight || 800") * 0.75)
                    if step <= 0:
                        step = 600

                    scroll_height = page.evaluate(scroll_step_js, 0)

                    while current_pos <= scroll_height:
                        scroll_height = page.evaluate(scroll_step_js, current_pos)
                        page.wait_for_timeout(300)

                        fragments_step = page.evaluate(JS_EXTRACTOR)
                        if fragments_step:
                            all_fragments.extend(fragments_step)

                        current_pos += step
                        if scroll_height <= 0:
                            break

                    raw_fragments = deduplicar_fragmentos(all_fragments)
                    consolidated_highlights = agrupar_fragmentos_dom(raw_fragments)

                    notes_enriched = 0
                    for highlight in consolidated_highlights:
                        text_detected = highlight.get("texto", "")
                        page_num = highlight.get("pagina", 1)
                        page_index = max(0, page_num - 1)
                        raw_rects = highlight.get("rects", [])
                        bg_color = parse_sciwheel_color_to_hex(highlight.get("color")) or "#fff59d"
                        
                        canvas_w = highlight.get("paginaWidth", 612)
                        canvas_h = highlight.get("paginaHeight", 792)

                        page_pdf_w, page_pdf_h = get_pdf_page_dimensions(pdf_path, page_index)

                        zotero_rects, bounding_box = transform_sciwheel_to_zotero_rects(
                            raw_rects, page_pdf_w, page_pdf_h, canvas_w, canvas_h
                        )

                        matched_note, ratio, best_candidate_notes = match_annotation_to_note(text_detected, item["notes"])

                        if matched_note:
                            matched_note["annotationType"] = "highlight"
                            matched_note["annotationText"] = text_detected
                            matched_note["annotationColor"] = bg_color
                            matched_note["annotationPosition"] = {
                                "pageIndex": page_index,
                                "rects": zotero_rects,
                                "rect": bounding_box
                            }
                            matched_note["source_type"] = "PDF_NATIVE"
                            notes_enriched += 1

                            unmatched_key = (sci_id, page_num, normalize_for_matching(text_detected))
                            if unmatched_key in self._unmatched_annotations_map:
                                del self._unmatched_annotations_map[unmatched_key]
                                self.unmatched_annotations = list(self._unmatched_annotations_map.values())
                        else:
                            notes_best_match_info = []
                            for cand_note in best_candidate_notes:
                                cand_hash = cand_note.get("hash") or build_note_hash(cand_note.get("html", ""))
                                notes_best_match_info.append({
                                    "hash": cand_hash,
                                    "ratio": round(ratio, 3),
                                    "raw_quote": cand_note.get("raw_quote", ""),
                                    "dateAdded": cand_note.get("dateAdded")
                                })

                            self.register_unmatched_annotation({
                                "sciwheel_id": sci_id,
                                "text_detected": text_detected,
                                "page": page_num,
                                "best_ratio": round(ratio, 3),
                                "notes_best_match": notes_best_match_info
                            })
                            self.logger.debug(
                                f"\t ⚠️ Highlight sin nota coincidente en ítem {sci_id} (ratio máx: {ratio:.2f}). Se omite creación sintética: '{text_detected[:50]}...'"
                            )

                    item["_geometry_extracted"] = True
                    item["_already_processed"] = True

                    if len(consolidated_highlights) > 0:
                        if notes_enriched >= len(consolidated_highlights):
                            self.geometry_stats["captured_full"] += 1
                        elif notes_enriched > 0:
                            self.geometry_stats["captured_partial"] += 1
                        else:
                            self.geometry_stats["failed_fallback"] += 1
                    else:
                        self.geometry_stats["failed_fallback"] += 1

                except Exception as e:
                    self.logger.warning(f"\t ⚠️ Error obteniendo geometría Playwright para ítem {sci_id}: {e}")
                    self.geometry_stats["failed_fallback"] += 1

            browser.close()

    def generate_validation_summary(self) -> Dict[str, Any]:
        total_items = len(self.global_references)
        items_with_pdf = sum(1 for item in self.global_references.values() if item.get("pdf_path") and Path(item["pdf_path"]).exists())
        items_without_pdf = sum(1 for item in self.global_references.values() if not item.get("sciwheel_has_pdf", False))
        items_pdf_failed = sum(1 for item in self.global_references.values() if item.get("sciwheel_has_pdf") and not (item.get("pdf_path") and Path(item["pdf_path"]).exists()))
        total_notes = sum(len(item.get("notes", [])) for item in self.global_references.values())
        notes_with_geometry = sum(1 for item in self.global_references.values() for n in item.get("notes", []) if "annotationPosition" in n)

        return {
            "total_items": total_items,
            "items_with_pdf": items_with_pdf,
            "items_without_pdf_in_sciwheel": items_without_pdf,
            "items_pdf_download_failed": items_pdf_failed,
            "total_collections": len(self.collections),
            "total_notes_count": total_notes,
            "notes_with_geometry_count": notes_with_geometry,
            "unmatched_annotations_count": len(self.unmatched_annotations),
            "geometry_stats": self.geometry_stats,
            "total_attachments_found": self.attachment_stats["found"],
            "attachments_downloaded_count": self.attachment_stats["success"],
            "attachments_failed_count": self.attachment_stats["failed"],
            "failed_items_count": len(self.failed_items),
            "failed_projects_count": len(self.failed_projects)
        }

    def save_consolidated_json_manifest(self) -> Dict[str, Any]:
        collections_list = [
            {"id": "cat_private", "name": "Private Projects", "parent_id": None, "item_ids": []},
            {"id": "cat_shared", "name": "Shared Projects", "parent_id": None, "item_ids": []}
        ]
        for col in self.collections.values():
            collections_list.append({
                "id": col["key"], "name": col["name"],
                "parent_id": col["parentCollection"], "item_ids": sorted(list(col["items"]))
            })

        formatted_items: List[Dict[str, Any]] = []
        for item_data in self.global_references.values():
            formatted_notes = []
            for n in item_data.get("notes", []):
                source_type = n.get("source_type")
                if not source_type:
                    source_type = "PDF_NATIVE" if "annotationPosition" in n else "WEB_LEAN_LIBRARY"

                note_dict = {
                    "hash": n.get("hash") or build_note_hash(n.get("html", "")),
                    "html": n.get("html", ""),
                    "dateAdded": n.get("dateAdded"),
                    "source_type": source_type
                }
                if "annotationPosition" in n:
                    note_dict["annotationType"] = n.get("annotationType", "highlight")
                    note_dict["annotationColor"] = n.get("annotationColor", "#fff59d")
                    note_dict["annotationText"] = n.get("annotationText", "")
                    note_dict["annotationPosition"] = n["annotationPosition"]

                formatted_notes.append(note_dict)

            formatted_attachments = []
            for att in item_data.get("attachments", []):
                formatted_attachments.append({
                    "resource_id": att.get("resource_id"),
                    "title": att.get("title", ""),
                    "filename": att.get("filename", ""),
                    "path": att.get("path"),
                    "md5": att.get("md5"),
                    "filesize": att.get("filesize"),
                    "mtime": att.get("mtime")
                })

            local_pdf_path = Path(item_data["pdf_path"]) if item_data.get("pdf_path") else None
            pdf_metadata = calculate_pdf_metadata(local_pdf_path)

            formatted_items.append({
                "sciwheel_id": str(item_data.get("sciwheel_id")),
                "sciwheel_uuid": item_data.get("sciwheel_uuid"),
                "pdf_resource_id": item_data.get("pdf_resource_id"),
                "sciwheel_has_pdf": item_data.get("sciwheel_has_pdf", False),
                "itemType": item_data.get("itemType", "journalArticle"),
                "title": item_data.get("title", ""),
                "creators": item_data.get("creators", []),
                "fields": item_data.get("fields", {}),
                "collection_ids": sorted(list(item_data.get("collection_ids", []))),
                "tags": [{"tag": t} for t in sorted(list(item_data.get("tags", [])))],
                "notes": formatted_notes,
                "pdf": pdf_metadata,
                "attachments": formatted_attachments
            })

        summary_report = self.generate_validation_summary()
        export_payload = {
            "manifest_version": 1,
            "extractor_version": "0.0.1",
            "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "summary": summary_report,
            "failed_items": self.failed_items,
            "failed_projects": self.failed_projects,
            "unmatched_annotations": self.unmatched_annotations,
            "collections": collections_list,
            "items": formatted_items
        }

        self.base_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.base_dir / self.args.json_output
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(export_payload, f, indent=2, ensure_ascii=False)

        self.logger.info(f"\t 📦 JSON consolidado guardado: {json_path.name}")
        return summary_report

    def print_validation_report(self, summary: Dict[str, Any]) -> None:
        print("\n================================================================================")
        print("REPORTE FINAL DE AUTO-VALIDACIÓN DE LA EXTRACCIÓN")
        print("================================================================================")
        print(f"  • Total de referencias procesadas          : {summary['total_items']}")
        print(f"  • PDFs disponibles localmente               : {summary['items_with_pdf']}")
        print(f"  • Ítems sin PDF en Sciwheel (Normal)        : {summary['items_without_pdf_in_sciwheel']}")
        print(f"  • PDFs faltantes por error de descarga      : {summary['items_pdf_download_failed']}")
        print(f"  • Archivos suplementarios procesados       : {summary['attachments_downloaded_count']} / {summary['total_attachments_found']} ({summary['attachments_failed_count']} errores)")
        print(f"  • Total de colecciones/proyectos            : {summary['total_collections']}")
        print(f"  • Total de notas HTML extraídas             : {summary['total_notes_count']}")
        print(f"  • Notas enriquecidas con geometría          : {summary['notes_with_geometry_count']}")
        if summary.get("unmatched_annotations_count", 0) > 0:
            print(f"  • Anotaciones sin nota asociada (omitidas)  : {summary['unmatched_annotations_count']}")
        print("  ------------------------------------------------------------------------------")
        print(f"  • Ítems evaluados con Playwright (DOM)      : {summary['geometry_stats']['targeted']}")
        print(f"  • Geometría capturada completa              : {summary['geometry_stats']['captured_full']}")
        print(f"  • Geometría capturada parcial               : {summary['geometry_stats']['captured_partial']}")
        print(f"  • Sin geometría (sin marcas detectadas)     : {summary['geometry_stats']['failed_fallback']}")
        print("================================================================================\n")

    def retry_failed_pdfs_pipeline(self) -> None:
        """Modo de reintento focalizado de descarga de PDFs únicamente para ítems fallidos/pendientes."""
        self.logger.info("\t 🔄 [Modo Reintento PDF] Cargando estado previo del JSON consolidado...")
        self.load_state_from_existing_json()

        if not self.global_references:
            self.logger.error("❌ No se encontraron referencias cargadas en el JSON. Verifique que el archivo exista.")
            return

        target_items = []
        for sci_id, item_ref in self.global_references.items():
            has_pdf_remote = item_ref.get("sciwheel_has_pdf", False)
            pdf_path = item_ref.get("pdf_path")
            pdf_exists = bool(pdf_path and Path(pdf_path).exists())

            if has_pdf_remote and not pdf_exists:
                target_items.append(item_ref)

        if not target_items:
            self.logger.info("\t ✨ ¡No hay referencias con PDFs faltantes para reintentar!")
            summary = self.save_consolidated_json_manifest()
            self.print_validation_report(summary)
            return

        self.logger.info(f"\t 🎯 Detectados {len(target_items)} ítems con PDF en Sciwheel pendiente de descarga local.")

        try:
            self.build_project_hierarchy()
        except Exception as e:
            self.logger.warning(f"\t ⚠️ No se pudo obtener la jerarquía completa de proyectos ({e}), continuando con datos locales...")

        target_item_ids: List[int] = []
        for item in target_items:
            sci_id = str(item["sciwheel_id"])
            self.logger.info(f"\t 🔍 [Ítem {sci_id}] Consultando metadatos, adjuntos y notas actualizadas vía API...")

            details = self.client.get_item_details(sci_id)
            if details and isinstance(details, dict):
                custom_data = details.get("custom") if isinstance(details.get("custom"), dict) else {}
                pdf_info = custom_data.get("pdf") or details.get("pdf") or details.get("pdfResource") or {}
                pdf_res_id = pdf_info.get("id") or pdf_info.get("resourceId") or details.get("resourceId")
                if pdf_res_id:
                    item["pdf_resource_id"] = pdf_res_id

                file_path = pdf_info.get("file-path", "") if isinstance(pdf_info, dict) else ""
                u_match = UUID_REGEX.search(file_path) if file_path else None
                if u_match:
                    item["sciwheel_uuid"] = u_match.group(1).lower()

                att_meta = self._extract_attachments_metadata(details, pdf_res_id)
                existing_att_ids = {str(a.get("resource_id")) for a in item.get("attachments", [])}
                for att in att_meta:
                    if str(att.get("resource_id")) not in existing_att_ids:
                        item.setdefault("attachments", []).append(att)

            try:
                comms = self.client.get_item_comments(sci_id)
                for c in comms:
                    self._append_comment_to_item(sci_id, c, default_source="WEB_LEAN_LIBRARY")
            except Exception as e:
                self.logger.warning(f"\t ⚠️ No se pudieron obtener comentarios directos del ítem {sci_id}: {e}")

            target_item_ids.append(int(sci_id))

        self.logger.info(f"\t 📥 Reintentando descarga de PDFs en lotes (sin backup de .bib) para {len(target_item_ids)} ítems...")
        pdf_zip_chunks = self.client.download_data_in_batches(target_item_ids, "PDF", chunk_size=self.args.pdf_chunk_size)
        
        recovered_ids: Set[str] = set()
        for batch_items, chunk_bytes in pdf_zip_chunks:
            saved = self._process_pdf_download_content(batch_items, chunk_bytes)
            recovered_ids.update(saved.keys())

            for sub_id in batch_items:
                str_sub = str(sub_id)
                item_ref = self.global_references.get(str_sub, {})
                if str_sub not in saved and not (item_ref.get("pdf_path") and Path(item_ref["pdf_path"]).exists()):
                    pdf_res_id = item_ref.get("pdf_resource_id")
                    if pdf_res_id:
                        ok, d_path = self.client.download_pdf_direct_resource(str_sub, pdf_res_id, self.pdfs_dir)
                        if ok and d_path:
                            item_ref["pdf_path"] = str(d_path.resolve())
                            item_ref["_already_processed"] = False
                            item_ref["_geometry_extracted"] = False
                            recovered_ids.add(str_sub)

        self.logger.info(f"\t 📥 Se recuperaron {len(recovered_ids)} PDFs exitosamente.")

        self.download_supplementary_attachments(target_item_ids)

        self.enrich_notes_with_spatial_geometry(target_sci_ids=recovered_ids)

        summary = self.save_consolidated_json_manifest()
        self.print_validation_report(summary)

    def execute_extraction_pipeline(self) -> None:
        if self.args.retry_failed_pdfs:
            self.retry_failed_pdfs_pipeline()
            return

        self.load_state_from_existing_json()
        flat_projects = self.build_project_hierarchy()
        if not flat_projects:
            self.logger.error("No hay proyectos seleccionados para extraer.")
            return

        projects_map = {p['key']: p for p in flat_projects}
        root_items_map: Dict[str, Set[int]] = {}

        for proj in flat_projects:
            try:
                item_ids = self.extract_project_metadata_and_notes(proj, projects_map)
                root_key = self.find_root_project_key(proj['key'], projects_map)
                if root_key not in root_items_map: root_items_map[root_key] = set()
                root_items_map[root_key].update(item_ids)
            except Exception as e:
                self.failed_projects.append({"project_id": proj.get("id"), "error": str(e)})

        for root_key, item_ids in root_items_map.items():
            try:
                self.export_project_assets_and_pdfs(projects_map[root_key], list(item_ids), projects_map)
            except Exception as e:
                self.logger.error(f"\t ❌ Error descargando adjuntos para raíz '{root_key}': {e}")

        self.enrich_notes_with_spatial_geometry()

        summary = self.save_consolidated_json_manifest()
        self.print_validation_report(summary)


def main() -> None:
    args = parse_cli_arguments()
    logger = configure_logger(args.verbose)

    if args.report:
        generate_standalone_report(args.report)
        return

    extractor = SciwheelDataExtractor(args, logger)
    extractor.execute_extraction_pipeline()

    print(f"🎉 Extracción finalizada con éxito. Datos almacenados en: {args.output_dir}")


if __name__ == "__main__":
    main()