#!/usr/bin/env python3
"""
Sciwheel → Zotero Importer (zotero_importer.py)

Propósito:
    Consume el manifiesto JSON consolidado producido por sciwheel_extractor_plus.py
    y crea, en la biblioteca LOCAL de Zotero 10+ (vía su Local Write API oficial en
    http://127.0.0.1:23119/api/), la jerarquía de colecciones, los ítems, sus notas
    hijas (highlights) y sus adjuntos PDF.

    Reconciliación aditiva pura: un ítem ya importado nunca se sobreescribe en sus
    campos bibliográficos. Solo se suman tags/colecciones/notas nuevas detectadas
    en una extracción posterior. La idempotencia se verifica contra el estado real
    de Zotero (tags marcadores), no contra un archivo de estado local.

    Ver DESIGN.md para el diseño técnico completo.

Modo de uso:
    Simulación (no escribe nada en Zotero, default):
        ./zotero_importer.py --manifest /ruta/hierarchy_and_metadata.json

    Ejecución real:
        ./zotero_importer.py --manifest /ruta/hierarchy_and_metadata.json -x

    Reintento de fallidos de una corrida anterior:
        ./zotero_importer.py --manifest /ruta/hierarchy_and_metadata.json -x \\
            --retry-failed /ruta/informe_importacion_<ts>.json
"""

import os
import sys
import re
import json
import time
import logging
import argparse
import hashlib
import colorsys
import mimetypes
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import Counter

import requests

# --------------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------------

DEFAULT_BASE_URL = "http://127.0.0.1:23119/api/"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "sciwheel-zotero-importer" / "config.json"
BATCH_SIZE_DEFAULT = 50
TAG_IMPORT_MARKER = "sciwheel-import"

# itemTypes válidos que el importador acepta del manifest (defensivo: si el
# extractor algún día emite algo fuera de este set, se reporta como inválido
# en vez de que Zotero lo rechace recién en la escritura).
KNOWN_ZOTERO_ITEM_TYPES = {
    "journalArticle", "book", "bookSection", "conferencePaper", "thesis",
    "preprint", "report", "webpage", "patent", "dataset", "magazineArticle",
    "newspaperArticle", "manuscript", "document", "letter", "interview",
    "film", "artwork", "presentation", "podcast", "computerProgram",
}


def id_tag(sciwheel_id: str) -> str:
    """Tag único e idempotente para un ítem, a partir de su sciwheel_id."""
    return f"sciwheel-id:{sciwheel_id}"


# Validación de entrada, no reparación: si 'date' trae la firma de un bug ya
# corregido en el extractor (CSL-JSON crudo, ej. "{'date-parts': [[2010,8,23]]}"
# en vez de "2010-08-23"), el importador se NIEGA a escribir ese valor en
# Zotero -- no intenta reconstruir la fecha correcta (eso es trabajo del
# extractor). Mismo criterio que ya se aplica a itemType/título/autores en
# validate_items(): dato claramente inválido -> se excluye ese campo, no se
# adivina. Sirve de red de seguridad si se reusa un manifest viejo.
BROKEN_DATE_PATTERN = re.compile(r"date-parts", re.IGNORECASE)


def sanitize_date_field(item: Dict[str, Any], logger: logging.Logger) -> None:
    """Si fields.date trae la firma de CSL-JSON crudo, se descarta ese campo
    (in-place) en vez de escribirlo en Zotero -- no se intenta reconstruir.
    No invalida el ítem entero: perder solo la fecha no amerita excluir toda
    la referencia."""
    date_val = item.get("fields", {}).get("date")
    if isinstance(date_val, str) and BROKEN_DATE_PATTERN.search(date_val):
        logger.warning(f"\t ⚠️ Ítem {item.get('sciwheel_id')}: 'date' con CSL-JSON crudo "
                        f"({date_val!r}) — se descarta ese campo, no se importa así.")
        item["fields"]["date"] = ""


ATT_TAG_PREFIX = "sciwheel-att:"


def att_tag(resource_id: Any) -> str:
    """Tag idempotente de un adjunto (PDF principal o suplementario), a partir
    del resource_id de Sciwheel."""
    return f"{ATT_TAG_PREFIX}{resource_id}"


_PATH_TAG_PREFIXES = ("Private_Projects/", "Shared_Projects/")


def is_path_tag(tag: str) -> bool:
    """Los tags 'Private_Projects/...'/'Shared_Projects/...' del manifest son
    marcadores de ruta que el importador usa para resolver collection_ids —
    nunca deben crearse como tags reales de Zotero (quedarían redundantes
    con la propia pertenencia a la colección, y confunden en la UI)."""
    return tag.startswith(_PATH_TAG_PREFIXES)


def is_bookkeeping_tag(tag: str) -> bool:
    """Tags propios del importador para idempotencia — se marcan como
    'automáticos' (type=1) en Zotero, que por defecto los oculta del
    selector de tags, a diferencia de los tags de contenido reales."""
    return (tag == TAG_IMPORT_MARKER or tag.startswith("sciwheel-id:")
            or tag.startswith("sciwheel-note:") or tag.startswith(ATT_TAG_PREFIX))


def zotero_tag(tag: str) -> Dict[str, Any]:
    """Construye el objeto de tag para la API, marcando como 'automático'
    (type: 1) los tags propios de idempotencia — Zotero los oculta del
    selector de tags por defecto, a diferencia de los tags de contenido."""
    obj: Dict[str, Any] = {"tag": tag}
    if is_bookkeeping_tag(tag):
        obj["type"] = 1
    return obj


# Paleta fija de colores de anotación que acepta Zotero — un hex arbitrario
# de Sciwheel se mapea al más cercano de esta lista, no se pasa tal cual.
ZOTERO_ANNOTATION_COLORS = [
    "#ffd400",  # amarillo
    "#ff6666",  # rojo
    "#5fb236",  # verde
    "#2ea8e5",  # azul
    "#a28ae5",  # violeta
    "#e56eee",  # magenta
    "#f19837",  # naranja
    "#aaaaaa",  # gris
]

COLOR_NAMES = {
    "#ffd400": "amarillo", "#ff6666": "rojo", "#5fb236": "verde",
    "#2ea8e5": "azul", "#a28ae5": "violeta", "#e56eee": "magenta",
    "#f19837": "naranja", "#aaaaaa": "gris",
}


def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore


def nearest_zotero_color(hex_color: Optional[str]) -> str:
    """Mapea un hex arbitrario al color más cercano de la paleta fija de Zotero,
    comparando por MATIZ (hue), no por distancia RGB cruda — un highlight
    pastel (poca saturación) mide "cerca" del gris en distancia RGB directa
    aunque su matiz sea claramente amarillo o celeste; comparar en HSV evita
    esa clasificación errónea."""
    if not hex_color:
        return ZOTERO_ANNOTATION_COLORS[0]
    try:
        r, g, b = _hex_to_rgb(hex_color)
    except Exception:
        return ZOTERO_ANNOTATION_COLORS[0]
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if s < 0.15 or v < 0.15:
        return "#aaaaaa"  # casi sin color (gris/blanco/negro) -> gris de Zotero
    best, best_dist = ZOTERO_ANNOTATION_COLORS[0], 1.0
    for candidate in ZOTERO_ANNOTATION_COLORS:
        if candidate == "#aaaaaa":
            continue  # el gris se decide arriba por saturación, no por matiz
        cr, cg, cb = _hex_to_rgb(candidate)
        ch, _, _ = colorsys.rgb_to_hsv(cr / 255, cg / 255, cb / 255)
        diff = abs(h - ch)
        dist = min(diff, 1 - diff)  # distancia circular de matiz
        if dist < best_dist:
            best, best_dist = candidate, dist
    return best


_QUOTE_RE = re.compile(r"<blockquote>(.*?)</blockquote>", re.S)


def extract_quote_from_html(html: str) -> str:
    """Extrae el texto citado (blockquote) del HTML de una nota."""
    m = _QUOTE_RE.search(html or "")
    return re.sub(r"<[^>]+>", "", m.group(1)) if m else (html or "")


def normalize_for_matching(text: str) -> str:
    """Normaliza texto de API vs. texto de DOM/anotación para poder
    compararlos: quita TODOS los espacios (no solo repetidos), porque la
    diferencia entre ambas fuentes es justamente dónde el render del PDF
    partió una palabra por guionado o salto de línea."""
    text = (text or "").lower()
    text = text.replace("\u00ad", "")
    text = re.sub(r"[\u2018\u2019]", "'", text)
    text = re.sub(r"[\u201c\u201d]", '"', text)
    text = re.sub(r"\s+", "", text)
    return text


def partition_and_filter_notes(notes: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Separa las notas de un ítem en (notas_planas, notas_con_anotacion), y
    descarta las notas 'huérfanas' (sin annotationPosition) cuyo texto ya
    está subsumido dentro del annotationText de otra nota hermana con
    anotación — evita crear contenido duplicado en Zotero cuando Sciwheel
    consolidó un resaltado corto dentro de uno más amplio.
    """
    with_annotation = [n for n in notes if n.get("annotationPosition")]
    without_annotation = [n for n in notes if not n.get("annotationPosition")]

    annotation_texts_normalized = [
        normalize_for_matching(n.get("annotationText", "")) for n in with_annotation
    ]

    plain_notes: List[Dict[str, Any]] = []
    for orphan in without_annotation:
        orphan_quote_norm = normalize_for_matching(extract_quote_from_html(orphan.get("html", "")))
        subsumed = bool(orphan_quote_norm) and any(
            orphan_quote_norm in ann_text for ann_text in annotation_texts_normalized
        )
        if not subsumed:
            plain_notes.append(orphan)
        # si está subsumida: se descarta silenciosamente (su contenido ya
        # está representado, más completo, en la anotación que la contiene)

    return plain_notes, with_annotation


def item_type_breakdown(items: List[Dict[str, Any]]) -> Counter:
    """Cuenta ítems por itemType (para el reporte, ej. 'journalArticle: 1809')."""
    return Counter(it.get("itemType", "?") for it in items)


def annotation_color_breakdown(items: List[Dict[str, Any]]) -> Counter:
    """Cuenta anotaciones (con geometría) por color YA MAPEADO a la paleta de
    Zotero — refleja lo que realmente se va a ver en el visor, no el hex
    arbitrario de Sciwheel."""
    counter: Counter = Counter()
    for it in items:
        for n in it.get("notes", []):
            if n.get("annotationPosition"):
                counter[nearest_zotero_color(n.get("annotationColor"))] += 1
    return counter


def sum_supplement_bytes(items: List[Dict[str, Any]]) -> int:
    return sum(a.get("filesize", 0) for it in items for a in it.get("attachments", []))


def sum_pdf_bytes(items: List[Dict[str, Any]]) -> int:
    return sum((it.get("pdf") or {}).get("filesize", 0) for it in items)


def fmt_mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.2f} MB"


def normalize_item_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validación de campos del manifest antes de escribirlos en Zotero —
    NUNCA reconstruye ni infiere un valor (eso es trabajo del extractor);
    solo se niega a escribir un campo que puede verificar como inválido:
      - url relativa ("/fulltext/doi/...") -> se descarta (no es una URL válida).
      - abstractNote con <br> -> saltos de línea.
    (El caso de 'date' con CSL-JSON crudo se descarta antes, en
    sanitize_date_field() / validate_items() — acá no hace falta repetirlo.)
    """
    out = dict(fields)
    url = out.get("url")
    if isinstance(url, str) and url and not url.lower().startswith(("http://", "https://")):
        out.pop("url", None)
    abstract = out.get("abstractNote")
    if isinstance(abstract, str):
        out["abstractNote"] = re.sub(r"<br\s*/?>", "\n", abstract, flags=re.I)
    return out


def clean_annotation_position(pos: Dict[str, Any]) -> Dict[str, Any]:
    """Zotero espera exactamente pageIndex + rects. El extractor puede sumar
    campos auxiliares (p. ej. 'rect', el bounding box) que no hace falta enviar."""
    return {"pageIndex": pos.get("pageIndex", 0), "rects": pos.get("rects", [])}


def match_existing_collections(manifest_collections: List[Dict[str, Any]],
                                existing: List[Dict[str, Any]]) -> Dict[str, str]:
    """
    Empareja las colecciones del manifest con las que ya existen en Zotero por
    (colección padre, nombre) — las colecciones no admiten tags, así que la
    ruta es la única identidad estable disponible. Devuelve
    {manifest_id: zotero_key} solo para las que ya existen; una colección
    cuyo padre no existe tampoco puede existir.
    existing: [{"key":..., "name":..., "parent": <key> | None}, ...]
    """
    index: Dict[Tuple[str, str], str] = {}
    for e in sorted(existing, key=lambda x: x["key"]):
        index.setdefault((e["parent"] or "", e["name"]), e["key"])

    by_id = {c["id"]: c for c in manifest_collections}
    memo: Dict[str, Optional[str]] = {}

    def find(cid: str) -> Optional[str]:
        if cid in memo:
            return memo[cid]
        col = by_id[cid]
        pid = col["parent_id"]
        if pid is None:
            parent_key = ""
        else:
            found_parent = find(pid) if pid in by_id else None
            if found_parent is None:
                memo[cid] = None
                return None
            parent_key = found_parent
        memo[cid] = index.get((parent_key, col["name"]))
        return memo[cid]

    result: Dict[str, str] = {}
    for cid in by_id:
        key = find(cid)
        if key:
            result[cid] = key
    return result


# --------------------------------------------------------------------------------
# Configuración persistida (API key local + Zotero-Server-ID asociado)
# --------------------------------------------------------------------------------

def load_config(config_path: Path) -> Dict[str, Any]:
    """Lee la configuración persistida (api_key, server_id). Vacía si no existe."""
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(config_path: Path, data: Dict[str, Any]) -> None:
    """Guarda la configuración con permisos restrictivos (600)."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(config_path, 0o600)
    except Exception:
        pass


# --------------------------------------------------------------------------------
# Logger (mismo criterio que el extractor: Quiet / INFO / DEBUG)
# --------------------------------------------------------------------------------

def configure_logger(verbose_level: int) -> logging.Logger:
    """Configura el logger según el nivel de verbosidad (0: Quiet, 1: INFO, 2: DEBUG)."""
    logger = logging.getLogger("ZoteroImporter")
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
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger


# --------------------------------------------------------------------------------
# Cliente de la Zotero Local Write API
# --------------------------------------------------------------------------------

class ZoteroAuthDenied(Exception):
    """El usuario rechazó el diálogo de autorización en Zotero."""


class AttachmentTooLarge(Exception):
    """El archivo supera --max-attachment-mb (la subida lo carga entero en memoria)."""


class ZoteroLocalClient:
    """
    Cliente para http://127.0.0.1:23119/api/users/0/ (Zotero 10+ Local API).

    Responsable de: obtener/renovar la Local API Key, chequear Zotero-Server-ID,
    y exponer operaciones de lectura/escritura de alto nivel usadas por el
    importador (colecciones, ítems, notas, adjuntos).
    """

    def __init__(self, base_url: str, config_path: Path, logger: logging.Logger,
                 app_name: str = "Sciwheel -> Zotero Importer") -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.library_url = self.base_url + "users/0/"
        self.config_path = config_path
        self.logger = logger
        self.app_name = app_name
        self.session = requests.Session()

        cfg = load_config(config_path)
        self.api_key: Optional[str] = cfg.get("api_key")
        self.known_server_id: Optional[str] = cfg.get("server_id")
        self.current_server_id: Optional[str] = None

    # -- infraestructura -----------------------------------------------------

    def get_server_id(self) -> str:
        """Obtiene (y cachea) el Zotero-Server-ID de la instancia corriendo."""
        if self.current_server_id:
            return self.current_server_id
        resp = self.session.get(self.base_url, timeout=10)
        server_id = resp.headers.get("Zotero-Server-ID", "")
        self.current_server_id = server_id
        if self.known_server_id and server_id and server_id != self.known_server_id:
            self.logger.warning(
                "\t ⚠️ El Zotero-Server-ID cambió respecto de la config guardada "
                "(¿otra base de datos de Zotero?). Se va a re-autorizar."
            )
            self.api_key = None
        return server_id

    def authorize(self) -> None:
        """Solicita una Local API Key vía POST /local/authorize (dispara el diálogo)."""
        server_id = self.get_server_id()
        headers = {"Content-Type": "application/json", "Zotero-Server-ID": server_id}
        resp = self.session.post(
            self.base_url + "local/authorize",
            headers=headers,
            json={"appName": self.app_name},
            timeout=120,  # el usuario tiene que interactuar con el diálogo
        )
        if resp.status_code == 403:
            raise ZoteroAuthDenied("El usuario rechazó la autorización en Zotero.")
        if resp.status_code == 429:
            raise RuntimeError("Demasiados pedidos de autorización en poco tiempo. Reintentá en un minuto.")
        resp.raise_for_status()
        data = resp.json()
        self.api_key = data["key"]
        remembered = data.get("remember", False)
        if not remembered:
            self.logger.warning(
                "\t ⚠️ Elegiste 'Allow' (no 'Always Allow'): esta key es de un solo uso. "
                "Convendría re-ejecutar y elegir 'Always Allow' para no tener que "
                "autorizar en cada lote."
            )
        save_config(self.config_path, {"api_key": self.api_key, "server_id": server_id})
        self.known_server_id = server_id

    def ensure_authorized(self) -> None:
        """Garantiza que haya una API key disponible antes de escribir."""
        self.get_server_id()
        if not self.api_key:
            self.logger.info("\t 🔑 Sin API key local guardada — solicitando autorización a Zotero...")
            self.authorize()

    def _headers(self, write: bool) -> Dict[str, str]:
        # OJO: no fuerces Content-Type acá. requests ya lo setea bien solo:
        # application/json cuando se llama con json=..., y
        # application/x-www-form-urlencoded cuando se llama con data=...
        # (como en las dos fases de /file). Forzarlo rompía la subida de PDFs
        # con 400 Bad Request — ver informe_importacion_20260925T234508Z.json.
        headers: Dict[str, str] = {}
        server_id = self.get_server_id()
        if server_id:
            headers["Zotero-Server-ID"] = server_id
        if write and self.api_key:
            headers["Zotero-API-Key"] = self.api_key
        return headers

    def _request(self, method: str, path: str, write: bool = False,
                 retry_auth: bool = True, **kwargs) -> requests.Response:
        """Wrapper con headers correctos y reautorización automática ante 401."""
        url = self.library_url + path.lstrip("/")
        headers = kwargs.pop("headers", {})
        headers.update(self._headers(write=write))
        self.logger.debug(f"\t 🌐 {method} {url} params={kwargs.get('params')} headers={list(headers.keys())}")
        resp = self.session.request(method, url, headers=headers, timeout=60, **kwargs)

        if write and resp.status_code == 401 and retry_auth:
            self.logger.info("\t 🔑 La API key fue rechazada (401) — re-autorizando...")
            self.api_key = None
            self.authorize()
            return self._request(method, path, write=write, retry_auth=False, **kwargs)

        return resp

    @staticmethod
    def _raise_verbose(resp: requests.Response, context: str) -> None:
        """Como resp.raise_for_status(), pero conservando el cuerpo de la respuesta
        en el mensaje de error — Zotero suele explicar el motivo real del 400 ahí,
        y descartarlo (como hace raise_for_status() solo) hace imposible diagnosticar."""
        if resp.status_code >= 400:
            body = (resp.text or "")[:500]
            raise requests.exceptions.HTTPError(
                f"{resp.status_code} en {context} — cuerpo de la respuesta: {body!r}",
                response=resp,
            )

    # -- lectura ---------------------------------------------------------

    def _get_all(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        GET paginado de forma explícita (limit/start) hasta agotar resultados.
        No se asume nada sobre la paginación por defecto de la Local API: un
        `limit` fijo SIN avanzar `start` devolvería solo los primeros N objetos
        (y con miles de ítems, el importador creería que casi todo es nuevo y
        lo duplicaría).
        """
        page_size = 100
        start = 0
        out: List[Dict[str, Any]] = []
        while True:
            p = dict(params or {})
            p.update({"format": "json", "limit": page_size, "start": start})
            resp = self._request("GET", path, params=p)
            self._raise_verbose(resp, f"GET {path}")
            page = resp.json()
            out.extend(page)
            if len(page) < page_size:
                return out
            start += page_size

    def fetch_collections(self) -> List[Dict[str, Any]]:
        """Colecciones existentes: [{"key", "name", "parent": <key> | None}]."""
        result = []
        for entry in self._get_all("collections"):
            d = entry.get("data", entry)
            result.append({"key": d.get("key"), "name": d.get("name"),
                           "parent": d.get("parentCollection") or None})
        return result

    @staticmethod
    def _tag_values(data: Dict[str, Any]) -> List[str]:
        return [t.get("tag", "") for t in data.get("tags", [])]

    def fetch_imported_items(self) -> Dict[str, Dict[str, Any]]:
        """
        Estado real de Zotero para todo lo ya importado (marcado con tags):
        {sciwheel_id: {"key", "version", "tags", "collections",
                        "attachments": {resource_id: key},     # adjuntos con tag sciwheel-att:
                        "untagged_attachment_keys": [key, ...], # adjuntos de corridas viejas
                        "child_note_hashes": set()}}            # notas Y anotaciones

        Se hace con pocas consultas en bloque (ítems top-level, adjuntos, notas
        y anotaciones), no una por ítem. Las anotaciones son hijas del ADJUNTO,
        no del ítem, así que `/items/<key>/children` no las devuelve: por eso
        se consultan por itemType y se vinculan al ítem a través de su adjunto.
        """
        tops = self._get_all("items/top", {"tag": TAG_IMPORT_MARKER})
        self.logger.debug(f"\t 🔎 fetch_imported_items: {len(tops)} ítems top-level con tag {TAG_IMPORT_MARKER}.")

        result: Dict[str, Dict[str, Any]] = {}
        key_to_sciid: Dict[str, str] = {}
        for entry in tops:
            data = entry.get("data", entry)
            tags = self._tag_values(data)
            sciwheel_id = next((t.split("sciwheel-id:", 1)[1] for t in tags if t.startswith("sciwheel-id:")), None)
            if not sciwheel_id:
                continue
            result[sciwheel_id] = {
                "key": data.get("key"), "version": data.get("version"),
                "tags": tags, "collections": data.get("collections", []),
                "attachments": {}, "untagged_attachment_keys": [],
                "child_note_hashes": set(), "date": data.get("date", ""),
            }
            key_to_sciid[data.get("key")] = sciwheel_id

        att_key_to_sciid: Dict[str, str] = {}
        for entry in self._get_all("items", {"tag": TAG_IMPORT_MARKER, "itemType": "attachment"}):
            d = entry.get("data", entry)
            sid = key_to_sciid.get(d.get("parentItem"))
            if not sid:
                continue
            att_key_to_sciid[d.get("key")] = sid
            rid = next((t.split(ATT_TAG_PREFIX, 1)[1] for t in self._tag_values(d)
                        if t.startswith(ATT_TAG_PREFIX)), None)
            if rid:
                result[sid]["attachments"][rid] = d.get("key")
            else:
                result[sid]["untagged_attachment_keys"].append(d.get("key"))

        for entry in self._get_all("items", {"tag": TAG_IMPORT_MARKER, "itemType": "note"}):
            d = entry.get("data", entry)
            sid = key_to_sciid.get(d.get("parentItem"))
            if sid:
                result[sid]["child_note_hashes"].update(
                    t for t in self._tag_values(d) if t.startswith("sciwheel-note:"))

        for entry in self._get_all("items", {"tag": TAG_IMPORT_MARKER, "itemType": "annotation"}):
            d = entry.get("data", entry)
            sid = att_key_to_sciid.get(d.get("parentItem"))
            if sid:
                result[sid]["child_note_hashes"].update(
                    t for t in self._tag_values(d) if t.startswith("sciwheel-note:"))

        return result

    # -- escritura: colecciones -------------------------------------------

    def create_collections_batch(self, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        batch: [{"name": str, "parentCollection": <key str> | False}, ...]
        Devuelve la respuesta cruda de Zotero (formato successful/failed).
        """
        resp = self._request("POST", "collections", write=True, json=batch)
        self._raise_verbose(resp, "create_collections_batch")
        return resp.json()

    # -- escritura: ítems ---------------------------------------------------

    def create_items_batch(self, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        resp = self._request("POST", "items", write=True, json=batch)
        self._raise_verbose(resp, "create_items_batch")
        return resp.json()

    def create_notes_batch(self, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.create_items_batch(batch)  # las notas se crean igual, vía POST /items

    def union_update_item(self, key: str, version: int,
                           add_tags: List[str], add_collections: List[str]) -> None:
        """PATCH de un ítem existente para sumar tags/colecciones (nunca otros campos)."""
        if not add_tags and not add_collections:
            return
        get_resp = self._request("GET", f"items/{key}", params={"format": "json"})
        self._raise_verbose(get_resp, "union_update_item (GET previo)")
        current = get_resp.json()["data"]

        current_tags = {t["tag"] for t in current.get("tags", [])}
        for t in add_tags:
            if t not in current_tags:
                current["tags"].append(zotero_tag(t))

        current_collections = set(current.get("collections", []))
        for c in add_collections:
            current_collections.add(c)
        current["collections"] = sorted(current_collections)
        current["version"] = version

        patch_resp = self._request("PUT", f"items/{key}", write=True, json=current,
                                    headers={"If-Unmodified-Since-Version": str(version)})
        self._raise_verbose(patch_resp, "union_update_item (PUT)")

    # -- escritura: adjuntos (subida en 3 fases) -----------------------------

    def delete_item(self, key: str, version: Optional[int]) -> None:
        """Borra un ítem propio (usado para no dejar adjuntos vacíos si falla la subida)."""
        headers = {"If-Unmodified-Since-Version": str(version)} if version is not None else {}
        resp = self._request("DELETE", f"items/{key}", write=True, headers=headers)
        self._raise_verbose(resp, f"delete_item {key}")

    def upload_attachment(self, parent_key: str, title: str, file_meta: Dict[str, Any],
                           resource_id: Optional[Any] = None) -> str:
        """
        file_meta: {"path", "md5", "filesize", "mtime" (ISO 8601), y opcional "filename"}
        Crea el ítem 'attachment' (imported_file) hijo de parent_key y sube el
        archivo en el flujo de 3 fases documentado por Zotero. Sirve tanto
        para el PDF principal como para archivos suplementarios (cualquier
        tipo de archivo). Devuelve la key del adjunto creado (la necesita
        quien vaya a colgarle anotaciones).

        Si la subida falla después de haber creado el ítem, se borra el
        adjunto vacío: de lo contrario quedaría marcado como "ya importado"
        (por su tag) sin archivo, y ningún reintento lo completaría.
        """
        filename = file_meta.get("filename") or Path(file_meta["path"]).name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        tags = [zotero_tag(TAG_IMPORT_MARKER)]
        if resource_id not in (None, ""):
            tags.append(zotero_tag(att_tag(resource_id)))

        # 1. Crear el ítem attachment hijo
        attachment_item = {
            "itemType": "attachment",
            "parentItem": parent_key,
            "linkMode": "imported_file",
            "title": title,
            "filename": filename,
            "contentType": content_type,
            "tags": tags,
        }
        create_resp = self.create_items_batch([attachment_item])
        successful = create_resp.get("successful") or create_resp.get("success") or {}
        if "0" not in successful:
            failed = create_resp.get("failed", {})
            raise RuntimeError(f"No se pudo crear el ítem attachment: {failed.get('0')}")
        att_data = successful["0"]
        att_key = att_data["key"] if isinstance(att_data, dict) else att_data
        att_version = att_data.get("version") if isinstance(att_data, dict) else None

        try:
            mtime_epoch_ms = int(
                datetime.strptime(file_meta["mtime"], "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=timezone.utc).timestamp() * 1000
            )
            phase1 = self._request(
                "POST", f"items/{att_key}/file", write=True,
                data={
                    "md5": file_meta["md5"],
                    "filename": filename,
                    "filesize": file_meta["filesize"],
                    "mtime": mtime_epoch_ms,
                },
                headers={"If-None-Match": "*"},
            )
            self._raise_verbose(phase1, "upload_attachment fase 1 (autorizar subida)")
            phase1_data = phase1.json()
            if phase1_data.get("exists"):
                return att_key  # ya estaba subido (mismo md5)

            upload_url = phase1_data["url"]
            with open(file_meta["path"], "rb") as f:
                file_bytes = f.read()
            # El endpoint local espera multipart/form-data con un campo "file"
            # (mismo contrato que el upload por POST-policy a S3 en la nube),
            # no el binario crudo en el cuerpo.
            phase2 = self.session.post(
                upload_url,
                files={"file": (filename, file_bytes, content_type)},
            )
            del file_bytes
            if phase2.status_code != 201:
                body = (phase2.text or "")[:500]
                raise RuntimeError(
                    f"Fase 2 de subida (POST a {upload_url}) falló con status "
                    f"{phase2.status_code} — cuerpo: {body!r}"
                )

            phase3 = self._request(
                "POST", f"items/{att_key}/file", write=True,
                data={"upload": phase1_data["uploadKey"]},
                headers={"If-None-Match": "*"},
            )
            self._raise_verbose(phase3, "upload_attachment fase 3 (confirmar subida)")
            return att_key
        except Exception:
            try:
                self.delete_item(att_key, att_version)
                self.logger.warning(f"\t 🧹 Se borró el adjunto vacío {att_key} tras fallar la subida.")
            except Exception as cleanup_exc:
                self.logger.warning(f"\t ⚠️ No se pudo borrar el adjunto vacío {att_key}: {cleanup_exc}")
            raise


# --------------------------------------------------------------------------------
# Carga y validación del manifest
# --------------------------------------------------------------------------------

def load_manifest(manifest_path: Path, logger: logging.Logger) -> Dict[str, Any]:
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    logger.info(
        f"\t 📂 Manifest cargado: {len(manifest.get('items', []))} ítems, "
        f"{len(manifest.get('collections', []))} colecciones "
        f"(extraído: {manifest.get('extracted_at', '¿?')})"
    )
    return manifest


def validate_items(manifest: Dict[str, Any], logger: logging.Logger) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Separa ítems válidos de inválidos (estructura insuficiente para Zotero)."""
    failed_extraction_ids = {
        str(fi.get("sciwheel_id")) for fi in manifest.get("failed_items", [])
    }
    valid: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []

    for item in manifest.get("items", []):
        sci_id = str(item.get("sciwheel_id"))
        if sci_id in failed_extraction_ids:
            invalid.append({"sciwheel_id": sci_id, "etapa": "extraccion",
                             "error_type": "ExtractionFailed",
                             "error_message": "Excluido: la extracción de este ítem había fallado."})
            continue
        if item.get("itemType") not in KNOWN_ZOTERO_ITEM_TYPES:
            invalid.append({"sciwheel_id": sci_id, "etapa": "validacion",
                             "error_type": "UnknownItemType",
                             "error_message": f"itemType desconocido: {item.get('itemType')!r}"})
            continue
        if not item.get("title") and not item.get("creators"):
            invalid.append({"sciwheel_id": sci_id, "etapa": "validacion",
                             "error_type": "InsufficientData",
                             "error_message": "Sin título ni autores."})
            continue
        sanitize_date_field(item, logger)
        valid.append(item)

    if invalid:
        logger.warning(f"\t ⚠️ {len(invalid)} ítems excluidos antes de tocar la red "
                        f"(ver el reporte final para el detalle).")
    return valid, invalid


# --------------------------------------------------------------------------------
# Orquestador principal
# --------------------------------------------------------------------------------

class SciwheelZoteroImporter:
    def __init__(self, args: argparse.Namespace, logger: logging.Logger) -> None:
        self.args = args
        self.logger = logger
        self.client = ZoteroLocalClient(args.zotero_base_url, args.config, logger)
        self.failed: List[Dict[str, Any]] = []
        self.counters: Dict[str, int] = {
            "colecciones_creadas": 0, "colecciones_existentes": 0,
            "items_creados": 0, "items_actualizados_aditivamente": 0,
            "items_sin_cambios": 0, "notas_creadas": 0, "notas_existentes": 0,
            "anotaciones_creadas": 0, "anotaciones_existentes": 0,
            "adjuntos_subidos": 0, "adjuntos_existentes": 0,
            "suplementarios_subidos": 0, "suplementarios_omitidos_por_tamano": 0,
        }
        self.existing_collection_map: Dict[str, str] = {}

    # -- helpers de batching -------------------------------------------------

    @staticmethod
    def _chunks(seq: List[Any], size: int) -> List[List[Any]]:
        return [seq[i:i + size] for i in range(0, len(seq), size)]

    def _record_failure(self, sciwheel_id: str, etapa: str, exc: Exception,
                         detail: Optional[str] = None) -> None:
        message = f"{detail}: {exc}" if detail else str(exc)
        self.logger.error(f"\t ❌ [{etapa}] Falló ítem {sciwheel_id}: {message}")
        self.failed.append({
            "sciwheel_id": sciwheel_id, "etapa": etapa,
            "error_type": type(exc).__name__, "error_message": message,
        })

    # -- Fase A: carga -------------------------------------------------------

    def load_and_filter(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        manifest = load_manifest(self.args.manifest, self.logger)
        valid_items, invalid_items = validate_items(manifest, self.logger)
        self.failed.extend(invalid_items)

        if self.args.retry_failed:
            with open(self.args.retry_failed, "r", encoding="utf-8") as f:
                prev_report = json.load(f)
            retry_ids = {str(fi["sciwheel_id"]) for fi in prev_report.get("failed_items", [])}
            valid_items = [it for it in valid_items if str(it["sciwheel_id"]) in retry_ids]
            self.logger.info(f"\t 🔁 --retry-failed: acotando la corrida a {len(valid_items)} "
                              f"ítems marcados como fallidos en '{self.args.retry_failed.name}'.")

        manifest["items"] = valid_items
        return manifest, invalid_items

    # -- Fase B: reconciliación ----------------------------------------------

    @staticmethod
    def main_pdf_key(item: Dict[str, Any], info: Dict[str, Any]) -> Optional[str]:
        """Key del adjunto PDF principal ya existente en Zotero (o None)."""
        rid = item.get("pdf_resource_id")
        if rid not in (None, "") and str(rid) in info["attachments"]:
            return info["attachments"][str(rid)]
        # adjuntos de corridas anteriores, sin tag de resource_id: el único
        # que existía entonces era el PDF principal
        if info["untagged_attachment_keys"]:
            return info["untagged_attachment_keys"][0]
        return None

    def reconcile(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info("\t 🔎 Consultando estado actual de Zotero (colecciones e ítems ya importados)...")
        self.existing_collection_map = match_existing_collections(
            manifest["collections"], self.client.fetch_collections())
        self.logger.info(f"\t 🔎 {len(self.existing_collection_map)} de {len(manifest['collections'])} "
                          f"colecciones del manifest ya existen en Zotero.")
        existing = self.client.fetch_imported_items()
        self.logger.info(f"\t 🔎 {len(existing)} ítems ya marcados como importados en Zotero.")

        plan = {"nuevos": [], "con_novedades": [], "sin_cambios": []}
        for item in manifest["items"]:
            sci_id = str(item["sciwheel_id"])
            if sci_id not in existing:
                plan["nuevos"].append(item)
                continue

            info = existing[sci_id]
            item_tags = {t["tag"] for t in item.get("tags", []) if not is_path_tag(t["tag"])} | {TAG_IMPORT_MARKER, id_tag(sci_id)}
            new_tags = sorted(item_tags - set(info["tags"]))

            # las colecciones del manifest son ids propios ("proj_123"); se
            # comparan por la key real de Zotero (si la colección aún no existe,
            # la membresía es nueva por definición)
            existing_keys = set(info["collections"])
            new_collections = sorted(
                cid for cid in item.get("collection_ids", [])
                if self.existing_collection_map.get(cid) not in existing_keys
            )

            relevant_notes, relevant_annotations = partition_and_filter_notes(item.get("notes", []))
            new_notes = [
                n for n in relevant_notes + relevant_annotations
                if n.get("hash") and n["hash"] not in info["child_note_hashes"]
            ]

            main_key = self.main_pdf_key(item, info)
            needs_attachment = bool(item.get("pdf")) and main_key is None
            missing_supplements = [
                a for a in item.get("attachments", [])
                if str(a.get("resource_id")) not in info["attachments"]
            ]

            if new_tags or new_collections or new_notes or needs_attachment or missing_supplements:
                plan["con_novedades"].append({
                    "item": item, "zotero_key": info["key"], "zotero_version": info["version"],
                    "new_tags": new_tags, "new_collections": new_collections,
                    "new_notes": new_notes, "needs_attachment": needs_attachment,
                    "attachment_key": main_key,
                    "missing_supplements": missing_supplements,
                })
            else:
                plan["sin_cambios"].append(item)

        return plan

    # -- Reporte detallado, compartido entre dry-run y ejecución real ----------

    def build_and_print_report(self, mode: str, plan: Dict[str, Any], invalid_items: List[Dict[str, Any]],
                                collections_to_create: List[Dict[str, Any]],
                                report_path: Optional[Path] = None) -> Dict[str, Any]:
        """
        Reporte analítico detallado, con el mismo formato en dry-run y en -x
        (mismas secciones; en dry-run son cifras "a procesar", en ejecución
        son cifras reales, con incidencias agrupadas por etapa de fallo).
        """
        relevant_items = plan["nuevos"] + [e["item"] for e in plan["con_novedades"]]
        tipos = item_type_breakdown(relevant_items)
        colores = annotation_color_breakdown(relevant_items)

        notas_planas = anotaciones = subsumidas = 0
        faltantes_en_disco: List[str] = []

        def cuenta_notas(item: Dict[str, Any], notas: List[Dict[str, Any]]) -> None:
            nonlocal notas_planas, anotaciones, subsumidas
            planas, con_anot = partition_and_filter_notes(item.get("notes", []))
            hashes = {n.get("hash") for n in notas}
            notas_planas += sum(1 for n in planas if n.get("hash") in hashes)
            anotaciones += sum(1 for n in con_anot if n.get("hash") in hashes)
            subsumidas += len(item.get("notes", [])) - len(planas) - len(con_anot)

        for it in plan["nuevos"]:
            cuenta_notas(it, it.get("notes", []))
        for e in plan["con_novedades"]:
            cuenta_notas(e["item"], e["new_notes"])

        pdfs_objetivo = [it for it in plan["nuevos"] if it.get("pdf")] + \
            [e["item"] for e in plan["con_novedades"] if e["needs_attachment"] and e["item"].get("pdf")]
        supl_objetivo = [a for it in plan["nuevos"] for a in it.get("attachments", [])] + \
            [a for e in plan["con_novedades"] for a in e["missing_supplements"]]

        for meta in [it["pdf"] for it in pdfs_objetivo] + supl_objetivo:
            if not os.path.exists(meta.get("path", "")):
                faltantes_en_disco.append(meta.get("path", "<sin ruta>"))

        bytes_pdf = sum(it["pdf"].get("filesize", 0) for it in pdfs_objetivo)
        bytes_supl = sum(a.get("filesize", 0) for a in supl_objetivo)

        if mode == "dry-run":
            colecciones_linea = [
                ("A crear", len(collections_to_create)),
                ("Ya existen en Zotero", len(self.existing_collection_map)),
            ]
            items_linea = [
                ("Nuevos a crear", len(plan["nuevos"])),
                ("Con novedades aditivas (tags/colecciones/notas/adjuntos)", len(plan["con_novedades"])),
                ("Sin cambios (ya al día)", len(plan["sin_cambios"])),
                ("Excluidos antes de tocar la red", len(invalid_items)),
            ]
            notas_linea = [
                ("Notas planas a crear", notas_planas),
                ("Anotaciones a crear (con geometría)", anotaciones),
                ("Huérfanas subsumidas, descartadas", subsumidas),
            ]
            adjuntos_linea = [
                ("PDFs principales a subir", len(pdfs_objetivo)),
                ("Peso total de PDFs principales", fmt_mb(bytes_pdf)),
                ("Suplementarios a subir", len(supl_objetivo)),
                ("Peso total de suplementarios", fmt_mb(bytes_supl)),
            ]
            incidencias_linea = [("Archivos del manifest ausentes en disco", len(faltantes_en_disco))]
        else:
            c = self.counters
            colecciones_linea = [("Creadas", c["colecciones_creadas"]), ("Ya existían", c["colecciones_existentes"])]
            items_linea = [
                ("Creados", c["items_creados"]),
                ("Actualizados aditivamente", c["items_actualizados_aditivamente"]),
                ("Sin cambios (ya al día)", c["items_sin_cambios"]),
                ("Excluidos antes de tocar la red", len(invalid_items)),
            ]
            notas_linea = [
                ("Notas planas creadas", c["notas_creadas"]),
                ("Anotaciones creadas (con geometría)", c["anotaciones_creadas"]),
            ]
            adjuntos_linea = [
                ("PDFs principales subidos", c["adjuntos_subidos"]),
                ("Suplementarios subidos", c["suplementarios_subidos"]),
                ("Suplementarios omitidos por tamaño (--max-attachment-mb)", c["suplementarios_omitidos_por_tamano"]),
            ]
            fallas_por_etapa = Counter(f["etapa"] for f in self.failed)
            incidencias_linea = [(f"Fallidos en etapa '{etapa}'", n) for etapa, n in fallas_por_etapa.most_common()]
            incidencias_linea.append(("Total fallidos", len(self.failed)))

        titulo = ("REPORTE DE SIMULACIÓN (dry-run) — no se escribió nada en Zotero" if mode == "dry-run"
                  else "REPORTE DETALLADO DE LA IMPORTACIÓN")
        print("\n" + "=" * 80)
        print(titulo)
        print("=" * 80)
        print(f"  • Manifest                                            : {self.args.manifest}")
        print("-" * 80)
        print("1. COLECCIONES")
        print("-" * 80)
        for k, v in colecciones_linea:
            print(f"  • {k:55s}: {v}")
        print("-" * 80)
        print("2. ÍTEMS BIBLIOGRÁFICOS")
        print("-" * 80)
        for k, v in items_linea:
            print(f"  • {k:55s}: {v}")
        if tipos:
            etiqueta_tipos = "Desglose por tipo de documento"
            print(f"  • {etiqueta_tipos:55s}: "
                  + ", ".join(f"{t}: {n}" for t, n in tipos.most_common()))
        print("-" * 80)
        print("3. NOTAS Y ANOTACIONES")
        print("-" * 80)
        for k, v in notas_linea:
            print(f"  • {k:55s}: {v}")
        if colores:
            etiqueta_colores = "Colores de anotación (mapeados a paleta Zotero)"
            print(f"  • {etiqueta_colores:55s}: "
                  + ", ".join(f"{COLOR_NAMES.get(c, c)}: {n}" for c, n in colores.most_common()))
        print("-" * 80)
        print("4. ADJUNTOS")
        print("-" * 80)
        for k, v in adjuntos_linea:
            print(f"  • {k:55s}: {v}")
        print("-" * 80)
        print("5. INCIDENCIAS")
        print("-" * 80)
        for k, v in incidencias_linea:
            print(f"  • {k:55s}: {v}")
        if faltantes_en_disco:
            print("\n  ⚠️ Archivos del manifest que NO existen en disco:")
            for p in faltantes_en_disco[:10]:
                print(f"      - {p}")
            if len(faltantes_en_disco) > 10:
                print(f"      ... y {len(faltantes_en_disco) - 10} más")
        print("=" * 80)
        if mode == "dry-run":
            print("Corré con -x / --execute para aplicar estos cambios.\n")
        elif report_path:
            print(f"Informe completo (JSON): {report_path}\n")

        return {
            "modo": mode,
            "colecciones": dict(colecciones_linea),
            "items": dict(items_linea),
            "items_por_tipo": dict(tipos),
            "notas": dict(notas_linea),
            "colores_anotacion": {COLOR_NAMES.get(c, c): n for c, n in colores.items()},
            "adjuntos": dict(adjuntos_linea),
            "incidencias": dict(incidencias_linea),
            "archivos_ausentes_en_disco": faltantes_en_disco,
        }

    # -- Fase D: escritura ----------------------------------------------------

    def execute_collections(self, manifest: Dict[str, Any]) -> Dict[str, str]:
        """Crea colecciones faltantes de raíz a hojas. Devuelve {manifest_id: zotero_key}."""
        # Las que ya existen en Zotero (emparejadas por padre+nombre en la
        # reconciliación) se reutilizan: crear todas de nuevo en cada corrida
        # duplicaría la jerarquía completa.
        key_map: Dict[str, str] = dict(self.existing_collection_map)
        self.counters["colecciones_existentes"] = len(key_map)

        # Orden topológico simple: repetir pasadas hasta resolver todo.
        pending = [c for c in manifest["collections"] if c["id"] not in key_map]
        safety_counter = 0
        while pending and safety_counter < 50:
            safety_counter += 1
            still_pending = []
            batch, batch_ids = [], []
            for col in pending:
                parent_id = col["parent_id"]
                if parent_id is None:
                    parent_key = False
                elif parent_id in key_map:
                    parent_key = key_map[parent_id]
                else:
                    still_pending.append(col)
                    continue
                batch.append({"name": col["name"], "parentCollection": parent_key})
                batch_ids.append(col["id"])

            for chunk_items, chunk_ids in zip(self._chunks(batch, self.args.batch_size),
                                               self._chunks(batch_ids, self.args.batch_size)):
                try:
                    resp = self.client.create_collections_batch(chunk_items)
                    successful = resp.get("successful") or resp.get("success") or {}
                    for idx_str, obj in successful.items():
                        idx = int(idx_str)
                        manifest_id = chunk_ids[idx]
                        key_map[manifest_id] = obj["key"] if isinstance(obj, dict) else obj
                        self.counters["colecciones_creadas"] += 1
                    for idx_str, err in resp.get("failed", {}).items():
                        idx = int(idx_str)
                        self._record_failure(chunk_ids[idx], "coleccion", RuntimeError(str(err)))
                except Exception as e:
                    for manifest_id in chunk_ids:
                        self._record_failure(manifest_id, "coleccion", e)

            pending = still_pending

        if pending:
            for col in pending:
                self._record_failure(col["id"], "coleccion",
                                      RuntimeError("No se pudo resolver su colección padre."))
        return key_map

    def execute_new_items(self, new_items: List[Dict[str, Any]], collection_key_map: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
        """Crea ítems nuevos por lotes. Devuelve {sciwheel_id: {key, version}}."""
        created: Dict[str, Dict[str, Any]] = {}
        for chunk in self._chunks(new_items, self.args.batch_size):
            payload = []
            for it in chunk:
                sci_id = str(it["sciwheel_id"])
                zot_collections = [collection_key_map[cid] for cid in it.get("collection_ids", [])
                                    if cid in collection_key_map]
                tags = [zotero_tag(t["tag"]) for t in it.get("tags", []) if not is_path_tag(t["tag"])]
                tags.append(zotero_tag(TAG_IMPORT_MARKER))
                tags.append(zotero_tag(id_tag(sci_id)))
                payload.append({
                    "itemType": it["itemType"],
                    "title": it.get("title", ""),
                    "creators": it.get("creators", []),
                    "tags": tags,
                    "collections": zot_collections,
                    **normalize_item_fields(it.get("fields", {})),
                })
            try:
                resp = self.client.create_items_batch(payload)
                successful = resp.get("successful") or resp.get("success") or {}
                for idx_str, obj in successful.items():
                    idx = int(idx_str)
                    sci_id = str(chunk[idx]["sciwheel_id"])
                    obj_data = obj if isinstance(obj, dict) else {"key": obj, "version": None}
                    created[sci_id] = {"key": obj_data.get("key", obj_data),
                                        "version": obj_data.get("version")}
                    self.counters["items_creados"] += 1
                for idx_str, err in resp.get("failed", {}).items():
                    idx = int(idx_str)
                    self._record_failure(str(chunk[idx]["sciwheel_id"]), "item_creacion", RuntimeError(str(err)))
            except Exception as e:
                for it in chunk:
                    self._record_failure(str(it["sciwheel_id"]), "item_creacion", e)
        return created

    def execute_unions(self, con_novedades: List[Dict[str, Any]], collection_key_map: Dict[str, str]) -> None:
        for entry in con_novedades:
            sci_id = str(entry["item"]["sciwheel_id"])
            try:
                add_collections = [collection_key_map[cid] for cid in entry["new_collections"]
                                    if cid in collection_key_map]
                self.client.union_update_item(
                    entry["zotero_key"], entry["zotero_version"],
                    add_tags=entry["new_tags"], add_collections=add_collections,
                )
                if entry["new_tags"] or add_collections:
                    self.counters["items_actualizados_aditivamente"] += 1
            except Exception as e:
                self._record_failure(sci_id, "item_reconciliacion_aditiva", e)

    def build_annotation_payload(self, note: Dict[str, Any], attachment_key: str) -> Dict[str, Any]:
        pos = clean_annotation_position(note["annotationPosition"])
        annotation_type = note.get("annotationType", "highlight")
        payload = {
            "itemType": "annotation",
            "parentItem": attachment_key,
            "annotationType": annotation_type,
            "annotationColor": nearest_zotero_color(note.get("annotationColor")),
            "annotationPageLabel": str(pos.get("pageIndex", 0) + 1),
            "annotationSortIndex": f"{pos.get('pageIndex', 0):05d}|000000|00000",
            "annotationPosition": json.dumps(pos),
            "tags": [zotero_tag(TAG_IMPORT_MARKER), zotero_tag(note["hash"])],
        }
        if annotation_type != "note":
            dom_text = note.get("annotationText", "")
            api_quote = extract_quote_from_html(note.get("html", ""))
            # El texto del DOM trae espacios de guionado ("com mon"); el de la
            # API es el limpio. Se usa el limpio solo si es el mismo contenido
            # (igual sin espacios), para no reemplazar un resaltado más amplio.
            same = api_quote and normalize_for_matching(api_quote) == normalize_for_matching(dom_text)
            payload["annotationText"] = api_quote.strip() if same else dom_text
        return payload

    def execute_notes_and_annotations(self, item_key: str, attachment_key: Optional[str],
                                       sci_id: str, notes: List[Dict[str, Any]],
                                       pdf_expected: bool = True) -> None:
        """
        Crea las notas/anotaciones nuevas de un ítem. Filtra primero las notas
        huérfanas subsumidas en un resaltado más amplio (ver DESIGN.md §12).
        Las notas con geometría se crean como itemType 'annotation' colgando
        del ADJUNTO PDF; el resto, como 'note' de siempre colgando del ítem.
        Si no hay adjunto (falló la subida o el ítem no tiene PDF), las notas
        con geometría degradan a nota de texto plano — nunca se pierden.
        """
        if not notes:
            return
        plain_notes, annotation_notes = partition_and_filter_notes(notes)

        if attachment_key is None and annotation_notes:
            if pdf_expected:
                # El ítem tiene PDF en el manifest pero no quedó adjunto (falló
                # la subida, ya registrada como fallo). Crear estos resaltados
                # como nota plana los marcaría como "ya importados" y un
                # reintento nunca los convertiría en anotaciones reales: se
                # difieren hasta que el PDF esté (--retry-failed).
                self.logger.warning(
                    f"\t ⏸️ Ítem {sci_id}: {len(annotation_notes)} resaltado(s) con geometría "
                    f"diferidos hasta que el PDF se adjunte (reintentar con --retry-failed)."
                )
            else:
                self.logger.warning(
                    f"\t ⚠️ Ítem {sci_id}: {len(annotation_notes)} resaltado(s) con geometría "
                    f"pero el ítem no tiene PDF — se crean como nota de texto plano."
                )
                plain_notes = plain_notes + annotation_notes
            annotation_notes = []

        for chunk in self._chunks(plain_notes, self.args.batch_size):
            payload = [{
                "itemType": "note",
                "parentItem": item_key,
                "note": n["html"],
                "tags": [zotero_tag(TAG_IMPORT_MARKER), zotero_tag(n["hash"])],
            } for n in chunk]
            try:
                resp = self.client.create_notes_batch(payload)
                successful = resp.get("successful") or resp.get("success") or {}
                self.counters["notas_creadas"] += len(successful)
                for idx_str, err in resp.get("failed", {}).items():
                    self._record_failure(sci_id, "nota", RuntimeError(str(err)))
            except Exception as e:
                self._record_failure(sci_id, "nota", e)

        for chunk in self._chunks(annotation_notes, self.args.batch_size):
            payload = [self.build_annotation_payload(n, attachment_key) for n in chunk]
            try:
                resp = self.client.create_notes_batch(payload)
                successful = resp.get("successful") or resp.get("success") or {}
                self.counters["anotaciones_creadas"] += len(successful)
                for idx_str, err in resp.get("failed", {}).items():
                    self._record_failure(sci_id, "anotacion", RuntimeError(str(err)))
            except Exception as e:
                self._record_failure(sci_id, "anotacion", e)

    def execute_attachment(self, item_key: str, sci_id: str, title: str,
                            pdf_meta: Dict[str, Any], resource_id: Optional[Any] = None) -> Optional[str]:
        """PDF principal. Devuelve la key del adjunto (o None si falló)."""
        try:
            att_key = self.client.upload_attachment(item_key, title, pdf_meta, resource_id=resource_id)
            self.counters["adjuntos_subidos"] += 1
            return att_key
        except Exception as e:
            self._record_failure(sci_id, "adjunto_pdf", e)
            return None

    def execute_supplements(self, item_key: str, sci_id: str,
                             supplements: List[Dict[str, Any]]) -> None:
        """Archivos suplementarios del ítem: adjuntos hermanos del PDF principal,
        cada uno aislado en su propio try/except (uno que falle no frena al resto)."""
        limit_bytes = self.args.max_attachment_mb * 1024 * 1024
        for att in supplements:
            label = att.get("filename") or att.get("title") or str(att.get("resource_id"))
            try:
                size = att.get("filesize") or os.path.getsize(att["path"])
                if size > limit_bytes:
                    self.counters["suplementarios_omitidos_por_tamano"] += 1
                    raise AttachmentTooLarge(
                        f"{size / 1048576:.0f} MB supera --max-attachment-mb={self.args.max_attachment_mb}")
                self.client.upload_attachment(item_key, att.get("title") or label, att,
                                               resource_id=att.get("resource_id"))
                self.counters["suplementarios_subidos"] += 1
            except Exception as e:
                self._record_failure(sci_id, "adjunto_suplementario", e, detail=label)

    # -- Fase E: reporte final -------------------------------------------------

    def write_final_report(self, report_dir: Path, plan: Dict[str, Any], invalid_items: List[Dict[str, Any]],
                            collections_to_create: List[Dict[str, Any]]) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = report_dir / f"informe_importacion_{timestamp}.json"

        detalle = self.build_and_print_report("execute", plan, invalid_items, collections_to_create,
                                               report_path=report_path)

        report = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "modo": "execute",
            "contadores": self.counters,   # se mantiene tal cual: lo usa --retry-failed
            "detalle": detalle,
            "failed_items": self.failed,   # se mantiene tal cual: lo usa --retry-failed
        }
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        return report_path

    # -- pipeline completo -------------------------------------------------

    def run(self) -> None:
        manifest, invalid_items = self.load_and_filter()

        if self.args.execute:
            self.client.ensure_authorized()

        plan = self.reconcile(manifest)

        collections_to_create = [
            c for c in manifest["collections"] if c["id"] not in self.existing_collection_map
        ]

        if not self.args.execute:
            self.build_and_print_report("dry-run", plan, invalid_items, collections_to_create)
            return

        collection_key_map = self.execute_collections(manifest)
        self.logger.info(f"\t 📁 Colecciones: {self.counters['colecciones_creadas']} creadas, "
                          f"{self.counters['colecciones_existentes']} ya existían.")
        new_item_keys = self.execute_new_items(plan["nuevos"], collection_key_map)
        self.logger.info(f"\t 📄 Ítems nuevos creados: {self.counters['items_creados']} de {len(plan['nuevos'])}.")

        # PDF principal PRIMERO (para tener su key), después notas/anotaciones —
        # las anotaciones con geometría cuelgan del adjunto, no del ítem.
        total = len(plan["nuevos"])
        for n, it in enumerate(plan["nuevos"], 1):
            sci_id = str(it["sciwheel_id"])
            if sci_id not in new_item_keys:
                continue  # falló su creación, ya quedó registrado
            key = new_item_keys[sci_id]["key"]
            attachment_key = None
            if it.get("pdf"):
                attachment_key = self.execute_attachment(key, sci_id, it.get("title", ""), it["pdf"],
                                                          resource_id=it.get("pdf_resource_id"))
            self.execute_notes_and_annotations(key, attachment_key, sci_id, it.get("notes", []),
                                                pdf_expected=bool(it.get("pdf")))
            if n % 100 == 0:
                self.logger.info(f"\t ⏳ PDFs, notas y anotaciones: {n}/{total} ítems nuevos procesados...")

        # reconciliación aditiva de ítems ya existentes
        self.execute_unions(plan["con_novedades"], collection_key_map)
        for entry in plan["con_novedades"]:
            sci_id = str(entry["item"]["sciwheel_id"])
            attachment_key = entry.get("attachment_key")
            if entry["needs_attachment"] and entry["item"].get("pdf"):
                attachment_key = self.execute_attachment(
                    entry["zotero_key"], sci_id, entry["item"].get("title", ""), entry["item"]["pdf"],
                    resource_id=entry["item"].get("pdf_resource_id"))
            if entry["new_notes"]:
                self.execute_notes_and_annotations(entry["zotero_key"], attachment_key, sci_id,
                                                    entry["new_notes"],
                                                    pdf_expected=bool(entry["item"].get("pdf")))

        # Suplementarios AL FINAL de todo: son los archivos más pesados y lentos.
        # Si la corrida se interrumpe acá, ya quedaron todos los ítems, notas,
        # anotaciones y PDFs; reejecutar completa solo lo que falte.
        pendientes = [
            (new_item_keys[str(it["sciwheel_id"])]["key"], str(it["sciwheel_id"]), it.get("attachments", []))
            for it in plan["nuevos"] if str(it["sciwheel_id"]) in new_item_keys and it.get("attachments")
        ] + [
            (e["zotero_key"], str(e["item"]["sciwheel_id"]), e["missing_supplements"])
            for e in plan["con_novedades"] if e["missing_supplements"]
        ]
        total_supl = sum(len(p[2]) for p in pendientes)
        if total_supl:
            self.logger.info(f"\t 📎 Subiendo {total_supl} archivos suplementarios de {len(pendientes)} ítems...")
        for n, (key, sci_id, supls) in enumerate(pendientes, 1):
            self.execute_supplements(key, sci_id, supls)
            if n % 50 == 0:
                self.logger.info(f"\t ⏳ Suplementarios: {n}/{len(pendientes)} ítems procesados...")

        self.counters["items_sin_cambios"] = len(plan["sin_cambios"])
        self.write_final_report(self.args.report_dir or self.args.manifest.parent,
                                 plan, invalid_items, collections_to_create)


# --------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------

def parse_cli_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Importa el manifiesto de Sciwheel a Zotero 10+ vía su Local Write API, "
                    "con reconciliación aditiva idempotente."
    )
    parser.add_argument("--manifest", type=Path, required=True,
                         help="Ruta a hierarchy_and_metadata.json generado por el extractor.")
    parser.add_argument("-x", "--execute", action="store_true",
                         help="Ejecuta cambios reales en Zotero. Sin esta bandera, corre en modo simulación (dry-run).")
    parser.add_argument("--retry-failed", type=Path, default=None,
                         help="Ruta a un informe_importacion_*.json previo: acota la corrida a sus ítems fallidos.")
    parser.add_argument("-v", "--verbose", type=int, default=1, choices=[0, 1, 2],
                         help="Nivel de verbosidad (0: Quiet, 1: INFO, 2: DEBUG).")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE_DEFAULT,
                         help=f"Ítems por lote de escritura (por defecto: {BATCH_SIZE_DEFAULT}, límite de Zotero).")
    parser.add_argument("--zotero-base-url", type=str, default=DEFAULT_BASE_URL,
                         help=f"Base URL de la Local API de Zotero (por defecto: {DEFAULT_BASE_URL}).")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                         help=f"Ruta al archivo de config con la API key local (por defecto: {DEFAULT_CONFIG_PATH}).")
    parser.add_argument("--max-attachment-mb", type=int, default=500,
                         help="Tamaño máximo por archivo adjunto/suplementario, en MB (por defecto: 500). "
                              "Cada archivo se carga entero en memoria al subirlo; los que lo superen se "
                              "omiten y quedan en el informe de fallidos.")
    parser.add_argument("--report-dir", type=Path, default=None,
                         help="Directorio donde guardar el informe final (por defecto: el mismo del manifest).")

    args = parser.parse_args()
    if not args.manifest.exists():
        parser.error(f"No se encontró el manifest: {args.manifest}")
    if args.retry_failed and not args.retry_failed.exists():
        parser.error(f"No se encontró el informe de --retry-failed: {args.retry_failed}")
    return args


def main() -> None:
    args = parse_cli_arguments()
    logger = configure_logger(args.verbose)
    importer = SciwheelZoteroImporter(args, logger)
    importer.run()


if __name__ == "__main__":
    main()