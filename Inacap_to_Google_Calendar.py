


# -*- coding: utf-8 -*-
"""
inacap_horario_to_ics.py
------------------------
- Login ADFS
- Abre Resumen Académico (ahí vive el bloque Horario)
- Extrae semana actual + (weeks-1) siguientes
- Parser desktop y fallback móvil
- Exporta un archivo .ics (iCalendar) con TZ America/Santiago (incluye VTIMEZONE)
- (Opcional) Empuja/actualiza eventos directamente en Google Calendar vía API

Requisitos:
  pip install selenium beautifulsoup4 google-api-python-client google-auth-httplib2 google-auth-oauthlib pytz

Credenciales SIGA (recomendado, variables de entorno):
  # Windows PowerShell
  $env:SIGA_USER="tu_correo@inacapmail.cl"; $env:SIGA_PASS="tu_clave"
  # Linux/Mac (bash)
  export SIGA_USER="tu_correo@inacapmail.cl"; export SIGA_PASS="tu_clave"

Credenciales Google:
  - Coloca 'credentials.json' (OAuth de tipo Escritorio) en el mismo directorio.
  - La primera vez pedirá consentimiento y guardará 'token.pickle'.

Ejemplos:
  # Genera .ics (2 semanas) sin API
  python inacap_horario_to_ics.py --weeks 2 --out inacap_horario.ics --headless

  # Genera .ics y empuja a tu calendario principal
  python inacap_horario_to_ics.py --weeks 2 --push --calendar_id primary --headless
"""

import os
import re
import sys
import time
import argparse
from html import unescape
from datetime import datetime, date, time as dtime
from getpass import getpass
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ====== CONFIG ======
URL_ADFS = ("https://adfs.inacap.cl/adfs/ls/?wtrealm=https://siga.inacap.cl/sts/&wa=wsignin1.0&"
            "wreply=https://siga.inacap.cl/sts/&wctx=https%3a%2f%2fadfs.inacap.cl%2fadfs%2fls%2f%3fwreply%3d"
            "https%3a%2f%2fintranet.inacap.cl%2ftportalvp%2falumnos-intranet%26wtrealm%3dhttps%3a%2f%2fintranet.inacap.cl%2f")

URL_SIGA_RESUMEN = "https://siga.inacap.cl/Inacap.Siga.ResumenAcademico/#/principal"
URL_SIGA_HORARIO = URL_SIGA_RESUMEN  # el horario está embebido aquí

# Selectores ADFS
SEL_ADFS_USER = (By.ID, "userNameInput")
SEL_ADFS_PASS = (By.ID, "passwordInput")

# Selectores bloque Horario (en Resumen)
SEL_SECCION = "#horario-seccion"
SEL_TABLA = "#horario-table"
SEL_RANGO_LABEL = ".card-header label.h3, .card-header label.h3.mb-0.mr-3"
SEL_ICONOS = "#horario-seccion button i.material-icons"  # chevron_left/chevron_right

# Zona horaria ICS y soporte de meses abreviados
TZID = "America/Santiago"
MESES = {"ene":1, "feb":2, "mar":3, "abr":4, "may":5, "jun":6,
         "jul":7, "ago":8, "sep":9, "oct":10, "nov":11, "dic":12}


# ====== UTILIDADES ======
def build_driver(headless=True):
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,1000")
    return webdriver.Chrome(options=opts)

def limpiar_texto(x: str) -> str:
    x = unescape(x or "")
    x = re.sub(r"\s+", " ", x)
    return x.strip()

def parse_fecha_rango(label: str):
    # Ej: "04 - 09 ago. 2025"
    txt = (label or "").strip().lower()
    m = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})\s+([a-zñ.]+)\.?\s+(\d{4})", txt)
    if not m:
        raise ValueError(f"No pude leer el rango de fechas desde: {label}")
    d1, d2, mes_txt, anio = m.groups()
    mes_txt = mes_txt.replace(".", "")
    if mes_txt not in MESES:
        raise ValueError(f"Mes no reconocido: {mes_txt}")
    return int(d1), int(d2), MESES[mes_txt], int(anio)

def hhmm_to_time(hhmm: str) -> dtime:
    h, m = [int(x) for x in hhmm.split(":")]
    return dtime(hour=h, minute=m)

def extraer_eventos_desde_html(html: str):
    """Parser desktop: tabla #horario-table"""
    soup = BeautifulSoup(html, "html.parser")

    # Rango semanal
    etiqueta_el = soup.select_one(SEL_RANGO_LABEL)
    etiqueta = etiqueta_el.get_text(strip=True) if etiqueta_el else ""
    if not etiqueta:
        etiqueta_node = soup.find(string=re.compile(r"\d{2}\s*-\s*\d{2}\s+[a-zñ.]+\s+\d{4}", re.I))
        etiqueta = etiqueta_node.strip() if etiqueta_node else ""
    d1, d2, mes, anio = parse_fecha_rango(etiqueta)

    tabla = soup.select_one(SEL_TABLA)
    if not tabla:
        return []  # deja que el caller intente fallback móvil

    # Encabezados: tomar TODAS las celdas del header excepto la primera (la de horas)
    header_row = tabla.select_one("thead tr")
    if not header_row:
        return []
    header_cells = header_row.find_all(["th", "td"])
    headers_dias = [limpiar_texto(c.get_text()) for c in header_cells[1:]]  # skip la 1ª

    # Mapear a fechas (último número del texto del header)
    fechas = []
    for h in headers_dias:
        m = re.search(r"(\d{1,2})$", h) or re.search(r"\b(\d{1,2})\b", h)
        fechas.append(date(anio, mes, int(m.group(1)))) if m else fechas.append(None)

    eventos = []
    for tr in tabla.select("tbody tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        bloque = limpiar_texto(cells[0].get_text())
        m = re.match(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", bloque)
        if not m:
            continue
        t_ini = hhmm_to_time(m.group(1))
        t_fin = hhmm_to_time(m.group(2))

        # celdas por día
        for idx, td in enumerate(cells[1:]):
            contenido = limpiar_texto(td.get_text(" "))
            if not contenido or contenido.lower().startswith("sin clases"):
                continue

            # Fecha de este día
            f = None
            if idx < len(fechas) and fechas[idx] is not None:
                f = fechas[idx]
            else:
                hh = headers_dias[idx] if idx < len(headers_dias) else ""
                mm = re.search(r"(\d{1,2})$", hh) or re.search(r"\b(\d{1,2})\b", hh)
                if mm:
                    f = date(anio, mes, int(mm.group(1)))
            if f is None:
                continue

            partes = [x.strip() for x in contenido.split(" / ") if x.strip()]
            resumen = partes[0] if partes else contenido
            eventos.append((f, t_ini, t_fin, resumen, contenido))
    return eventos

def extraer_eventos_fallback_movil(html: str):
    """Fallback móvil: lista #scheduleMob con items por día"""
    soup = BeautifulSoup(html, "html.parser")

    etiqueta_el = soup.select_one(SEL_RANGO_LABEL)
    etiqueta = etiqueta_el.get_text(strip=True) if etiqueta_el else ""
    d1, d2, mes, anio = parse_fecha_rango(etiqueta)

    eventos = []
    for art in soup.select("#scheduleMob .schedule article"):
        titulo_el = art.select_one(".schedule-title")
        if not titulo_el:
            continue
        titulo = limpiar_texto(titulo_el.get_text())
        m = re.search(r"(\d{1,2})$", titulo)
        if not m:
            continue
        f = date(anio, mes, int(m.group(1)))

        for item in art.select(".schedule-item-list > *"):
            txt = limpiar_texto(item.get_text(" "))
            if not txt or txt.lower().startswith("sin clases"):
                continue
            m2 = re.match(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s+(.+)", txt)
            if not m2:
                continue
            t_ini = hhmm_to_time(m2.group(1))
            t_fin = hhmm_to_time(m2.group(2))
            contenido = m2.group(3).strip()
            resumen = contenido.split(" / ")[0]
            eventos.append((f, t_ini, t_fin, resumen, contenido))
    return eventos

# ====== GENERACIÓN ICS (con VTIMEZONE ======
def construir_evento(uid_counter, resumen, fecha, t_ini, t_fin, descripcion=""):
    """
    Crea un VEVENT usando TZID=America/Santiago (horas tal como aparecen en SIGA).
    """
    resumen = (resumen or "").replace("\n", " ").strip()
    desc = (descripcion or "").replace("\n", "\\n")
    dtstamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    dtstart = f"TZID={TZID}:{fecha.strftime('%Y%m%d')}T{t_ini.strftime('%H%M%S')}"
    dtend   = f"TZID={TZID}:{fecha.strftime('%Y%m%d')}T{t_fin.strftime('%H%M%S')}"
    uid = f"inacap-{fecha.strftime('%Y%m%d')}-{t_ini.strftime('%H%M')}-{uid_counter}@siga"
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{dtstamp}\r\n"
        f"DTSTART;{dtstart}\r\n"
        f"DTEND;{dtend}\r\n"
        f"SUMMARY:{resumen}\r\n"
        f"DESCRIPTION:{desc}\r\n"
        "END:VEVENT"
    )

def exportar_ics(eventos, salida="inacap_horario.ics", nombre="Horario INACAP"):
    """
    Exporta el calendario incluyendo un bloque VTIMEZONE para America/Santiago.
    Usa CRLF (\r\n) para máxima compatibilidad con Google Calendar.
    """
    vtimezone = (
        "BEGIN:VTIMEZONE\r\n"
        f"TZID:{TZID}\r\n"
        f"X-LIC-LOCATION:{TZID}\r\n"
        "BEGIN:STANDARD\r\n"
        "TZOFFSETFROM:-0300\r\n"
        "TZOFFSETTO:-0400\r\n"
        "TZNAME:-04\r\n"
        "DTSTART:19700426T000000\r\n"
        "RRULE:FREQ=YEARLY;BYMONTH=4;BYDAY=4SU\r\n"
        "END:STANDARD\r\n"
        "BEGIN:DAYLIGHT\r\n"
        "TZOFFSETFROM:-0400\r\n"
        "TZOFFSETTO:-0300\r\n"
        "TZNAME:-03\r\n"
        "DTSTART:19700906T000000\r\n"
        "RRULE:FREQ=YEARLY;BYMONTH=9;BYDAY=1SU\r\n"
        "END:DAYLIGHT\r\n"
        "END:VTIMEZONE\r\n"
    )
    bloques = [construir_evento(i + 1, ev[3], ev[0], ev[1], ev[2], ev[4]) for i, ev in enumerate(eventos)]
# ✅ sin f-string dentro del join
    contenido = (
        "BEGIN:VCALENDAR\r\n"
        "PRODID:-//INACAP->GoogleCalendar//ES\r\n"
        "VERSION:2.0\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:PUBLISH\r\n"
        f"X-WR-CALNAME:{nombre}\r\n"
        "X-WR-TIMEZONE:America/Santiago\r\n"
        f"{vtimezone}"
        + "\r\n".join(bloques) + "\r\n"
        "END:VCALENDAR\r\n"
    )

    with open(salida, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(contenido)
    print(f"Listo: generado {salida} con {len(eventos)} eventos.")

# ====== GOOGLE CALENDAR API (push directo) ======
import hashlib
import pickle
import pytz
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_FILE = "token.pickle"

def get_calendar_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists("credentials.json"):
                raise RuntimeError("Falta credentials.json (OAuth de Google).")
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as token:
            pickle.dump(creds, token)
    return build("calendar", "v3", credentials=creds)

def _event_id_from_uid(uid: str) -> str:
    # Google exige [a-z0-9_-] y tamaño limitado: usamos hash estable
    h = hashlib.sha1(uid.encode("utf-8")).hexdigest()
    return f"inacap-{h}"

def push_to_google_calendar(calendar_id: str, eventos):
    """
    Inserta/actualiza eventos en Google Calendar.
    eventos: lista de tu script -> (fecha, t_ini, t_fin, resumen, descripcion)
    """
    service = get_calendar_service()
    tz = TZID
    tzinfo = pytz.timezone(tz)

    for i, (f, t_ini, t_fin, resumen, descripcion) in enumerate(eventos, start=1):
        start_dt = tzinfo.localize(datetime.combine(f, t_ini))
        end_dt   = tzinfo.localize(datetime.combine(f, t_fin))

        uid = f"inacap-{f.strftime('%Y%m%d')}-{t_ini.strftime('%H%M')}-{i}@siga"
        event_id = _event_id_from_uid(uid)

        body = {
            "id": event_id,   # upsert estable: update si existe, insert si no
            "summary": resumen,
            "description": descripcion,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": tz},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": tz},
        }
        try:
            service.events().update(calendarId=calendar_id, eventId=event_id, body=body).execute()
            print(f"Actualizado: {resumen} ({start_dt})")
        except Exception:
            service.events().insert(calendarId=calendar_id, body=body).execute()
            print(f"Creado: {resumen} ({start_dt})")

# ====== FLUJO SELENIUM ======
def login_adfs_y_ir_a_resumen(driver, user, pwd):
    wait = WebDriverWait(driver, 30)
    driver.get(URL_ADFS)
    usuario_input = wait.until(EC.presence_of_element_located(SEL_ADFS_USER))
    clave_input = wait.until(EC.presence_of_element_located(SEL_ADFS_PASS))
    usuario_input.clear(); usuario_input.send_keys(user)
    clave_input.clear(); clave_input.send_keys(pwd); clave_input.send_keys(Keys.RETURN)
    # Al volver a intranet, saltamos al resumen SIGA
    wait.until(EC.url_contains("intranet.inacap.cl/tportalvp/alumnos-intranet"))
    driver.get(URL_SIGA_RESUMEN)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

def abrir_bloque_horario(driver):
    wait = WebDriverWait(driver, 25)
    driver.get(URL_SIGA_HORARIO)
    # Esperar sección y (al menos) el label del rango
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SEL_SECCION)))
    try:
        seccion = driver.find_element(By.CSS_SELECTOR, SEL_SECCION)
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", seccion)
    except Exception:
        pass
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SEL_RANGO_LABEL)))

def capturar_semana_html(driver):
    time.sleep(0.7)  # margen por render Angular
    return driver.page_source

def mover_semana(driver, direccion="next"):
    wait = WebDriverWait(driver, 10)
    icons = driver.find_elements(By.CSS_SELECTOR, SEL_ICONOS)
    target = None
    for ic in icons:
        txt = (ic.text or "").strip()
        if direccion == "next" and txt == "chevron_right":
            target = ic; break
        if direccion == "prev" and txt == "chevron_left":
            target = ic; break
    if not target:
        raise RuntimeError("No encontré el botón para cambiar de semana en #horario-seccion.")
    target.click()
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SEL_RANGO_LABEL)))
    time.sleep(0.5)

# ====== MAIN ======
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=2, help="Semanas a capturar (1 = solo actual).")
    ap.add_argument("--out", type=str, default="inacap_horario.ics", help="Archivo .ics de salida.")
    ap.add_argument("--headless", action="store_true", help="Chrome en modo headless.")
    ap.add_argument("--dump", action="store_true", help="Guardar dump horario_dump.html para depurar.")
    ap.add_argument("--push", action="store_true", help="Enviar/actualizar eventos a Google Calendar.")
    ap.add_argument("--calendar_id", type=str, default="primary", help="ID de calendario destino (p. ej. 'primary').")
    args = ap.parse_args()

    from getpass import getpass

    # 1) Primero intenta leer de variables de entorno (ideal para GitHub Actions)
    user = os.getenv("SIGA_USER")
    pwd = os.getenv("SIGA_PASS")

    # 2) Si estás local y no definiste variables, pídelo por consola para no hardcodear
    if not user:
        user = input("Usuario SIGA (correo): ").strip()
    if not pwd:
        pwd = getpass("Contraseña SIGA: ").strip()

    if not user or not pwd:
        print("Faltan credenciales. Define SIGA_USER y SIGA_PASS o introdúcelas por consola.")
        sys.exit(1)


    driver = build_driver(headless=args.headless)

    try:
        # 1) Login + abrir bloque horario
        login_adfs_y_ir_a_resumen(driver, user, pwd)
        abrir_bloque_horario(driver)

        # Forzar semana actual con "Hoy" (si está el botón)
        try:
            btn_hoy = driver.find_element(By.XPATH, "//section[@id='horario-seccion']//button[normalize-space()='Hoy']")
            btn_hoy.click()
            time.sleep(0.6)
        except Exception:
            pass

        # 2) Capturar semanas
        acumulados = []
        for i in range(args.weeks):
            html = capturar_semana_html(driver)

            if args.dump and i == 0:
                try:
                    sec = driver.find_element(By.CSS_SELECTOR, SEL_SECCION)
                    with open("horario_dump.html", "w", encoding="utf-8") as f:
                        f.write(sec.get_attribute("outerHTML"))
                    print("Dump guardado: horario_dump.html")
                except Exception:
                    pass

            evs = extraer_eventos_desde_html(html)
            if not evs:
                print("Parser desktop no encontró eventos; probando fallback móvil…")
                evs = extraer_eventos_fallback_movil(html)
            acumulados.extend(evs)

            if i < args.weeks - 1:
                mover_semana(driver, "next")

        # 3) Deduplicar
        key = lambda ev: (ev[0].isoformat(), ev[1].strftime("%H:%M"), ev[2].strftime("%H:%M"), ev[3], ev[4])
        uniq = list({key(ev): ev for ev in acumulados}.values())

        # 4) Exportar ICS (si estamos en Actions, publicar en /public)
        out_path = args.out
        if os.getenv("GITHUB_ACTIONS", "").lower() == "true":
            os.makedirs("public", exist_ok=True)
            # si no pasaste --out, forzamos a public/
            if out_path == "inacap_horario.ics":
                out_path = "public/inacap_horario.ics"

        exportar_ics(uniq, salida=out_path)


        # 5) (Opcional) Empujar a Google Calendar
        if args.push:
            push_to_google_calendar(calendar_id=args.calendar_id, eventos=uniq)
            print(f"Sincronización con Google Calendar completada en '{args.calendar_id}'.")

    except Exception as e:
        print(f"[ERROR] {e}")
        try:
            driver.save_screenshot("error_siga.png")
            print("Guardé captura: error_siga.png")
        except Exception:
            pass
        sys.exit(2)
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
