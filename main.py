# main.py
from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import JSONResponse
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, validator
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Table
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session
import requests
import csv, io, os

# =========================
# Config & Auth
# =========================

API_KEY = os.getenv("API_KEY", "").strip()
WA_TOKEN = os.getenv("WA_TOKEN", "").strip()
WA_PHONE_ID = os.getenv("WA_PHONE_ID", "").strip()
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "").strip()
META_BASE = f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}"

def _require_wh_env():
    if not WA_TOKEN or not WA_PHONE_ID:
        raise HTTPException(status_code=500, detail="WhatsApp env not configured")

async def enforce_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    if not API_KEY or not x_api_key or x_api_key.strip() != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
from fastapi.security.api_key import APIKeyHeader
from fastapi.openapi.models import APIKey, APIKeyIn
from fastapi.openapi.utils import get_openapi

api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)

app = FastAPI(
    title="Eebii Notify API",
    description="Eebii WhatsApp Notification API",
    version="0.1.0",
    dependencies=[Depends(enforce_api_key)],
)

# 👇 Add OpenAPI override for "Authorize" button
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    openapi_schema["components"]["securitySchemes"] = {
        "apiKey": {
            "type": "apiKey",
            "name": "X-API-Key",
            "in": "header"
        }
    }
    openapi_schema["security"] = [{"apiKey": []}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi

app = FastAPI(
    title="Eebii Notify API",
    description="Eebii WhatsApp Notification API",
    version="0.1.0",
    dependencies=[Depends(enforce_api_key)],
)

# =========================
# Database (SQLite)
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
    id = Column(Integer, primary_key=True)
    name = Column(String(200))
    phone = Column(String(32), unique=True, index=True)

class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), unique=True, index=True)
    members = relationship("Contact", secondary=group_members, backref="groups")

Base.metadata.create_all(bind=engine)

def db() -> Session:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()

# =========================
# Pydantic Schemas
# =========================

class ContactIn(BaseModel):
    name: str = Field(..., example="Abhilash")
    phone: str = Field(..., example="+919633511519")

    @validator("phone")
    def phone_in_e164(cls, v):
        v = v.strip()
        if not v.startswith("+") or not v[1:].isdigit():
            raise ValueError("phone must be E.164 format like +919999999999")
        return v

class ContactList(BaseModel):
    id: int
    name: str
    phone: str

class GroupCreate(BaseModel):
    name: str = Field(..., example="Customers")

class GroupAddMembers(BaseModel):
    group_id: int
    phones: List[str]

class TextSend(BaseModel):
    to: str
    text: str = Field(..., example="Hello from Eebii!")

    @validator("to")
    def to_e164(cls, v):
        v = v.strip()
        if not v.startswith("+") or not v[1:].isdigit():
            raise ValueError("to must be E.164 format like +91xxxxxxxxxx")
        return v

class TemplateComponent(BaseModel):
    type: str = Field(..., example="body")
    parameters: List[Dict[str, Any]] = Field(default_factory=list)

class TemplateSend(BaseModel):
    to: str
    name: str = Field(..., example="hello_world")
    language: str = Field("en_US", example="en_US")
    components: Optional[List[TemplateComponent]] = None

    @validator("to")
    def to_e164(cls, v):
        v = v.strip()
        if not v.startswith("+") or not v[1:].isdigit():
            raise ValueError("to must be E.164 format like +91xxxxxxxxxx")
        return v

class MediaSend(BaseModel):
    to: str
    media_id: str
    caption: Optional[str] = None

    @validator("to")
    def to_e164(cls, v):
        v = v.strip()
        if not v.startswith("+") or not v[1:].isdigit():
            raise ValueError("to must be E.164 format like +91xxxxxxxxxx")
        return v

class BulkItem(BaseModel):
    to: str
    type: str  # "text" | "template" | "media"
    text: Optional[str] = None
    template: Optional[TemplateSend] = None
    media: Optional[MediaSend] = None

class BulkPayload(BaseModel):
    items: List[BulkItem]

# =========================
# Helpers
# =========================

def wa_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json",
    }

def wa_post(path: str, payload: Dict[str, Any]) -> requests.Response:
    _require_wh_env()
    url = f"{META_BASE}{path}"
    return requests.post(url, headers=wa_headers(), json=payload, timeout=30)

# =========================
# Root / Health
# =========================

@app.get("/")
def root():
    return {
        "service": "eebii-notify",
        "version": "0.1.0",
        "whatsapp_ready": bool(WA_TOKEN and WA_PHONE_ID),
    }

# =========================
# Contacts
# =========================

@app.get("/contacts", response_model=List[ContactList])
def list_contacts(session: Session = Depends(db)):
    rows = session.query(Contact).order_by(Contact.id.desc()).all()
    return [ContactList(id=r.id, name=r.name, phone=r.phone) for r in rows]

@app.post("/contacts", response_model=ContactList)
def add_contact(payload: ContactIn, session: Session = Depends(db)):
    exists = session.query(Contact).filter(Contact.phone == payload.phone).first()
    if exists:
        raise HTTPException(status_code=409, detail="Contact phone already exists")
    row = Contact(name=payload.name, phone=payload.phone)
    session.add(row)
    session.commit()
    session.refresh(row)
    return ContactList(id=row.id, name=row.name, phone=row.phone)

@app.post("/contacts/import")
async def import_contacts(file: UploadFile = File(...), session: Session = Depends(db)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Upload a CSV file")
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
    added, skipped = 0, 0
    for row in reader:
        name = (row.get("name") or "").strip()
        phone = (row.get("phone") or "").strip()
        if not name or not phone:
            skipped += 1
            continue
        if not (phone.startswith("+") and phone[1:].isdigit()):
            skipped += 1
            continue
        if session.query(Contact).filter(Contact.phone == phone).first():
            skipped += 1
            continue
        session.add(Contact(name=name, phone=phone))
        added += 1
    session.commit()
    return {"added": added, "skipped": skipped}

# =========================
# Groups
# =========================

@app.get("/groups")
def list_groups(session: Session = Depends(db)):
    groups = session.query(Group).order_by(Group.id.desc()).all()
    return [{"id": g.id, "name": g.name, "members": len(g.members)} for g in groups]

@app.post("/groups")
def create_group(payload: GroupCreate, session: Session = Depends(db)):
    if session.query(Group).filter(Group.name == payload.name).first():
        raise HTTPException(status_code=409, detail="Group name already exists")
    g = Group(name=payload.name)
    session.add(g)
    session.commit()
    session.refresh(g)
    return {"id": g.id, "name": g.name}

@app.get("/groups/{gid}/members")
def group_members_list(gid: int, session: Session = Depends(db)):
    g = session.query(Group).get(gid)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    return [{"id": c.id, "name": c.name, "phone": c.phone} for c in g.members]

@app.post("/groups/add")
def add_members(payload: GroupAddMembers, session: Session = Depends(db)):
    g = session.query(Group).get(payload.group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    added = 0
    for p in payload.phones:
        p = p.strip()
        if not (p.startswith("+") and p[1:].isdigit()):
            continue
        c = session.query(Contact).filter(Contact.phone == p).first()
        if not c:
            c = Contact(name=p, phone=p)
            session.add(c)
            session.flush()
        if c not in g.members:
            g.members.append(c)
            added += 1
    session.commit()
    return {"group_id": g.id, "added": added, "members": len(g.members)}

# =========================
# WhatsApp Sending
# =========================

@app.post("/send/text")
def send_text(payload: TextSend):
    _require_wh_env()
    body = {
        "messaging_product": "whatsapp",
        "to": payload.to.replace("+", ""),
        "type": "text",
        "text": {"preview_url": False, "body": payload.text},
    }
    r = wa_post("/messages", body)
    if r.status_code >= 400:
        return JSONResponse(status_code=r.status_code, content=r.json())
    return r.json()

@app.post("/send/template")
def send_template(payload: TemplateSend):
    _require_wh_env()
    tpl: Dict[str, Any] = {
        "name": payload.name,
        "language": {"code": payload.language},
    }
    if payload.components:
        tpl["components"] = [c.dict() for c in payload.components]
    body = {
        "messaging_product": "whatsapp",
        "to": payload.to.replace("+", ""),
        "type": "template",
        "template": tpl,
    }
    r = wa_post("/messages", body)
    if r.status_code >= 400:
        return JSONResponse(status_code=r.status_code, content=r.json())
    return r.json()

@app.post("/media/upload")
async def media_upload(file: UploadFile = File(...)):
    _require_wh_env()
    # Upload media to Meta
    url = f"{META_BASE}/media"
    headers = {"Authorization": f"Bearer {WA_TOKEN}"}
    files = {"file": (file.filename, await file.read(), file.content_type)}
    data = {"messaging_product": "whatsapp"}
    rr = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    if rr.status_code >= 400:
        return JSONResponse(status_code=rr.status_code, content=rr.json())
    return rr.json()  # { "id": "<media_id>" }

@app.post("/send/media")
def send_media(payload: MediaSend):
    _require_wh_env()
    body = {
        "messaging_product": "whatsapp",
        "to": payload.to.replace("+", ""),
        "type": "image",
        "image": {"id": payload.media_id},
    }
    if payload.caption:
        body["image"]["caption"] = payload.caption
    r = wa_post("/messages", body)
    if r.status_code >= 400:
        return JSONResponse(status_code=r.status_code, content=r.json())
    return r.json()

# =========================
# Bulk
# =========================

@app.post("/bulk/preview")
def bulk_preview(payload: BulkPayload):
    count = len(payload.items or [])
    types = {i.type for i in payload.items}
    return {"items": count, "types": sorted(list(types))}

@app.post("/send/bulk")
def send_bulk(payload: BulkPayload):
    results = []
    for item in payload.items:
        try:
            if item.type == "text" and item.text and item.to:
                res = send_text(TextSend(to=item.to, text=item.text))
            elif item.type == "template" and item.template:
                res = send_template(item.template)
            elif item.type == "media" and item.media:
                res = send_media(item.media)
            else:
                res = {"error": "invalid bulk item"}
        except HTTPException as e:
            res = {"status_code": e.status_code, "detail": e.detail}
        results.append(res)
    return {"results": results}

# =========================
# Webhooks
# =========================

@app.get("/webhook/whatsapp")
def verify_whatsapp(mode: Optional[str] = None,
                    challenge: Optional[str] = None,
                    verify_token: Optional[str] = Header(None, alias="hub.verify_token"),
                    hub_mode: Optional[str] = Header(None, alias="hub.mode"),
                    hub_challenge: Optional[str] = Header(None, alias="hub.challenge"),
                    q_mode: Optional[str] = None,
                    q_token: Optional[str] = None,
                    q_challenge: Optional[str] = None):
    # Meta calls with query params: hub.mode, hub.verify_token, hub.challenge
    # FastAPI header tricks can be flaky here; use request.query_params usually.
    # Simpler: pull from query via dependency injection is verbose,
    # so we’ll accept both styles by reading the environment token only.
    from fastapi import Request
    # Re-parse query:
    # (workaround due to some gateways)
    return JSONResponse(content={"detail": "Use query params"}, status_code=200)

from fastapi import Request
@app.get("/webhook/whatsapp")
async def webhook_verify(request: Request):
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        return JSONResponse(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Forbidden")

@app.post("/webhook/whatsapp")
async def webhook_receive(data: Dict[str, Any]):
    # For now, just echo what Meta sends (message status, inbound, etc.)
    return {"received": True}

# =========================
# Run (local)
# =========================

# Local dev: uvicorn main:app --reload --port 8000
