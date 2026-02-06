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

# --- 1. CONFIGURACIÓN E INICIO ---
load_dotenv()

# Configuración de Remitentes Seguros
try:
    if os.getenv("REMITENTES_PERMITIDOS"):
        REMITENTES_BCI = json.loads(os.getenv("REMITENTES_PERMITIDOS"))
    else:
        # Default por si no está en el .env
        REMITENTES_BCI = ["contacto@bci.cl", "transferencias@bci.cl", "notificaciones@bci.cl"]
except:
    REMITENTES_BCI = ["contacto@bci.cl", "transferencias@bci.cl", "notificaciones@bci.cl"]

# Inicialización de Clientes
GROQ_CLIENT = Groq(api_key=os.getenv("GROQ_API_KEY"))
NOTION = Client(auth=os.getenv("NOTION_TOKEN"))
TELEGRAM = Bot(token=os.getenv("TELEGRAM_TOKEN"))

# IDs y Variables
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NOTION_BUDGET_ID = os.getenv("NOTION_BUDGET_DB_ID") # ID Base de Datos Presupuestos
NOTION_DB_ID = os.getenv("NOTION_DB_ID")             # ID Base de Datos Transacciones

# --- 2. FUNCIONES DE LIMPIEZA Y ANÁLISIS ---

def limpiar_html(html_content):
    """Extrae texto limpio de los correos HTML del banco"""
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
    """Usa Llama 3 para categorizar Gasto vs Ingreso con reglas chilenas"""
    print(f"🧠 IA Analizando ({len(texto_limpio)} caracteres)...")
    
    # Lista de categorías que debe coincidir con tu Notion
    categorias_validas = [
        "Comida", "Transporte", "Vivienda", "Ocio", 
        "Ropa", "Supermercado", "Salud", 
        "Servicios", "Transferencias", "Hogar", "Otros",
        "Ingreso" # <--- Categoría clave para el sueldo
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

# --- 3. FUNCIONES DE NOTION ---

def guardar_en_notion(data):
    """Guarda tanto Ingresos como Gastos en la tabla Transacciones"""
    if not data or data.get('monto', 0) == 0: return
    print(f"💾 Guardando: {data['comercio']} (${data['monto']})")
    try:
        NOTION.pages.create(
            parent={"database_id": NOTION_DB_ID},
            properties={
                "Nombre": {"title": [{"text": {"content": data["comercio"]}}]},
                "Monto": {"number": data["monto"]},
                "Categoria": {"select": {"name": data["categoria"]}},
                "Fecha": {"date": {"start": data["fecha"]}},
                "Banco": {"select": {"name": "BCI"}}
            }
        )
        print("✅ ¡Éxito en Notion (Transacciones)!")
    except Exception as e:
        print(f"❌ Error Notion Guardar: {e}")

def resetear_ciclo_presupuestario():
    """Se ejecuta al detectar el SUELDO. Pone 'Gastado' en 0 en toda la tabla Presupuestos."""
    print("🔄 DETECTADO SUELDO: Reiniciando contadores de presupuesto...")
    
    try:
        if not NOTION_BUDGET_ID:
            print("⚠️ Faltan configurar NOTION_BUDGET_DB_ID en .env")
            return False

        # 1. Obtener todas las filas de presupuestos
        pages = NOTION.databases.query(database_id=NOTION_BUDGET_ID)["results"]
        
        if not pages:
            print("⚠️ La tabla de presupuestos está vacía o no se puede leer.")
            return False

        # 2. Iterar y poner Gastado en 0
        for page in pages:
            NOTION.pages.update(
                page_id=page["id"],
                properties={
                    "Gastado": {"number": 0}
                }
            )
        print("✅ Ciclo reiniciado correctamente.")
        return True
    except Exception as e:
        print(f"❌ Error reiniciando ciclo: {e}")
        return False

def actualizar_presupuesto(categoria_gasto, monto_gasto):
    """Busca la categoría en Presupuestos y suma el gasto."""
    print(f"💰 Calculando presupuesto para: {categoria_gasto}...")
    
    try:
        if not NOTION_BUDGET_ID: return None

        # Buscar la fila del presupuesto
        response = NOTION.databases.query(
            database_id=NOTION_BUDGET_ID,
            filter={
                "property": "Categoria",
                "title": {"equals": categoria_gasto}
            }
        )
        
        if not response["results"]:
            print(f"ℹ️ Sin presupuesto definido para {categoria_gasto}")
            return None

        page = response["results"][0]
        page_id = page["id"]
        props = page["properties"]
        
        # Leer valores (con seguridad por si están vacíos)
        limite = props.get("Monto Limite", {}).get("number") or 0
        gastado_actual = props.get("Gastado", {}).get("number") or 0
        
        nuevo_gastado = gastado_actual + monto_gasto
        restante = limite - nuevo_gastado
        
        # Actualizar Notion
        NOTION.pages.update(
            page_id=page_id,
            properties={"Gastado": {"number": nuevo_gastado}}
        )
        
        return {"limite": limite, "restante": restante}
    except Exception as e:
        print(f"❌ Error actualizando presupuesto: {e}")
        return None

# --- 4. NOTIFICACIONES ---

async def notificar_telegram(data, info_presupuesto=None, es_sueldo=False):
    # CASO 1: LLEGÓ EL SUELDO
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

    # CASO 2: GASTO NORMAL
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
    print("🗓️ Iniciando ejecución diaria...")
    fecha_ayer = date.today() - timedelta(days=1)

    with MailBox('imap.gmail.com').login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS")) as mailbox:
        # Buscamos correos desde ayer
        for msg in mailbox.fetch(A(date_gte=fecha_ayer)):
            
            if msg.from_ in REMITENTES_BCI:
                print(f"📩 Procesando: {msg.subject}")

                html_raw = msg.html or msg.text
                texto_limpio = limpiar_html(html_raw)
                
                # 1. La IA decide qué es (Gasto o Ingreso)
                data = analizar_con_ia(texto_limpio, msg.subject)
                
                if data:
                    # --- LÓGICA DE SUELDO ---
                    if data['categoria'] == "Ingreso":
                        print(f"🤑 DETECTADO INGRESO: {data['monto']}")
                        
                        # Guardamos el ingreso en Notion (Historial)
                        guardar_en_notion(data)
                        
                        # Si es un monto grande (> 500k) O viene de Assetplan -> RESET
                        if data['monto'] > 500000 or "ASSETPLAN" in data['comercio'].upper():
                            exito = resetear_ciclo_presupuestario()
                            if exito:
                                await notificar_telegram(data, es_sueldo=True)
                        else:
                            # Ingreso menor, solo avisar
                            await TELEGRAM.send_message(chat_id=CHAT_ID, text=f"💰 Ingreso extra: ${data['monto']:,} de {data['comercio']}")
                            
                    # --- LÓGICA DE GASTO ---
                    else:
                        guardar_en_notion(data)
                        # Calcular Saldo Restante
                        info_presu = actualizar_presupuesto(data['categoria'], data['monto'])
                        # Avisar con semáforo
                        await notificar_telegram(data, info_presupuesto=info_presu)

    print("✅ Fin del proceso.")

if __name__ == "__main__":
    asyncio.run(main())