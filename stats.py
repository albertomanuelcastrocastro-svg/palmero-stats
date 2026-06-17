"""
PALMERO - Historiador de Señales (v3: + laboratorio de configuraciones)
==========================================================================
Servicio independiente y de SOLO LECTURA sobre signals_log.json.
No modifica superb-growth ni signals_log.json.

Simula la operativa de PALMERO (SL/TP1/TP2/tramo final) usando velas
reales de Binance. Ademas de la configuracion "actual" (la que produccion
usa de verdad), prueba un puñado de configuraciones alternativas sobre
las mismas señales para comparar resultado medio y winrate.

Limitacion conocida: solo se usan maximos/minimos de cada vela (no hay
datos tick a tick); si en una misma vela se cruzan dos niveles, se asume
el orden mas favorable. Ventana de busqueda: hasta ~33h desde la señal.

AVISO: con pocas señales por TF, el laboratorio es orientativo, no una
optimizacion fiable. Sirve para detectar tendencias, no para fijar
parametros definitivos todavia.
"""

import os
import time
import json
import base64
import threading
import requests
from datetime import datetime, timezone

from flask import Flask, jsonify

app = Flask(__name__)

SIGNALS_URL = "https://raw.githubusercontent.com/albertomanuelcastrocastro-svg/palmero-bot-pytho/main/signals_log.json"

STATS_REPO = os.environ.get("STATS_REPO", "albertomanuelcastrocastro-svg/palmero-stats")
STATS_FILE = "resultados.json"
GH_TOKEN = os.environ.get("GITHUB_TOKEN")

BINANCE_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"

MAX_PAGINAS = 2
POLL_SECONDS = 600

CONFIGS = {
    "actual": {
        "sl_pct": -0.005, "tp1_pct": 0.005, "tp1_peso": 0.40,
        "tp2_pct": 0.008, "tp2_peso": 0.30,
        "stop_tras_tp1_pct": 0.0, "stop_tras_tp2_pct": 0.0,
    },
    "margen_suave": {
        "sl_pct": -0.005, "tp1_pct": 0.005, "tp1_peso": 0.40,
        "tp2_pct": 0.008, "tp2_peso": 0.30,
        "stop_tras_tp1_pct": -0.0015, "stop_tras_tp2_pct": -0.0015,
    },
    "margen_amplio": {
        "sl_pct": -0.005, "tp1_pct": 0.005, "tp1_peso": 0.40,
        "tp2_pct": 0.008, "tp2_peso": 0.30,
        "stop_tras_tp1_pct": -0.003, "stop_tras_tp2_pct": -0.003,
    },
    "sl_amplio": {
        "sl_pct": -0.008, "tp1_pct": 0.005, "tp1_peso": 0.40,
        "tp2_pct": 0.008, "tp2_peso": 0.30,
        "stop_tras_tp1_pct": 0.0, "stop_tras_tp2_pct": 0.0,
    },
    "tp_mas_cerca": {
        "sl_pct": -0.005, "tp1_pct": 0.003, "tp1_peso": 0.40,
        "tp2_pct": 0.006, "tp2_peso": 0.30,
        "stop_tras_tp1_pct": 0.0, "stop_tras_tp2_pct": 0.0,
    },
}

_lock = threading.Lock()
_velas_cache = {}


def gh_headers():
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "User-Agent": "palmero-historiador",
        "Accept": "application/vnd.github+json",
    }


def load_signals():
    try:
        resp = requests.get(SIGNALS_URL, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("Error leyendo signals_log:", e)
        return []


def load_resultados():
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{STATS_REPO}/contents/{STATS_FILE}",
            headers=gh_headers(), timeout=10,
        )
        if resp.status_code == 404:
            return {}, None
        resp.raise_for_status()
        j = resp.json()
        decoded = base64.b64decode(j["content"]).decode("utf-8")
        return json.loads(decoded), j["sha"]
    except Exception as e:
        print("Error leyendo resultados:", e)
        return {}, None


def save_resultados(data, sha):
    try:
        content = base64.b64encode(json.dumps(data, indent=2).encode("utf-8")).decode("utf-8")
        body = {"message": f"Actualizar resultados {datetime.now(timezone.utc).isoformat()}", "content": content}
        if sha:
            body["sha"] = sha
        resp = requests.put(
            f"https://api.github.com/repos/{STATS_REPO}/contents/{STATS_FILE}",
            headers=gh_headers(), json=body, timeout=10,
        )
        if not resp.ok:
            print("Error guardando resultados:", resp.text)
    except Exception as e:
        print("Error guardando resultados:", e)


def fetch_klines_range(symbol, start_ms, end_ms):
    velas = []
    cursor = start_ms
    for _ in range(MAX_PAGINAS):
        if cursor >= end_ms:
            break
        try:
            params = {"symbol": symbol, "interval": "1m", "startTime": cursor, "endTime": end_ms, "limit": 1000}
            resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:
            print("Error klines:", symbol, e)
            break
        if not raw:
            break
        velas.extend(raw)
        cursor = raw[-1][6] + 1
        if len(raw) < 1000:
            break
    return velas


def obtener_velas(signal):
    sid = str(signal["id"])
    if sid in _velas_cache:
        return _velas_cache[sid]
    ts = datetime.fromisoformat(signal["timestamp"].replace("Z", "+00:00"))
    start_ms = int(ts.timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    velas = fetch_klines_range(signal["simbolo"], start_ms, end_ms)
    _velas_cache[sid] = velas
    return velas


def simular_trade_config(signal, velas, cfg):
    if not velas:
        return None
    entry = float(signal["precio"])
    es_long = "LONG" in signal["tipo"]
    dir_mult = 1 if es_long else -1

    fase = 1
    realizado = 0.0
    estado = None

    for k in velas:
        high = float(k[2])
        low = float(k[3])
        avance_high = dir_mult * (high - entry) / entry
        avance_low = dir_mult * (low - entry) / entry

        if fase == 1:
            if avance_low <= cfg["sl_pct"]:
                estado = "cerrada_sl"
                realizado = cfg["sl_pct"]
                break
            if avance_high >= cfg["tp1_pct"]:
                realizado += cfg["tp1_peso"] * cfg["tp1_pct"]
                fase = 2

        if fase == 2:
            if avance_low <= cfg["stop_tras_tp1_pct"]:
                peso_resto = 1 - cfg["tp1_peso"]
                realizado += peso_resto * cfg["stop_tras_tp1_pct"]
                estado = "cerrada_be1"
                break
            if avance_high >= cfg["tp2_pct"]:
                realizado += cfg["tp2_peso"] * cfg["tp2_pct"]
                fase = 3

        if fase == 3:
            if avance_low <= cfg["stop_tras_tp2_pct"]:
                peso_resto = 1 - cfg["tp1_peso"] - cfg["tp2_peso"]
                realizado += peso_resto * cfg["stop_tras_tp2_pct"]
                estado = "cerrada_be2"
                break

    if estado is None:
        precio_ultimo = float(velas[-1][4])
        avance_actual = dir_mult * (precio_ultimo - entry) / entry
        if fase == 1:
            estado = "abierta_fase1"
            resultado = avance_actual
        elif fase == 2:
            peso_resto = 1 - cfg["tp1_peso"]
            resultado = realizado + peso_resto * avance_actual
            estado = "abierta_fase2"
        else:
            peso_resto = 1 - cfg["tp1_peso"] - cfg["tp2_peso"]
            resultado = realizado + peso_resto * avance_actual
            estado = "abierta_fase3"
    else:
        resultado = realizado

    return {"estado": estado, "resultado_pct": round(resultado * 100, 3)}


def procesar():
    while True:
        try:
            with _lock:
                signals = load_signals()
                resultados, sha = load_resultados()
                cambiado = False
                ahora = datetime.now(timezone.utc).isoformat()

                for s in signals:
                    sid = str(s["id"])
                    existente = resultados.get(sid)
                    if existente and existente.get("estado", "").startswith("cerrada"):
                        continue

                    velas = obtener_velas(s)
                    sim = simular_trade_config(s, velas, CONFIGS["actual"])
                    if not sim:
                        continue

                    resultados[sid] = {
                        "simbolo": s["simbolo"], "tipo": s["tipo"], "tf": s["tf"],
                        "precio_entrada": float(s["precio"]), "timestamp": s["timestamp"],
                        "estado": sim["estado"], "resultado_pct": sim["resultado_pct"],
                        "actualizado_utc": ahora,
                    }
                    cambiado = True

                if cambiado:
                    save_resultados(resultados, sha)
                    print(f"[{ahora}] Resultados actualizados")

        except Exception as e:
            print("Error en bucle de procesamiento:", e)

        time.sleep(POLL_SECONDS)


_hilo_iniciado = False


def iniciar_hilo():
    global _hilo_iniciado
    if not _hilo_iniciado:
        _hilo_iniciado = True
        t = threading.Thread(target=procesar, daemon=True)
        t.start()


iniciar_hilo()


def calcular_resumen():
    resultados, _ = load_resultados()
    grupos = {}
    for sid, r in resultados.items():
        clave = (r["simbolo"], r["tf"])
        grupos.setdefault(clave, {"cerradas": [], "abiertas": []})
        if r["estado"].startswith("cerrada"):
            grupos[clave]["cerradas"].append(r["resultado_pct"])
        elif r["estado"].startswith("abierta"):
            grupos[clave]["abiertas"].append(r["resultado_pct"])

    salida = []
    for (simbolo, tf), v in grupos.items():
        cerradas = v["cerradas"]
        abiertas = v["abiertas"]
        fila = {"simbolo": simbolo, "tf": tf, "n_cerradas": len(cerradas), "n_abiertas": len(abiertas)}
        if cerradas:
            fila["resultado_promedio_pct"] = round(sum(cerradas) / len(cerradas), 3)
            fila["winrate_pct"] = round(sum(1 for x in cerradas if x > 0) / len(cerradas) * 100, 1)
        else:
            fila["resultado_promedio_pct"] = None
            fila["winrate_pct"] = None
        if abiertas:
            fila["flotante_promedio_pct"] = round(sum(abiertas) / len(abiertas), 3)
        salida.append(fila)

    salida.sort(key=lambda f: (f["simbolo"], f["tf"]))
    return salida


def calcular_laboratorio():
    signals = load_signals()
    salida = []
    for nombre, cfg in CONFIGS.items():
        valores = []
        for s in signals:
            velas = obtener_velas(s)
            r = simular_trade_config(s, velas, cfg)
            if r:
                valores.append(r["resultado_pct"])
        if valores:
            fila = {
                "config": nombre, "n": len(valores),
                "resultado_promedio_pct": round(sum(valores) / len(valores), 3),
                "winrate_pct": round(sum(1 for x in valores if x > 0) / len(valores) * 100, 1),
            }
        else:
            fila = {"config": nombre, "n": 0, "resultado_promedio_pct": None, "winrate_pct": None}
        salida.append(fila)
    return salida


@app.route("/")
def home():
    return jsonify({
        "servicio": "PALMERO - Historiador de señales (v3, + laboratorio)",
        "nota": "Solo lectura sobre signals_log.json. No modifica superb-growth.",
        "endpoints": [
            "/stats - resumen real (config actual) por simbolo y TF",
            "/stats/raw - resultados detallados",
            "/stats/t/<bust> - version sin cache",
            "/laboratorio - compara configuraciones SL/TP alternativas",
            "/laboratorio/t/<bust> - version sin cache",
        ],
    })


@app.route("/stats")
def stats():
    return jsonify({"timestamp_utc": datetime.now(timezone.utc).isoformat(), "resumen": calcular_resumen()})


@app.route("/stats/raw")
def stats_raw():
    resultados, _ = load_resultados()
    return jsonify(resultados)


@app.route("/stats/t/<bust>")
def stats_nocache(bust):
    return stats()


@app.route("/laboratorio")
def laboratorio():
    return jsonify({"timestamp_utc": datetime.now(timezone.utc).isoformat(), "comparacion": calcular_laboratorio()})


@app.route("/laboratorio/t/<bust>")
def laboratorio_nocache(bust):
    return laboratorio()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
