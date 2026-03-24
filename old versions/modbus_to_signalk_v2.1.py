#!/usr/bin/env python3
import json
import asyncio
import logging
import aiohttp
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from pymodbus.client import ModbusSerialClient
import websockets

MODBUS_PORT      = "/dev/ttyUSB0"
MODBUS_DEVICE_ID = 1
SIGNALK_WS_URL   = "ws://localhost:3443/signalk/v1/stream"
SEND_INTERVAL_S  = 10
LOG_FILE         = "/home/adria.marti/PythonCode/modbus/modbus.log"

# Calibración tanques: RAW 0=vacío, MAX=lleno
TANQUE1_MAX = 111
TANQUE2_MAX = 103

# Telegram
TELEGRAM_TOKEN   = "bot8207416844:AAEoXaMlCYHgllRzAqyqkZ84HK4ootZIqnw"         # ← pon tu token
TELEGRAM_CHAT_ID = "6761385591"

# Umbrales de alerta
TEMP_ALERTA  = 45.0   # °C
GAS_ALERTA   = 500    # RAW

# ── Logging ──────────────────────────────────────────────────────────────────
log = logging.getLogger("modbus_signalk")
log.setLevel(logging.DEBUG)

fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(fmt)

fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3,
                         encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(fmt)

log.addHandler(ch)
log.addHandler(fh)
# ─────────────────────────────────────────────────────────────────────────────

client = ModbusSerialClient(
    port=MODBUS_PORT,
    baudrate=9600,
    timeout=3,
    parity="N",
    stopbits=1,
    bytesize=8
)

log.info("SALON INTERIOR Modbus -> Signal K (WebSocket)")
if client.connect():
    log.info("Conectado Arduino ID1 - Salon Interior")
else:
    log.error(f"No conecta {MODBUS_PORT}")
    raise SystemExit(1)


async def enviar_telegram(mensaje: str):
    """Manda un mensaje al bot de Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    log.info(f"Telegram enviado: {mensaje}")
                else:
                    log.warning(f"Telegram error HTTP {resp.status}")
    except Exception as e:
        log.error(f"Telegram fallo: {e!r}")


def build_delta(temp_c, pressure_hpa, mq2_raw, tanque1_raw, tanque2_raw, sentina):
    temp_k      = float(temp_c) + 273.15
    pressure_pa = float(pressure_hpa) * 100.0
    tanque1_pct = max(0, min(100, tanque1_raw * 100 / TANQUE1_MAX))
    tanque2_pct = max(0, min(100, tanque2_raw * 100 / TANQUE2_MAX))

    return {
        "context": "vessels.self",
        "updates": [{
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": {"label": "ArduinoID1", "type": "Modbus"},
            "values": [
                {"path": "environment.inside.temperature", "value": temp_k},
                {"path": "environment.inside.pressure",    "value": pressure_pa},
                {"path": "environment.inside.gas.mq2.raw","value": int(mq2_raw)},
                {"path": "tanks.water.tank1.level",        "value": tanque1_pct},
                {"path": "tanks.water.tank2.level",        "value": tanque2_pct},
                {"path": "tanks.bilge.level",              "value": bool(sentina)},
            ]
        }]
    }


async def main_loop():
    backoff = 1

    # Estado anterior para detectar cambios (no mandar la misma alerta en bucle)
    alerta_sentina   = False
    alerta_temp      = False
    alerta_gas       = False
    alerta_arduino   = False

    # Aviso de inicio
    await enviar_telegram("✅ <b>Barco Monitor arrancado</b>\nVigilando salón interior...")

    while True:
        try:
            log.debug(f"Conectando WebSocket {SIGNALK_WS_URL}...")
            async with websockets.connect(SIGNALK_WS_URL) as ws:
                backoff = 1
                log.info("WebSocket Signal K conectado")

                # Si estaba caído y vuelve, avisamos
                if alerta_arduino:
                    alerta_arduino = False
                    await enviar_telegram("✅ <b>Arduino reconectado</b>\nComunicación Modbus restaurada.")

                try:
                    hello = await asyncio.wait_for(ws.recv(), timeout=2)
                    log.debug(f"WS hello: {hello}")
                except Exception:
                    pass

                while True:
                    result = client.read_holding_registers(
                        address=0, count=6, device_id=MODBUS_DEVICE_ID
                    )

                    if result.isError():
                        log.warning(f"Error lectura Modbus (device_id={MODBUS_DEVICE_ID}): {result}")

                        # Alerta Arduino desconectado (solo una vez)
                        if not alerta_arduino:
                            alerta_arduino = True
                            await enviar_telegram(
                                "🔌 <b>ALERTA: Arduino desconectado</b>\n"
                                "No hay respuesta Modbus. Revisad la conexión RS485."
                            )

                        await asyncio.sleep(SEND_INTERVAL_S)
                        continue

                    # Arduino responde — reseteamos alerta si estaba activa
                    if alerta_arduino:
                        alerta_arduino = False
                        await enviar_telegram("✅ <b>Arduino reconectado</b>\nComunicación Modbus restaurada.")

                    temp_raw    = result.registers[0]
                    press_raw   = result.registers[1]
                    gas_raw     = result.registers[2]
                    tanque1_raw = result.registers[3]
                    tanque2_raw = result.registers[4]
                    sentina_raw = result.registers[5]

                    temp_c       = temp_raw / 10.0
                    pressure_hpa = float(press_raw)
                    mq2_raw_int  = int(gas_raw)
                    tanque1_pct  = max(0, min(100, tanque1_raw * 100 / TANQUE1_MAX))
                    tanque2_pct  = max(0, min(100, tanque2_raw * 100 / TANQUE2_MAX))
                    sentina      = bool(sentina_raw)

                    if temp_c > 100 or pressure_hpa > 1500:
                        log.warning(f"Sensores inicializando (T={temp_c} P={pressure_hpa})")
                        await asyncio.sleep(SEND_INTERVAL_S)
                        continue

                    # ── ALERTAS ───────────────────────────────────────────────

                    # Sentina
                    if sentina and not alerta_sentina:
                        alerta_sentina = True
                        await enviar_telegram(
                            "🚨 <b>ALERTA: SENTINA CON AGUA</b>\n"
                            "Se ha detectado agua en la sentina. Revisad el barco."
                        )
                    elif not sentina and alerta_sentina:
                        alerta_sentina = False
                        await enviar_telegram("✅ <b>Sentina seca</b>\nYa no hay agua en la sentina.")

                    # Temperatura
                    if temp_c >= TEMP_ALERTA and not alerta_temp:
                        alerta_temp = True
                        await enviar_telegram(
                            f"🌡️ <b>ALERTA: Temperatura alta</b>\n"
                            f"Temperatura en salón: <b>{temp_c:.1f}°C</b>\n"
                            f"Umbral: {TEMP_ALERTA}°C"
                        )
                    elif temp_c < TEMP_ALERTA - 2 and alerta_temp:  # histéresis de 2°C
                        alerta_temp = False
                        await enviar_telegram(f"✅ <b>Temperatura normalizada</b>: {temp_c:.1f}°C")

                    # Gas (ignoramos el valor 999 de inicialización)
                    if mq2_raw_int != 999 and mq2_raw_int >= GAS_ALERTA and not alerta_gas:
                        alerta_gas = True
                        await enviar_telegram(
                            f"💨 <b>ALERTA: Gas detectado</b>\n"
                            f"Nivel RAW: <b>{mq2_raw_int}</b>\n"
                            f"Umbral: {GAS_ALERTA}. Ventilad el barco."
                        )
                    elif mq2_raw_int < GAS_ALERTA - 50 and alerta_gas:  # histéresis de 50
                        alerta_gas = False
                        await enviar_telegram(f"✅ <b>Gas normalizado</b>: RAW {mq2_raw_int}")

                    # ─────────────────────────────────────────────────────────

                    log.info(
                        f"T:{temp_c:5.1f}C | "
                        f"P:{pressure_hpa:4.0f}hPa | "
                        f"MQ:{mq2_raw_int:4d} | "
                        f"T1:{tanque1_pct:5.1f}% | "
                        f"T2:{tanque2_pct:5.1f}% | "
                        f"S:{'AGUA' if sentina else 'SEC'} -> Signal K"
                    )

                    delta = build_delta(temp_c, pressure_hpa, mq2_raw_int,
                                        tanque1_raw, tanque2_raw, sentina_raw)
                    await ws.send(json.dumps(delta))
                    await asyncio.sleep(SEND_INTERVAL_S)

        except Exception as e:
            log.error(f"WS caido/error: {e!r} (reintentando en {backoff}s)")

            # Alerta Arduino/conexión caída
            if not alerta_arduino:
                alerta_arduino = True
                await enviar_telegram(
                    f"🔌 <b>ALERTA: Arduino desconectado</b>\n"
                    f"Error: {e!r}\nReintentando en {backoff}s..."
                )

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


try:
    asyncio.run(main_loop())
except KeyboardInterrupt:
    log.info("Parado por el usuario")
finally:
    client.close()
    log.info("Puerto Modbus cerrado")