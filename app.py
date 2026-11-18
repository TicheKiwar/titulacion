#!/usr/bin/env python3
"""
Seguimiento de Titulación FISEI – UTA · Período Julio–Diciembre 2026
Modalidades: Proyecto de Investigación y Artículo Académico.

Solo usa la biblioteca estándar de Python (3.8+).
  Local:          python app.py            -> abre http://localhost:8765
  Hosting WSGI:   from app import application   (p. ej. PythonAnywhere)
Datos: titulacion.db (SQLite) y la carpeta documentos/ (o la ruta en TITULACION_DATA).
"""
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import urllib.parse
import webbrowser
import zipfile
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
import time
from http.cookies import SimpleCookie
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("TITULACION_DATA", BASE)
DB_PATH = os.path.join(DATA, "titulacion.db")
DOCS_DIR = os.path.join(DATA, "documentos")
TPL_DIR = os.path.join(BASE, "plantillas")
STATIC_DIR = os.path.join(BASE, "static")
PORT = int(os.environ.get("PORT", "8765"))
ECUADOR = timezone(timedelta(hours=-5))  # America/Guayaquil (sin horario de verano)


def ahora_local():
    """Hora de Ecuador continental, aunque el servidor esté en UTC (p. ej. PythonAnywhere)."""
    return datetime.now(ECUADOR).replace(tzinfo=None)

# --------------------------------------------------------------------------
# Datos semilla (extraídos del Cronograma y la Agenda Jul–Dic 2026 y de los
# formatos/anexos oficiales). modalidad: 'ambas' | 'proyecto' | 'articulo'
# --------------------------------------------------------------------------
FASES = [
    ("designacion", 1, "Designación de calificadores", "#2f6fdb"),
    ("tramites", 2, "Trámites administrativos para graduación", "#c26a1b"),
    ("sustentacion", 3, "Sustentación (defensa oral)", "#178a5b"),
]

# clave, nombre, modalidad, plantilla (o None), nota
DOCUMENTOS = [
    ("anexo10_p", "Solicitud de calificación del informe final – Anexo 10 (Proyecto)", "proyecto", "Anexo10_Calificadores_Proyecto.doc",
     "Dirigida al Presidente de Consejo Directivo. Firmas electrónicas o autógrafas, no mixtas ni imagen pegada."),
    ("anexo10_a", "Solicitud de calificación del informe final – Anexo 10 (Artículo)", "articulo", "Anexo10_Calificadores_Articulo.doc",
     "Tema en español e inglés si la revista lo requirió."),
    ("cedula", "Copia de cédula y papeleta de votación actualizada", "ambas", None,
     "A color, legible, tamaño real, en un solo archivo."),
    ("empresa", "Informe de cumplimiento del 100% del coordinador empresarial (si aplica)", "proyecto", "Anexo_Cumplimiento_empresarial.docx",
     "Solo si el trabajo se realizó en una empresa/institución. El tema debe ser el aprobado."),
    ("tutor100", "Informe de cumplimiento del 100% del docente tutor (Anexo 11)", "ambas", None,
     "El tutor lo envía por Quipux a la Secretaría de la Unidad de Titulación y por correo al estudiante, firmado."),
    ("mensuales", "Informes mensuales de avance del tutor (Anexo 5)", "ambas", None,
     "Enviados por el tutor vía Quipux y por correo. Todos con el mismo tema aprobado."),
    ("constancia", "Constancia de publicación o aceptación del artículo (revista)", "articulo", None, None),
    ("aval_informes", "Aval de revisión de informes mensuales e informe final (Ing. Andrea Sánchez)", "ambas", None,
     "Emitido por la Secretaría de la Unidad de Titulación FISEI."),
    ("turnitin", "Informe de similitud Turnitin desde la cuenta del tutor (≤ 10%)", "proyecto", None,
     "El tutor lo envía a Consejo Directivo por Quipux y al estudiante por correo."),
    ("res_tema", "Resolución de aprobación de modalidad y tema (y modificaciones si las hay)", "ambas", None, None),
    ("folio", "Certificado de folio y matrícula (período actual)", "ambas", None, "Obtenido del sistema."),
    ("trabajo_calif", "Trabajo de titulación en PDF OCR (para calificación)", "ambas", None, None),
    ("res_calif", "Resolución de designación de calificadores", "ambas", None,
     "Notificada por Consejo Directivo (hasta el 24-nov)."),
    ("nota1", "Calificación del calificador 1 (Anexo 12)", "ambas", "Anexo12_Consignacion_calificacion.docx",
     "Solicitar las notas en la Unidad de Titulación."),
    ("nota2", "Calificación del calificador 2 (Anexo 12)", "ambas", "Anexo12_Consignacion_calificacion.docx", None),
    ("noadeudar_reg", "Comprobante de solicitud registrada (no adeudar)", "ambas", "Procedimiento_no_adeudar.docx",
     "Captura del mensaje «Su solicitud ha sido registrada» en servicios.uta.edu.ec/SistemaIntegrado."),
    ("noadeudar", "Formulario único de no adeudar (con todas las firmas)", "ambas", "Procedimiento_no_adeudar.docx",
     "Válido 30 días desde su emisión. Debe estar vigente al ingresar la petición de día y hora. Registra la fecha de emisión."),
    ("anexo15", "Solicitud del certificado de aptitud legal (Anexo 15)", "ambas", "Anexo15_Certificado_aptitud_legal.docx",
     "Dirigida al Decano. En formato OCR."),
    ("record", "Certificado de récord académico actualizado", "ambas", None,
     "Debe registrar matrícula en titulación del período actual con la leyenda EN PROCESO."),
    ("habilitantes", "Certificación de requisitos habilitantes actualizada", "ambas", None,
     "Solicitar a Coordinación de carrera: matrícula en noveno, asignaturas, fechas, segunda lengua, cultura física, prácticas, vinculación, modalidad."),
    ("aptitud", "Certificado de aptitud legal (emitido)", "ambas", None, None),
    ("trabajo_sin_firmas", "Trabajo / hojas preliminares SIN firmas (PDF OCR) para Biblioteca", "ambas", None,
     "Proyecto: preliminares + trabajo en un solo archivo. Artículo: solo hojas preliminares unificadas (sin unir con el artículo)."),
    ("aval_inicial", "Aval inicial de Biblioteca FISEI", "ambas", None, None),
    ("trabajo_firmado", "Trabajo / hojas preliminares FIRMADAS (PDF OCR)", "ambas", None,
     "Firmas una por una: estudiante → calificadores → tutor → Decano. Nombre: Apellidos Nombres Cédula.pdf"),
    ("aval_final", "Aval final de Biblioteca FISEI", "ambas", None, None),
    ("bachiller", "Certificado de registro de título de bachiller (MinEduc)", "ambas", None,
     "No sirve copia del acta de grado del colegio."),
    ("solicitud_sust", "Solicitud de sustentación oral – día y hora (Anexo 14)", "ambas", None,
     "Dirigida al Presidente de Consejo Directivo. Dirección y correo institucional obligatorios."),
    ("res_sust", "Resolución con fecha y hora de sustentación", "ambas", None, None),
    ("ficha_oral", "Ficha de calificación de la sustentación oral (referencia)", "ambas", "Ficha_calificacion_sustentacion_oral.docx",
     "Formato que usa el tribunal. Útil para preparar la defensa."),
]

# Plantilla por modalidad para documentos que cambian de formato
PLANTILLA_MODALIDAD = {
    "solicitud_sust": {"proyecto": "Anexo14_Sustentacion_Proyecto.docx", "articulo": "Anexo14_Sustentacion_Articulo.docx"},
    "trabajo_sin_firmas": {"proyecto": "Portada_Preliminares_Proyecto.docx", "articulo": "Portada_Preliminares_Articulo.docx"},
}

# id, fase, orden, titulo, actor, inicio, fin, deps, destino, descripcion, [(doc, opcional)]
PASOS = [
    ("D1", "designacion", 1, "Enviar informe final + solicitud de designación de calificadores", "estudiante",
     "2026-07-01 00:00", "2026-11-19 18:00", [],
     "informacion.fisei@uta.edu.ec",
     "Enviar la solicitud (Anexo 10) y todos los requisitos en una carpeta comprimida, cada archivo numerado en el orden "
     "establecido, sin unificar, SOLO desde el correo institucional. Se puede ingresar antes del 19-nov, pero no después. "
     "Si falta un requisito, el trámite se devuelve.",
     [("anexo10_p", 0), ("anexo10_a", 0), ("cedula", 0), ("empresa", 1), ("tutor100", 0), ("mensuales", 0),
      ("constancia", 0), ("aval_informes", 0), ("turnitin", 0), ("res_tema", 0), ("folio", 0), ("trabajo_calif", 0)]),
    ("D2", "designacion", 2, "Sesión de Consejo Directivo", "consejo",
     "2026-11-20 00:00", "2026-11-20 23:59", ["D1"], None,
     "Consejo Directivo designa el par calificador y sus suplentes.", []),
    ("D3", "designacion", 3, "Proceso administrativo", "consejo",
     "2026-11-23 00:00", "2026-11-23 23:59", ["D2"], None, "Trámite interno de la Facultad.", []),
    ("D4", "designacion", 4, "Notificación de la resolución de calificadores", "consejo",
     "2026-11-24 00:00", "2026-11-24 23:59", ["D3"], None,
     "Recibes la resolución con tus dos calificadores. Súbela aquí: la necesitarás para la sustentación.",
     [("res_calif", 0)]),
    ("D5", "designacion", 5, "Entrega de calificaciones por los docentes", "docentes",
     "2026-11-24 00:00", "2026-12-04 23:59", ["D4"], None,
     "Los calificadores tienen 8 días término. El promedio debe ser ≥ 7/10; si es menor se convoca a reunión de "
     "modificaciones y presentas el trabajo corregido (formato de recalificación). En artículo académico la nota mínima "
     "no puede ser inferior a 7. Solicita las notas en la Unidad de Titulación.",
     [("nota1", 0), ("nota2", 0)]),

    ("T1", "tramites", 6, "Solicitar en el sistema el Formulario de no adeudar", "estudiante",
     "2026-11-16 00:00", "2026-11-20 18:00", [],
     "servicios.uta.edu.ec/SistemaIntegrado",
     "Gestión → Formularios de NO Adeudar → Registrar solicitud (Grado: Facultad, Carrera). No usar Internet Explorer. "
     "Las órdenes se despachan diariamente en orden cronológico.",
     [("noadeudar_reg", 1)]),
    ("T2", "tramites", 7, "Emisión del formulario con todas las firmas", "dependencias",
     "2026-11-16 00:00", "2026-12-07 23:59", ["T1"], None,
     "Secretaría y dependencias firman el formulario. Revisa el avance en Menú Gestión Solicitud → Avance. "
     "Cuando lo tengas, súbelo e ingresa su fecha de emisión (vale 30 días).",
     [("noadeudar", 0)]),
    ("T3", "tramites", 8, "Solicitar el certificado de aptitud legal", "estudiante",
     "2026-11-16 00:00", "2026-12-08 18:00", ["D5", "T2"],
     "informacion.fisei@uta.edu.ec",
     "Fecha y hora IMPOSTERGABLE. Enviar Anexo 15 (OCR) y adjuntos en carpeta comprimida, numerados, desde el correo "
     "institucional. Requiere las dos notas de los calificadores y el formulario de no adeudar vigente.",
     [("anexo15", 0), ("cedula", 0), ("record", 0), ("habilitantes", 0), ("noadeudar", 0), ("tutor100", 0),
      ("nota1", 0), ("nota2", 0)]),
    ("T4", "tramites", 9, "Elaboración del certificado de aptitud legal", "secretaria",
     "2026-11-16 00:00", "2026-12-10 16:00", ["T3"], None,
     "La Facultad elabora el certificado. Súbelo cuando lo recibas.", [("aptitud", 0)]),

    ("S1", "sustentacion", 10, "Revisión de Biblioteca (aval inicial, sin firmas)", "estudiante",
     "2026-11-24 00:00", "2026-12-09 18:00", [],
     "edissonhtoaza@uta.edu.ec",
     "Enviar el trabajo (Word convertido a PDF OCR) SIN firmas al Ing. Edisson Toaza. Asunto: «PROYECTO DE INVESTIGACIÓN – "
     "APELLIDOS NOMBRES» (o ARTÍCULO ACADÉMICO). Usa el formato de portada y hojas preliminares. Conviene hacerlo cuando ya "
     "tengas el trabajo definitivo (tras las calificaciones).",
     [("trabajo_sin_firmas", 0), ("aval_inicial", 0)]),
    ("S2", "sustentacion", 11, "Recoger firmas: estudiante → calificadores → tutor → Decano", "estudiante",
     "2026-11-24 00:00", "2026-12-09 18:00", ["S1", "D4"],
     "decanato.fisei@uta.edu.ec (última firma)",
     "Firmas autógrafas o electrónicas (no mixtas, no imagen pegada), recibiendo cada firma antes de pedir la siguiente. "
     "La última firma es del Decano vía decanato.fisei@uta.edu.ec. Deja margen: los firmantes pueden tardar.",
     [("trabajo_firmado", 0)]),
    ("S3", "sustentacion", 12, "Aval final de Biblioteca", "estudiante",
     "2026-11-24 00:00", "2026-12-10 12:00", ["S2"],
     "edissonhtoaza@uta.edu.ec",
     "Reenviar el trabajo firmado al Ing. Edisson Toaza para validar firmas y emitir el aval final.",
     [("aval_final", 0)]),
    ("S4", "sustentacion", 13, "Obtener certificado de registro de título de bachiller", "estudiante",
     "2026-07-01 00:00", "2026-12-10 12:00", [],
     "servicios.educacion.gob.ec (títulos refrendados)",
     "Descárgalo del Ministerio de Educación. Puedes hacerlo ya mismo.",
     [("bachiller", 0)]),
    ("S5", "sustentacion", 14, "Ingresar solicitud de día y hora de sustentación", "estudiante",
     "2026-07-01 00:00", "2026-12-10 18:00", ["D5", "T4", "S3", "S4"],
     "informacion.fisei@uta.edu.ec",
     "Enviar la solicitud (Anexo 14) con adjuntos en carpeta comprimida, numerados y sin unificar, desde el correo "
     "institucional. Deben ser los MISMOS documentos presentados para el certificado de aptitud legal. Si cumples todo, "
     "puedes ingresarla antes de la fecha máxima.",
     [("solicitud_sust", 0), ("aptitud", 0), ("cedula", 0), ("record", 0), ("habilitantes", 0), ("noadeudar", 0),
      ("nota1", 0), ("nota2", 0), ("bachiller", 0), ("res_tema", 0), ("res_calif", 0), ("empresa", 1),
      ("trabajo_firmado", 0), ("aval_final", 0)]),
    ("S6", "sustentacion", 15, "Sesión de Consejo Directivo", "consejo",
     "2026-12-11 00:00", "2026-12-11 23:59", ["S5"], None, "Consejo Directivo fija lugar, fecha y hora.", []),
    ("S7", "sustentacion", 16, "Proceso administrativo y notificación de la resolución", "consejo",
     "2026-12-14 00:00", "2026-12-16 23:59", ["S6"], None,
     "Recibes la resolución con fecha y hora de tu defensa. Súbela aquí.", [("res_sust", 0)]),
    ("S8", "sustentacion", 17, "Defensa oral", "estudiante",
     "2026-12-14 00:00", "2026-12-21 23:59", ["S7"], None,
     "Defensas hasta el lunes 21 de diciembre (hasta agotar cupos). Revisa la ficha de calificación oral para prepararte.",
     [("ficha_oral", 1)]),
]

# Datos del estudiante y del tema (asistente inicial / "Mis datos")
PERFIL = ["nombre", "cedula", "carrera", "correo", "telefono", "direccion", "tema", "tema_en", "tutor",
          "empresa_nombre", "revista"]
EDITABLES = set(PERFIL) | {"modalidad", "empresa", "nota_cal1", "nota_cal2"}

ACTORES = {
    "estudiante": "Tú", "consejo": "Consejo Directivo", "docentes": "Calificadores",
    "dependencias": "Dependencias / Tesorería", "secretaria": "Secretaría FISEI",
}


# --------------------------------------------------------------------------
# Base de datos
# --------------------------------------------------------------------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS config (clave TEXT PRIMARY KEY, valor TEXT);
CREATE TABLE IF NOT EXISTS fases (id TEXT PRIMARY KEY, orden INTEGER, nombre TEXT, color TEXT);
CREATE TABLE IF NOT EXISTS documentos (
    clave TEXT PRIMARY KEY, nombre TEXT, modalidad TEXT, plantilla TEXT, nota TEXT);
CREATE TABLE IF NOT EXISTS pasos (
    id TEXT PRIMARY KEY, fase TEXT REFERENCES fases(id), orden INTEGER, titulo TEXT, actor TEXT,
    inicio TEXT, fin TEXT, destino TEXT, descripcion TEXT,
    completado INTEGER DEFAULT 0, completado_en TEXT, notas TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS dependencias (paso TEXT REFERENCES pasos(id), requiere TEXT REFERENCES pasos(id),
    PRIMARY KEY (paso, requiere));
CREATE TABLE IF NOT EXISTS paso_documentos (paso TEXT REFERENCES pasos(id), documento TEXT REFERENCES documentos(clave),
    orden INTEGER, opcional INTEGER DEFAULT 0, PRIMARY KEY (paso, documento));
CREATE TABLE IF NOT EXISTS archivos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, documento TEXT REFERENCES documentos(clave),
    nombre_original TEXT, ruta TEXT, tamano INTEGER, subido_en TEXT, fecha_emision TEXT);
"""


def init_db():
    os.makedirs(DOCS_DIR, exist_ok=True)
    con = db()
    con.executescript(SCHEMA)
    # Catálogo: se re-sincroniza siempre (sin tocar progreso del estudiante)
    for f in FASES:
        con.execute("INSERT OR REPLACE INTO fases VALUES (?,?,?,?)", f)
    for d in DOCUMENTOS:
        con.execute("INSERT OR REPLACE INTO documentos VALUES (?,?,?,?,?)", d)
    for (pid, fase, orden, titulo, actor, ini, fin, deps, destino, desc, docs) in PASOS:
        con.execute("""INSERT INTO pasos (id,fase,orden,titulo,actor,inicio,fin,destino,descripcion)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET fase=excluded.fase, orden=excluded.orden, titulo=excluded.titulo,
                       actor=excluded.actor, inicio=excluded.inicio, fin=excluded.fin, destino=excluded.destino,
                       descripcion=excluded.descripcion""",
                    (pid, fase, orden, titulo, actor, ini, fin, destino, desc))
        con.execute("DELETE FROM dependencias WHERE paso=?", (pid,))
        for r in deps:
            con.execute("INSERT INTO dependencias VALUES (?,?)", (pid, r))
        con.execute("DELETE FROM paso_documentos WHERE paso=?", (pid,))
        for i, (doc, opc) in enumerate(docs):
            con.execute("INSERT INTO paso_documentos VALUES (?,?,?,?)", (pid, doc, i, opc))
    for k, v in [("modalidad", "proyecto"), ("nota_cal1", ""), ("nota_cal2", ""), ("empresa", "no"),
                 ("configurado", "0"), ("secreto", secrets.token_hex(32)), ("clave_hash", "")] + [(k, "") for k in PERFIL]:
        con.execute("INSERT OR IGNORE INTO config VALUES (?,?)", (k, v))
    con.commit()
    con.close()


# --------------------------------------------------------------------------
# Lógica de estado
# --------------------------------------------------------------------------
def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def aplica(modalidad_doc, modalidad, clave, cfg):
    if clave == "empresa" and cfg.get("empresa") != "si":
        return False
    return modalidad_doc == "ambas" or modalidad_doc == modalidad


def estado(hoy=None):
    ahora = hoy or ahora_local()
    con = db()
    cfg = {r["clave"]: r["valor"] for r in con.execute("SELECT * FROM config")}
    mod = cfg.get("modalidad", "proyecto")
    docs = {r["clave"]: dict(r) for r in con.execute("SELECT * FROM documentos")}
    archivos = {}
    for r in con.execute("SELECT * FROM archivos ORDER BY id"):
        archivos.setdefault(r["documento"], []).append({
            "id": r["id"], "nombre": r["nombre_original"], "tamano": r["tamano"],
            "subido_en": r["subido_en"], "fecha_emision": r["fecha_emision"]})
    pasos = [dict(r) for r in con.execute("SELECT * FROM pasos ORDER BY orden")]
    deps = {}
    for r in con.execute("SELECT * FROM dependencias"):
        deps.setdefault(r["paso"], []).append(r["requiere"])
    pdocs = {}
    for r in con.execute("SELECT * FROM paso_documentos ORDER BY orden"):
        pdocs.setdefault(r["paso"], []).append((r["documento"], r["opcional"]))
    fases = [dict(r) for r in con.execute("SELECT * FROM fases ORDER BY orden")]
    con.close()

    por_id = {p["id"]: p for p in pasos}
    alertas = []

    # Vigencia del formulario de no adeudar
    na = [a for a in archivos.get("noadeudar", []) if a.get("fecha_emision")]
    vence_na = None
    if na:
        emision = datetime.strptime(na[-1]["fecha_emision"], "%Y-%m-%d")
        vence_na = emision + timedelta(days=30)
        if vence_na < parse_dt("2026-12-10 18:00") and not por_id["S5"]["completado"]:
            alertas.append({"nivel": "alto", "texto":
                f"Tu formulario de no adeudar vence el {vence_na:%d/%m/%Y}, antes de la fecha máxima de solicitud de "
                f"día y hora (10/12). Debe estar vigente cuando ingreses esa solicitud: envíala antes o pide uno nuevo."})
        elif vence_na < ahora:
            alertas.append({"nivel": "alto", "texto": f"Tu formulario de no adeudar venció el {vence_na:%d/%m/%Y}."})

    # Promedio de calificaciones
    try:
        n1, n2 = float(cfg.get("nota_cal1") or "x"), float(cfg.get("nota_cal2") or "x")
        prom = round((n1 + n2) / 2, 2)
        if prom < 7:
            alertas.append({"nivel": "alto", "texto":
                f"El promedio de calificaciones es {prom}/10 (< 7). Se convoca a reunión de modificaciones y tienes "
                f"8 días término para presentar el trabajo corregido (plantilla de recalificación)."})
    except ValueError:
        prom = None

    # Documentos aplicables por paso
    for p in pasos:
        lista = []
        for clave, opc in pdocs.get(p["id"], []):
            d = docs[clave]
            if not aplica(d["modalidad"], mod, clave, cfg):
                continue
            tpl = PLANTILLA_MODALIDAD.get(clave, {}).get(mod) or d["plantilla"]
            lista.append({"clave": clave, "nombre": d["nombre"], "nota": d["nota"], "opcional": bool(opc),
                          "plantilla": tpl, "archivos": archivos.get(clave, [])})
        p["documentos"] = lista
        req = [x for x in lista if not x["opcional"]]
        p["docs_total"] = len(req)
        p["docs_listos"] = sum(1 for x in req if x["archivos"])
        p["marcado"] = bool(p["completado"])
        p["auto"] = None
        # Pasos de la Facultad cuyo resultado es un documento: si ya lo subiste, ocurrió.
        if not p["completado"] and p["actor"] != "estudiante" and req and p["docs_listos"] == len(req):
            p["completado"], p["auto"] = 1, "documento recibido"

    # Si un paso está hecho, todos los que requiere también (transitivo).
    def marcar_previos(pid):
        for d in deps.get(pid, []):
            if not por_id[d]["completado"]:
                por_id[d]["completado"], por_id[d]["auto"] = 1, "implícito por un paso posterior"
            marcar_previos(d)
    for p in pasos:
        if p["completado"]:
            marcar_previos(p["id"])

    for p in pasos:
        ini, fin = parse_dt(p["inicio"]), parse_dt(p["fin"])
        p["deps"] = deps.get(p["id"], [])
        pend = [d for d in p["deps"] if not por_id[d]["completado"]]
        p["deps_pendientes"] = [{"id": d, "titulo": por_id[d]["titulo"]} for d in pend]
        p["actor_nombre"] = ACTORES.get(p["actor"], p["actor"])
        p["dias_restantes"] = (fin - ahora).total_seconds() / 86400
        p["dias_para_abrir"] = (ini - ahora).total_seconds() / 86400

        if p["completado"]:
            s = "hecho"
        elif ahora > fin:
            s = "vencido" if p["actor"] == "estudiante" else "en_espera"
        elif pend:
            s = "bloqueado"
        elif ahora < ini:
            s = "proximo"
        else:
            s = "disponible" if p["actor"] == "estudiante" else "en_espera"
        p["estado"] = s
        p["puedo_hacerlo"] = (s == "disponible")

    # ¿Dónde estoy? Primer paso pendiente; acciones disponibles ahora
    actual = next((p for p in pasos if not p["completado"]), None)
    disponibles = [p["id"] for p in pasos if p["puedo_hacerlo"]]
    urgente = sorted([p for p in pasos if p["estado"] in ("disponible", "bloqueado") and p["actor"] == "estudiante"],
                     key=lambda p: p["fin"])
    for p in pasos:
        if p["estado"] == "vencido" and p["actor"] == "estudiante":
            alertas.append({"nivel": "alto", "texto": f"Venció el plazo de «{p['titulo']}» ({parse_dt(p['fin']):%d/%m %H:%M}). "
                                                       f"Si ya lo hiciste, márcalo como hecho."})
    hechos = sum(1 for p in pasos if p["completado"])
    return {
        "ahora": ahora.strftime("%Y-%m-%d %H:%M"),
        "config": cfg, "fases": fases, "pasos": pasos,
        "actual": actual["id"] if actual else None,
        "disponibles": disponibles,
        "siguiente_plazo": urgente[0]["id"] if urgente else None,
        "progreso": {"hechos": hechos, "total": len(pasos)},
        "promedio": prom, "vence_noadeudar": vence_na.strftime("%Y-%m-%d") if vence_na else None,
        "alertas": alertas,
    }


def safe_name(s):
    s = re.sub(r"[^\w\-. áéíóúÁÉÍÓÚñÑ()%–,]", "_", s).strip()
    return s[:120] or "archivo"


def nombre_corto(nombre):
    """Quita aclaraciones entre paréntesis (p. ej. «(si aplica)»), salvo las que nombran un Anexo."""
    return re.sub(r"\s*\((?![^)]*Anexo)[^)]*\)", "", nombre).strip()


def zip_paso(pid):
    st = estado()
    p = next(x for x in st["pasos"] if x["id"] == pid)
    con = db()
    buf = io.BytesIO()
    faltan = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # Numeración seguida: los opcionales sin archivo no aplican y no ocupan número
        docs = [d for d in p["documentos"] if not d["opcional"] or d["archivos"]]
        for n, d in enumerate(docs, 1):
            if not d["archivos"]:
                if not d["opcional"]:
                    faltan.append(f"{n:02d}. {nombre_corto(d['nombre'])}")
                continue
            for j, a in enumerate(d["archivos"]):
                r = con.execute("SELECT ruta FROM archivos WHERE id=?", (a["id"],)).fetchone()
                ext = os.path.splitext(a["nombre"])[1]
                suf = f" ({j + 1})" if len(d["archivos"]) > 1 else ""
                nombre = f"{n:02d}. {safe_name(nombre_corto(d['nombre']))[:80]}{suf}{ext}"
                z.write(os.path.join(DOCS_DIR, r["ruta"]), nombre)
        if faltan:
            z.writestr("00. FALTAN ESTOS DOCUMENTOS.txt", "\n".join("- " + f for f in faltan))
    con.close()
    return buf.getvalue()


# --------------------------------------------------------------------------
# Autenticación (contraseña definida en el asistente inicial)
# --------------------------------------------------------------------------
def get_cfg(*keys):
    con = db()
    r = {row["clave"]: row["valor"] for row in con.execute(
        "SELECT * FROM config WHERE clave IN (%s)" % ",".join("?" * len(keys)), keys)}
    con.close()
    return r


def set_cfg(d):
    con = db()
    for k, v in d.items():
        con.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (k, str(v)))
    con.commit()
    con.close()


def hash_clave(clave, sal=None):
    sal = sal or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", clave.encode(), sal.encode(), 200_000).hex()
    return f"{sal}${h}"


def verificar_clave(clave, guardado):
    if not guardado or "$" not in guardado:
        return False
    sal = guardado.split("$", 1)[0]
    return hmac.compare_digest(hash_clave(clave, sal), guardado)


def firmar(exp, secreto):
    return hmac.new(secreto.encode(), str(exp).encode(), hashlib.sha256).hexdigest()


def sesion_valida(environ):
    c = get_cfg("secreto", "clave_hash")
    if not c.get("clave_hash"):
        return True  # sin contraseña (solo posible si se omitió en uso local)
    ck = SimpleCookie(environ.get("HTTP_COOKIE", ""))
    if "sesion" not in ck:
        return False
    try:
        exp, firma = ck["sesion"].value.split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(firma, firmar(exp, c["secreto"]))
    except (ValueError, KeyError):
        return False


def cookie_sesion(environ, dias=30):
    exp = int(time.time()) + dias * 86400
    secreto = get_cfg("secreto")["secreto"]
    https = environ.get("wsgi.url_scheme") == "https" or environ.get("HTTP_X_FORWARDED_PROTO") == "https"
    return ("Set-Cookie", f"sesion={exp}.{firmar(exp, secreto)}; Path=/; Max-Age={dias * 86400}; HttpOnly; "
                          f"SameSite=Lax{'; Secure' if https else ''}")


# --------------------------------------------------------------------------
# Respaldo / restauración
# --------------------------------------------------------------------------
def crear_respaldo():
    buf = io.BytesIO()
    con = db()
    tmp = DB_PATH + ".bak"
    bk = sqlite3.connect(tmp)
    con.backup(bk)
    bk.close()
    con.close()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp, "titulacion.db")
        for raiz, _, archivos in os.walk(DOCS_DIR):
            for a in archivos:
                full = os.path.join(raiz, a)
                z.write(full, os.path.join("documentos", os.path.relpath(full, DOCS_DIR)))
    os.remove(tmp)
    return buf.getvalue()


def restaurar_respaldo(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    nombres = z.namelist()
    if "titulacion.db" not in nombres:
        raise ValueError("El ZIP no contiene titulacion.db")
    for n in nombres:
        if n.startswith("/") or ".." in n.split("/"):
            raise ValueError("Ruta no válida en el respaldo")
    tmp = DB_PATH + ".nuevo"
    with open(tmp, "wb") as f:
        f.write(z.read("titulacion.db"))
    sqlite3.connect(tmp).execute("SELECT count(*) FROM pasos").fetchone()  # valida
    os.replace(tmp, DB_PATH)
    if os.path.isdir(DOCS_DIR):
        import shutil
        shutil.rmtree(DOCS_DIR)
    os.makedirs(DOCS_DIR, exist_ok=True)
    for n in nombres:
        if n.startswith("documentos/") and not n.endswith("/"):
            dest = os.path.join(DOCS_DIR, n[len("documentos/"):])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(z.read(n))
    init_db()


# --------------------------------------------------------------------------
# Aplicación WSGI
# --------------------------------------------------------------------------
_iniciado = False
PUBLICAS = {"/", "/index.html", "/api/sesion", "/api/login", "/api/setup", "/api/restaurar-inicial"}


class Resp(Exception):
    def __init__(self, status, body, ctype="application/json; charset=utf-8", headers=None):
        self.status, self.body, self.ctype, self.headers = status, body, ctype, headers or []


def J(obj, status="200 OK", headers=None):
    return Resp(status, json.dumps(obj, ensure_ascii=False).encode(), headers=headers)


def archivo_resp(path, download_name=None):
    if not os.path.isfile(path):
        return J({"error": "no encontrado"}, "404 Not Found")
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    h = []
    if download_name:
        h.append(("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(download_name)))
    with open(path, "rb") as f:
        return Resp("200 OK", f.read(), ctype, h)


# --------------------------------------------------------------------------
# Llenado de formatos .docx con los datos del estudiante.
# Solo se cambia el texto dentro de los <w:t> existentes: estilos, tablas,
# márgenes, imágenes y numeración quedan intactos.
# --------------------------------------------------------------------------
_RE_PARRAFO = re.compile(r"<w:p[ >].*?</w:p>", re.S)
_RE_TEXTO = re.compile(r"(<w:t(?: [^>]*)?>)([^<]*)(</w:t>)")
_PUNTOS = r"[….\s]*[….]"  # relleno de puntos/elipsis de los formatos


def _xml_esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_unesc(s):
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&apos;", "'").replace("&amp;", "&"))


def _reemplazar_en_parrafo(p, reglas):
    """Aplica las reglas (regex, reemplazo) al texto completo del párrafo aunque esté
    partido en varios runs. El texto nuevo queda en el run donde empezaba el texto
    original, así conserva su formato (negrita, fuente, tamaño...)."""
    ms = list(_RE_TEXTO.finditer(p))
    if not ms:
        return p
    textos = [_xml_unesc(m.group(2)) for m in ms]
    cambiado = False
    for patron, repl in reglas:
        # cada regla se aplica una sola vez por párrafo
        m = re.search(patron, "".join(textos))
        if not m:
            continue
        nuevo = m.expand(repl)
        if nuevo == m.group(0):
            continue
        a, b = m.span()
        res, pos, puesto = [], 0, False
        for t in textos:
            ini, fin = pos, pos + len(t)
            pos = fin
            izq = t[:max(0, min(a, fin) - ini)]
            der = t[max(0, b - ini):] if b < fin else ""
            if not puesto and ini <= a < fin:
                res.append(izq + nuevo + der)
                puesto = True
            elif fin <= a or ini >= b:
                res.append(t)
            else:
                res.append(izq + der)
        textos = res
        cambiado = True
    if not cambiado:
        return p
    salida, ult = [], 0
    for m, t in zip(ms, textos):
        tag = m.group(1)
        if "xml:space" not in tag:
            tag = tag[:-1] + ' xml:space="preserve">'
        salida.append(p[ult:m.start()] + tag + _xml_esc(t) + m.group(3))
        ult = m.end()
    salida.append(p[ult:])
    return "".join(salida)


def _titulo_carrera(carrera):
    c = (carrera or "").strip()
    especiales = {"software": "de Software", "industrial": "Industrial"}
    for k, v in especiales.items():
        if c.lower() in (k, "ingeniería " + k, "ingenieria " + k):
            return v
    return ("en " + c) if c else ""


def _reglas_plantilla(nombre_tpl, c):
    c = {k: (v or "").replace("\\", "\\\\") for k, v in c.items()}  # evita escapes en re
    nom, ced, car = c.get("nombre", ""), c.get("cedula", ""), c.get("carrera", "")
    tema, tel, dirc, mail = c.get("tema", ""), c.get("telefono", ""), c.get("direccion", ""), c.get("correo", "")
    mod = {"proyecto": "Proyecto de Investigación", "articulo": "Artículo Académico"}.get(c.get("modalidad"), "")
    hoy = ahora_local()
    fecha = f"Ambato, {hoy:%d/%m/%Y}"

    def si(valor, regla):
        return [regla] if valor else []

    estudiante = (
        [(r"Lugar,\s*dd\s*/\s*mm\s*/\s*aa", fecha)]
        + si(nom, (r"\(Nombres? [Cc]ompletos del [Ee]studiante\)" + _PUNTOS + r"\s*", nom + " "))
        + si(ced, (r"(cédula de ciudadanía No\.?)\s*" + _PUNTOS, r"\g<1> " + ced))
        + si(car, (r"([Cc]arrera de)\s*" + _PUNTOS, r"\g<1> " + car))
        + si(tema, (r"(tema:\s*[\"“])" + _PUNTOS + r"([\"”])", r"\g<1>" + tema + r"\g<2>"))
        + si(car, (r"(título de Ingeniero)" + _PUNTOS, r"\g<1> " + _titulo_carrera(car)))
        + si(mod, (r"(bajo la modalidad) \(obligatorio\)" + _PUNTOS, r"\g<1> " + mod))
    )
    firma = (
        si(nom, (r"^\s*Nombre del estudiante\s*$", nom))
        + si(ced, (r"^\s*Cédula de Ciudadanía\s*$", "Cédula de Ciudadanía: " + ced))
        + si(tel, (r"^\s*No teléfono celular\s*$", "No teléfono celular: " + tel))
        + si(dirc, (r"^\s*Dirección\s*\(obligatorio\)\s*$", "Dirección: " + dirc))
        + si(mail, (r"^\s*Correo electrónico( institucional)?(\s*\(obligatorio\))?\s*$",
                    r"Correo electrónico\g<1>: " + mail))
    )
    if nombre_tpl.startswith(("Anexo14_", "Anexo15_", "Anexo_Solicitud_recalificacion")):
        return estudiante + firma
    if nombre_tpl == "Anexo_Cumplimiento_empresarial.docx":
        return (
            [(r"Lugar,\s*dd\s*/\s*mm\s*/\s*aa", fecha)]
            + si(car, (r"^(\s*Carrera de)" + _PUNTOS + r"\s*$", r"\g<1> " + car))
            + si(c.get("empresa_nombre"), (r"(de la Empresa)\s*" + _PUNTOS + r"\s*,",
                                           r"\g<1> " + c.get("empresa_nombre", "") + ","))
            + si(tema, (r"(tema:\s*[\"“])" + _PUNTOS + r"([\"”])", r"\g<1>" + tema + r"\g<2>"))
            + si(nom, (r"(señor/ita)" + _PUNTOS, r"\g<1> " + nom))
            + si(ced, (r"(cédula de ciudadanía)" + _PUNTOS, r"\g<1> " + ced))
            + si(car, (r"(estudiante de la Carrera de)\s*" + _PUNTOS, r"\g<1> " + car + " "))
        )
    if nombre_tpl == "Ficha_calificacion_sustentacion_oral.docx":
        return (si(car, (r"(CARRERA:)\s*" + _PUNTOS, r"\g<1> " + car.upper()))
                + si(nom, (r"(NOMBRE DEL GRADUANDO:)\s*$", r"\g<1> " + nom.upper())))
    return []


def plantilla_llena(path):
    """Devuelve los bytes del .docx con los datos del estudiante, o None si no aplica."""
    nombre_tpl = os.path.basename(path)
    if not nombre_tpl.lower().endswith(".docx") or not os.path.isfile(path):
        return None
    reglas = _reglas_plantilla(nombre_tpl, get_cfg(*PERFIL, "modalidad"))
    if not reglas:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(buf, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "word/document.xml":
                xml = data.decode("utf-8")
                xml = _RE_PARRAFO.sub(lambda m: _reemplazar_en_parrafo(m.group(0), reglas), xml)
                data = xml.encode("utf-8")
            zout.writestr(info, data)
    return buf.getvalue()


def leer_cuerpo(environ, limite=200 * 1024 * 1024):
    n = int(environ.get("CONTENT_LENGTH") or 0)
    if n > limite:
        raise J({"error": "Archivo demasiado grande"}, "413 Payload Too Large")
    return environ["wsgi.input"].read(n) if n > 0 else b""


def leer_json(environ):
    try:
        return json.loads(leer_cuerpo(environ) or b"{}")
    except ValueError:
        raise J({"error": "JSON inválido"}, "400 Bad Request")


def parse_hoy(q):
    h = q.get("hoy", [None])[0]
    if not h:
        return None
    try:
        return datetime.strptime(h + " 12:00", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def rutear(environ):
    metodo = environ["REQUEST_METHOD"]
    path = urllib.parse.unquote(environ.get("PATH_INFO") or "/")
    q = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    cfg = get_cfg("configurado", "clave_hash")
    configurado = cfg.get("configurado") == "1"

    if metodo == "GET" and path in ("/", "/index.html"):
        return archivo_resp(os.path.join(STATIC_DIR, "index.html"))

    if path == "/api/sesion":
        return J({"configurado": configurado, "autenticado": configurado and sesion_valida(environ),
                  "con_clave": bool(cfg.get("clave_hash"))})

    if path == "/api/setup" and metodo == "POST":
        if configurado:
            return J({"error": "Ya está configurado"}, "403 Forbidden")
        d = leer_json(environ)
        clave = d.pop("clave", "")
        if len(clave) < 6:
            return J({"error": "La contraseña debe tener al menos 6 caracteres"}, "400 Bad Request")
        datos = {k: str(v).strip() for k, v in d.items() if k in EDITABLES}
        datos.update({"clave_hash": hash_clave(clave), "configurado": "1"})
        set_cfg(datos)
        return J({"ok": True}, headers=[cookie_sesion(environ)])

    if path == "/api/restaurar-inicial" and metodo == "POST":
        if configurado:
            return J({"error": "Ya está configurado"}, "403 Forbidden")
        try:
            restaurar_respaldo(leer_cuerpo(environ))
        except (ValueError, zipfile.BadZipFile, sqlite3.DatabaseError) as e:
            return J({"error": f"No se pudo restaurar: {e}"}, "400 Bad Request")
        return J({"ok": True})

    if path == "/api/login" and metodo == "POST":
        d = leer_json(environ)
        if verificar_clave(d.get("clave", ""), cfg.get("clave_hash")):
            return J({"ok": True}, headers=[cookie_sesion(environ)])
        time.sleep(1)
        return J({"error": "Contraseña incorrecta"}, "401 Unauthorized")

    if path == "/api/logout":
        return J({"ok": True}, headers=[("Set-Cookie", "sesion=; Path=/; Max-Age=0")])

    # ---- Todo lo demás requiere configuración y sesión ----
    if not configurado or not sesion_valida(environ):
        return J({"error": "no autenticado"}, "401 Unauthorized")

    if metodo == "GET":
        if path == "/api/estado":
            st = estado(parse_hoy(q))
            st["config"] = {k: v for k, v in st["config"].items() if k not in ("secreto", "clave_hash")}
            return J(st)
        m = re.fullmatch(r"/archivo/(\d+)", path)
        if m:
            con = db()
            r = con.execute("SELECT * FROM archivos WHERE id=?", (int(m.group(1)),)).fetchone()
            con.close()
            if not r:
                return J({"error": "no encontrado"}, "404 Not Found")
            dl = q.get("dl", ["0"])[0] == "1"
            return archivo_resp(os.path.join(DOCS_DIR, r["ruta"]), r["nombre_original"] if dl else None)
        m = re.fullmatch(r"/plantilla/([^/]+)", path)
        if m:
            name = os.path.basename(m.group(1))
            ruta = os.path.join(TPL_DIR, name)
            if q.get("blanco", ["0"])[0] != "1":
                lleno = plantilla_llena(ruta)
                if lleno:
                    c = get_cfg("nombre")
                    base, ext = os.path.splitext(name)
                    dl = f"{base} - {safe_name(c.get('nombre') or 'lleno')}{ext}"
                    return Resp("200 OK", lleno, mimetypes.guess_type(name)[0] or "application/octet-stream",
                                [("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(dl))])
            return archivo_resp(ruta, name)
        m = re.fullmatch(r"/api/paso/(\w+)/zip", path)
        if m:
            c = get_cfg("nombre", "cedula")
            base = " ".join(x for x in (c.get("nombre"), c.get("cedula")) if x) or "tramite"
            nombre = f"{m.group(1)} - {safe_name(base)}.zip"
            return Resp("200 OK", zip_paso(m.group(1)), "application/zip",
                        [("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(nombre))])
        if path == "/api/respaldo":
            nombre = f"respaldo_titulacion_{ahora_local():%Y%m%d_%H%M}.zip"
            return Resp("200 OK", crear_respaldo(), "application/zip",
                        [("Content-Disposition", f"attachment; filename={nombre}")])

    if metodo == "POST":
        if path == "/api/config":
            d = leer_json(environ)
            set_cfg({k: v for k, v in d.items() if k in EDITABLES})
            return J({"ok": True})
        if path == "/api/clave":
            d = leer_json(environ)
            if not verificar_clave(d.get("actual", ""), cfg.get("clave_hash")):
                return J({"error": "La contraseña actual no es correcta"}, "400 Bad Request")
            if len(d.get("nueva", "")) < 6:
                return J({"error": "La nueva contraseña debe tener al menos 6 caracteres"}, "400 Bad Request")
            set_cfg({"clave_hash": hash_clave(d["nueva"]), "secreto": secrets.token_hex(32)})
            return J({"ok": True}, headers=[cookie_sesion(environ)])
        if path == "/api/restaurar":
            try:
                restaurar_respaldo(leer_cuerpo(environ))
            except (ValueError, zipfile.BadZipFile, sqlite3.DatabaseError) as e:
                return J({"error": f"No se pudo restaurar: {e}"}, "400 Bad Request")
            return J({"ok": True})
        m = re.fullmatch(r"/api/paso/(\w+)", path)
        if m:
            d = leer_json(environ)
            con = db()
            if "completado" in d:
                con.execute("UPDATE pasos SET completado=?, completado_en=? WHERE id=?",
                            (1 if d["completado"] else 0,
                             ahora_local().strftime("%Y-%m-%d %H:%M") if d["completado"] else None, m.group(1)))
            if "notas" in d:
                con.execute("UPDATE pasos SET notas=? WHERE id=?", (d["notas"], m.group(1)))
            con.commit()
            con.close()
            return J({"ok": True})
        m = re.fullmatch(r"/api/documento/(\w+)", path)
        if m:
            clave = m.group(1)
            con = db()
            existe = con.execute("SELECT 1 FROM documentos WHERE clave=?", (clave,)).fetchone()
            con.close()
            if not existe:
                return J({"error": "documento desconocido"}, "404 Not Found")
            body = leer_cuerpo(environ)
            if not body:
                return J({"error": "archivo vacío"}, "400 Bad Request")
            nombre = os.path.basename(urllib.parse.unquote(environ.get("HTTP_X_FILENAME") or "archivo"))
            fecha = environ.get("HTTP_X_FECHA_EMISION") or None
            os.makedirs(os.path.join(DOCS_DIR, clave), exist_ok=True)
            rel = os.path.join(clave, f"{ahora_local():%Y%m%d-%H%M%S}_{secrets.token_hex(2)}_{safe_name(nombre)}")
            with open(os.path.join(DOCS_DIR, rel), "wb") as f:
                f.write(body)
            con = db()
            con.execute("INSERT INTO archivos (documento,nombre_original,ruta,tamano,subido_en,fecha_emision) "
                        "VALUES (?,?,?,?,?,?)",
                        (clave, nombre, rel, len(body), ahora_local().strftime("%Y-%m-%d %H:%M"), fecha))
            con.commit()
            con.close()
            return J({"ok": True})
        m = re.fullmatch(r"/api/archivo/(\d+)/fecha", path)
        if m:
            d = leer_json(environ)
            con = db()
            con.execute("UPDATE archivos SET fecha_emision=? WHERE id=?", (d.get("fecha") or None, int(m.group(1))))
            con.commit()
            con.close()
            return J({"ok": True})

    if metodo == "DELETE":
        m = re.fullmatch(r"/api/archivo/(\d+)", path)
        if m:
            con = db()
            r = con.execute("SELECT ruta FROM archivos WHERE id=?", (int(m.group(1)),)).fetchone()
            if r:
                try:
                    os.remove(os.path.join(DOCS_DIR, r["ruta"]))
                except OSError:
                    pass
                con.execute("DELETE FROM archivos WHERE id=?", (int(m.group(1)),))
                con.commit()
            con.close()
            return J({"ok": True})

    return J({"error": "ruta no encontrada"}, "404 Not Found")


def application(environ, start_response):
    global _iniciado
    if not _iniciado:
        init_db()
        _iniciado = True
    try:
        r = rutear(environ)
    except Resp as e:
        r = e
    except Exception as e:  # noqa
        r = J({"error": f"Error interno: {e}"}, "500 Internal Server Error")
    headers = [("Content-Type", r.ctype), ("Content-Length", str(len(r.body))), ("Cache-Control", "no-store"),
               ("X-Content-Type-Options", "nosniff")] + r.headers
    start_response(r.status, headers)
    return [r.body]


class _Servidor(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _Silencioso(WSGIRequestHandler):
    def log_message(self, *a):
        pass


def main():
    init_db()
    host = os.environ.get("HOST", "127.0.0.1")
    srv = make_server(host, PORT, application, server_class=_Servidor, handler_class=_Silencioso)
    url = f"http://localhost:{PORT}"
    print(f"Seguimiento de Titulación en {url}  (Ctrl+C para cerrar)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrado.")


if __name__ == "__main__":
    main()
