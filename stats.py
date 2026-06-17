"""
PALMERO - Historiador de Señales
==================================
Servicio independiente y de SOLO LECTURA sobre signals_log.json.
No modifica superb-growth ni signals_log.json. Mide automáticamente
qué pasó con el precio después de cada señal (a 30min, 2h y 24h) y
guarda el resultado en su propio archivo, en su propio repo.

Objetivo: comparar rendimiento real entre TFs (TF1, TF3, TF5, TF15)
sin depender de revisión manual.

Despliegue: Railway, servicio nuevo e independiente.
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

BINANCE_PRICE_URL = "https://data-api.binance.vision/api/v3/ticker/price"

CHECKPOINTS = {
    "r_30m": 30 * 60,
    "r_2h": 2 * 60 * 60,
    "r_24h": 24 * 60 * 60,
}

POLL_SECONDS = 180

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


def get_price(symbol):
    try:
        resp = requests.get(BINANCE_PRICE_URL, params={"symbol": symbol}, timeout=10)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        print("Error precio Binance:", symbol, e)
        return None


def procesar():
    while True:
        try:
            with _lock:
                signals = load_signals()
                resultados, sha = load_resultados()
                cambiado = False
                ahora = datetime.now(timezone.utc)

                for s in signals:
                    sid = str(s["id"])
                    if sid not in resultados:
                        resultados[sid] = {
                            "simbolo": s["simbolo"],
                            "tipo": s["tipo"],
                            "tf": s["tf"],
                            "precio_entrada": float(s["precio"]),
                            "timestamp": s["timestamp"],
                        }

                    entry = resultados[sid]
                    ts = datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00"))
                    elapsed = (ahora - ts).total_seconds()

                    for campo, segundos in CHECKPOINTS.items():
                        if campo not in entry and elapsed >= segundos:
                            precio_actual = get_price(s["simbolo"])
                            if precio_actual is not None:
                                cambio_pct = round(
                                    (precio_actual - entry["precio_entrada"]) / entry["precio_entrada"] * 100, 3
                                )
                                entry[campo] = cambio_pct
                                cambiado = True

                if cambiado:
                    save_resultados(resultados, sha)
                    print(f"[{ahora.isoformat()}] Resultados actualizados")

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
    resumen = {}
    for sid, r in resultados.items():
        clave = (r["simbolo"], r["tf"])
        if clave not in resumen:
            resumen[clave] = {"n": 0, "r_30m": [], "r_2h": [], "r_24h": []}
        resumen[clave]["n"] += 1
        for campo in ["r_30m", "r_2h", "r_24h"]:
            if campo in r:
                resumen[clave][campo].append(r[campo])

    salida = []
    for (simbolo, tf), v in resumen.items():
        fila = {"simbolo": simbolo, "tf": tf, "n_senales": v["n"]}
        for campo in ["r_30m", "r_2h", "r_24h"]:
            valores = v[campo]
            if valores:
                fila[campo + "_promedio"] = round(sum(valores) / len(valores), 3)
                fila[campo + "_winrate"] = round(
                    sum(1 for x in valores if x > 0) / len(valores) * 100, 1
                )
                fila[campo + "_n"] = len(valores)
            else:
                fila[campo + "_promedio"] = None
                fila[campo + "_winrate"] = None
                fila[campo + "_n"] = 0
        salida.append(fila)

    salida.sort(key=lambda f: (f["simbolo"], f["tf"]))
    return salida


@app.route("/")
def home():
    return jsonify({
        "servicio": "PALMERO - Historiador de señales",
        "nota": "Solo lectura sobre signals_log.json. No modifica superb-growth.",
        "endpoints": ["/stats - resumen por simbolo y TF", "/stats/raw - resultados detallados", "/stats/t/<bust> - version sin cache"],
    })


@app.route("/stats")
def stats():
    return jsonify({
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "resumen": calcular_resumen(),
    })


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
