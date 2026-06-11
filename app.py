import os
import threading
import json
import paho.mqtt.client as mqtt_client
from datetime import datetime
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── CONFIGURAÇÕES GLOBAIS ────────────────────────────────────────
API_KEY = os.environ.get("API_KEY", "chave-local-dev")

if not API_KEY:
    raise RuntimeError("API_KEY não definida nas variáveis de ambiente!")

DB_FILE = "db.json"

MQTT_BROKER = os.environ.get("MQTT_BROKER", "broker.hivemq.com")
MQTT_USER   = os.environ.get("MQTT_USER", "")
MQTT_PASS   = os.environ.get("MQTT_PASS", "")
MQTT_PORT   = 1883

# ── BASE DE DADOS JSON ───────────────────────────────────────────
def load_db():
    if not os.path.exists(DB_FILE):
        return {"dispositivos": {}, "animais": {}, "leituras": []}
    with open(DB_FILE, "r") as f:
        return json.load(f)

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)

# ── SEGURANÇA OWASP (API Key) ────────────────────────────────────
def require_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-API-KEY") != API_KEY:
            return jsonify({"erro": "Acesso Não Autorizado. API Key inválida."}), 401
        return f(*args, **kwargs)
    return decorated

# ── DASHBOARD ────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return send_from_directory(".", "dashboard.html")

# ── PROVISIONAMENTO ──────────────────────────────────────────────

@app.route("/api/dispositivos", methods=["POST"])
@require_key
def adicionar_dispositivo():
    """Adiciona/Regista um novo dispositivo Pico W (Add Device)"""
    db = load_db()
    data = request.json or {}
    device_id = data.get("device_id")

    if not device_id:
        return jsonify({"erro": "O campo 'device_id' é obrigatório."}), 400

    db["dispositivos"][device_id] = {
        "device_id":      device_id,
        "localizacao":    data.get("localizacao", "Alimentador Central"),
        "status":         "ativo",
        "data_registo":   datetime.now().isoformat(),
        "last_seen":      None,
        "total_leituras": 0
    }
    save_db(db)
    return jsonify({"mensagem": "Novo hardware provisionado.", "dispositivo": db["dispositivos"][device_id]}), 201


@app.route("/api/dispositivos/<device_id>", methods=["PUT"])
@require_key
def substituir_dispositivo(device_id):
    """Substitui um hardware antigo por um novo (Replace Device)"""
    db = load_db()
    data = request.json or {}
    novo_device_id = data.get("novo_device_id")

    if device_id not in db["dispositivos"]:
        return jsonify({"erro": "Dispositivo não encontrado."}), 404
    if not novo_device_id:
        return jsonify({"erro": "É necessário fornecer o 'novo_device_id'."}), 400

    config_antiga = db["dispositivos"].pop(device_id)
    db["dispositivos"][novo_device_id] = {
        "device_id":      novo_device_id,
        "localizacao":    config_antiga["localizacao"],
        "status":         "ativo",
        "data_registo":   datetime.now().isoformat(),
        "last_seen":      None,
        "total_leituras": 0
    }
    save_db(db)
    return jsonify({"mensagem": f"Hardware {device_id} substituído por {novo_device_id}."}), 200


@app.route("/api/dispositivos/<device_id>", methods=["DELETE"])
@require_key
def remover_dispositivo(device_id):
    """Remove um dispositivo do ecossistema (Remove Device)"""
    db = load_db()
    if device_id not in db["dispositivos"]:
        return jsonify({"erro": "Dispositivo não encontrado."}), 404

    del db["dispositivos"][device_id]
    save_db(db)
    return jsonify({"mensagem": f"Dispositivo {device_id} removido."}), 200

# ── MANAGEMENT SERVICE ───────────────────────────────────────────

@app.route("/api/dispositivos", methods=["GET"])
@require_key
def listar_dispositivos():
    """Lista todos os dispositivos com estado e last_seen"""
    db = load_db()
    return jsonify(list(db["dispositivos"].values()))


@app.route("/api/dispositivos/<device_id>", methods=["GET"])
@require_key
def estado_dispositivo(device_id):
    """Estado detalhado de um dispositivo: status, last_seen, total leituras"""
    db = load_db()
    if device_id not in db["dispositivos"]:
        return jsonify({"erro": "Dispositivo não encontrado."}), 404
    return jsonify(db["dispositivos"][device_id])

# ── SENSOR (RFID) — leituras via MQTT, histórico via REST ────────

@app.route("/api/leituras", methods=["GET"])
@require_key
def listar_leituras():
    """Histórico completo de leituras do sensor RFID"""
    db = load_db()
    return jsonify(db.get("leituras", []))


@app.route("/api/leituras/ultimo", methods=["GET"])
@require_key
def ultima_leitura():
    """Última leitura registada em tempo real"""
    db = load_db()
    if not db["leituras"]:
        return jsonify({"mensagem": "Nenhum registo ainda."}), 200
    return jsonify(db["leituras"][-1])

# ── ATUADORES ────────────────────────────────────────────────────

@app.route("/api/atuador/alimentar", methods=["POST"])
@require_key
def alimentar_manual():
    """Aciona um alimentador remotamente via MQTT"""
    data   = request.json or {}
    comida = data.get("comida")
    device = data.get("device_id")

    if comida not in ["A", "B"]:
        return jsonify({"erro": "Escolha 'A' ou 'B'."}), 400

    cmd_payload = json.dumps({"cmd": "alimentar", "tipo": comida})
    topic = f"petfeeder/{device}/cmd" if device else "petfeeder/broadcast/cmd"
    mqtt.publish(topic, cmd_payload)

    return jsonify({
        "status":    "Comando MQTT enviado",
        "topico":    topic,
        "timestamp": datetime.now().isoformat()
    }), 200


@app.route("/api/atuador/reset", methods=["POST"])
@require_key
def reset_atuadores():
    """Corte de emergência — desliga todos os atuadores via MQTT"""
    data   = request.json or {}
    device = data.get("device_id")
    topic  = f"petfeeder/{device}/cmd" if device else "petfeeder/broadcast/cmd"
    mqtt.publish(topic, json.dumps({"cmd": "reset"}))

    return jsonify({
        "status":    "Reset MQTT enviado",
        "timestamp": datetime.now().isoformat()
    }), 200

# ── ANIMAIS ──────────────────────────────────────────────────────

@app.route("/api/animais", methods=["GET"])
@require_key
def listar_animais():
    """Lista todos os animais e os seus cartões RFID"""
    db = load_db()
    return jsonify(list(db.get("animais", {}).values()))


@app.route("/api/animais", methods=["POST"])
@require_key
def adicionar_animal():
    """Associa um cartão RFID a um animal e tipo de ração"""
    db = load_db()
    data    = request.json or {}
    card_id = data.get("card_id")

    if not card_id:
        return jsonify({"erro": "O campo 'card_id' é obrigatório."}), 400
    if len(str(card_id)) > 50:
        return jsonify({"erro": "card_id demasiado longo."}), 400

    db["animais"][card_id] = {
        "card_id": card_id,
        "nome":    data.get("nome", "Animal Anónimo"),
        "comida":  data.get("comida", "A")
    }
    save_db(db)
    return jsonify({"mensagem": "Associação efetuada.", "animal": db["animais"][card_id]}), 201

# ── CALLBACKS MQTT ───────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Flask MQTT: ligado ao broker!")
        client.subscribe("petfeeder/+/leitura")
        client.subscribe("petfeeder/+/status")
    else:
        print(f"Flask MQTT: falha de ligação (código {rc})")


def on_message(client, userdata, msg):
    try:
        data  = json.loads(msg.payload.decode())
        topic = msg.topic

        # Valida API key em todas as mensagens (OWASP)
        if data.get("api_key") != API_KEY:
            print(f"MQTT: mensagem rejeitada — API key inválida ({topic})")
            return

        if "/leitura" in topic:
            _processar_leitura(client, data)
        elif "/status" in topic:
            _atualizar_status(data)

    except Exception as e:
        print(f"MQTT on_message erro: {e}")


def _processar_leitura(client, data):
    db        = load_db()
    card_id   = data.get("card_id")
    device_id = data.get("device_id")

    if not card_id or not device_id:
        return
    if device_id not in db["dispositivos"]:
        print(f"MQTT: dispositivo {device_id} não provisionado — ignorado")
        return

    # Atualiza management service
    db["dispositivos"][device_id]["last_seen"]      = datetime.now().isoformat()
    db["dispositivos"][device_id]["total_leituras"] = \
        db["dispositivos"][device_id].get("total_leituras", 0) + 1

    animal     = db["animais"].get(card_id)
    autorizado = animal is not None

    leitura = {
        "card_id":    card_id,
        "device_id":  device_id,
        "timestamp":  datetime.now().isoformat(),
        "autorizado": autorizado,
        "nome":       animal["nome"]   if autorizado else "Desconhecido",
        "comida":     animal["comida"] if autorizado else None
    }
    db["leituras"].append(leitura)
    save_db(db)
    print(f"MQTT leitura: {leitura['nome']} — autorizado: {autorizado}")

    # Se autorizado, publica comando de volta para o atuador
    if autorizado:
        cmd = json.dumps({"cmd": "alimentar", "tipo": animal["comida"]})
        client.publish(f"petfeeder/{device_id}/cmd", cmd)
        print(f"Comando enviado: alimentar {animal['comida']} → {device_id}")


def _atualizar_status(data):
    db        = load_db()
    device_id = data.get("device")
    if device_id and device_id in db["dispositivos"]:
        db["dispositivos"][device_id]["status"]    = data.get("status", "online")
        db["dispositivos"][device_id]["last_seen"] = datetime.now().isoformat()
        save_db(db)
        print(f"MQTT status: {device_id} → {data.get('status')}")

# ── THREAD MQTT ──────────────────────────────────────────────────

def start_mqtt():
    global mqtt
    mqtt = mqtt_client.Client(client_id="flask-server", clean_session=True)

    if MQTT_USER and MQTT_PASS:
        mqtt.username_pw_set(MQTT_USER, MQTT_PASS)

    mqtt.on_connect = on_connect
    mqtt.on_message = on_message
    mqtt.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    mqtt.loop_forever()

mqtt_thread = threading.Thread(target=start_mqtt, daemon=True)
mqtt_thread.start()

# ── ARRANQUE ─────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
