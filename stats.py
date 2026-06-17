"""
PALMERO - Historiador de Señales (v2: simulación real de SL/TP)
==================================================================
Servicio independiente y de SOLO LECTURA sobre signals_log.json.
No modifica superb-growth ni signals_log.json.

Para cada señal, descarga las velas de 1m reales desde Binance
(desde el momento de la señal hasta ahora) y simula la operativa
exacta de PALMERO: SL -0.5%, TP1 +0.5% (40%), TP2 +0.8% (30%),
resto (30%) con stop en breakeven tras TP2.

Limitación conocida: solo se usan los maximos/minimos de cada vela
(no hay datos tick a tick), así que si en una misma vela se cruzan
dos niveles, se asume el orden mas favorable (toca antes el nivel
de avance que el de retroceso). Ventana de busqueda: hasta ~33h
desde la señal (2 paginas de 1000 velas de 1m); si no se resuelve
en ese plazo, se marca "abierta" y se sigue con el precio mas
reciente disponible.
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

SL = -0.005
TP1 = 0.005
TP2 = 0.008
PESO_TP1 = 0.40
PESO_TP2 = 0.30
PESO_TP3 = 0.30
MAX_PAGINAS = 2

POLL_SECONDS = 600

_lock = threading.Lock()


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
            headers=gh_headers(),
            timeout=10,
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
        body = {
            "message": f"Actualizar resultados {datetime.now(timezone.utc).isoformat()}",
            "content": content,
        }
        if sha:
            body["sha"] = sha
        resp = requests.put(
            f"https://api.github.com/repos/{STATS_REPO}/contents/{STATS_FILE}",
            headers=gh_headers(),
            json=body,
            timeout=10,
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
            params = {
                "symbol": symbol, "interval": "1m",
                "startTime": cursor, "endTime": end_ms, "limit": 1000,
            }
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


def simular_trade(signal):
    entry = float(signal["precio"])
    es_long = "LONG" in signal["tipo"]
    dir_mult = 1 if es_long else -1
    symbol = signal["simbolo"]

    ts = datetime.fromisoformat(signal["timestamp"].replace("Z", "+00:00"))
    start_ms = int(ts.timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    velas = fetch_klines_range(symbol, start_ms, end_ms)
    if not velas:
        return {"estado": "sin_datos", "resultado_pct": None, "velas_analizadas": 0}

    fase = 1
    realizado = 0.0
    estado = None

    for k in velas:
        high = float(k[2])
        low = float(k[3])
        avance_high = dir_mult * (high - entry) / entry
        avance_low = dir_mult * (low - entry) / entry

        if fase == 1:
            if avance_low <= SL:
                estado = "cerrada_sl"
                realizado = SL
                break
            if avance_high >= TP1:
                realizado += PESO_TP1 * TP1
                fase = 2

        if fase == 2:
            if avance_low <= 0:
                estado = "cerrada_be1"
                break
            if avance_high >= TP2:
                realizado += PESO_TP2 * TP2
                fase = 3

        if fase == 3:
            if avance_low <= 0:
                estado = "cerrada_be2"
                break

    if estado is None:
        precio_ultimo = float(velas[-1][4])
        avance_actual = dir_mult * (precio_ultimo - entry) / entry
        if fase == 1:
            estado = "abierta_fase1"
            resultado_pct = round(avance_actual * 100, 3)
        elif fase == 2:
            estado = "abierta_fase2"
            resultado_pct = round((realizado + 0.60 * avance_actual) * 100, 3)
        else:
            estado = "abierta_fase3"
            resultado_pct = round((realizado + PESO_TP3 * avance_actual) * 100, 3)
    else:
        resultado_pct = round(realizado * 100, 3)

    return {"estado": estado, "resultado_pct": resultado_pct, "velas_analizadas": len(velas)}


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

                    sim = simular_trade(s)
                    if sim["resultado_pct"] is None:
                        continue

                    resultados[sid] = {
                        "simbolo": s["simbolo"],
                        "tipo": s["tipo"],
                        "tf": s["tf"],
                        "precio_entrada": float(s["precio"]),
                        "timestamp": s["timestamp"],
                        "estado": sim["estado"],
                        "resultado_pct": sim["resultado_pct"],
                        "velas_analizadas": sim["velas_analizadas"],
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
        fila = {
            "simbolo": simbolo, "tf": tf,
            "n_cerradas": len(cerradas),
            "n_abiertas": len(abiertas),
        }
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


@app.route("/")
def home():
    return jsonify({
        "servicio": "PALMERO - Historiador de señales (v2, simula SL/TP reales)",
        "nota": "Solo lectura sobre signals_log.json. No modifica superb-growth.",
        "endpoints": ["/stats - resumen por simbolo y TF", "/stats/raw - resultados detallados", "/stats/t/<bust> - version sin cache"],
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
