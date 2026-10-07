import os
import json
import logging
import threading
import time
import asyncio
from datetime import datetime, timedelta
from collections import Counter
from io import BytesIO
from functools import wraps
from flask import Flask
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== CONFIGURACION ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
CACHE_TTL = int(os.environ.get("CACHE_TTL", 60))

# Contraseña de seguridad (por defecto 142536)
BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "142536")

# Lista en memoria de chats autorizados
authorized_chats = set()

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
ws_cajas = sh.worksheet("Cajas")

# ==================== CACHE ====================
_cache = {}
_cache_time = {}

def get_sheet_data(sheet_name, force=False):
    now = time.time()
    if not force and sheet_name in _cache and (now - _cache_time.get(sheet_name, 0)) < CACHE_TTL:
        return _cache[sheet_name]

    t0 = time.time()
    if sheet_name == "Reclamos":
        ws = ws_reclamos
    elif sheet_name == "Clientes":
        ws = ws_clientes
    else:
        ws = ws_cajas

    try:
        result = ws.spreadsheet.values_get(
            ws.title,
            params={"dateTimeRenderOption": "FORMATTED_STRING"}
        )
        values = result.get("values", [])
    except Exception:
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
    global sh, ws_reclamos, ws_clientes, ws_cajas
    clear_cache()
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws_reclamos = sh.worksheet("Reclamos")
    ws_clientes = sh.worksheet("Clientes")
    ws_cajas = sh.worksheet("Cajas")
    logger.info("🔄 Spreadsheet reabierto forzando refresh")

def clear_cache():
    _cache.clear()
    _cache_time.clear()

# ==================== SEGURIDAD (MIDDLEWARE) ====================
def requires_auth(func):
    """Decorador para restringir el acceso solo a chats logueados."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.message:
            return
            
        chat_id = update.effective_chat.id
        if chat_id not in authorized_chats:
            await update.message.reply_text(
                "⛔ <b>Acceso denegado.</b>\nNo estás autorizado para usar este bot. "
                "Por favor, ingresá la contraseña usando:\n\n"
                "<code>/login CONTRASEÑA</code>", 
                parse_mode="HTML"
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para ingresar la contraseña y autorizar el chat."""
    if not context.args:
        await update.message.reply_text("⚠️ Usá el formato: <code>/login CONTRASEÑA</code>", parse_mode="HTML")
        return
        
    password = context.args[0]
    if password == BOT_PASSWORD:
        authorized_chats.add(update.effective_chat.id)
        await update.message.reply_text("✅ <b>Acceso concedido.</b>\nYa podés utilizar todos los comandos del bot.", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Contraseña incorrecta.", parse_mode="HTML")

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

async def send_long_message(update, text, parse_mode="HTML", **kwargs):
    max_len = 4000
    if len(text) <= max_len:
        return await update.message.reply_text(text, parse_mode=parse_mode, **kwargs)
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
        await update.message.reply_text(part + suffix, parse_mode=parse_mode, **kwargs)

def format_cliente(row):
    nombre = safe_str(row.get("Nombre"))
    direccion = safe_str(row.get("Dirección"))
    telefono = safe_str(row.get("Teléfono"))
    precinto = safe_str(row.get("N° de Precinto"))
    caja = safe_str(row.get("Caja NAP"))
    plan = safe_str(row.get("Plan"))
    sector = safe_str(row.get("Sector"))
    lat = safe_str(row.get("Latitud"))
    lon = safe_str(row.get("Longitud"))

    html = f"<b>👤 Cliente #{safe_str(row.get('Nº Cliente'))}</b>\n"
    html += f"├ <b>Nombre:</b> {nombre}\n"
    html += f"├ <b>Dirección:</b> {direccion}\n"
    html += f"├ <b>Teléfono:</b> {telefono or '—'}\n"
    html += f"├ <b>Precinto:</b> {precinto or 'No asignado'}\n"
    html += f"├ <b>Caja NAP:</b> {caja or 'Sin caja'}\n"
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

    html = f"<b>{pref}{fecha}</b> | {tipo}\n"
    html += f"├ <b>Estado:</b> {estado or '—'}\n"
    html += f"└ <b>Técnico:</b> {tecnico or '—'}\n"
    return html

# ==================== COMANDOS ====================
@requires_auth
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Bot de Reclamos — Fusión</b>\n\n"
        "Comandos disponibles:\n\n"
        "• <b>/cliente</b> &lt;número&gt; — Ficha del cliente + historial\n"
        "• <b>/precinto</b> &lt;número&gt; — Buscar cliente por precinto\n"
        "• <b>/caja</b> &lt;numero&gt; — Info física y puertos activos\n"
        "• <b>/resumen</b> — Resumen de la jornada de hoy\n"
        "• <b>/topmes</b> — Ranking técnicos últimos 30 días\n"
        "• <b>/actualizar</b> — Forzar recarga de datos del Sheet\n\n"
        "Ejemplo: <code>/cliente 6331</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

@requires_auth
async def actualizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 <b>Actualizando datos...</b>", parse_mode="HTML")
    try:
        force_refresh()
        await asyncio.sleep(1)
        reclamos = get_sheet_data("Reclamos", force=True)
        clientes = get_sheet_data("Clientes", force=True)
        cajas = get_sheet_data("Cajas", force=True)

        ahora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        respuesta = (
            f"✅ <b>Datos actualizados</b>\n\n"
            f"├ <b>Fecha:</b> {ahora}\n"
            f"├ <b>Reclamos:</b> {len(reclamos)} registros\n"
            f"├ <b>Clientes:</b> {len(clientes)} registros\n"
            f"├ <b>Cajas NAP:</b> {len(cajas)} registros\n"
            f"└ <b>Cache:</b> {CACHE_TTL}s\n\n"
            f"<i>Listo para consultar.</i>"
        )
        await msg.edit_text(respuesta, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error al actualizar: {e}")
        await msg.edit_text(f"❌ <b>Error al actualizar:</b> {e}", parse_mode="HTML")

@requires_auth
async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reclamos = get_sheet_data("Reclamos")
    hoy = datetime.now().date()

    hoy_reclamos = [r for r in reclamos if is_today(safe_str(r.get("Fecha y hora")))]
    generados = len(hoy_reclamos)

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

    await update.message.reply_text(msg, parse_mode="HTML")

@requires_auth
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
    msg += f"\n<b>📋 Últimos Reclamos ({len(historial)} de {total_reclamos}):</b>\n\n"
    if historial:
        for i, r in enumerate(historial, 1):
            msg += format_reclamo(r, i) + "\n"
    else:
        msg += "<i>Sin reclamos registrados.</i>\n"

    await send_long_message(update, msg, disable_web_page_preview=True)

@requires_auth
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
    await send_long_message(update, msg[:4000], disable_web_page_preview=True)

@requires_auth
async def caja_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/caja 450</code>", parse_mode="HTML")
        return

    numero = safe_str(context.args[0])
    cajas = get_sheet_data("Cajas")
    caja_row = next((c for c in cajas if safe_str(c.get("N De Caja")) == numero), None)

    if not caja_row:
        await update.message.reply_text(f"❌ Caja NAP <b>{numero}</b> no encontrada en el sistema.", parse_mode="HTML")
        return

    sector = safe_str(caja_row.get("Sector"))
    barrio = safe_str(caja_row.get("Barrio"))
    splitter = safe_str(caja_row.get("Splitter"))
    lat = safe_str(caja_row.get("Latitud"))
    lon = safe_str(caja_row.get("Longitud"))
    
    # Extraer puertos ocupados
    precintos_caja = []
    for i in range(1, 17):
        p = safe_str(caja_row.get(f"Precinto {i}"))
        if p:
            precintos_caja.append((i, p))
            
    # Traer clientes para cruzar los datos
    clientes = get_sheet_data("Clientes")
    clientes_dict = {safe_str(c.get("N° de Precinto")): c for c in clientes if safe_str(c.get("N° de Precinto"))}

    # Armado del mensaje
    msg = f"<b>📦 Caja NAP {numero}</b>\n\n"
    msg += f"├ <b>Sector:</b> {sector or '—'}\n"
    msg += f"├ <b>Barrio:</b> {barrio or '—'}\n"
    msg += f"├ <b>Splitter:</b> {splitter or '—'}\n"

    if lat and lon:
        maps_url = f"https://www.google.com/maps?q={lat},{lon}"
        msg += f"└ <b>📍 Ubicación:</b> <a href='{maps_url}'>Ver en Google Maps</a>\n\n"
    else:
        msg += f"└ <b>📍 Ubicación:</b> No disponible\n\n"

    msg += f"<b>🔌 Puertos Ocupados ({len(precintos_caja)}):</b>\n"
    if not precintos_caja:
        msg += "<i>Todos los puertos están libres.</i>\n"
    else:
        for puerto, p_num in precintos_caja:
            cli = clientes_dict.get(p_num)
            if cli:
                nombre = safe_str(cli.get('Nombre'))
                nro_cli = safe_str(cli.get('Nº Cliente'))
                msg += f"• <b>P{puerto}</b>: {p_num} 🟢 #{nro_cli} - {nombre}\n"
            else:
                msg += f"• <b>P{puerto}</b>: {p_num} 🔴 <i>Sin cliente en sistema</i>\n"

    await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)

@requires_auth
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

# ==================== MAIN ====================
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_error_handler(error_handler)

    # El login no lleva el decorador de auth
    application.add_handler(CommandHandler("login", login))
    
    # Comandos seguros
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cliente", cliente))
    application.add_handler(CommandHandler("precinto", precinto))
    application.add_handler(CommandHandler("caja", caja_cmd))
    application.add_handler(CommandHandler("resumen", resumen))
    application.add_handler(CommandHandler("topmes", topmes))
    application.add_handler(CommandHandler("actualizar", actualizar))

    if os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
        logger.info("🚀 Modo Render detectado. Iniciando servidor web para health-check...")
        threading.Thread(target=run_web_server, daemon=True).start()

    logger.info("🤖 Bot iniciado. Esperando comandos...")
    application.run_polling(
        drop_pending_updates=True,
        poll_interval=2.0
    )

if __name__ == "__main__":
    main()