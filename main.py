# main.py
import os, io, csv, uuid, requests
from typing import List, Optional, Any, Dict
from fastapi import (
    FastAPI, UploadFile, File, Header, Depends, HTTPException, status
)
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine, Column, Integer, String, ForeignKey, Text
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session
from fastapi.security.api_key import APIKeyHeader

# --------------------------------------------------------------------------------------
# ENV & CONSTANTS
# --------------------------------------------------------------------------------------
API_KEY            = os.getenv("API_KEY", "")
WA_TOKEN           = os.getenv("WA_TOKEN", "")
WA_PHONE_ID        = os.getenv("WA_PHONE_ID", "")
META_VERIFY_TOKEN  = os.getenv("META_VERIFY_TOKEN", "")
META_BASE          = f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}"

if not WA_TOKEN or not WA_PHONE_ID:
    print("WARNING: WA_TOKEN or WA_PHONE_ID not set; WhatsApp calls will fail.")

# --------------------------------------------------------------------------------------
# APP & SECURITY (global API key enforcement)
# --------------------------------------------------------------------------------------
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def enforce_api_key(x_api_key: Optional[str] = Depends(api_key_header)):
    if not (API_KEY and x_api_key == API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")

app = FastAPI(
    title="Eebii Notify API",
    description="Eebii WhatsApp Notification API",
    version="0.1.0",
    dependencies=[Depends(enforce_api_key)]  # all routes protected by default
)

# --------------------------------------------------------------------------------------
# DB (SQLite)
# --------------------------------------------------------------------------------------
Base = declarative_base()
engine = create_engine("sqlite:///./eebii.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Contact(Base):
    __tablename__ = "contacts"
    id      = Column(Integer, primary_key=True)
    name    = Column(String(200))
    phone   = Column(String(32), unique=True, index=True)

class Group(Base):
    __tablename__ = "groups"
    id      = Column(Integer, primary_key=True)
    name    = Column(String(200), unique=True)

class GroupMember(Base):
    __tablename__ = "group_members"
    id        = Column(Integer, primary_key=True)
    group_id  = Column(Integer, ForeignKey("groups.id"))
    contact_id= Column(Integer, ForeignKey("contacts.id"))
    group     = relationship("Group", backref="members")
    contact   = relationship("Contact")

class StoredMedia(Base):
    __tablename__ = "media"
    id        = Column(Integer, primary_key=True)
    media_id  = Column(String(128), index=True)  # WhatsApp media id
    media_type= Column(String(32))               # image|video|audio|document
    caption   = Column(Text, nullable=True)

Base.metadata.create_all(bind=engine)

def db() -> Session:
    dbs = SessionLocal()
    try:
        yield dbs
    finally:
        dbs.close()

# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------
class AddContact(BaseModel):
    name: str
    phone: str

class ImportContactsResp(BaseModel):
    added: int
    duplicates: int

class CreateGroupReq(BaseModel):
    name: str

class AddMembersReq(BaseModel):
    group_id: int
    phones: List[str]

class SendTextReq(BaseModel):
    to: str
    text: str

class TemplateComponentParam(BaseModel):
    type: str = "text"
    text: Optional[str] = None

class TemplateComponent(BaseModel):
    type: str = "body"
    parameters: Optional[List[TemplateComponentParam]] = None

class SendTemplateReq(BaseModel):
    to: str
    template_name: str
    language: str = "en_US"
    components: Optional[List[TemplateComponent]] = None

class SendMediaReq(BaseModel):
    to: str
    media_type: str  # image|video|audio|document
    media_id: str
    caption: Optional[str] = None

class BulkRecipient(BaseModel):
    to: str
    text: Optional[str] = None
    # You can extend for template/media per-recipient if needed

class BulkReq(BaseModel):
    mode: str = Field(..., description="text|template|media")
    recipients: Optional[List[BulkRecipient]] = None
    group_id: Optional[int] = None
    # template fields
    template_name: Optional[str] = None
    language: Optional[str] = "en_US"
    components: Optional[List[TemplateComponent]] = None
    # media fields
    media_type: Optional[str] = None
    media_id: Optional[str] = None
    caption: Optional[str] = None

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def wa_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json"
    }

def send_wa_text(to: str, text: str) -> Dict[str, Any]:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text}
    }
    r = requests.post(f"{META_BASE}/messages", headers=wa_headers(), json=payload, timeout=30)
    return {"status_code": r.status_code, "body": r.json() if r.content else None}

def send_wa_template(to: str, template_name: str, language: str, components: Optional[List[Dict]] = None) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language}
        }
    }
    if components:
        # Convert Pydantic to plain dict if needed
        comp = [c if isinstance(c, dict) else c.dict(exclude_none=True) for c in components]
        data["template"]["components"] = comp
    r = requests.post(f"{META_BASE}/messages", headers=wa_headers(), json=data, timeout=30)
    return {"status_code": r.status_code, "body": r.json() if r.content else None}

def send_wa_media(to: str, media_type: str, media_id: str, caption: Optional[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": media_type,
        media_type: {"id": media_id}
    }
    if caption and media_type in ("image", "video", "document"):
        payload[media_type]["caption"] = caption
    r = requests.post(f"{META_BASE}/messages", headers=wa_headers(), json=payload, timeout=30)
    return {"status_code": r.status_code, "body": r.json() if r.content else None}

# --------------------------------------------------------------------------------------
# Public root (still requires API key due to global dependency)
# --------------------------------------------------------------------------------------
@app.get("/")
def root():
    return {"service": "eebii-notify", "ok": True}

# --------------------------------------------------------------------------------------
# Contacts
# --------------------------------------------------------------------------------------
@app.get("/contacts")
def list_contacts(dbs: Session = Depends(db)):
    rows = dbs.query(Contact).order_by(Contact.id.desc()).all()
    return [{"id": c.id, "name": c.name, "phone": c.phone} for c in rows]

@app.post("/contacts")
def add_contact(req: AddContact, dbs: Session = Depends(db)):
    exists = dbs.query(Contact).filter(Contact.phone == req.phone).first()
    if exists:
        return {"id": exists.id, "name": exists.name, "phone": exists.phone, "duplicate": True}
    row = Contact(name=req.name, phone=req.phone)
    dbs.add(row)
    dbs.commit()
    return {"id": row.id, "name": row.name, "phone": row.phone}

@app.post("/contacts/import", response_model=ImportContactsResp)
async def import_contacts(file: UploadFile = File(...), dbs: Session = Depends(db)):
    content = await file.read()
    added = 0
    dup = 0
    reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
    for rec in reader:
        name = rec.get("name") or rec.get("Name") or ""
        phone = rec.get("phone") or rec.get("Phone") or ""
        if not phone:
            continue
        if dbs.query(Contact).filter(Contact.phone == phone).first():
            dup += 1
            continue
        dbs.add(Contact(name=name, phone=phone))
        added += 1
    dbs.commit()
    return ImportContactsResp(added=added, duplicates=dup)

# --------------------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------------------
@app.get("/groups")
def list_groups(dbs: Session = Depends(db)):
    rows = dbs.query(Group).all()
    return [{"id": g.id, "name": g.name} for g in rows]

@app.post("/groups")
def create_group(req: CreateGroupReq, dbs: Session = Depends(db)):
    if dbs.query(Group).filter(Group.name == req.name).first():
        raise HTTPException(400, "Group name exists")
    g = Group(name=req.name)
    dbs.add(g)
    dbs.commit()
    return {"id": g.id, "name": g.name}

@app.post("/groups/add")
def add_members(req: AddMembersReq, dbs: Session = Depends(db)):
    g = dbs.query(Group).get(req.group_id)
    if not g:
        raise HTTPException(404, "Group not found")
    added = 0
    for phone in req.phones:
        c = dbs.query(Contact).filter(Contact.phone == phone).first()
        if not c:
            c = Contact(name=phone, phone=phone)
            dbs.add(c); dbs.flush()
        exists = dbs.query(GroupMember).filter(
            GroupMember.group_id == g.id, GroupMember.contact_id == c.id
        ).first()
        if not exists:
            dbs.add(GroupMember(group_id=g.id, contact_id=c.id)); added += 1
    dbs.commit()
    return {"group": g.id, "added": added}

@app.get("/groups/{gid}/members")
def group_members(gid: int, dbs: Session = Depends(db)):
    g = dbs.query(Group).get(gid)
    if not g:
        raise HTTPException(404, "Group not found")
    members = dbs.query(GroupMember).filter(GroupMember.group_id == g.id).all()
    out = []
    for m in members:
        c = dbs.query(Contact).get(m.contact_id)
        if c:
            out.append({"id": c.id, "name": c.name, "phone": c.phone})
    return out

# --------------------------------------------------------------------------------------
# Templates (list current templates from Meta)
# --------------------------------------------------------------------------------------
@app.get("/templates")
def list_templates():
    url = f"https://graph.facebook.com/v20.0/{os.getenv('WABA_ID','')}/message_templates"
    # WABA_ID is optional; if you don't have it set, return basic info
    if not os.getenv("WABA_ID"):
        return {"note": "Set WABA_ID to fetch templates list from Meta"}
    r = requests.get(url, headers={"Authorization": f"Bearer {WA_TOKEN}"}, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code, "raw": r.text}

# --------------------------------------------------------------------------------------
# Send: text / template / media
# --------------------------------------------------------------------------------------
@app.post("/send/text")
def send_text(req: SendTextReq):
    if not req.to or not req.text:
        raise HTTPException(400, "to and text are required")
    res = send_wa_text(req.to, req.text)
    return res

@app.post("/send/template")
def send_template(req: SendTemplateReq):
    if not (req.to and req.template_name and req.language):
        raise HTTPException(400, "to, template_name, language required")
    comps = [c.dict(exclude_none=True) for c in (req.components or [])]
    res = send_wa_template(req.to, req.template_name, req.language, comps or None)
    return res

@app.post("/media/upload")
async def media_upload(file: UploadFile = File(...), media_type: str = "document"):
    # Upload media binary to Meta to get media_id
    files = {"file": (file.filename, await file.read(), file.content_type)}
    params = {"messaging_product": "whatsapp"}
    headers = {"Authorization": f"Bearer {WA_TOKEN}"}
    url = f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}/media"
    r = requests.post(url, headers=headers, files=files, data=params, timeout=60)
    j = r.json() if r.content else {}
    if r.status_code != 200:
        raise HTTPException(r.status_code, j)
    mid = j.get("id")
    # Store locally for re-use if you want
    SessionLocal().add(StoredMedia(media_id=mid, media_type=media_type, caption=None))
    SessionLocal().commit()
    return {"media_id": mid, "status": "uploaded"}

@app.post("/send/media")
def send_media(req: SendMediaReq):
    if not (req.to and req.media_type and req.media_id):
        raise HTTPException(400, "to, media_type, media_id required")
    res = send_wa_media(req.to, req.media_type, req.media_id, req.caption)
    return res

# --------------------------------------------------------------------------------------
# Bulk preview & send
# --------------------------------------------------------------------------------------
@app.post("/bulk/preview")
def bulk_preview(req: BulkReq, dbs: Session = Depends(db)):
    # Return expanded list of recipients that will be messaged
    targets: List[str] = []
    if req.recipients:
        targets = [r.to for r in req.recipients]
    elif req.group_id:
        members = dbs.query(GroupMember).filter(GroupMember.group_id == req.group_id).all()
        for m in members:
            c = dbs.query(Contact).get(m.contact_id)
            if c: targets.append(c.phone)
    return {
        "mode": req.mode,
        "count": len(targets),
        "targets": targets
    }

@app.post("/send/bulk")
def send_bulk(req: BulkReq, dbs: Session = Depends(db)):
    results = []
    # Build target list
    targets: List[BulkRecipient] = []
    if req.recipients:
        targets = req.recipients
    elif req.group_id:
        members = dbs.query(GroupMember).filter(GroupMember.group_id == req.group_id).all()
        for m in members:
            c = dbs.query(Contact).get(m.contact_id)
            if c:
                targets.append(BulkRecipient(to=c.phone, text=req.caption or ""))  # text placeholder
    else:
        raise HTTPException(400, "Provide recipients or group_id")

    # Send by mode
    for r in targets:
        if req.mode == "text":
            if not r.text:
                raise HTTPException(400, "text required in recipients for text mode")
            results.append({"to": r.to, **send_wa_text(r.to, r.text)})
        elif req.mode == "template":
            if not req.template_name:
                raise HTTPException(400, "template_name required")
            comps = [c.dict(exclude_none=True) for c in (req.components or [])]
            results.append({"to": r.to, **send_wa_template(r.to, req.template_name, req.language or "en_US", comps or None)})
        elif req.mode == "media":
            if not (req.media_type and req.media_id):
                raise HTTPException(400, "media_type and media_id required")
            results.append({"to": r.to, **send_wa_media(r.to, req.media_type, req.media_id, req.caption)})
        else:
            raise HTTPException(400, "mode must be text|template|media")

    return {"sent": len(results), "results": results}

# --------------------------------------------------------------------------------------
# Webhook (open – NO API key)
# --------------------------------------------------------------------------------------
@app.get("/webhook/whatsapp", dependencies=[])  # must be open for Meta verify
def webhook_verify(mode: str, challenge: str, verify_token: str):
    if verify_token != META_VERIFY_TOKEN:
        raise HTTPException(403, "Bad verify token")
    return PlainTextResponse(content=challenge)

@app.post("/webhook/whatsapp", dependencies=[])  # receive messages/status
async def webhook_receive(payload: Dict[str, Any]):
    # You can log or process here
    return JSONResponse({"received": True})
