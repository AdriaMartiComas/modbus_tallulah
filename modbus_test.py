from pymodbus.client import ModbusSerialClient
from datetime import datetime
import time

client = ModbusSerialClient(
    port='/dev/ttyUSB0',
    baudrate=9600,
    timeout=3,
    parity='N',
    stopbits=1,
    bytesize=8
)

if client.connect():
    print("Conectado, enviando petición...")
    while True:
        timestamp = datetime.now().strftime("%H:%M:%S")
        result = client.read_holding_registers(address=0, count=6, device_id=1)
        if result.isError():
            print(f"[{timestamp}] Error: {result}")
        else:
            print(f"[{timestamp}] Registros: {result.registers}")
        time.sleep(3)
