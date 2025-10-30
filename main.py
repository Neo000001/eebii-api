# main.py
from fastapi import FastAPI, HTTPException, UploadFile, File, Header
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from typing import Optional
import requests, os, csv, io

# ---------------------------
# FastAPI & Config
# ---------------------------
app = FastAPI(title="Eebii Notify API")
# ======================
# Enable Authorize button in Swagger
# ======================
from fastapi.openapi.utils import get_openapi

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version="0.1.0",
        routes=app.routes,
        description="Eebii WhatsApp Notification API"
    )
    openapi_schema.setdefault("components", {}).setdefault("securitySchemes", {})
    openapi_schema["components"]["securitySchemes"]["APIKeyHeader"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "Enter your Eebii Notify API Key here"
    }
    openapi_schema["security"] = [{"APIKeyHeader": []}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi


API_KEY      = os.getenv("API_KEY", "")
WA_TOKEN     = os.getenv("WA_TOKEN", "")
WA_PHONE_ID  = os.getenv("WA_PHONE_ID", "")
if not (WA_TOKEN and WA_PHONE_ID):
    print("WARNING: WA_TOKEN / WA_PHONE_ID not set. WhatsApp calls will fail.")
META_BASE = f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}"

def check_key(key: str | None):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# ---------------------------
# Database (SQLite)
# ---------------------------
engine = create_engine("sqlite:///eebii.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class Contact(Base):
    __tablename__ = "contacts"
    id    = Column(Integer, primary_key=True)
    name  = Column(String, nullable=False)
    phone = Column(String, nullable=False)

class Group(Base):
    __tablename__ = "groups"
    id    = Column(Integer, primary_key=True)
    name  = Column(String, nullable=False)
    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan")

class GroupMember(Base):
    __tablename__ = "group_members"
    id         = Column(Integer, primary_key=True)
    group_id   = Column(Integer, ForeignKey("groups.id"))
    contact_id = Column(Integer, ForeignKey("contacts.id"))
    group      = relationship("Group", back_populates="members")

Base.metadata.create_all(engine)

def db_open():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------------------
# Root / Health
# ---------------------------
@app.get("/")
def root():
    return {"ok": True, "service": "Eebii Notify API"}

# ---------------------------
# Contacts
# ---------------------------
@app.get("/contacts")
def list_contacts(x_api_key: str = Header(None)):
    check_key(x_api_key)
    db = next(db_open())
    rows = db.query(Contact).all()
    return [{"id": c.id, "name": c.name, "phone": c.phone} for c in rows]

@app.post("/contacts")
def add_contact(body: dict, x_api_key: str = Header(None)):
    check_key(x_api_key)
    name  = (body.get("name")  or "").strip()
    phone = (body.get("phone") or "").strip()
    if not name or not phone:
        raise HTTPException(400, "name and phone are required")
    db = next(db_open())
    c = Contact(name=name, phone=phone)
    db.add(c); db.commit(); db.refresh(c)
    return {"id": c.id, "name": c.name, "phone": c.phone}

@app.post("/contacts/import")
def import_contacts(file: UploadFile = File(...), x_api_key: str = Header(None)):
    """
    CSV headers: name,phone,group   (group is optional)
    Creates contacts; if 'group' provided, creates/links group membership.
    """
    check_key(x_api_key)
    content = file.file.read().decode("utf-8", errors="ignore")
    reader  = csv.DictReader(io.StringIO(content))
    have = {h.strip().lower() for h in (reader.fieldnames or [])}
    if not {"name","phone"}.issubset(have):
        raise HTTPException(400, "CSV must have headers: name,phone (optional: group)")

    db = next(db_open())
    added = grouped = 0
    errors = []
    group_cache: dict[str,int] = {}

    for i, row in enumerate(reader, start=2):
        try:
            name  = (row.get("name")  or "").strip()
            phone = (row.get("phone") or "").strip()
            gname = (row.get("group") or "").strip()
            if not name or not phone:
                errors.append({"line": i, "error": "missing name/phone"})
                continue
            c = Contact(name=name, phone=phone)
            db.add(c); db.commit(); db.refresh(c)
            added += 1

            if gname:
                gid = group_cache.get(gname)
                if gid is None:
                    g = db.query(Group).filter(Group.name == gname).first()
                    if not g:
                        g = Group(name=gname); db.add(g); db.commit(); db.refresh(g)
                    gid = g.id; group_cache[gname] = gid
                db.add(GroupMember(group_id=gid, contact_id=c.id)); db.commit()
                grouped += 1
        except Exception as e:
            db.rollback()
            errors.append({"line": i, "error": str(e)})

    return {"added_contacts": added, "group_links_created": grouped, "errors": errors}

# ---------------------------
# Groups
# ---------------------------
@app.get("/groups")
def list_groups(x_api_key: str = Header(None)):
    check_key(x_api_key)
    db = next(db_open())
    rows = db.query(Group).all()
    return [{"id": g.id, "name": g.name} for g in rows]

@app.post("/groups")
def create_group(body: dict, x_api_key: str = Header(None)):
    check_key(x_api_key)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    db = next(db_open())
    g = Group(name=name)
    db.add(g); db.commit(); db.refresh(g)
    return {"id": g.id, "name": g.name}

@app.post("/groups/add")
def add_members(body: dict, x_api_key: str = Header(None)):
    check_key(x_api_key)
    gid = body.get("group_id")
    ids = body.get("contact_ids") or []
    if not gid or not ids:
        raise HTTPException(400, "group_id and contact_ids are required")
    db = next(db_open())
    for cid in ids:
        db.add(GroupMember(group_id=gid, contact_id=int(cid)))
    db.commit()
    return {"ok": True, "linked": len(ids)}

@app.get("/groups/{gid}/members")
def group_members(gid: int, x_api_key: str = Header(None)):
    check_key(x_api_key)
    db = next(db_open())
    m = db.query(GroupMember).filter(GroupMember.group_id == gid).all()
    out = []
    for row in m:
        c = db.query(Contact).get(row.contact_id)
        if c: out.append({"id": c.id, "name": c.name, "phone": c.phone})
    return out

# ---------------------------
# WhatsApp helpers
# ---------------------------
def wa_post(path: str, payload=None, files=None, data=None):
    url = f"{META_BASE}/{path.lstrip('/')}"
    hdr = {"Authorization": f"Bearer {WA_TOKEN}"}
    return requests.post(url, headers=hdr, json=payload, files=files, data=data)

# ---------------------------
# Templates
# ---------------------------
@app.get("/templates")
def list_templates(x_api_key: str = Header(None)):
    check_key(x_api_key)
    url = f"{META_BASE}/message_templates"
    r = requests.get(url, headers={"Authorization": f"Bearer {WA_TOKEN}"}, params={"limit": 100})
    return r.json()

# ---------------------------
# Single send
# ---------------------------
@app.post("/send/text")
def send_text(body: dict, x_api_key: str = Header(None)):
    check_key(x_api_key)
    to   = (body.get("to")   or "").strip()
    text = (body.get("text") or "").strip()
    if not to or not text:
        raise HTTPException(400, "to and text are required")
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":text}}
    r = wa_post("messages", payload=payload)
    return JSONResponse(r.json(), status_code=r.status_code)

@app.post("/send/template")
def send_template(body: dict, x_api_key: str = Header(None)):
    check_key(x_api_key)
    to   = (body.get("to") or "").strip()
    name = (body.get("template_name") or "").strip()
    lang = (body.get("language") or "en_US").strip()
    if not to or not name:
        raise HTTPException(400, "to and template_name are required")
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": name, "language": {"code": lang}}
    }
    if body.get("components"):
        payload["template"]["components"] = body["components"]
    r = wa_post("messages", payload=payload)
    return JSONResponse(r.json(), status_code=r.status_code)

@app.post("/media/upload")
def media_upload(file: UploadFile = File(...), x_api_key: str = Header(None)):
    check_key(x_api_key)
    files = {"file": (file.filename, file.file, file.content_type)}
    data  = {"messaging_product": "whatsapp"}
    r = wa_post("media", files=files, data=data)
    return JSONResponse(r.json(), status_code=r.status_code)

@app.post("/send/media")
def send_media(body: dict, x_api_key: str = Header(None)):
    check_key(x_api_key)
    to   = (body.get("to") or "").strip()
    mid  = (body.get("media_id") or "").strip()
    mtyp = (body.get("media_type") or "document").strip()  # image|video|audio|document
    if not to or not mid:
        raise HTTPException(400, "to and media_id are required")
    content = {"id": mid}
    cap = body.get("caption")
    if cap: content["caption"] = cap
    payload = {"messaging_product":"whatsapp","to":to,"type":mtyp, mtyp: content}
    r = wa_post("messages", payload=payload)
    return JSONResponse(r.json(), status_code=r.status_code)

# ---------------------------
# Bulk preview & send
# ---------------------------
def _render_template_preview(tname: str, lang: str, components: Optional[dict]) -> str:
    try:
        url = f"{META_BASE}/message_templates"
        res = requests.get(url, headers={"Authorization": f"Bearer {WA_TOKEN}"}, params={"limit": 100})
        data = res.json().get("data", [])
        tmpl = next((t for t in data if t.get("name")==tname), None)
        if not tmpl:
            return f"{tname} ({lang})"
        body_text = ""
        for c in tmpl.get("components", []):
            if c.get("type") == "BODY":
                body_text = c.get("text","")
                break
        if not components or not body_text:
            return body_text or f"{tname} ({lang})"
        # simple {{1}} replacement using provided parameters
        vals = []
        for comp in components.get("body", []):
            for p in comp.get("parameters", []):
                if p.get("type") == "text":
                    vals.append(str(p.get("text","")))
        for idx, val in enumerate(vals, start=1):
            body_text = body_text.replace(f"{{{{{idx}}}}}", val)
        return body_text
    except Exception:
        return f"{tname} ({lang})"

@app.post("/bulk/preview")
def bulk_preview(body: dict, x_api_key: str = Header(None)):
    """
    body:
      group_id, mode: text|template|media
      text / template_name(+language + components) / media_type+media_id(+caption)
    """
    check_key(x_api_key)
    db = next(db_open())
    gid  = body.get("group_id")
    mode = body.get("mode")
    if not gid or not mode:
        raise HTTPException(400, "group_id and mode are required")

    members = db.query(GroupMember).filter(GroupMember.group_id == gid).all()
    phones  = []
    for m in members:
        c = db.query(Contact).get(m.contact_id)
        if c: phones.append(c.phone)

    if mode == "text":
        preview = (body.get("text") or "")
    elif mode == "template":
        preview = _render_template_preview(
            body.get("template_name",""),
            body.get("language","en_US"),
            body.get("components")
        )
    elif mode == "media":
        preview = f"{body.get('media_type','document')} caption: {body.get('caption','')}"
    else:
        raise HTTPException(400, "invalid mode")

    return {"recipients": len(phones), "sample_recipients": phones[:5], "rendered_preview": preview}

@app.post("/send/bulk")
def bulk_send(body: dict, x_api_key: str = Header(None)):
    """
    body:
      group_id, mode: text|template|media
      text / template_name(+language + components) / media_type+media_id(+caption)
    """
    check_key(x_api_key)
    db = next(db_open())
    gid  = body.get("group_id")
    mode = body.get("mode")
    if not gid or not mode:
        raise HTTPException(400, "group_id and mode are required")

    members = db.query(GroupMember).filter(GroupMember.group_id == gid).all()
    results = []

    for m in members:
        c = db.query(Contact).get(m.contact_id)
        if not c: continue

        if mode == "text":
            text = (body.get("text") or "")
            payload = {"messaging_product":"whatsapp","to":c.phone,"type":"text","text":{"body":text}}

        elif mode == "template":
            tname = (body.get("template_name") or "")
            lang  = (body.get("language") or "en_US")
            payload = {
                "messaging_product":"whatsapp",
                "to": c.phone,
                "type":"template",
                "template":{"name":tname,"language":{"code":lang}}
            }
            if body.get("components"):
                payload["template"]["components"] = body["components"]

        elif mode == "media":
            mtyp = (body.get("media_type") or "document")
            mid  = (body.get("media_id") or "")
            content = {"id": mid}
            cap = body.get("caption")
            if cap: content["caption"] = cap
            payload = {"messaging_product":"whatsapp","to":c.phone,"type":mtyp, mtyp:content}

        else:
            raise HTTPException(400, "invalid mode")

        r = wa_post("messages", payload=payload)
        results.append({"to": c.phone, "status": r.status_code})

    return {"sent": len(results), "results": results}
