import os
import requests
from dotenv import load_dotenv

load_dotenv()

AIRLABS_KEY = os.getenv("AIRLABS_API_KEY")
# İleride 2. veya 3. API'leri entegre etmek için hazır altyapı
AVIATION_EDGE_KEY = os.getenv("AVIATION_EDGE_KEY") 

BBOX = "25.6,35.8,44.8,42.2"

def get_combined_flights():
    combined_data = {}

    # 1. KAYNAK: AirLabs Verisi Çekiliyor
    try:
        airlabs_url = f"https://airlabs.co/api/v9/flights?api_key={AIRLABS_KEY}&_bbox={BBOX}"
        response = requests.get(airlabs_url, timeout=10)
        if response.status_code == 200:
            flights = response.json().get("response", [])
            for f in flights:
                hex_code = f.get("hex", "").lower()
                if not hex_code:
                    continue
                
                # Standart veri yapımız
                combined_data[hex_code] = {
                    "hex": hex_code,
                    "callsign": (f.get("flight_icao") or f.get("flight_iata") or "").upper(),
                    "aircraft_icao": (f.get("aircraft_icao") or "").upper(),
                    "alt": f.get("alt", 0),
                    "speed": f.get("speed", 0),
                    "lat": f.get("lat"),
                    "lng": f.get("lng"),
                    "dep_iata": f.get("dep_iata", ""),
                    "arr_iata": f.get("arr_iata", ""),
                    "source": "AirLabs"
                }
    except Exception as e:
        print(f"AirLabs hatası: {e}")

    # 2. KAYNAK: Örn. Aviation Edge veya ADS-B Hub (Gelecekte aktif edilecek alan)
    # Bu alanda ikinci API'den gelen veri döngüye sokulup eğer 'hex_code' 
    # combined_data içinde zaten varsa, eksik alanları (örn: boş gelen model adını) dolduracak.
    
    if AVIATION_EDGE_KEY:
        try:
            # Örnek entegrasyon mantığı:
            # ae_url = f"https://aviation-edge.com/v2/public/flights?key={AVIATION_EDGE_KEY}"
            # ... veriler çekilir ...
            # if hex_code in combined_data: 
            #     if not combined_data[hex_code]["aircraft_icao"]: combined_data[hex_code]["aircraft_icao"] = yeni_model
            pass
        except Exception as e:
            print(f"İkinci API hatası: {e}")

    return list(combined_data.values())
