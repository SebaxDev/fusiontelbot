import os
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from flask import Flask
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== CONFIGURACIÓN ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

# ==================== LOGGING ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== MINI SERVIDOR WEB (para Render) ====================
app = Flask(__name__)

@app.route("/")
def health():
    return "✅ Bot Fusión activo", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

# ==================== GOOGLE SHEETS ====================
def get_google_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise ValueError("Falta la variable de entorno GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(credentials)

gc = get_google_client()
sh = gc.open_by_key(SPREADSHEET_ID)
ws_reclamos = sh.worksheet("Reclamos")
ws_clientes = sh.worksheet("Clientes")

# ==================== CACHE TTL ====================
_cache = {}
_cache_time = {}
CACHE_TTL = 60  # segundos

def get_cached_records(worksheet, cache_name):
    now = time.time()
    if cache_name in _cache and (now - _cache_time.get(cache_name, 0)) < CACHE_TTL:
        return _cache[cache_name]
    records = worksheet.get_all_records()
    _cache[cache_name] = records
    _cache_time[cache_name] = now
    logger.info(f"🔄 Caché actualizado: {cache_name} ({len(records)} regs)")
    return records

def invalidate_cache():
    _cache.clear()
    _cache_time.clear()

# ==================== HELPERS ====================
def safe_str(value):
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in ("nan", "none", "null", "nat"):
        return ""
    return s

def parse_fecha(fecha_str):
    s = safe_str(fecha_str).strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def classify_reclamo(row):
    estado = safe_str(row.get("Estado")).lower().strip()
    tecnico = safe_str(row.get("Técnico")).strip()
    if estado == "resuelto":
        return "verificado"
    if estado in ("desconexión", "desconexion", "desconexion a pedido"):
        return "desconexion"
    if not tecnico:
        return "pendiente"
    if estado and estado not in ("resuelto", "desconexión", "desconexion"):
        return "curso"
    if not estado and tecnico:
        return "curso"
    return "pendiente"

def format_cliente(row):
    nombre = safe_str(row.get("Nombre"))
    direccion = safe_str(row.get("Dirección"))
    telefono = safe_str(row.get("Teléfono"))
    precinto = safe_str(row.get("N° de Precinto"))
    plan = safe_str(row.get("Plan"))
    sector = safe_str(row.get("Sector"))
    lat = safe_str(row.get("Latitud"))
    lon = safe_str(row.get("Longitud"))

    html = f"<b>👤 Cliente #{safe_str(row.get('Nº Cliente'))}</b>
"
    html += f"├ <b>Nombre:</b> {nombre}
"
    html += f"├ <b>Dirección:</b> {direccion}
"
    html += f"├ <b>Teléfono:</b> {telefono or '—'}
"
    html += f"├ <b>Precinto:</b> {precinto or 'No asignado'}
"
    html += f"├ <b>Plan:</b> {plan or '—'}
"
    html += f"├ <b>Sector:</b> {sector or '—'}
"
    if lat and lon:
        maps_url = f"https://www.google.com/maps?q={lat},{lon}"
        html += f"└ <b>📍 Ubicación:</b> <a href='{maps_url}'>Ver en Google Maps</a>
"
    else:
        html += f"└ <b>📍 Ubicación:</b> No disponible
"
    return html

def format_reclamo(row, idx=None):
    pref = f"{idx}. " if idx else ""
    fecha = safe_str(row.get("Fecha y hora"))
    tipo = safe_str(row.get("Tipo de reclamo"))
    estado = safe_str(row.get("Estado"))
    tecnico = safe_str(row.get("Técnico"))
    detalles = safe_str(row.get("Detalles"))
    num_cliente = safe_str(row.get("Nº Cliente"))
    nombre = safe_str(row.get("Nombre"))

    html = f"<b>{pref}{fecha}</b> | {tipo}
"
    html += f"├ <b>Cliente:</b> #{num_cliente} — {nombre}
"
    html += f"├ <b>Estado:</b> {estado or '—'}
"
    html += f"├ <b>Técnico:</b> {tecnico or '—'}
"
    if detalles:
        html += f"└ <b>Detalle:</b> {detalles[:80]}{'...' if len(detalles) > 80 else ''}
"
    return html

def format_reclamo_detalle(row, idx=None):
    pref = f"{idx}. " if idx else ""
    fecha = safe_str(row.get("Fecha y hora"))
    tipo = safe_str(row.get("Tipo de reclamo"))
    estado = safe_str(row.get("Estado"))
    tecnico = safe_str(row.get("Técnico"))
    detalles = safe_str(row.get("Detalles"))
    num_cliente = safe_str(row.get("Nº Cliente"))
    nombre = safe_str(row.get("Nombre"))
    direccion = safe_str(row.get("Dirección"))
    precinto = safe_str(row.get("N° de Precinto"))

    html = f"<b>{pref}{fecha}</b> | {tipo}
"
    html += f"├ <b>Cliente:</b> #{num_cliente} — {nombre}
"
    html += f"├ <b>Dirección:</b> {direccion}
"
    html += f"├ <b>Estado:</b> {estado or '—'}
"
    html += f"├ <b>Técnico:</b> {tecnico or '—'}
"
    html += f"├ <b>Precinto:</b> {precinto or '—'}
"
    if detalles:
        html += f"└ <b>Detalle:</b> {detalles[:120]}{'...' if len(detalles) > 120 else ''}
"
    return html

# ==================== COMANDOS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Bot de Reclamos — Fusión</b>

"
        "Comandos disponibles:

"
        "• <b>/cliente</b> &lt;número&gt; — Ficha del cliente + historial
"
        "• <b>/precinto</b> &lt;número&gt; — Buscar cliente por precinto
"
        "• <b>/historial</b> &lt;número&gt; — Todos los reclamos de un cliente
"
        "• <b>/ubicacion</b> &lt;número&gt; — Link de Maps del cliente
"
        "• <b>/reclamo</b> &lt;ID&gt; — Buscar reclamo por ID
"
        "• <b>/tecnico</b> &lt;nombre&gt; — Reclamos en curso y verificados de un técnico
"
        "• <b>/nombre</b> &lt;texto&gt; — Buscar cliente por nombre
"
        "• <b>/recientes</b> &lt;N&gt; — Últimos N reclamos
"
        "• <b>/resumen</b> — Resumen diario y totales
"
        "• <b>/pendientes</b> — Lista completa de reclamos pendientes
"
        "• <b>/topmes</b> — Ranking de técnicos últimos 30 días
"
        "• <b>/sectores</b> — Mapa de sectores con estadísticas

"
        "Ejemplo: <code>/cliente 6331</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reclamos = get_cached_records(ws_reclamos, "reclamos")
    hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    manana = hoy + timedelta(days=1)

    generados_hoy = 0
    total_curso = 0
    total_verificado = 0
    total_pendiente = 0
    hoy_curso = 0
    hoy_verificado = 0
    hoy_pendiente = 0

    for r in reclamos:
        fecha = parse_fecha(safe_str(r.get("Fecha y hora")))
        cat = classify_reclamo(r)

        if fecha and hoy <= fecha < manana:
            generados_hoy += 1
            if cat == "curso":
                hoy_curso += 1
            elif cat == "verificado":
                hoy_verificado += 1
            elif cat == "pendiente":
                hoy_pendiente += 1

        if cat == "curso":
            total_curso += 1
        elif cat == "verificado":
            total_verificado += 1
        elif cat == "pendiente":
            total_pendiente += 1

    msg = (
        f"<b>📊 Resumen General</b>

"
        f"<b>📅 Hoy ({hoy.strftime('%d/%m/%Y')}):</b>
"
        f"├ Generados: <b>{generados_hoy}</b>
"
        f"├ 🔧 En curso: <b>{hoy_curso}</b>
"
        f"├ ✅ Verificados: <b>{hoy_verificado}</b>
"
        f"└ ⏳ Pendientes: <b>{hoy_pendiente}</b>

"
        f"<b>📈 Totales activos:</b>
"
        f"├ 🔧 En curso: <b>{total_curso}</b>
"
        f"├ ✅ Verificados: <b>{total_verificado}</b>
"
        f"└ ⏳ Pendientes: <b>{total_pendiente}</b>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

async def cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/cliente 6331</code>", parse_mode="HTML")
        return

    num = safe_str(context.args[0])
    clientes = get_cached_records(ws_clientes, "clientes")
    reclamos = get_cached_records(ws_reclamos, "reclamos")

    cliente_row = next((c for c in clientes if safe_str(c.get("Nº Cliente")) == num), None)
    if not cliente_row:
        await update.message.reply_text(f"❌ Cliente <b>#{num}</b> no encontrado.", parse_mode="HTML")
        return

    historial = [r for r in reclamos if safe_str(r.get("Nº Cliente")) == num]
    total_reclamos = len(historial)
    historial = historial[-5:]
    historial.reverse()

    msg = format_cliente(cliente_row)
    msg += f"
<b>📋 Historial ({len(historial)} de {total_reclamos}):</b>

"
    if historial:
        for i, r in enumerate(historial, 1):
            msg += format_reclamo(r, i) + "
"
    else:
        msg += "<i>Sin reclamos registrados.</i>
"

    if len(msg) > 4000:
        msg = msg[:4000] + "

<i>... mensaje truncado</i>"
    await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)

async def precinto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/precinto 4209200</code>", parse_mode="HTML")
        return

    p = safe_str(context.args[0])
    clientes = get_cached_records(ws_clientes, "clientes")
    found = [c for c in clientes if safe_str(c.get("N° de Precinto")) == p]

    if not found:
        await update.message.reply_text(f"❌ Precinto <code>{p}</code> no asignado a ningún cliente.", parse_mode="HTML")
        return

    msg = f"<b>🏷️ Precinto {p}</b>

"
    for c in found:
        msg += format_cliente(c) + "
"
    await update.message.reply_text(msg[:4000], parse_mode="HTML", disable_web_page_preview=True)

async def historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/historial 6331</code>", parse_mode="HTML")
        return

    num = safe_str(context.args[0])
    reclamos = get_cached_records(ws_reclamos, "reclamos")
    historial = [r for r in reclamos if safe_str(r.get("Nº Cliente")) == num]

    if not historial:
        await update.message.reply_text(f"❌ Cliente <b>#{num}</b> no tiene reclamos.", parse_mode="HTML")
        return

    historial = historial[-10:]
    historial.reverse()
    msg = f"<b>📜 Historial completo — Cliente #{num}</b>

"
    for i, r in enumerate(historial, 1):
        msg += format_reclamo(r, i) + "
"

    if len(msg) > 4000:
        msg = msg[:4000] + "

<i>... mostrando últimos 10</i>"
    await update.message.reply_text(msg, parse_mode="HTML")

async def ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/ubicacion 6331</code>", parse_mode="HTML")
        return

    num = safe_str(context.args[0])
    clientes = get_cached_records(ws_clientes, "clientes")
    cliente_row = next((c for c in clientes if safe_str(c.get("Nº Cliente")) == num), None)

    if not cliente_row:
        await update.message.reply_text(f"❌ Cliente <b>#{num}</b> no encontrado.", parse_mode="HTML")
        return

    lat = safe_str(cliente_row.get("Latitud"))
    lon = safe_str(cliente_row.get("Longitud"))
    nombre = safe_str(cliente_row.get("Nombre"))
    direccion = safe_str(cliente_row.get("Dirección"))

    if lat and lon:
        maps_url = f"https://www.google.com/maps?q={lat},{lon}"
        msg = (
            f"<b>📍 Cliente #{num} — {nombre}</b>
"
            f"{direccion}

"
            f"<a href='{maps_url}'>🗺️ Abrir Google Maps</a>

"
            f"<code>Lat:</code> {lat}
<code>Lon:</code> {lon}"
        )
    else:
        msg = f"<b>📍 Cliente #{num} — {nombre}</b>

<i>No tiene coordenadas cargadas.</i>"
    await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=False)

async def reclamo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/reclamo A64C13C0</code>", parse_mode="HTML")
        return

    rid = safe_str(context.args[0])
    reclamos = get_cached_records(ws_reclamos, "reclamos")
    found = [r for r in reclamos if safe_str(r.get("ID Reclamo")) == rid]

    if not found:
        await update.message.reply_text(f"❌ Reclamo <code>{rid}</code> no encontrado.", parse_mode="HTML")
        return

    r = found[0]
    msg = (
        f"<b>📋 Detalle del Reclamo</b>

"
        f"├ <b>Cliente:</b> #{safe_str(r.get('Nº Cliente'))} — {safe_str(r.get('Nombre'))}
"
        f"├ <b>Fecha:</b> {safe_str(r.get('Fecha y hora'))}
"
        f"├ <b>Tipo:</b> {safe_str(r.get('Tipo de reclamo'))}
"
        f"├ <b>Estado:</b> {safe_str(r.get('Estado'))}
"
        f"├ <b>Técnico:</b> {safe_str(r.get('Técnico')) or '—'}
"
        f"├ <b>Precinto:</b> {safe_str(r.get('N° de Precinto')) or '—'}
"
        f"├ <b>Dirección:</b> {safe_str(r.get('Dirección'))}
"
        f"├ <b>Teléfono:</b> {safe_str(r.get('Teléfono')) or '—'}
"
        f"└ <b>Detalle:</b> {safe_str(r.get('Detalles')) or '—'}
"
    )
    await update.message.reply_text(msg, parse_mode="HTML")