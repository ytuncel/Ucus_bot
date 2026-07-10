import os
import requests
from flask import Flask, jsonify, render_template
from flask_cors import CORS  # CORS kütüphanesini dahil ediyoruz
from dotenv import load_dotenv

load_dotenv()

# index.html dosyanız main.py ile aynı klasördeyse template_folder="." kalabilir
app = Flask(__name__, template_folder=".") 

# [KRİTİK GÜNCELLEME]: Tarayıcıların ve Telegram'ın sunucuya bağlanmasına izin veriyoruz
CORS(app) 

AIRLABS_KEY = os.getenv("AIRLABS_API_KEY")
BBOX = "25.6,35.8,44.8,42.2"

def get_combined_flights():
    combined_data = {}
    if not AIRLABS_KEY:
        print("HATA: AIRLABS_API_KEY bulunamadı! .env veya Render ayarlarını kontrol edin.")
        return []
        
    try:
        airlabs_url = f"https://airlabs.co/api/v9/flights?api_key={AIRLABS_KEY}&_bbox={BBOX}"
        response = requests.get(airlabs_url, timeout=10)
        if response.status_code == 200:
            flights = response.json().get("response", [])
            for f in flights:
                hex_code = f.get("hex", "").lower()
                if not hex_code:
                    continue
                combined_data[hex_code] = {
                    "hex": hex_code,
                    "flight_icao": f.get("flight_icao"),
                    "flight_iata": f.get("flight_iata"),
                    "aircraft_icao": f.get("aircraft_icao"),
                    "alt": f.get("alt", 0),
                    "speed": f.get("speed", 0),
                    "lat": f.get("lat"),
                    "lng": f.get("lng"),
                    "dep_iata": f.get("dep_iata", ""),
                    "arr_iata": f.get("arr_iata", "")
                }
    except Exception as e:
        print(f"AirLabs veri çekme hatası: {e}")
    return list(combined_data.values())

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/flights')
def api_flights():
    flights = get_combined_flights()
    return jsonify(flights)

if __name__ == '__main__':
    # Render canlı ortamda portu otomatik belirler, yerelde 5000 portunu kullanır
    port = int(os.environ.get("PORT", 5000))
    # [ÇOK ÖNEMLİ]: host='0.0.0.0' Render'ın dış dünyaya açılması için şarttır
    app.run(host='0.0.0.0', port=port, debug=False)
