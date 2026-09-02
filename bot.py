import os
import json
import logging
import threading
from flask import Flask
import gspread
from oauth2client.service_account import ServiceAccountCredentials
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
# Esto evita que Render "duerma" el servicio porque siempre hay un puerto activo
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
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(credentials)

gc = get_google_client()
sh = gc.open_by_key(SPREADSHEET_ID)
ws_reclamos = sh.worksheet("Reclamos")
ws_clientes = sh.worksheet("Clientes")

# ==================== HELPERS ====================
def safe_str(value):
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in ("nan", "none", "null", "nat"):
        return ""
    return s

def format_cliente(row):
    nombre = safe_str(row.get("Nombre"))
    direccion = safe_str(row.get("Dirección"))
    telefono = safe_str(row.get("Teléfono"))
    precinto = safe_str(row.get("N° de Precinto"))
    plan = safe_str(row.get("Plan"))
    sector = safe_str(row.get("Sector"))
    lat = safe_str(row.get("Latitud"))
    lon = safe_str(row.get("Longitud"))
    cliente_id = safe_str(row.get("ID Cliente"))
    
    html = f"<b>👤 Cliente #{safe_str(row.get('Nº Cliente'))}</b>\n"
    html += f"├ <b>Nombre:</b> {nombre}\n"
    html += f"├ <b>Dirección:</b> {direccion}\n"
    html += f"├ <b>Teléfono:</b> {telefono or '—'}\n"
    html += f"├ <b>Precinto:</b> {precinto or 'No asignado'}\n"
    html += f"├ <b>Plan:</b> {plan or '—'}\n"
    html += f"├ <b>Sector:</b> {sector or '—'}\n"
    html += f"├ <b>ID Cliente:</b> <code>{cliente_id}</code>\n"
    if lat and lon:
        maps_url = f"https://www.google.com/maps?q={lat},{lon}"
        html += f"└ <b>📍 Ubicación:</b> <a href='{maps_url}'>Ver en Google Maps</a>\n"
    else:
        html += f"└ <b>📍 Ubicación:</b> No disponible\n"
    return html

def format_reclamo(row, idx=None):
    pref = f"{idx}. " if idx else ""
    fecha = safe_str(row.get("Fecha y hora"))
    tipo = safe_str(row.get("Tipo de reclamo"))
    estado = safe_str(row.get("Estado"))
    tecnico = safe_str(row.get("Técnico"))
    detalles = safe_str(row.get("Detalles"))
    rec_id = safe_str(row.get("ID Reclamo"))
    
    html = f"<b>{pref}{fecha}</b> | {tipo}\n"
    html += f"├ <b>Estado:</b> {estado}\n"
    html += f"├ <b>Técnico:</b> {tecnico or '—'}\n"
    html += f"├ <b>ID:</b> <code>{rec_id}</code>\n"
    if detalles:
        html += f"└ <b>Detalle:</b> {detalles[:80]}{'...' if len(detalles) > 80 else ''}\n"
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
        "• <b>/recientes</b> &lt;N&gt; — Últimos N reclamos\n\n"
        "Ejemplo: <code>/cliente 6331</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/cliente 6331</code>", parse_mode="HTML")
        return
    
    num = safe_str(context.args[0])
    clientes = ws_clientes.get_all_records()
    reclamos = ws_reclamos.get_all_records()
    
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
    
    if len(msg) > 4000:
        msg = msg[:4000] + "\n\n<i>... mensaje truncado</i>"
    await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)

async def precinto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/precinto 4209200</code>", parse_mode="HTML")
        return
    
    p = safe_str(context.args[0])
    clientes = ws_clientes.get_all_records()
    found = [c for c in clientes if safe_str(c.get("N° de Precinto")) == p]
    
    if not found:
        await update.message.reply_text(f"❌ Precinto <code>{p}</code> no asignado a ningún cliente.", parse_mode="HTML")
        return
    
    msg = f"<b>🏷️ Precinto {p}</b>\n\n"
    for c in found:
        msg += format_cliente(c) + "\n"
    await update.message.reply_text(msg[:4000], parse_mode="HTML", disable_web_page_preview=True)

async def historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/historial 6331</code>", parse_mode="HTML")
        return
    
    num = safe_str(context.args[0])
    reclamos = ws_reclamos.get_all_records()
    historial = [r for r in reclamos if safe_str(r.get("Nº Cliente")) == num]
    
    if not historial:
        await update.message.reply_text(f"❌ Cliente <b>#{num}</b> no tiene reclamos.", parse_mode="HTML")
        return
    
    historial = historial[-10:]
    historial.reverse()
    msg = f"<b>📜 Historial completo — Cliente #{num}</b>\n\n"
    for i, r in enumerate(historial, 1):
        msg += format_reclamo(r, i) + "\n"
    
    if len(msg) > 4000:
        msg = msg[:4000] + "\n\n<i>... mostrando últimos 10</i>"
    await update.message.reply_text(msg, parse_mode="HTML")

async def ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/ubicacion 6331</code>", parse_mode="HTML")
        return
    
    num = safe_str(context.args[0])
    clientes = ws_clientes.get_all_records()
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
    reclamos = ws_reclamos.get_all_records()
    found = [r for r in reclamos if safe_str(r.get("ID Reclamo")) == rid]
    
    if not found:
        await update.message.reply_text(f"❌ Reclamo <code>{rid}</code> no encontrado.", parse_mode="HTML")
        return
    
    r = found[0]
    msg = (
        f"<b>📋 Reclamo {rid}</b>\n\n"
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
    reclamos = ws_reclamos.get_all_records()
    found = [r for r in reclamos if nombre.lower() in safe_str(r.get("Técnico")).lower()]
    
    if not found:
        await update.message.reply_text(f"❌ No hay reclamos asignados a <b>{nombre}</b>.", parse_mode="HTML")
        return
    
    found = found[-5:]
    found.reverse()
    msg = f"<b>👷 Reclamos de {nombre}</b> ({len(found)} mostrados)\n\n"
    for i, r in enumerate(found, 1):
        msg += format_reclamo(r, i) + "\n"
    await update.message.reply_text(msg[:4000], parse_mode="HTML")

async def nombre_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usá: <code>/nombre BENITEZ</code>", parse_mode="HTML")
        return
    
    texto = " ".join(context.args).lower()
    clientes = ws_clientes.get_all_records()
    found = [c for c in clientes if texto in safe_str(c.get("Nombre")).lower()]
    
    if not found:
        await update.message.reply_text(f"❌ No se encontró cliente con <b>{texto}</b>.", parse_mode="HTML")
        return
    
    msg = f"<b>🔍 Resultados ({len(found)}):</b>\n\n"
    for c in found[:5]:
        msg += format_cliente(c) + "\n"
    await update.message.reply_text(msg[:4000], parse_mode="HTML", disable_web_page_preview=True)

async def recientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(context.args[0]) if context.args else 5
    except ValueError:
        n = 5
    n = max(1, min(n, 20))
    
    reclamos = ws_reclamos.get_all_records()
    ultimos = reclamos[-n:]
    ultimos.reverse()
    
    msg = f"<b>📅 Últimos {n} reclamos:</b>\n\n"
    for i, r in enumerate(ultimos, 1):
        msg += format_reclamo(r, i) + "\n"
    await update.message.reply_text(msg[:4000], parse_mode="HTML")

# ==================== MAIN ====================
def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cliente", cliente))
    application.add_handler(CommandHandler("precinto", precinto))
    application.add_handler(CommandHandler("historial", historial))
    application.add_handler(CommandHandler("ubicacion", ubicacion))
    application.add_handler(CommandHandler("reclamo", reclamo_cmd))
    application.add_handler(CommandHandler("tecnico", tecnico))
    application.add_handler(CommandHandler("nombre", nombre_cmd))
    application.add_handler(CommandHandler("recientes", recientes))
    
    # Si estamos en Render, iniciar servidor web en paralelo para health-check
    if os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
        logger.info("🚀 Modo Render detectado. Iniciando servidor de health-check...")
        threading.Thread(target=run_web_server, daemon=True).start()
    
    logger.info("🤖 Bot iniciado. Esperando mensajes...")
    application.run_polling()

if __name__ == "__main__":
    main()