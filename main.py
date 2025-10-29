import os, requests
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "eebii_verify_token")
WA_PHONE_ID = os.getenv("WA_PHONE_ID")   # your Phone Number ID (for now)
WA_TOKEN = os.getenv("WA_TOKEN")         # your Access Token (for now)

app = FastAPI(title="Eebii Notify API")

@app.get("/webhook/whatsapp")
async def verify(req: Request):
    q = dict(req.query_params)
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == META_VERIFY_TOKEN:
        return int(q.get("hub.challenge", "0"))
    raise HTTPException(403, "Verification failed")

@app.post("/webhook/whatsapp")
async def receive(payload: dict):
    print("WEBHOOK EVENT:", payload)
    return {"ok": True}

class SendIn(BaseModel):
    to: str
    template: str = "hello_world"
    lang: str = "en_US"
    variables: list[str] = []

@app.post("/send")
async def send_msg(data: SendIn):
    if not WA_PHONE_ID or not WA_TOKEN:
        raise HTTPException(400, "Missing WA_PHONE_ID or WA_TOKEN")
    url = f"https://graph.facebook.com/v21.0/{WA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}"}
    body = {
        "messaging_product": "whatsapp",
        "to": data.to,
        "type": "template",
        "template": {
            "name": data.template,
            "language": {"code": data.lang},
            "components": [{
                "type":"body",
                "parameters":[{"type":"text","text":v} for v in data.variables]
            }] if data.variables else []
        }
    }
    r = requests.post(url, json=body, headers=headers, timeout=20)
    return {"status": r.status_code, "response": r.json()}
# --- Simple in-memory store for a single connected tenant (MVP) ---
TENANT = {"phone_id": os.getenv("WA_PHONE_ID"), "token": os.getenv("WA_TOKEN")}

from fastapi import Depends

class ConnectIn(BaseModel):
    phone_number_id: str
    access_token: str

@app.post("/wa/connect")
async def wa_connect(data: ConnectIn):
    # Save customer's own creds (MVP: in-memory)
    TENANT["phone_id"] = data.phone_number_id.strip()
    TENANT["token"] = data.access_token.strip()
    return {"ok": True}

@app.post("/send2")
async def send_msg_tenant(data: SendIn):
    # Use the tenant's saved creds
    phone_id = TENANT.get("phone_id")
    token = TENANT.get("token")
    if not phone_id or not token:
        raise HTTPException(400, "No WhatsApp account connected yet")
    url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "messaging_product":"whatsapp",
        "to": data.to,
        "type":"template",
        "template":{
            "name": data.template,
            "language":{"code": data.lang},
            "components":[{"type":"body","parameters":[{"type":"text","text":v} for v in data.variables]}] if data.variables else []
        }
    }
    r = requests.post(url, json=body, headers=headers, timeout=20)
    return {"status": r.status_code, "response": r.json()}

