#!/usr/bin/env python3
import json
import asyncio
from datetime import datetime, timezone

from pymodbus.client import ModbusSerialClient
import websockets

MODBUS_PORT = "/dev/ttyUSB0"
SIGNALK_WS_URL = "ws://localhost:3000/signalk/v1/stream"
SEND_INTERVAL_S = 10

client = ModbusSerialClient(
    port=MODBUS_PORT,
    baudrate=9600,
    timeout=3,
    parity="N",
    stopbits=1,
    bytesize=8
)

print("🚀 SALÓN INTERIOR Modbus → Signal K (WebSocket)")
if client.connect():
    print("✅ Conectado Arduino ID1 - Salón Interior (Ctrl+C para salir)")
else:
    print(f"❌ No conecta {MODBUS_PORT}")
    raise SystemExit(1)

def build_delta(temp_c: float, pressure_hpa: float, mq2_raw: int):
    # Signal K recomienda:
    # - temperatura en Kelvin
    # - presión en Pascales
    temp_k = float(temp_c) + 273.15
    pressure_pa = float(pressure_hpa) * 100.0

    return {
        "context": "vessels.self",
        "updates": [{
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": {"label": "ArduinoID1", "type": "Modbus"},
            "values": [
                {"path": "environment.inside.temperature", "value": temp_k},
                {"path": "environment.inside.pressure", "value": pressure_pa},
                {"path": "environment.inside.gas.mq2.raw", "value": int(mq2_raw)},
            ]
        }]
    }

async def main_loop():
    backoff = 1
    while True:
        try:
            async with websockets.connect(SIGNALK_WS_URL) as ws:
                backoff = 1

                # hello (opcional)
                try:
                    hello = await asyncio.wait_for(ws.recv(), timeout=2)
                    print("WS hello:", hello)
                except Exception:
                    pass

                while True:
                    timestamp = datetime.now().strftime("%H:%M:%S")

                    result = client.read_holding_registers(address=0, count=3)
                    if result.isError():
                        print(f"[{timestamp}] Error lectura Modbus")
                        await asyncio.sleep(SEND_INTERVAL_S)
                        continue

                    temp_raw = result.registers[0]
                    press_raw = result.registers[1]
                    gas_raw = result.registers[2]

                    temp_c = temp_raw / 10.0
                    pressure_hpa = float(press_raw)  # según tu comentario: ya viene en hPa
                    mq2_raw = int(gas_raw)

                    # Filtro suave (NO bloqueamos por gas, porque MQ-2 puede ser alto)
                    if temp_c > 100 or pressure_hpa > 1500:
                        print(f"[{timestamp}] Sensores inicializando...")
                        await asyncio.sleep(SEND_INTERVAL_S)
                        continue

                    print(f"[{timestamp}] {temp_c:5.1f}°C | {pressure_hpa:4.0f}hPa | MQ2:{mq2_raw:4d} ✅ Signal K")

                    delta = build_delta(temp_c, pressure_hpa, mq2_raw)

                    # DEBUG (si quieres ver exactamente qué mandas)
                    # print("DEBUG delta:", json.dumps(delta))

                    await ws.send(json.dumps(delta))
                    await asyncio.sleep(SEND_INTERVAL_S)

        except Exception as e:
            print(f"⚠️  WS caído/error: {e!r} (reintentando en {backoff}s)")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

try:
    asyncio.run(main_loop())
except KeyboardInterrupt:
    print("\n🛑 Parando...")
finally:
    client.close()