# main.py
import os
import io
import csv
from typing import Optional, List, Dict, Any

from fastapi import (
    FastAPI, HTTPException, Depends, UploadFile, File, Header
)
from fastapi.responses import JSONResponse
from fastapi.security.api_key import APIKeyHeader

from sqlalchemy import (
    create_engine, Column, Integer, String, ForeignKey, Text
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship

import requests

# =========================
# Config & Security
# =========================
API_KEY = (os.getenv("API_KEY") or "").strip()
WA_TOKEN = (os.getenv("WA_TOKEN") or "").strip()
WA_PHONE_ID = (os.getenv("WA_PHONE_ID") or "").strip()

META_BASE = f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}"

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def enforce_api_key(x_api_key: Optional[str] = Depends(api_key_header)):
    if not API_KEY:
        # Server misconfiguration → makes it obvious in logs / responses
        raise HTTPException(status_code=500, detail="Server API key not configured")
    client = (x_api_key or "").strip()
    if client != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

app = FastAPI(
    title="Eebii Notify API",
    description="Eebii WhatsApp Notification API",
    version="0.1.0",
    dependencies=[Depends(enforce_api_key)],  # protect all routes by default
)

# public endpoints (docs & webhook) – remove global dependency there
@app.get("/", dependencies=[])
def root():
    return {"ok": True, "name": "Eebii Notify API"}

# =========================
# DB (SQLite)
# =========================
Base = declarative_base()
engine = create_engine("sqlite:///./eebii.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)
    name = Column(String(200))
    phone = Column(String(32), unique=True, index=True)

class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), unique=True)

class GroupMember(Base):
    __tablename__ = "group_members"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"))
    contact_id = Column(Integer, ForeignKey("contacts.id"))
    group = relationship("Group", lazy="joined")
    contact = relationship("Contact", lazy="joined")

class Media(Base):
    __tablename__ = "media"
    id = Column(Integer, primary_key=True)
    wa_media_id = Column(String(128), index=True)
    filename = Column(String(255))
    mime = Column(String(80))
    note = Column(Text, nullable=True)

Base.metadata.create_all(bind=engine)

def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()

# =========================
# Helpers (Meta senders)
# =========================
def _ensure_meta_ready():
    if not WA_TOKEN or not WA_PHONE_ID:
        raise HTTPException(status_code=500, detail="WhatsApp API not configured")

def _wa_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json"
    }

def send_text(to: str, text: str) -> Dict[str, Any]:
    _ensure_meta_ready()
    url = f"{META_BASE}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    r = requests.post(url, headers=_wa_headers(), json=payload, timeout=30)
    if not r.ok:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

def send_template(to: str, template: str, lang: str = "en_US", variables: Optional[List[str]] = None) -> Dict[str, Any]:
    _ensure_meta_ready()
    components = []
    if variables:
        components = [{
            "type": "body",
            "parameters": [{"type": "text", "text": v} for v in variables]
        }]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": lang},
            "components": components
        }
    }
    r = requests.post(f"{META_BASE}/messages", headers=_wa_headers(), json=payload, timeout=30)
    if not r.ok:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

def upload_media_to_meta(file_bytes: bytes, mime: str, filename: str) -> str:
    _ensure_meta_ready()
    # NOTE: media upload uses multipart/form-data and different header
    url = f"{META_BASE}/media"
    headers = {"Authorization": f"Bearer {WA_TOKEN}"}
    files = {
        "file": (filename, file_bytes, mime),
        "type": (None, mime)
    }
    r = requests.post(url, headers=headers, files=files, timeout=60)
    if not r.ok:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    data = r.json()
    return data.get("id")

def send_media(to: str, wa_media_id: str, mime: str, caption: Optional[str] = None) -> Dict[str, Any]:
    _ensure_meta_ready()
    media_type = "image" if mime.startswith("image/") else "document"
    media_payload = {"id": wa_media_id}
    if media_type == "document" and caption:
        media_payload["caption"] = caption
    if media_type == "image" and caption:
        media_payload["caption"] = caption

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": media_type,
        media_type: media_payload
    }
    r = requests.post(f"{META_BASE}/messages", headers=_wa_headers(), json=payload, timeout=30)
    if not r.ok:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

# =========================
# Contacts
# =========================
@app.get("/contacts", dependencies=[Depends(enforce_api_key)])
def list_contacts(q: Optional[str] = None, s=Depends(db)):
    qry = s.query(Contact)
    if q:
        like = f"%{q}%"
        qry = qry.filter((Contact.name.like(like)) | (Contact.phone.like(like)))
    items = [{"id": c.id, "name": c.name, "phone": c.phone} for c in qry.order_by(Contact.id.desc()).all()]
    return {"count": len(items), "items": items}

@app.post("/contacts", dependencies=[Depends(enforce_api_key)])
def add_contact(body: Dict[str, str], s=Depends(db)):
    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=422, detail="phone is required")
    if s.query(Contact).filter(Contact.phone == phone).first():
        raise HTTPException(status_code=422, detail="phone already exists")
    c = Contact(name=name or phone, phone=phone)
    s.add(c); s.commit()
    return {"id": c.id, "name": c.name, "phone": c.phone}

@app.post("/contacts/import", dependencies=[Depends(enforce_api_key)])
async def import_contacts(csv_file: UploadFile = File(...), s=Depends(db)):
    if not csv_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=422, detail="Upload a CSV file")
    content = await csv_file.read()
    added = 0
    reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
    for row in reader:
        phone = (row.get("phone") or "").strip()
        name = (row.get("name") or "").strip()
        if not phone:
            continue
        if s.query(Contact).filter(Contact.phone == phone).first():
            continue
        s.add(Contact(name=name or phone, phone=phone))
        added += 1
    s.commit()
    return {"added": added}

# =========================
# Groups
# =========================
@app.get("/groups", dependencies=[Depends(enforce_api_key)])
def list_groups(s=Depends(db)):
    items = [{"id": g.id, "name": g.name} for g in s.query(Group).order_by(Group.id.desc()).all()]
    return {"count": len(items), "items": items}

@app.post("/groups", dependencies=[Depends(enforce_api_key)])
def create_group(body: Dict[str, str], s=Depends(db)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    if s.query(Group).filter(Group.name == name).first():
        raise HTTPException(status_code=422, detail="group already exists")
    g = Group(name=name)
    s.add(g); s.commit()
    return {"id": g.id, "name": g.name}

@app.post("/groups/add", dependencies=[Depends(enforce_api_key)])
def add_members(body: Dict[str, Any], s=Depends(db)):
    gid = body.get("group_id")
    phones = body.get("phones") or []
    g = s.query(Group).get(gid)
    if not g:
        raise HTTPException(status_code=404, detail="group not found")
    added = 0
    for p in phones:
        contact = s.query(Contact).filter(Contact.phone == p).first()
        if not contact:
            contact = Contact(name=p, phone=p)
            s.add(contact); s.flush()
        exists = (
            s.query(GroupMember)
            .filter(GroupMember.group_id == g.id, GroupMember.contact_id == contact.id)
            .first()
        )
        if not exists:
            s.add(GroupMember(group_id=g.id, contact_id=contact.id))
            added += 1
    s.commit()
    return {"group_id": g.id, "added": added}

@app.get("/groups/{gid}/members", dependencies=[Depends(enforce_api_key)])
def group_members(gid: int, s=Depends(db)):
    g = s.query(Group).get(gid)
    if not g:
        raise HTTPException(status_code=404, detail="group not found")
    members = (
        s.query(GroupMember)
        .filter(GroupMember.group_id == g.id)
        .all()
    )
    items = [{"id": m.contact.id, "name": m.contact.name, "phone": m.contact.phone} for m in members]
    return {"group": {"id": g.id, "name": g.name}, "count": len(items), "items": items}

# =========================
# Templates (Meta)
# =========================
@app.get("/templates", dependencies=[Depends(enforce_api_key)])
def list_templates():
    _ensure_meta_ready()
    url = f"https://graph.facebook.com/v20.0/{os.getenv('WABA_ID')}/message_templates"
    # If you don’t have WABA_ID set yet, fall back to /phone_number/templates (limited)
    alt_url = f"{META_BASE}/message_templates"
    headers = {"Authorization": f"Bearer {WA_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 400 or r.status_code == 403:
        r = requests.get(alt_url, headers=headers, timeout=30)
    if not r.ok:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

# =========================
# Sending
# =========================
@app.post("/send/text", dependencies=[Depends(enforce_api_key)])
def api_send_text(body: Dict[str, str]):
    to = (body.get("to") or "").strip()
    text = (body.get("text") or body.get("message") or "").strip()
    if not to or not text:
        raise HTTPException(status_code=400, detail="to and text are required")
    return send_text(to, text)

@app.post("/send/template", dependencies=[Depends(enforce_api_key)])
def api_send_template(body: Dict[str, Any]):
    to = (body.get("to") or "").strip()
    template = (body.get("template") or "").strip()
    lang = (body.get("lang") or "en_US").strip()
    variables = body.get("variables") or []
    if not to or not template:
        raise HTTPException(status_code=400, detail="to and template are required")
    return send_template(to, template, lang, variables)

@app.post("/media/upload", dependencies=[Depends(enforce_api_key)])
async def api_media_upload(file: UploadFile = File(...), note: Optional[str] = None, s=Depends(db)):
    data = await file.read()
    wa_id = upload_media_to_meta(data, file.content_type or "application/octet-stream", file.filename or "file")
    m = Media(wa_media_id=wa_id, filename=file.filename, mime=file.content_type or "application/octet-stream", note=note)
    s.add(m); s.commit()
    return {"media_id": m.id, "wa_media_id": wa_id, "filename": m.filename, "mime": m.mime}

@app.post("/send/media", dependencies=[Depends(enforce_api_key)])
def api_send_media(body: Dict[str, Any], s=Depends(db)):
    to = (body.get("to") or "").strip()
    wa_media_id = (body.get("wa_media_id") or "").strip()
    caption = (body.get("caption") or "").strip() or None
    if not to or not wa_media_id:
        raise HTTPException(status_code=400, detail="to and wa_media_id are required")
    media = s.query(Media).filter(Media.wa_media_id == wa_media_id).first()
    mime = media.mime if media else "application/octet-stream"
    return send_media(to, wa_media_id, mime, caption)

# =========================
# Bulk
# =========================
@app.post("/bulk/preview", dependencies=[Depends(enforce_api_key)])
def bulk_preview(body: Dict[str, Any], s=Depends(db)):
    """
    Body:
      {
        "group_id": 1,  (optional)
        "phones": ["+91..."],  (optional)
        "mode": "text" | "template" | "media",
        "text": "...",
        "template": {"name":"hello_world","lang":"en_US","variables":["A","B"]},
        "media": {"wa_media_id":"...", "caption": "..."}
      }
    """
    mode = (body.get("mode") or "text").strip()
    phones: List[str] = body.get("phones") or []
    gid = body.get("group_id")
    if gid:
        members = (
            s.query(GroupMember)
            .filter(GroupMember.group_id == gid)
            .all()
        )
        phones.extend([m.contact.phone for m in members])
    phones = sorted(list({p.strip() for p in phones if p and p.strip()}))
    if not phones:
        raise HTTPException(status_code=422, detail="No recipients found")
    preview = {"count": len(phones), "phones": phones, "mode": mode}
    if mode == "text":
        preview["text"] = body.get("text") or ""
    elif mode == "template":
        preview["template"] = body.get("template") or {}
    elif mode == "media":
        preview["media"] = body.get("media") or {}
    else:
        raise HTTPException(status_code=422, detail="Unknown mode")
    return preview

@app.post("/send/bulk", dependencies=[Depends(enforce_api_key)])
def bulk_send(body: Dict[str, Any], s=Depends(db)):
    mode = (body.get("mode") or "text").strip()
    phones: List[str] = body.get("phones") or []
    gid = body.get("group_id")
    if gid:
        members = s.query(GroupMember).filter(GroupMember.group_id == gid).all()
        phones.extend([m.contact.phone for m in members])
    phones = sorted(list({p.strip() for p in phones if p and p.strip()}))
    if not phones:
        raise HTTPException(status_code=422, detail="No recipients found")

    sent, failed = [], []
    for p in phones:
        try:
            if mode == "text":
                txt = (body.get("text") or "").strip()
                send_text(p, txt)
            elif mode == "template":
                tpl = body.get("template") or {}
                send_template(p, tpl.get("name", ""), tpl.get("lang", "en_US"), tpl.get("variables", []))
            elif mode == "media":
                media = body.get("media") or {}
                wa_id = (media.get("wa_media_id") or "").strip()
                caption = (media.get("caption") or None)
                if not wa_id:
                    raise ValueError("wa_media_id missing")
                db_media = s.query(Media).filter(Media.wa_media_id == wa_id).first()
                mime = db_media.mime if db_media else "application/octet-stream"
                send_media(p, wa_id, mime, caption)
            else:
                raise ValueError("Unknown mode")
            sent.append(p)
        except Exception as e:
            failed.append({"phone": p, "error": str(e)})
    return {"requested": len(phones), "sent": len(sent), "failed": failed}

# =========================
# Webhook (public)
# =========================
@app.get("/webhook/whatsapp", dependencies=[])
def verify(mode: Optional[str] = None, challenge: Optional[str] = None, token: Optional[str] = None):
    verify_token = (os.getenv("META_VERIFY_TOKEN") or "").strip()
    if token and verify_token and token == verify_token:
        return JSONResponse(content=challenge or "")
    raise HTTPException(status_code=403, detail="Verification failed")

@app.post("/webhook/whatsapp", dependencies=[])
def receive(body: Dict[str, Any]):
    # You can expand this to log or route inbound messages
    return {"received": True}
