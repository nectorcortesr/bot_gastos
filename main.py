import os
import json
import asyncio
from datetime import date, timedelta
from dotenv import load_dotenv
from imap_tools import MailBox, A
from groq import Groq
from notion_client import Client
from telegram import Bot
from bs4 import BeautifulSoup

# Cargar variables
load_dotenv()

# --- CONFIGURACIÓN ---
REMITENTES_BCI = ["contacto@bci.cl", "transferencias@bci.cl", "notificaciones@bci.cl"]

GROQ_CLIENT = Groq(api_key=os.getenv("GROQ_API_KEY"))
NOTION = Client(auth=os.getenv("NOTION_TOKEN"))
TELEGRAM = Bot(token=os.getenv("TELEGRAM_TOKEN"))
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
# ---------------------

def limpiar_html(html_content):
    if not html_content: return ""
    soup = BeautifulSoup(html_content, 'html.parser')
    for script in soup(["script", "style"]):
        script.extract()
    text = soup.get_text(separator=' ')
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    text = '\n'.join(chunk for chunk in chunks if chunk)
    return text

def analizar_con_ia(texto_limpio, asunto):
    print(f"🧠 IA Analizando texto limpio ({len(texto_limpio)} caracteres)...")
    
    prompt = f"""
    Eres un asistente contable. Extrae datos de este comprobante bancario.
    
    ASUNTO: "{asunto}"
    CONTENIDO DEL CORREO:
    '''{texto_limpio[:2000]}''' 
    
    INSTRUCCIONES:
    1. Si es TRANSFERENCIA:
       - 'comercio': Nombre del DESTINATARIO.
       - 'categoria': "Transferencias".
    2. Si es COMPRA:
       - 'comercio': Nombre del local/tienda.
       - 'categoria': Elige [Comida, Transporte, Vivienda, Ocio, Servicios, Supermercado].
    3. 'monto': SÓLO NÚMEROS (Ej: si dice $10.000 devuelve 10000).
    4. 'fecha': Formato ISO 8601 (YYYY-MM-DD) hoy si no sale.
    
    Responde JSON puro:
    {{
        "comercio": "Nombre",
        "monto": 0,
        "categoria": "Categoria",
        "fecha": "YYYY-MM-DD"
    }}
    """
    
    try:
        chat_completion = GROQ_CLIENT.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            response_format={"type": "json_object"}
        )
        return json.loads(chat_completion.choices[0].message.content)
    except Exception as e:
        print(f"❌ Error IA: {e}")
        return None

def guardar_en_notion(data):
    if not data: return
    print(f"💾 Guardando: {data['comercio']} (${data['monto']})")
    try:
        NOTION.pages.create(
            parent={"database_id": os.getenv("NOTION_DB_ID")},
            properties={
                "Nombre": {"title": [{"text": {"content": data["comercio"]}}]},
                "Monto": {"number": data["monto"]},
                "Categoria": {"select": {"name": data["categoria"]}},
                "Fecha": {"date": {"start": data["fecha"]}},
                "Banco": {"select": {"name": "BCI"}}
            }
        )
        print("✅ ¡Éxito en Notion!")
    except Exception as e:
        print(f"❌ Error Notion: {e}")

async def notificar_telegram(data):
    if not data: return
    mensaje = (
        f"💸 **Gasto Procesado**\n"
        f"📍 {data['comercio']}\n"
        f"💰 ${data['monto']:,}\n"
        f"📂 {data['categoria']}"
    )
    try:
        await TELEGRAM.send_message(chat_id=CHAT_ID, text=mensaje, parse_mode="Markdown")
    except Exception as e:
        print(f"❌ Error Telegram: {e}")

async def main():
    print("🗓️ Iniciando ejecución diaria (Batch)...")
    
    fecha_ayer = date.today() - timedelta(days=1)
    print(f"🔎 Buscando correos desde: {fecha_ayer}")

    with MailBox('imap.gmail.com').login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS")) as mailbox:
        criterios = A(date_gte=fecha_ayer)
        
        for msg in mailbox.fetch(criterios):
            
            if msg.from_ in REMITENTES_BCI:
                print(f"📩 Procesando: {msg.subject} ({msg.date.date()})")
                
                html_raw = msg.html or msg.text
                texto_limpio = limpiar_html(html_raw)
                
                data = analizar_con_ia(texto_limpio, msg.subject)
                if data:
                    guardar_en_notion(data)
                    await notificar_telegram(data)

    print("✅ Ejecución diaria terminada. Apagando.")

if __name__ == "__main__":
    asyncio.run(main())