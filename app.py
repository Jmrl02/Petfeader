import os  
import threading
import json  # Usar o módulo json nativo do Python
import paho.mqtt.client as mqtt_client
from datetime import datetime
from functools import wraps
from flask import Flask, jsonify, request
from flask_cors import CORS  # <-- NOVO: Importar Flask-CORS

app = Flask(__name__)
CORS(app)  # <-- NOVO: Ativar CORS para permitir a ligação do Painel HTML

# ── CONFIGURAÇÕES GLOBAIS ────────────────────────────────────────
API_KEY = os.environ.get("API_KEY", "chave-local-dev") # Ajustado para o padrão do laboratório
DB_FILE = "db.json"

# Configuração MQTT ajustada para o broker público gratuito
MQTT_BROKER = os.environ.get("MQTT_BROKER", "broker.hivemq.com")
MQTT_USER   = os.environ.get("MQTT_USER", "")
MQTT_PASS   = os.environ.get("MQTT_PASS", "")
MQTT_PORT   = 1883  # Porta 1883 (sem TLS nativo no broker público)

# ── UTILS (BASE DE DADOS JSON) ───────────────────────────────────
def load_db():
    if not os.path.exists(DB_FILE):
        return {"dispositivos": {}, "animais": {}, "leituras": []}
    with open(DB_FILE, "r") as f:
        return json.load(f)

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)

# ── DECORADOR DE SEGURANÇA (OWASP) ───────────────────────────────
def require_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-API-KEY") != API_KEY:
            return jsonify({"erro": "Acesso Não Autorizado. API Key inválida."}), 401
        return f(*args, **kwargs)
    return decorated

# ── CALLBACKS DO SUBSCRIBER MQTT ─────────────────────────────────
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Flask MQTT: Ligado ao broker com sucesso!")
        # Subscreve aos tópicos específicos do teu dispositivo
        client.subscribe("petfeeder/e6614864d3583534/leitura")
        client.subscribe("petfeeder/e6614864d3583534/status")
    else:
        print(f"Flask MQTT: Falha de ligação (código {rc})")

def on_message(client, userdata, msg):
    """Recebe publicações do dispositivo e processa"""
    try:
        data = json.loads(msg.payload.decode())
        topic = msg.topic

        # Validação da API key no payload (Segurança OWASP)
        if data.get("api_key") != API_KEY:
            print(f"MQTT: Mensagem rejeitada — API key inválida ({topic})")
            return

        if "/leitura" in topic:
            _processar_leitura(client, data)
        elif "/status" in topic:
            _atualizar_status(data)

    except Exception as e:
        print(f"MQTT on_message erro: {e}")

def _processar_leitura(client, data):
    db = load_db()
    card_id   = data.get("card_id")
    device_id = data.get("device_id")

    if not card_id or not device_id:
        return
    if device_id not in db["dispositivos"]:
        print(f"MQTT: Dispositivo {device_id} não provisionado — ignorado")
        return

    # Atualiza o estado de atividade do hardware no Management Service
    db["dispositivos"][device_id]["last_seen"] = datetime.now().isoformat()
    db["dispositivos"][device_id]["total_leituras"] = db["dispositivos"][device_id].get("total_leituras", 0) + 1

    animal = db["animais"].get(card_id)
    autorizado = animal is not None

    leitura = {
        "card_id":    card_id,
        "device_id":  device_id,
        "timestamp":  datetime.now().isoformat(),
        "autorizado": autorizado,
        "nome":       animal["nome"] if autorizado else "Desconhecido",
        "comida":     animal["comida"] if autorizado else None
    }
    db["leituras"].append(leitura)
    save_db(db)
    
    nome_animal = animal['nome'] if autorizado else card_id
    print(f"MQTT leitura guardada: {nome_animal} (Autorizado: {autorizado})")

    # ── CAMINHO DE VOLTA: SE AUTORIZADO, MANDA ALIMENTAR ────────────
    if autorizado:
        cmd_payload = json.dumps({
            "cmd": "alimentar",
            "tipo": animal.get("comida", "A")
        })
        topic_cmd = f"petfeeder/{device_id}/cmd"
        client.publish(topic_cmd, cmd_payload)
        print(f"Sucesso: Comando automático enviado para {topic_cmd}")

def _atualizar_status(data):
    db = load_db()
    device_id = data.get("device")
    if device_id and device_id in db["dispositivos"]:
        db["dispositivos"][device_id]["status"] = data.get("status", "online")
        db["dispositivos"][device_id]["last_seen"] = datetime.now().isoformat()
        save_db(db)
        print(f"MQTT Status: Dispositivo {device_id} está {data.get('status')}")

# ── ROTAS REST API (PROVISIONAMENTO E MANAGEMENT) ────────────────
@app.route("/api/dispositivos", methods=["POST"])
@require_key
def adicionar_dispositivo():
    db = load_db()
    data = request.json or {}
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"erro": "O campo 'device_id' é obrigatório."}), 400
    db["dispositivos"][device_id] = {
        "device_id": device_id,
        "localizacao": data.get("localizacao", "Alimentador Central"),
        "status": "ativo",
        "data_registo": datetime.now().isoformat(),
        "last_seen": None,
        "total_leituras": 0
    }
    save_db(db)
    return jsonify({"mensagem": "Novo hardware provisionado.", "dispositivo": db["dispositivos"][device_id]}), 201

@app.route("/api/dispositivos", methods=["GET"])
@require_key
def listar_dispositivos():
    db = load_db()
    return jsonify(list(db["dispositivos"].values()))

@app.route("/api/animais", methods=["POST"])
@require_key
def adicionar_animal():
    db = load_db()
    data = request.json or {}
    card_id = data.get("card_id")
    if not card_id:
        return jsonify({"erro": "O campo 'card_id' é obrigatório."}), 400
    db["animais"][card_id] = {
        "card_id": card_id,
        "nome": data.get("nome", "Animal Anónimo"),
        "comida": data.get("comida", "A")
    }
    save_db(db)
    return jsonify({"mensagem": "Associação de animal efetuada.", "animal": db["animais"][card_id]}), 201

# ── NOVO: ROTA PARA LISTAR ANIMAIS (Necessária para o Painel HTML) ──
@app.route("/api/animais", methods=["GET"])
@require_key
def listar_animais():
    db = load_db()
    # Como os animais estão guardados como objeto, convertemos para lista para o JS ler
    return jsonify(list(db.get("animais", {}).values()))

# ── NOVO: ROTA PARA LISTAR HISTÓRICO DE LEITURAS (Resolve o Erro 404) ──
@app.route("/api/leituras", methods=["GET"])
@require_key
def listar_leituras():
    db = load_db()
    return jsonify(db.get("leituras", []))

# ── ENDPOINTS DE CONTROLO MANUAL (VIA HTTP POST PARA TRANSMITIR MQTT) ──
@app.route("/api/atuador/alimentar", methods=["POST"])
@require_key
def alimentar_manual():
    data    = request.json or {}
    comida  = data.get("comida")
    device  = data.get("device_id")

    if comida not in ["A", "B"]:
        return jsonify({"erro": "Escolha 'A' ou 'B'."}), 400

    cmd_payload = json.dumps({"cmd": "alimentar", "tipo": comida})
    topic = f"petfeeder/{device}/cmd" if device else "petfeeder/+/cmd"
    
    mqtt.publish(topic, cmd_payload)
    return jsonify({"status": "Comando MQTT manual enviado", "topico": topic}), 200

@app.route("/api/atuador/reset", methods=["POST"])
@require_key
def reset_atuadores():
    data   = request.json or {}
    device = data.get("device_id")
    topic  = f"petfeeder/{device}/cmd" if device else "petfeeder/broadcast/cmd"
    
    mqtt.publish(topic, json.dumps({"cmd": "reset"}))
    return jsonify({"status": "Reset MQTT enviado"}), 200

# ── INICIALIZAÇÃO DA THREAD MQTT BACKGROUND ──────────────────────
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

if __name__ == "__main__":
    # use_reloader=False evita que o Windows execute a thread MQTT em duplicado
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)