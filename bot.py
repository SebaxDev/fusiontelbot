import os
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from collections import Counter
from io import BytesIO
from flask import Flask
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== CONFIGURACION ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
CACHE_TTL = int(os.environ.get("CACHE_TTL", 60))

# ==================== LOGGING ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== ERROR HANDLER ====================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import Conflict
    if isinstance(context.error, Conflict):
        logger.warning("⚠️ Conflict detectado (otra instancia activa). Reintentando...")
        return
    logger.error(f"❌ Error no manejado: {context.error}", exc_info=context.error)

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

# ==================== CACHE ====================
_cache = {}
_cache_time = {}

def get_sheet_data(sheet_name, force=False):
    now = time.time()
    if not force and sheet_name in _cache and (now - _cache_time.get(sheet_name, 0)) < CACHE_TTL:
        return _cache[sheet_name]

    t0 = time.time()
    ws = ws_reclamos if sheet_name == "Reclamos" else ws_clientes

    # Usar values_get directo al API para saltar cache interno de gspread
    try:
        result = ws.spreadsheet.values_get(
            ws.title,
            params={"dateTimeRenderOption": "FORMATTED_STRING"}
        )
        values = result.get("values", [])
    except Exception:
        # Fallback si falla el método directo
        values = ws.get_all_values()

    if values and len(values) > 1:
        headers = [str(h).strip() if h else "" for h in values[0]]
        data = []
        for row in values[1:]:
            record = {}
            for i, header in enumerate(headers):
                record[header] = row[i] if i < len(row) else ""
            data.append(record)
    else:
        data = []

    _cache[sheet_name] = data
    _cache_time[sheet_name] = now
    elapsed = time.time() - t0
    logger.info(f"📡 {sheet_name} recargado: {len(data)} registros ({elapsed:.1f}s)")
    return data

def force_refresh():
    """Reabre el spreadsheet por completo para invalidar todo cache"""
    global sh, ws_reclamos, ws_clientes
    clear_cache()
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws_reclamos = sh.worksheet("Reclamos")
    ws_clientes = sh.worksheet("Clientes")
    logger.info("🔄 Spreadsheet reabierto forzando refresh")

def clear_cache():
    _cache.clear()
    _cache_time.clear()

# ==================== HELPERS ====================
def safe_str(value):
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in ("nan", "none", "null", "nat", "*", "—"):
        return ""
    return s

def parse_fecha(fecha_str):
    try:
        return datetime.strptime(str(fecha_str).strip(), "%d/%m/%Y %H:%M")
    except Exception:
        try:
            return datetime.strptime(str(fecha_str).strip(), "%d/%m/%Y")
        except Exception:
            return None

def is_today(fecha_str):
    dt = parse_fecha(fecha_str)
    if not dt:
        return False
    return dt.date() == datetime.now().date()

def is_last_30_days(fecha_str):
    dt = parse_fecha(fecha_str)
    if not dt:
        return False
    return datetime.now() - dt <= timedelta(days=30)

def tiene_tecnico(row):
    t = safe_str(row.get("Técnico"))
    return t != "" and t.lower() not in ("base", "oficina", "sin técnico")

def send_long_message(update, text, parse_mode="HTML", **kwargs):
    max_len = 4000
    if len(text) <= max_len:
        return update.message.reply_text(text, parse_mode=parse_mode, **kwargs)
    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        idx = text.rfind("\n\n", 0, max_len)
        if idx == -1:
            idx = text.rfind("\n", 0, max_len)
        if idx == -1:
            idx = max_len
        parts.append(text[:idx])
        text = text[idx:].lstrip()
    for i, part in enumerate(parts):
        suffix = "\n\n<i>...continúa...</i>" if i < len(parts) - 1 else ""
        update.message.reply_text(part + suffix, parse_mode=parse_mode, **kwargs)

def format_cliente(row):
    nombre = safe_str(row.get("Nombre"))
    direccion = safe_str(row.get("Dirección"))
    telefono = safe_str(row.get("Teléfono"))
    precinto = safe_str(row.get("N° de Precinto"))
    plan = safe_str(row.get("Plan"))
    sector = safe_str(row.get("Sector"))
    lat = safe_str(row.get("Latitud"))
    lon = safe_str(row.get("Longitud"))

    html = f"<b>👤 Cliente #{safe_str(row.get('Nº Cliente'))}</b>\n"
    html += f"├ <b>Nombre:</b> {nombre}\n"
    html += f"├ <b>Dirección:</b> {direccion}\n"
    html += f"├ <b>Teléfono:</b> {telefono or '—'}\n"
    html += f"├ <b>Precinto:</b> {precinto or 'No asignado'}\n"
    html += f"├ <b>Plan:</b> {plan or '—'}\n"
    html += f"├ <b>Sector:</b> {sector or '—'}\n"
    if lat and lon:
        maps_url = f"https://www.google.com/maps?q={lat},{lon}"
        html += f"└ <b>📍 Ubicación:</b> <a href='{maps_url}'>Ver en Google Maps</a>\n"
    else:
        html += f"└ <b>📍 Ubicación:</b> No disponible\n"
    return html

def format_reclamo(row, idx=None, show_cliente=True):
    pref = f"{idx}. " if idx else ""
    fecha = safe_str(row.get("Fecha y hora"))
    tipo = safe_str(row.get("Tipo de reclamo"))
    estado = safe_str(row.get("Estado"))
    tecnico = safe_str(row.get("Técnico"))
    detalles = safe_str(row.get("Detalles"))
    num_cliente = safe_str(row.get("Nº Cliente"))
    nombre = safe_str(row.get("Nombre"))

    html = f"<b>{pref}{fecha}</b> | {tipo}\n"
    if show_cliente:
        html += f"├ <b>Cliente:</b> #{num_cliente} — {nombre}\n"
    html += f"├ <b>Estado:</b> {estado or '—'}\n"
    html += f"├ <b>Técnico:</b> {tecnico or '—'}\n"
    if detalles:
        html += f"└ <b>Detalle:</b> {detalles[:80]}{'...' if len(detalles) > 80 else ''}\n"
    else:
        html += f"└ <b>Detalle:</b> —\n"
    return html

# ==================== COMANDOS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Bot de Reclamos — Fusión</b>\n\n"
        "Comandos disponibles:\n\n"
        "• <b>/cliente</b> &lt;número&gt; — Ficha del cliente + historial\n"
        "• <b>/precinto</b> &lt;número&gt; — Buscar cliente por precinto\n"
        "• <b>/historial</b> &lt;número&gt; — Todos los reclamos de un cliente\n"
        "• <b>/ubicacion</b> &lt;número&gt; — Link de Maps del cliente\n"
        "• <b>/reclamo</b> &lt;ID&gt; — Buscar reclamo por ID\n"
        "• <b>/tecnico</b> &lt;nombre&gt; — Reclamos de un técnico\n"
        "• <b>/nombre</b> &lt;texto&gt; — Buscar cliente por nombre\n"
        "• <b>/recientes</b> &lt;N&gt; — Últimos N reclamos\n"
        "• <b>/resumen</b> — Resumen de hoy\n"
        "• <b>/pendientes</b> — Lista completa de pendientes\n"
        "• <b>/topmes</b> — Ranking técnicos últimos 30 días\n"
        "• <b>/mapa</b> &lt;sector&gt; — Mapa de reclamos por sector\n"
        "• <b>/actualizar</b> — Forzar recarga de datos del Sheet\n\n"
        "Ejemplo: <code>/cliente 6331</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def actualizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fuerza la recarga completa desde Google Sheets"""
    msg = await update.message.reply_text("🔄 <b>Actualizando datos...</b>", parse_mode="HTML")
    try:
        # Reabrir spreadsheet por completo para romper cache de Google
        force_refresh()
        # Esperar un momento para que el cache de Google se actualice
        await asyncio.sleep(1)
        # Forzar lectura con el método directo al API
        reclamos = get_sheet_data("Reclamos", force=True)
        clientes = get_sheet_data("Clientes", force=True)

        ahora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        respuesta = (
            f"✅ <b>Datos actualizados</b>\n\n"
            f"├ <b>Fecha:</b> {ahora}\n"
            f"├ <b>Reclamos:</b> {len(reclamos)} registros\n"
            f"├ <b>Clientes:</b> {len(clientes)} registros\n"
            f"└ <b>Cache:</b> {CACHE_TTL}s\n\n"
            f"<i>Ahora usá /resumen para ver datos frescos.</i>"
        )
        await msg.edit_text(respuesta, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error al actualizar: {e}")
        await msg.edit_text(f"❌ <b>Error al actualizar:</b> {e}", parse_mode="HTML")

async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reclamos = get_sheet_data("Reclamos")
    hoy = datetime.now().date()

    hoy_reclamos = [r for r in reclamos if is_today(safe_str(r.get("Fecha y hora")))]
    generados = len(hoy_reclamos)

    # Clasificación clara: cada reclamo cae en UNA sola categoría
    resueltos = 0
    en_curso = 0
    pendientes = 0

    for r in hoy_reclamos:
        estado = safe_str(r.get("Estado")).lower()
        if estado == "resuelto":
            resueltos += 1
        elif tiene_tecnico(r):
            en_curso += 1
        else:
            pendientes += 1

    # Info de cache para que sepás si estás viendo datos frescos
    cache_age = int(time.time() - _cache_time.get("Reclamos", 0))
    if cache_age < 5:
        cache_str = "ahora mismo"
    elif cache_age < 60:
        cache_str = f"hace {cache_age}s"
    else:
        cache_str = f"hace {cache_age // 60}min {cache_age % 60}s"

    msg = (
        f"<b>📊 Resumen del día — {hoy.strftime('%d/%m/%Y')}</b>\n"
        f"<i>🕐 Datos: {cache_str}</i>\n\n"
        f"├ <b>📝 Generados hoy:</b> {generados}\n"
        f"├ <b>✅ Resueltos:</b> {resueltos}\n"
        f"├ <b>🔧 En curso:</b> {en_curso}\n"
        f"└ <b>⏳ Pendientes:</b> {pendientes}\n"
    )

    # Verificación: la suma tiene que dar generados
    suma = resueltos + en_curso + pendientes
    if suma != generados:
        msg += f"\n⚠️ <i>Atención: {generados - suma} reclamo(s) con estado no reconocido. "
        msg += f"Usá /actualizar antes de consultar.</i>"

    await update.message.reply_text(msg, parse_mode="HTML")

async def cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/cliente 6331</code>", parse_mode="HTML")
        return

    num = safe_str(context.args[0])
    clientes = get_sheet_data("Clientes")
    reclamos = get_sheet_data("Reclamos")

    cliente_row = next((c for c in clientes if safe_str(c.get("Nº Cliente")) == num), None)
    if not cliente_row:
        await update.message.reply_text(f"❌ Cliente <b>#{num}</b> no encontrado.", parse_mode="HTML")
        return

    historial = [r for r in reclamos if safe_str(r.get("Nº Cliente")) == num]
    total_reclamos = len(historial)
    historial = historial[-5:]
    historial.reverse()

    msg = format_cliente(cliente_row)
    msg += f"\n<b>📋 Historial ({len(historial)} de {total_reclamos}):</b>\n\n"
    if historial:
        for i, r in enumerate(historial, 1):
            msg += format_reclamo(r, i) + "\n"
    else:
        msg += "<i>Sin reclamos registrados.</i>\n"

    send_long_message(update, msg, disable_web_page_preview=True)

async def precinto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/precinto 4209200</code>", parse_mode="HTML")
        return

    p = safe_str(context.args[0])
    clientes = get_sheet_data("Clientes")
    found = [c for c in clientes if safe_str(c.get("N° de Precinto")) == p]

    if not found:
        await update.message.reply_text(f"❌ Precinto <code>{p}</code> no asignado a ningún cliente.", parse_mode="HTML")
        return

    msg = f"<b>🏷️ Precinto {p}</b>\n\n"
    for c in found:
        msg += format_cliente(c) + "\n"
    send_long_message(update, msg[:4000], disable_web_page_preview=True)

async def historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/historial 6331</code>", parse_mode="HTML")
        return

    num = safe_str(context.args[0])
    reclamos = get_sheet_data("Reclamos")
    historial = [r for r in reclamos if safe_str(r.get("Nº Cliente")) == num]

    if not historial:
        await update.message.reply_text(f"❌ Cliente <b>#{num}</b> no tiene reclamos.", parse_mode="HTML")
        return

    historial = historial[-10:]
    historial.reverse()
    msg = f"<b>📜 Historial completo — Cliente #{num}</b>\n\n"
    for i, r in enumerate(historial, 1):
        msg += format_reclamo(r, i) + "\n"

    send_long_message(update, msg)

async def ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/ubicacion 6331</code>", parse_mode="HTML")
        return

    num = safe_str(context.args[0])
    clientes = get_sheet_data("Clientes")
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
            f"<b>📍 Cliente #{num} — {nombre}</b>\n"
            f"{direccion}\n\n"
            f"<a href='{maps_url}'>🗺️ Abrir Google Maps</a>\n\n"
            f"<code>Lat:</code> {lat}\n<code>Lon:</code> {lon}"
        )
    else:
        msg = f"<b>📍 Cliente #{num} — {nombre}</b>\n\n<i>No tiene coordenadas cargadas.</i>"
    await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=False)

async def reclamo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/reclamo A64C13C0</code>", parse_mode="HTML")
        return

    rid = safe_str(context.args[0])
    reclamos = get_sheet_data("Reclamos")
    found = [r for r in reclamos if safe_str(r.get("ID Reclamo")) == rid]

    if not found:
        await update.message.reply_text(f"❌ Reclamo <code>{rid}</code> no encontrado.", parse_mode="HTML")
        return

    r = found[0]
    msg = (
        f"<b>📋 Reclamo</b>\n\n"
        f"├ <b>Cliente:</b> #{safe_str(r.get('Nº Cliente'))} — {safe_str(r.get('Nombre'))}\n"
        f"├ <b>Fecha:</b> {safe_str(r.get('Fecha y hora'))}\n"
        f"├ <b>Tipo:</b> {safe_str(r.get('Tipo de reclamo'))}\n"
        f"├ <b>Estado:</b> {safe_str(r.get('Estado'))}\n"
        f"├ <b>Técnico:</b> {safe_str(r.get('Técnico')) or '—'}\n"
        f"├ <b>Precinto:</b> {safe_str(r.get('N° de Precinto')) or '—'}\n"
        f"├ <b>Dirección:</b> {safe_str(r.get('Dirección'))}\n"
        f"├ <b>Teléfono:</b> {safe_str(r.get('Teléfono')) or '—'}\n"
        f"└ <b>Detalle:</b> {safe_str(r.get('Detalles')) or '—'}\n"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

async def tecnico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/tecnico ROQUE</code>", parse_mode="HTML")
        return

    nombre = " ".join(context.args)
    reclamos = get_sheet_data("Reclamos")
    found = [r for r in reclamos if nombre.lower() in safe_str(r.get("Técnico")).lower()]

    if not found:
        await update.message.reply_text(f"❌ No hay reclamos asignados a <b>{nombre}</b>.", parse_mode="HTML")
        return

    en_curso = [r for r in found if safe_str(r.get("Estado")).lower() != "resuelto"]
    verificados = [r for r in found if safe_str(r.get("Estado")).lower() == "resuelto"]

    msg = f"<b>👷 Reclamos de {nombre}</b>\n\n"

    if en_curso:
        msg += f"<b>🔧 En curso ({len(en_curso)}):</b>\n\n"
        for i, r in enumerate(en_curso[-10:], 1):
            msg += format_reclamo(r, i) + "\n"
    else:
        msg += "<b>🔧 En curso:</b> <i>Ninguno</i>\n\n"

    if verificados:
        msg += f"<b>✅ Resueltos ({len(verificados)}):</b>\n\n"
        for i, r in enumerate(verificados[-5:], 1):
            msg += format_reclamo(r, i) + "\n"
    else:
        msg += "<b>✅ Resueltos:</b> <i>Ninguno</i>\n"

    send_long_message(update, msg)

async def nombre_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/nombre BENITEZ</code>", parse_mode="HTML")
        return

    texto = " ".join(context.args).lower()
    clientes = get_sheet_data("Clientes")
    found = [c for c in clientes if texto in safe_str(c.get("Nombre")).lower()]

    if not found:
        await update.message.reply_text(f"❌ No se encontró cliente con <b>{texto}</b>.", parse_mode="HTML")
        return

    msg = f"<b>🔍 Resultados ({len(found)}):</b>\n\n"
    for c in found[:5]:
        msg += format_cliente(c) + "\n"
    send_long_message(update, msg[:4000], disable_web_page_preview=True)

async def recientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(context.args[0]) if context.args else 5
    except ValueError:
        n = 5
    n = max(1, min(n, 20))

    reclamos = get_sheet_data("Reclamos")
    ultimos = reclamos[-n:]
    ultimos.reverse()

    msg = f"<b>📅 Últimos {n} reclamos:</b>\n\n"
    for i, r in enumerate(ultimos, 1):
        msg += format_reclamo(r, i, show_cliente=True) + "\n"
    send_long_message(update, msg)

async def pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reclamos = get_sheet_data("Reclamos")
    pendientes_list = [r for r in reclamos if not tiene_tecnico(r) and safe_str(r.get("Estado")).lower() != "resuelto"]

    if not pendientes_list:
        await update.message.reply_text("✅ <b>No hay reclamos pendientes.</b>", parse_mode="HTML")
        return

    pendientes_list.sort(key=lambda x: parse_fecha(safe_str(x.get("Fecha y hora"))) or datetime.min, reverse=True)

    msg = f"<b>⏳ Reclamos Pendientes ({len(pendientes_list)}):</b>\n\n"
    for i, r in enumerate(pendientes_list, 1):
        num = safe_str(r.get("Nº Cliente"))
        nombre = safe_str(r.get("Nombre"))
        direccion = safe_str(r.get("Dirección"))
        tipo = safe_str(r.get("Tipo de reclamo"))
        fecha = safe_str(r.get("Fecha y hora"))
        msg += f"{i}. <b>#{num}</b> — {nombre}\n"
        msg += f"   📍 {direccion}\n"
        msg += f"   🏷️ {tipo} | 📅 {fecha}\n\n"

    send_long_message(update, msg)

async def topmes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reclamos = get_sheet_data("Reclamos")

    resueltos = [r for r in reclamos if safe_str(r.get("Estado")).lower() == "resuelto" and is_last_30_days(safe_str(r.get("Fecha y hora")))]

    if not resueltos:
        await update.message.reply_text("📉 <b>No hay reclamos resueltos en los últimos 30 días.</b>", parse_mode="HTML")
        return

    conteo = Counter()
    for r in resueltos:
        tecnicos = safe_str(r.get("Técnico"))
        if not tecnicos:
            continue
        for t in tecnicos.replace(", ", ",").replace(" y ", ",").replace(" / ", ",").split(","):
            t = t.strip().upper()
            if t and t not in ("BASE", "OFICINA"):
                conteo[t] += 1

    if not conteo:
        await update.message.reply_text("📉 <b>No hay técnicos con reclamos resueltos.</b>", parse_mode="HTML")
        return

    ranking = conteo.most_common()
    msg = "<b>🏆 Top Técnicos — Últimos 30 días</b>\n\n"
    for i, (tec, cant) in enumerate(ranking, 1):
        msg += f"{i}. <b>{tec}</b> ({cant} Resueltos)\n"

    await update.message.reply_text(msg, parse_mode="HTML")

async def mapa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        await update.message.reply_text("⚠️ <b>Mapa no disponible:</b> falta instalar <code>matplotlib</code>.", parse_mode="HTML")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/mapa 7</code>", parse_mode="HTML")
        return

    sector = safe_str(context.args[0])
    reclamos = get_sheet_data("Reclamos")

    puntos = []
    for r in reclamos:
        if safe_str(r.get("Sector")) != sector:
            continue
        lat = safe_str(r.get("Latitud"))
        lon = safe_str(r.get("Longitud"))
        if lat and lon:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                estado = safe_str(r.get("Estado"))
                puntos.append((lat_f, lon_f, estado))
            except ValueError:
                continue

    if not puntos:
        await update.message.reply_text(f"❌ <b>Sector {sector}:</b> no hay coordenadas disponibles.", parse_mode="HTML")
        return

    fig, ax = plt.subplots(figsize=(10, 8))

    for lat, lon, estado in puntos:
        color = "green" if estado.lower() == "resuelto" else "orange" if estado else "red"
        ax.plot(lon, lat, marker='o', color=color, markersize=8)

    ax.set_title(f"Mapa Sector {sector} — {len(puntos)} puntos")
    ax.set_xlabel("Longitud")
    ax.set_ylabel("Latitud")
    ax.grid(True)

    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)

    await update.message.reply_photo(photo=buf, caption=f"🗺️ <b>Mapa Sector {sector}</b>\n{len(puntos)} puntos cargados.")

# ==================== MAIN ====================
import asyncio

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_error_handler(error_handler)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cliente", cliente))
    application.add_handler(CommandHandler("precinto", precinto))
    application.add_handler(CommandHandler("historial", historial))
    application.add_handler(CommandHandler("ubicacion", ubicacion))
    application.add_handler(CommandHandler("reclamo", reclamo_cmd))
    application.add_handler(CommandHandler("tecnico", tecnico))
    application.add_handler(CommandHandler("nombre", nombre_cmd))
    application.add_handler(CommandHandler("recientes", recientes))
    application.add_handler(CommandHandler("resumen", resumen))
    application.add_handler(CommandHandler("pendientes", pendientes))
    application.add_handler(CommandHandler("topmes", topmes))
    application.add_handler(CommandHandler("mapa", mapa))
    application.add_handler(CommandHandler("actualizar", actualizar))

    if os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
        logger.info("🚀 Modo Render detectado. Iniciando servidor de health-check...")
        threading.Thread(target=run_web_server, daemon=True).start()

    logger.info("🤖 Bot iniciado. Esperando mensajes...")
    application.run_polling(
        drop_pending_updates=True,
        poll_interval=2.0
    )

if __name__ == "__main__":
    main()