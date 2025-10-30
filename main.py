from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Header
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
import requests, os

# --- FastAPI Setup ---
app = FastAPI(title="Eebii Notify API")

API_KEY = os.getenv("API_KEY", "")
WA_TOKEN = os.getenv("WA_TOKEN", "")
WA_PHONE_ID = os.getenv("WA_PHONE_ID", "")
META_URL = f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}"

# --- Database Setup ---
engine = create_engine("sqlite:///eebii.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    phone = Column(String)

class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    members = relationship("GroupMember", back_populates="group")

class GroupMember(Base):
    __tablename__ = "group_members"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"))
    contact_id = Column(Integer, ForeignKey("contacts.id"))
    group = relationship("Group", back_populates="members")

Base.metadata.create_all(engine)

# --- Helper ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def check_key(key: str = Header(None)):
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# --- Routes ---
@app.get("/")
def root():
    return {"ok": True, "service": "Eebii Notify API"}

@app.post("/contacts")
def add_contact(data: dict, request: Request, x_api_key: str = Header(None)):
    check_key(x_api_key)
    db = next(get_db())
    contact = Contact(name=data["name"], phone=data["phone"])
    db.add(contact)
    db.commit()
    return {"id": contact.id, "name": contact.name}

@app.post("/groups")
def add_group(data: dict, x_api_key: str = Header(None)):
    check_key(x_api_key)
    db = next(get_db())
    g = Group(name=data["name"])
    db.add(g)
    db.commit()
    return {"id": g.id, "name": g.name}

@app.post("/groups/add")
def add_members(data: dict, x_api_key: str = Header(None)):
    check_key(x_api_key)
    db = next(get_db())
    for cid in data["contact_ids"]:
        gm = GroupMember(group_id=data["group_id"], contact_id=cid)
        db.add(gm)
    db.commit()
    return {"ok": True}

@app.get("/groups/{gid}/members")
def list_members(gid: int, x_api_key: str = Header(None)):
    check_key(x_api_key)
    db = next(get_db())
    members = db.query(GroupMember).filter(GroupMember.group_id == gid).all()
    return [{"contact_id": m.contact_id} for m in members]

@app.get("/templates")
def list_templates(x_api_key: str = Header(None)):
    check_key(x_api_key)
    r = requests.get(f"{META_URL}/message_templates", headers={"Authorization": f"Bearer {WA_TOKEN}"})
    return r.json()

@app.post("/send/text")
def send_text(data: dict, x_api_key: str = Header(None)):
    check_key(x_api_key)
    payload = {
        "messaging_product": "whatsapp",
        "to": data["to"],
        "type": "text",
        "text": {"body": data["text"]}
    }
    r = requests.post(f"{META_URL}/messages", headers={"Authorization": f"Bearer {WA_TOKEN}"}, json=payload)
    return r.json()

@app.post("/send/template")
def send_template(data: dict, x_api_key: str = Header(None)):
    check_key(x_api_key)
    payload = {
        "messaging_product": "whatsapp",
        "to": data["to"],
        "type": "template",
        "template": {"name": data["template_name"], "language": {"code": data.get("language","en_US")}}
    }
    r = requests.post(f"{META_URL}/messages", headers={"Authorization": f"Bearer {WA_TOKEN}"}, json=payload)
    return r.json()

@app.post("/media/upload")
def upload_media(file: UploadFile = File(...), x_api_key: str = Header(None)):
    check_key(x_api_key)
    files = {'file': (file.filename, file.file, file.content_type)}
    data = {'messaging_product': 'whatsapp'}
    r = requests.post(f"{META_URL}/media", headers={"Authorization": f"Bearer {WA_TOKEN}"}, files=files, data=data)
    return r.json()

@app.post("/send/media")
def send_media(data: dict, x_api_key: str = Header(None)):
    check_key(x_api_key)
    payload = {
        "messaging_product": "whatsapp",
        "to": data["to"],
        "type": data["media_type"],
        data["media_type"]: {"id": data["media_id"], "caption": data.get("caption","")}
    }
    r = requests.post(f"{META_URL}/messages", headers={"Authorization": f"Bearer {WA_TOKEN}"}, json=payload)
    return r.json()

@app.post("/send/bulk")
def send_bulk(data: dict, x_api_key: str = Header(None)):
    check_key(x_api_key)
    db = next(get_db())
    members = db.query(GroupMember).filter(GroupMember.group_id == data["group_id"]).all()
    phones = [db.query(Contact).get(m.contact_id).phone for m in members]
    results = []
    for phone in phones:
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": data["text"]}
        }
        r = requests.post(f"{META_URL}/messages", headers={"Authorization": f"Bearer {WA_TOKEN}"}, json=payload)
        results.append({phone: r.status_code})
    return {"sent": results}
