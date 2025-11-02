# main.py — Eebii Notify API (final)

import os
import io
import csv
from typing import Optional, List, Dict, Any

import requests
from fastapi import (
    FastAPI, Depends, Header, HTTPException,
    UploadFile, File
)
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi

from sqlalchemy import (
    create_engine, Column, Integer, String, Table, ForeignKey
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session

# =========================
# Env & Meta configuration
# =========================
API_KEY = os.getenv("API_KEY", "").strip()
WA_TOKEN = os.getenv("WA_TOKEN", "").strip()
WA_PHONE_ID = os.getenv("WA_PHONE_ID", "").strip()
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "").strip()
META_BASE = f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}"

# ====================================
# API key enforcement (global guard)
# ====================================
async def enforce_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    if not API_KEY or not x_api_key or x_api_key.strip() != API_KEY:
        # Do NOT leak the expected key — just say it's invalid
        raise HTTPException(status_code=401, detail="Invalid API key")

# ====================================
# FastAPI app
# ====================================
app = FastAPI(
    title="Eebii Notify API",
    version="0.1.0",
    description="Eebii WhatsApp Notification API",
    dependencies=[Depends(enforce_api_key)],  # protect all routes
)

# ------------------------------------
# Add real Authorize button in Swagger
# ------------------------------------
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schema["components"]["securitySchemes"]["apiKey"] = {
        "type": "apiKey",
        "name": "X-API-Key",
        "in": "header",
    }
    schema["security"] = [{"apiKey": []}]  # apply globally

    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi

# =========================
# SQLite (contacts/groups)
# =========================
Base = declarative_base()
engine = create_engine("sqlite:///./eebii.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

group_members = Table(
    "group_members",
    Base.metadata,
    Column("group_id", Integer, ForeignKey("groups.id"), primary_key=True),
    Column("contact_id", Integer, ForeignKey("contacts.id"), primary_key=True),
)

class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200))
    phone = Column(String(32), unique=True, index=True)

class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), unique=True, index=True)
    members = relationship("Contact", secondary=group_members, backref="groups")

Base.metadata.create_all(bind=engine)

# ==============
# Util helpers
# ==============
def db() -> Session:
    try:
        s = SessionLocal()
        yield s
    finally:
        s.close()

def require_meta() -> None:
    if not WA_TOKEN or not WA_PHONE_ID:
        raise HTTPException(status_code=500, detail="WhatsApp not configured")

def e164(number: str) -> str:
    n = number.strip()
    if not (n.startswith("+") or n.isdigit()):
        raise HTTPException(422, detail="Phone must be E.164 or digits")
    if n.isdigit():
        raise HTTPException(422, detail="Include country code (use +CCxxxxxxxxx)")
    return n

# ==============
# Root
# ==============
@app.get("/")
def root():
    return {"ok": True, "name": "Eebii Notify API"}

# ==============
# Contacts
# ==============
@app.get("/contacts")
def list_contacts(s: Session = Depends(db)):
    rows = s.query(Contact).order_by(Contact.id.desc()).all()
    return [{"id": r.id, "name": r.name, "phone": r.phone} for r in rows]

@app.post("/contacts")
def add_contact(payload: Dict[str, Any], s: Session = Depends(db)):
    name = (payload.get("name") or "").strip()
    phone = e164(payload.get("phone") or "")
    if not name:
        raise HTTPException(422, detail="name is required")

    if s.query(Contact).filter(Contact.phone == phone).first():
        raise HTTPException(409, detail="phone already exists")

    c = Contact(name=name, phone=phone)
    s.add(c)
    s.commit()
    s.refresh(c)
    return {"id": c.id, "name": c.name, "phone": c.phone}

@app.post("/contacts/import")
async def import_contacts(file: UploadFile = File(...), s: Session = Depends(db)):
    """
    CSV with headers: name,phone
    """
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(422, detail="Only CSV allowed")

    data = (await file.read()).decode("utf-8", "ignore")
    reader = csv.DictReader(io.StringIO(data))
    added, skipped = 0, 0
    for row in reader:
        name = (row.get("name") or "").strip()
        phone_raw = (row.get("phone") or "").strip()
        if not name or not phone_raw:
            skipped += 1
            continue
        try:
            phone = e164(phone_raw)
        except HTTPException:
            skipped += 1
            continue

        if s.query(Contact).filter(Contact.phone == phone).first():
            skipped += 1
            continue

        s.add(Contact(name=name, phone=phone))
        added += 1

    s.commit()
    return {"added": added, "skipped": skipped}

# ==============
# Groups
# ==============
@app.get("/groups")
def list_groups(s: Session = Depends(db)):
    rows = s.query(Group).order_by(Group.id.desc()).all()
    return [{"id": g.id, "name": g.name, "members": len(g.members)} for g in rows]

@app.post("/groups")
def create_group(payload: Dict[str, Any], s: Session = Depends(db)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(422, detail="name is required")
    if s.query(Group).filter(Group.name == name).first():
        raise HTTPException(409, detail="group exists")
    g = Group(name=name)
    s.add(g)
    s.commit()
    s.refresh(g)
    return {"id": g.id, "name": g.name}

@app.get("/groups/{gid}/members")
def group_members_list(gid: int, s: Session = Depends(db)):
    g = s.get(Group, gid)
    if not g:
        raise HTTPException(404, detail="group not found")
    return [{"id": m.id, "name": m.name, "phone": m.phone} for m in g.members]

@app.post("/groups/add")
def group_add_members(payload: Dict[str, Any], s: Session = Depends(db)):
    gid = payload.get("group_id")
    phones: List[str] = payload.get("phones") or []
    if not gid or not phones:
        raise HTTPException(422, detail="group_id and phones required")

    g = s.get(Group, gid)
    if not g:
        raise HTTPException(404, detail="group not found")

    attached, missing = 0, 0
    for p in phones:
        try:
            phone = e164(p)
        except HTTPException:
            missing += 1
            continue
        c = s.query(Contact).filter(Contact.phone == phone).first()
        if not c:
            missing += 1
            continue
        if c not in g.members:
            g.members.append(c)
            attached += 1

    s.commit()
    return {"attached": attached, "missing": missing}

# ==========================
# WhatsApp: helpers & calls
# ==========================
def _wa_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json",
    }

def _wa_send_text(to: str, text: str) -> Dict[str, Any]:
    require_meta()
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    r = requests.post(f"{META_BASE}/messages", headers=_wa_headers(), json=body, timeout=30)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, detail=r.text)
    return r.json()

# ==============
# Send endpoints
# ==============
@app.get("/templates")
def list_templates():
    # You can expand this later to live-list from Meta if needed.
    return [{"name": "hello_world", "lang": "en_US"}]

@app.post("/send/text")
def send_text(payload: Dict[str, Any]):
    to = e164(payload.get("to") or "")
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(422, detail="text is required")
    return _wa_send_text(to, text)

@app.post("/send/template")
def send_template(payload: Dict[str, Any]):
    require_meta()
    to = e164(payload.get("to") or "")
    template = (payload.get("template") or "").strip()
    lang = (payload.get("lang") or "en_US").strip()
    components = payload.get("components") or []

    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": template, "language": {"code": lang}},
    }
    if components:
        body["template"]["components"] = components

    r = requests.post(f"{META_BASE}/messages", headers=_wa_headers(), json=body, timeout=30)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, detail=r.text)
    return r.json()

@app.post("/media/upload")
async def media_upload(file: UploadFile = File(...)):
    require_meta()
    # WhatsApp expects multipart to /media
    files = {"file": (file.filename, await file.read(), file.content_type or "application/octet-stream")}
    data = {"messaging_product": "whatsapp"}
    r = requests.post(f"{META_BASE}/media", headers={"Authorization": f"Bearer {WA_TOKEN}"}, files=files, data=data, timeout=60)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, detail=r.text)
    return r.json()

@app.post("/send/media")
def send_media(payload: Dict[str, Any]):
    require_meta()
    to = e164(payload.get("to") or "")
    media_id = (payload.get("media_id") or "").strip()
    caption = (payload.get("caption") or "").strip()

    if not media_id:
        raise HTTPException(422, detail="media_id is required")

    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {"id": media_id, **({"caption": caption} if caption else {})},
    }
    r = requests.post(f"{META_BASE}/messages", headers=_wa_headers(), json=body, timeout=30)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, detail=r.text)
    return r.json()

@app.post("/bulk/preview")
def bulk_preview(payload: Dict[str, Any], s: Session = Depends(db)):
    """
    payload = { "group_id": 1, "mode": "text"|"template", ... }
    """
    gid = payload.get("group_id")
    g = s.get(Group, gid) if gid else None
    if not g:
        raise HTTPException(404, detail="group not found")
    return {"count": len(g.members), "phones": [m.phone for m in g.members]}

@app.post("/send/bulk")
def send_bulk(payload: Dict[str, Any], s: Session = Depends(db)):
    require_meta()
    gid = payload.get("group_id")
    mode = (payload.get("mode") or "text").strip()

    g = s.get(Group, gid) if gid else None
    if not g:
        raise HTTPException(404, detail="group not found")

    results = []
    if mode == "text":
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(422, detail="text is required for text mode")
        for m in g.members:
            try:
                results.append({"to": m.phone, "result": _wa_send_text(m.phone, text)})
            except HTTPException as e:
                results.append({"to": m.phone, "error": e.detail})
    elif mode == "template":
        template = (payload.get("template") or "").strip()
        lang = (payload.get("lang") or "en_US").strip()
        components = payload.get("components") or []
        for m in g.members:
            body = {
                "messaging_product": "whatsapp",
                "to": m.phone,
                "type": "template",
                "template": {"name": template, "language": {"code": lang}},
            }
            if components:
                body["template"]["components"] = components
            r = requests.post(f"{META_BASE}/messages", headers=_wa_headers(), json=body, timeout=30)
            if r.status_code >= 400:
                results.append({"to": m.phone, "error": r.text})
            else:
                results.append({"to": m.phone, "result": r.json()})
    else:
        raise HTTPException(422, detail="mode must be text or template")

    return {"sent": len(results), "results": results}
