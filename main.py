import os
import json
import asyncio
import requests # <--- NUEVA LIBRERÍA (ESTÁNDAR)
from datetime import date, timedelta
from dotenv import load_dotenv
from imap_tools import MailBox, A
from groq import Groq
from telegram import Bot
from bs4 import BeautifulSoup

# --- 1. CONFIGURACIÓN E INICIO ---
load_dotenv()

# Configuración de Remitentes Seguros
try:
    if os.getenv("REMITENTES_PERMITIDOS"):
        REMITENTES_BCI = json.loads(os.getenv("REMITENTES_PERMITIDOS"))
    else:
        REMITENTES_BCI = ["contacto@bci.cl", "transferencias@bci.cl", "notificaciones@bci.cl"]
except:
    REMITENTES_BCI = ["contacto@bci.cl", "transferencias@bci.cl", "notificaciones@bci.cl"]

# Inicialización de Clientes
GROQ_CLIENT = Groq(api_key=os.getenv("GROQ_API_KEY"))
TELEGRAM = Bot(token=os.getenv("TELEGRAM_TOKEN"))

# IDs y Variables
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NOTION_BUDGET_ID = os.getenv("NOTION_BUDGET_DB_ID") 
NOTION_DB_ID = os.getenv("NOTION_DB_ID") 
NOTION_TOKEN = os.getenv("NOTION_TOKEN")

# --- FUNCIONES AUXILIARES NOTION (SIN LIBRERÍA ROTA) ---

def notion_api_request(endpoint, method="POST", payload=None):
    """Función maestra para hablar con Notion sin usar la librería cliente"""
    url = f"https://api.notion.com/v1/{endpoint}"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    try:
        if method == "POST":
            response = requests.post(url, headers=headers, json=payload)
        else:
            response = requests.get(url, headers=headers)
            
        # Si hay error (400, 404, 401), lanzamos excepción para ver el mensaje
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as err:
        print(f"❌ Error HTTP Notion: {err}")
        print(f"📩 Respuesta Notion: {response.text}") # <--- AQUÍ VEREMOS EL ERROR REAL
        return None

# --- 2. FUNCIONES DE LIMPIEZA Y ANÁLISIS ---

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
    print(f"🧠 IA Analizando ({len(texto_limpio)} caracteres)...")
    
    categorias_validas = [
        "Comida", "Transporte", "Vivienda", "Ocio", 
        "Ropa", "Supermercado", "Salud", 
        "Servicios", "Transferencias", "Hogar", "Otros",
        "Ingreso"
    ]

    prompt = f"""
    Eres un contador personal experto en Chile. Analiza este correo del banco BCI.
    
    ASUNTO: "{asunto}"
    TEXTO: '''{texto_limpio[:3000]}''' 
    
    REGLAS DE CLASIFICACIÓN (PRIORIDAD ALTA):
    1. INGRESO / SUELDO (CRÍTICO):
       - Si el texto menciona "ASSETPLAN", "Remuneraciones", "Sueldo", o "Abono" de un empleador -> Clasifica como "Ingreso".
       - Fíjate en el "Monto transferido".
    2. Ropa / Retail:
       - H&M, ZARA, FALABELLA, PARIS, RIPLEY, BUBBLE GUMMERS, LINDA SARAH -> Clasifica como "Ropa".
    3. Bencineras / Auto:
       - COPEC, SHELL, PETROBRAS, ENEX, JIS PARKING -> Clasifica como "Transporte".
    4. Supermercado:
       - LIDER, UNIMARC, JUMBO, TUU, OXXO, OK MARKET -> Clasifica como "Supermercado".
    5. Hogar / Construcción:
       - SODIMAC, EASY, CONSTRUMART -> Clasifica como "Hogar".
    6. Salud:
       - INTEGRAMEDICA, REDSALUD, CLINICA, CRUZ VERDE, AHUMADA -> Clasifica como "Salud".
    7. Apps:
       - UBER (viaje), CABIFY -> "Transporte".
       - UBER EATS, RAPPI -> "Comida".

    INSTRUCCIONES DE EXTRACCIÓN:
    - 'comercio': 
        * Si es Ingreso: Pon el nombre del origen (ej: "ASSETPLAN" o "Mi Empresa").
        * Si es Gasto: Nombre del local.
    - 'monto': Entero sin puntos (Ej: 2277019).
    - 'categoria': Estrictamente de la lista: {categorias_validas}.
    
    Responde JSON puro:
    {{
        "comercio": "string",
        "monto": 0,
        "categoria": "string",
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

# --- 3. FUNCIONES DE NOTION (VERSIÓN REQUESTS) ---

def guardar_en_notion(data):
    if not data or data.get('monto', 0) == 0: return
    print(f"💾 Guardando: {data['comercio']} (${data['monto']})")
    
    payload = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": {
            "Nombre": {"title": [{"text": {"content": data["comercio"]}}]},
            "Monto": {"number": data["monto"]},
            "Categoria": {"select": {"name": data["categoria"]}},
            "Fecha": {"date": {"start": data["fecha"]}},
            "Banco": {"select": {"name": "BCI"}}
        }
    }
    
    res = notion_api_request("pages", "POST", payload)
    if res:
        print("✅ ¡Éxito en Notion (Transacciones)!")

def resetear_ciclo_presupuestario():
    print("🔄 DETECTADO SUELDO: Reiniciando contadores de presupuesto...")
    if not NOTION_BUDGET_ID: return False

    # 1. Buscar todas las páginas
    res = notion_api_request(f"databases/{NOTION_BUDGET_ID}/query", "POST", {})
    if not res: return False
    
    pages = res.get("results", [])
    for page in pages:
        # 2. Update a 0
        notion_api_request(f"pages/{page['id']}", "PATCH", {
            "properties": {"Gastado": {"number": 0}}
        })
        
    print("✅ Ciclo reiniciado correctamente.")
    return True

def actualizar_presupuesto(categoria_gasto, monto_gasto):
    print(f"💰 Calculando presupuesto para: {categoria_gasto}...")
    
    if not NOTION_BUDGET_ID: 
        print("⚠️ No hay ID de presupuesto configurado")
        return None

    # 1. Buscar categoría
    payload = {
        "filter": {
            "property": "Categoría", # <--- CON TILDE, COMO TU FOTO
            "title": {"equals": categoria_gasto}
        }
    }
    
    res = notion_api_request(f"databases/{NOTION_BUDGET_ID}/query", "POST", payload)
    
    # DEBUG EXTREMO: Si falla, sabremos por qué
    if not res:
        print("❌ Error consultando la base de datos de Presupuestos.")
        return None
        
    results = res.get("results", [])
    
    if not results:
        print(f"⚠️ La consulta funcionó (200 OK) pero NO encontró la categoría '{categoria_gasto}'.")
        print("👉 Posible causa: ¿La categoría en 'Transacciones' está escrita IDÉNTICA en 'Presupuestos'?")
        print("👉 Revisa tildes, mayúsculas y espacios.")
        return None

    page = results[0]
    props = page["properties"]
    
    # Leer valores
    try:
        limite = props.get("Monto Limite", {}).get("number") or 0
        gastado_actual = props.get("Gastado", {}).get("number") or 0
    except KeyError:
        print(f"❌ Error leyendo columnas. Tus columnas son: {list(props.keys())}")
        print("👉 Asegúrate que se llamen 'Monto Limite' y 'Gastado' (tipo Number)")
        return None
    
    nuevo_gastado = gastado_actual + monto_gasto
    restante = limite - nuevo_gastado
    
    # 2. Actualizar
    update_payload = {
        "properties": {"Gastado": {"number": nuevo_gastado}}
    }
    update_res = notion_api_request(f"pages/{page['id']}", "PATCH", update_payload)
    
    if update_res:
        print(f"✅ Presupuesto actualizado. Restante: ${restante}")
        return {"limite": limite, "restante": restante}
    
    return None

# --- 4. NOTIFICACIONES ---

async def notificar_telegram(data, info_presupuesto=None, es_sueldo=False):
    if es_sueldo:
        mensaje = (
            f"💰 **¡LLEGÓ EL SUELDO!** 💰\n"
            f"🏢 Origen: {data['comercio']}\n"
            f"💵 Monto: ${data['monto']:,}\n\n"
            f"🔄 **Ciclo Reiniciado:** He puesto todos los contadores de 'Gastado' a 0.\n"
            f"¡El nuevo mes financiero comienza hoy! 🚀"
        )
        await TELEGRAM.send_message(chat_id=CHAT_ID, text=mensaje, parse_mode="Markdown")
        return

    if not data: return
    
    mensaje = (
        f"💸 **Gasto Detectado**\n"
        f"📍 {data['comercio']}\n"
        f"💰 ${data['monto']:,}\n"
        f"📂 {data['categoria']}\n"
    )
    
    if info_presupuesto:
        icono = "🟢"
        restante = info_presupuesto['restante']
        limite = info_presupuesto['limite']

        if limite > 0:
            pct_restante = restante / limite
            if restante < 0:
                icono = "🔴 **EXCEDIDO**"
            elif pct_restante < 0.2:
                icono = "🟠 **Queda poco**"
            
        mensaje += (
            f"-------------------\n"
            f"📊 **Presupuesto {data['categoria']}**\n"
            f"Restante: ${restante:,} {icono}"
        )

    try:
        await TELEGRAM.send_message(chat_id=CHAT_ID, text=mensaje, parse_mode="Markdown")
    except Exception as e:
        print(f"❌ Error Telegram: {e}")

# --- 5. BUCLE PRINCIPAL ---

async def main():
    print("🗓️ Iniciando ejecución por estado (Solo No Leídos)...")

    with MailBox('imap.gmail.com').login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS")) as mailbox:
        
        for msg in mailbox.fetch(A(seen=False)): 
            
            if msg.from_ in REMITENTES_BCI:
                print(f"📩 Nuevo correo detectado: {msg.subject}")

                html_raw = msg.html or msg.text
                texto_limpio = limpiar_html(html_raw)
                
                data = analizar_con_ia(texto_limpio, msg.subject)
                
                if data:
                    if data['categoria'] == "Ingreso":
                        guardar_en_notion(data)
                        if data['monto'] > 500000 or "ASSETPLAN" in data['comercio'].upper():
                            exito = resetear_ciclo_presupuestario()
                            if exito:
                                await notificar_telegram(data, es_sueldo=True)
                    else:
                        guardar_en_notion(data)
                        info_presu = actualizar_presupuesto(data['categoria'], data['monto'])
                        await notificar_telegram(data, info_presupuesto=info_presu)

    print("✅ Fin del proceso. Los correos procesados ahora están marcados como leídos.")

if __name__ == "__main__":
    asyncio.run(main())