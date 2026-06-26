import logging
import os
import re
import json
import base64
import tempfile
from datetime import datetime, timedelta
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TIMEZONE = pytz.timezone("Asia/Tashkent")
DB_PATH = "vazifalar.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

Base = declarative_base()

class Vazifa(Base):
    __tablename__ = "vazifalar"
    id = Column(Integer, primary_key=True, autoincrement=True)
    nomi = Column(Text, nullable=False)
    kategoriya = Column(String(20), default="shaxsiy")
    masul = Column(String(100), default="Xusniddin")
    sana = Column(String(20), default="")
    vaqt = Column(String(10), default="")
    eslatma_vaqt = Column(String(30), default="")
    holat = Column(String(20), default="kutilmoqda")
    yaratilgan = Column(DateTime, default=datetime.utcnow)
    eslatma_yuborildi = Column(Integer, default=0)

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

KAT_EMOJI = {"gb": "GB", "davlat": "Davlat", "shaxsiy": "Shaxsiy"}
KAT_NOMI = {"gb": "Golden Brain", "davlat": "Davlat ishi", "shaxsiy": "Shaxsiy"}
HOLAT_EMOJI = {"kutilmoqda": "[kutilmoqda]", "chala": "[chala]", "bajarildi": "[OK]", "bajarilmadi": "[XATO]"}

def audio_to_text(audio_bytes):
    if not GROQ_API_KEY:
        return ""
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_bytes)
            path = f.name
        try:
            with open(path, "rb") as af:
                t = client.audio.transcriptions.create(
                    file=("audio.ogg", af.read(), "audio/ogg"),
                    model="whisper-large-v3-turbo",
                    language="uz",
                    response_format="text"
                )
            return t.strip() if isinstance(t, str) else t.text.strip()
        finally:
            try:
                os.unlink(path)
            except:
                pass
    except Exception as e:
        logger.error(f"Audio xatosi: {e}")
        return ""

async def image_to_text(img_bytes):
    if not OPENAI_API_KEY:
        return ""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        b64 = base64.b64encode(img_bytes).decode()
        r = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}", "detail": "high"
                }},
                {"type": "text", "text": "Rasmdan barcha matnni va vazifalarni O'zbek tilida ajrat."}
            ]}],
            max_tokens=2000
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Rasm tahlil xatosi: {e}")
        return ""

def text_to_tasks_ai(text):
    if not GROQ_API_KEY:
        return None
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        bugun = datetime.now(TIMEZONE).strftime("%d.%m.%Y")
        system_prompt = f"""Vazifa boshqarish AI. Matndan vazifalar ajrat. JSON qaytar:
{{"vazifalar": [{{"nomi": "nom", "kategoriya": "gb|davlat|shaxsiy", "sana": "DD.MM.YYYY yoki null", "vaqt": "HH:MM yoki null", "eslatma": "NN daqiqa oldin yoki null"}}]}}
#gb=Golden Brain, #davlat=Davlat, #shaxsiy=Shaxsiy. Bugun: {bugun}"""
        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.1,
            max_tokens=800,
            response_format={"type": "json_object"}
        )
        return json.loads(r.choices[0].message.content)
    except Exception as e:
        logger.error(f"AI xatosi: {e}")
        return None

def kat_detect(matn):
    m = matn.lower()
    if "#gb" in m or "golden brain" in m:
        return "gb"
    if "#davlat" in m or "davlat ishi" in m:
        return "davlat"
    if "#shaxsiy" in m or "shaxsiy" in m:
        return "shaxsiy"
    return None

def sana_parse(matn):
    sana_str, vaqt_str, eslatma_str = "", "", ""
    v = re.search(r"(\d{1,2}):(\d{2})", matn)
    if v:
        vaqt_str = v.group(0)
    if "bugun" in matn.lower():
        sana_str = datetime.now(TIMEZONE).strftime("%d.%m.%Y")
    elif "ertaga" in matn.lower():
        sana_str = (datetime.now(TIMEZONE) + timedelta(days=1)).strftime("%d.%m.%Y")
    else:
        s = re.search(r"(\d{1,2})[-./ ](\d{1,2})", matn)
        if s:
            sana_str = s.group(0)
    e = re.search(r"(\d+)\s*(daqiqa|soat)\s*oldin", matn.lower())
    if e:
        eslatma_str = f"{e.group(1)} {e.group(2)} oldin"
    return sana_str, vaqt_str, eslatma_str

def vazifa_text(v):
    kat = KAT_NOMI.get(v.kategoriya, v.kategoriya)
    holat = HOLAT_EMOJI.get(v.holat, "?")
    return (
        f"{holat} <b>{v.nomi}</b>\n"
        f"  Bolim: {KAT_EMOJI.get(v.kategoriya, '')} {kat}\n"
        f"  Masul: {v.masul}\n"
        f"  Sana: {v.sana or '--'}\n"
        f"  Vaqt: {v.vaqt or '--'}\n"
        f"  Eslatma: {v.eslatma_vaqt or '--'}"
    )

def hisobot_text(session, sarlavha):
    bugun = datetime.now(TIMEZONE).strftime("%d.%m.%Y")
    oylar = {
        "January": "Yanvar", "February": "Fevral", "March": "Mart", "April": "Aprel",
        "May": "May", "June": "Iyun", "July": "Iyul", "August": "Avgust",
        "September": "Sentabr", "October": "Oktabr", "November": "Noyabr", "December": "Dekabr"
    }
    hafta = {
        "Monday": "Dushanba", "Tuesday": "Seshanba", "Wednesday": "Chorshanba",
        "Thursday": "Payshanba", "Friday": "Juma", "Saturday": "Shanba", "Sunday": "Yakshanba"
    }
    dt = datetime.now(TIMEZONE)
    uz_sana = f"{dt.day}-{oylar.get(dt.strftime('%B'), dt.strftime('%B'))} {hafta.get(dt.strftime('%A'), dt.strftime('%A'))}"

    def stat(kat):
        all_v = session.query(Vazifa).filter_by(kategoriya=kat).all()
        return (
            sum(1 for x in all_v if x.holat == "bajarildi"),
            sum(1 for x in all_v if x.holat == "chala"),
            sum(1 for x in all_v if x.holat == "bajarilmadi")
        )

    gb_b, gb_c, gb_m = stat("gb")
    dv_b, dv_c, dv_m = stat("davlat")
    sh_b, sh_c, sh_m = stat("shaxsiy")
    bugungi = session.query(Vazifa).filter(
        Vazifa.sana == bugun,
        Vazifa.holat.in_(["kutilmoqda", "chala"])
    ).all()

    txt = (
        f"================\n{sarlavha}\n{uz_sana}\n================\n\n"
        f"GOLDEN BRAIN:\n  OK: {gb_b}  Chala: {gb_c}  XATO: {gb_m}\n\n"
        f"DAVLAT ISHI:\n  OK: {dv_b}  Chala: {dv_c}  XATO: {dv_m}\n\n"
        f"SHAXSIY:\n  OK: {sh_b}  Chala: {sh_c}  XATO: {sh_m}\n\n"
        f"================\n"
    )
    if bugungi:
        txt += "BUGUNGI VAZIFALAR:\n"
        for i, v in enumerate(bugungi, 1):
            txt += f"  {i}. {v.nomi} [{v.vaqt or '--'}]\n"
    else:
        txt += "Bugun uchun vazifa yoq\n"
    txt += "================"
    return txt

def jadval_text(session):
    vazifalar = session.query(Vazifa).order_by(Vazifa.id).all()
    if not vazifalar:
        return "Hozircha vazifalar yoq."
    lines = ["<b>VAZIFALAR JADVALI</b>\n================"]
    for v in vazifalar:
        kat = KAT_NOMI.get(v.kategoriya, v.kategoriya)
        holat = HOLAT_EMOJI.get(v.holat, "?")
        lines.append(f"<b>#{v.id}</b> | {holat} {v.nomi}\n  {kat} | {v.masul} | {v.sana or '--'} {v.vaqt or '--'}\n")
    lines.append("================")
    return "\n".join(lines)

def holat_keyboard(vazifa_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("OK - Bajarildi", callback_data=f"holat_{vazifa_id}_bajarildi"),
            InlineKeyboardButton("Chala", callback_data=f"holat_{vazifa_id}_chala")
        ],
        [
            InlineKeyboardButton("XATO - Bajarilmadi", callback_data=f"holat_{vazifa_id}_bajarilmadi"),
            InlineKeyboardButton("O'chir", callback_data=f"ochir_{vazifa_id}")
        ]
    ])

def kategoriya_keyboard(matn):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Golden Brain #gb", callback_data=f"kat_gb|{matn[:200]}"),
            InlineKeyboardButton("Davlat ishi #davlat", callback_data=f"kat_davlat|{matn[:200]}")
        ],
        [InlineKeyboardButton("Shaxsiy #shaxsiy", callback_data=f"kat_shaxsiy|{matn[:200]}")]
    ])

def tasdiqlash_keyboard(vazifa_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Ha, saqlash", callback_data=f"saqlash_{vazifa_id}"),
        InlineKeyboardButton("O'zgartirish", callback_data=f"ozgartir_{vazifa_id}"),
        InlineKeyboardButton("Yoq", callback_data=f"ochir_{vazifa_id}")
    ]])

async def cmd_start(update, ctx):
    ai_groq = "AI faol" if GROQ_API_KEY else "AI sozlanmagan"
    ai_openai = "Rasm AI faol" if OPENAI_API_KEY else "Rasm AI sozlanmagan"
    await update.message.reply_text(
        f"<b>Salom, Xusniddin!</b>\n\n"
        f"<b>XM Task Manager</b>\n"
        f"Holat: {ai_groq} | {ai_openai}\n\n"
        f"Matn, ovoz yoki rasm yuboring - bot tushunadi!\n\n"
        f"Barcha buyruqlar: /yordam",
        parse_mode="HTML"
    )

async def cmd_yordam(update, ctx):
    ai_status = "Faol (Groq)" if GROQ_API_KEY else "Sozlanmagan"
    img_status = "Faol (OpenAI GPT-4o)" if OPENAI_API_KEY else "Sozlanmagan"
    await update.message.reply_text(
        f"<b>BUYRUQLAR</b>\n\n"
        f"/start - Boshlash\n"
        f"/hisobot - Joriy holat\n"
        f"/jadval - Barcha vazifalar\n"
        f"/gb - Golden Brain vazifalari\n"
        f"/davlat - Davlat ishi vazifalari\n"
        f"/shaxsiy - Shaxsiy vazifalar\n"
        f"/tugallanmagan - Tugallanmaganlar\n"
        f"/barchasi - Barcha vazifalar\n"
        f"/yordam - Bu menyu\n\n"
        f"<b>AI holati:</b>\n"
        f"Matn/Ovoz: {ai_status}\n"
        f"Rasm: {img_status}\n\n"
        f"<b>Vazifa qoshish:</b>\n"
        f"Matn yozing, ovoz yuboring, yoki rasm yuboring\n"
        f"Teglar: #gb #davlat #shaxsiy",
        parse_mode="HTML"
    )

async def cmd_hisobot(update, ctx):
    session = Session()
    try:
        txt = hisobot_text(session, "JORIY HOLAT")
        await update.message.reply_text(txt, parse_mode="HTML")
    finally:
        session.close()

async def cmd_jadval(update, ctx):
    session = Session()
    try:
        txt = jadval_text(session)
        await update.message.reply_text(txt, parse_mode="HTML")
    finally:
        session.close()

async def cmd_kategoriya(update, ctx, kat):
    session = Session()
    try:
        vazifalar = session.query(Vazifa).filter_by(kategoriya=kat).all()
        kat_n = KAT_NOMI.get(kat, kat)
        if not vazifalar:
            await update.message.reply_text(f"<b>{kat_n}</b> bolimida vazifalar yoq.", parse_mode="HTML")
            return
        txt = f"<b>{kat_n.upper()} VAZIFALARI</b>\n================\n\n"
        for v in vazifalar:
            txt += vazifa_text(v) + "\n\n"
        if len(txt) > 4000:
            txt = txt[:4000] + "...\n(Ko'p vazifalar, qisman ko'rsatilmoqda)"
        await update.message.reply_text(txt, parse_mode="HTML")
    finally:
        session.close()

async def cmd_gb(update, ctx):
    await cmd_kategoriya(update, ctx, "gb")

async def cmd_davlat(update, ctx):
    await cmd_kategoriya(update, ctx, "davlat")

async def cmd_shaxsiy(update, ctx):
    await cmd_kategoriya(update, ctx, "shaxsiy")

async def cmd_tugallanmagan(update, ctx):
    session = Session()
    try:
        vazifalar = session.query(Vazifa).filter(Vazifa.holat.in_(["chala", "bajarilmadi"])).all()
        if not vazifalar:
            await update.message.reply_text("Barcha vazifalar bajarilgan!", parse_mode="HTML")
            return
        txt = "<b>TUGALLANMAGAN VAZIFALAR</b>\n================\n\n"
        for v in vazifalar:
            txt += vazifa_text(v) + "\n\n"
        await update.message.reply_text(txt, parse_mode="HTML")
    finally:
        session.close()

async def cmd_barchasi(update, ctx):
    session = Session()
    try:
        vazifalar = session.query(Vazifa).order_by(Vazifa.yaratilgan.desc()).all()
        if not vazifalar:
            await update.message.reply_text("Hozircha vazifalar yoq.", parse_mode="HTML")
            return
        txt = "<b>BARCHA VAZIFALAR</b>\n================\n\n"
        for v in vazifalar:
            txt += vazifa_text(v) + "\n\n"
        if len(txt) > 4000:
            txt = txt[:4000] + "...\n(Ko'p vazifalar)"
        await update.message.reply_text(txt, parse_mode="HTML")
    finally:
        session.close()

async def matn_handler(update, ctx):
    matn = update.message.text.strip()
    kichik = matn.lower()

    if any(s in kichik for s in ["hisobot", "holat", "necha"]):
        session = Session()
        try:
            txt = hisobot_text(session, "JORIY HOLAT")
            await update.message.reply_text(txt, parse_mode="HTML")
        finally:
            session.close()
        return

    if any(s in kichik for s in ["jadval", "barchasi", "excel"]):
        session = Session()
        try:
            txt = jadval_text(session)
            await update.message.reply_text(txt, parse_mode="HTML")
        finally:
            session.close()
        return

    ai_result = text_to_tasks_ai(matn)
    if ai_result and ai_result.get("vazifalar"):
        vazifalar_list = ai_result["vazifalar"]
        session = Session()
        try:
            saved = []
            for item in vazifalar_list:
                v = Vazifa(
                    nomi=item.get("nomi", matn)[:500],
                    kategoriya=item.get("kategoriya", "shaxsiy"),
                    masul="Xusniddin",
                    sana=item.get("sana") or "",
                    vaqt=item.get("vaqt") or "",
                    eslatma_vaqt=item.get("eslatma") or "",
                    holat="kutilmoqda"
                )
                session.add(v)
                session.commit()
                session.refresh(v)
                saved.append(v)

            if len(saved) == 1:
                v = saved[0]
                txt = (
                    f"AI aniqladim:\n\n1. {v.nomi}\n"
                    f"  Bolim: {KAT_NOMI.get(v.kategoriya, v.kategoriya)}\n"
                    f"  Sana: {v.sana or '--'}\n"
                    f"  Vaqt: {v.vaqt or '--'}\n"
                    f"  Eslatma: {v.eslatma_vaqt or '--'}\n\nTogrimii?"
                )
                await update.message.reply_text(txt, reply_markup=tasdiqlash_keyboard(v.id), parse_mode="HTML")
            else:
                txt = f"AI {len(saved)} ta vazifa aniqladi va saqladi!\n\n"
                for v in saved:
                    txt += vazifa_text(v) + "\n\n"
                await update.message.reply_text(txt, parse_mode="HTML")
        finally:
            session.close()
        return

    kat = kat_detect(matn)
    sana_str, vaqt_str, eslatma_str = sana_parse(matn)

    if not kat:
        await update.message.reply_text(
            f"Qaysi bolimga qoshish?\n\n<i>{matn[:200]}</i>",
            reply_markup=kategoriya_keyboard(matn),
            parse_mode="HTML"
        )
        return

    session = Session()
    try:
        v = Vazifa(
            nomi=matn[:500],
            kategoriya=kat,
            masul="Xusniddin",
            sana=sana_str,
            vaqt=vaqt_str,
            eslatma_vaqt=eslatma_str,
            holat="kutilmoqda"
        )
        session.add(v)
        session.commit()
        session.refresh(v)
        txt = (
            f"Aniqladim:\n\n1. {v.nomi}\n"
            f"  Bolim: {KAT_NOMI.get(kat, kat)}\n"
            f"  Sana: {v.sana or '--'}\n"
            f"  Vaqt: {v.vaqt or '--'}\n"
            f"  Eslatma: {v.eslatma_vaqt or '--'}\n\nTogrimii?"
        )
        await update.message.reply_text(txt, reply_markup=tasdiqlash_keyboard(v.id), parse_mode="HTML")
    finally:
        session.close()

async def voice_handler(update, ctx):
    try:
        audio = update.message.voice or update.message.audio
        if not audio:
            return
        await update.message.reply_text("Ovoz qabul qilindi, tahlil qilinmoqda...")
        file = await ctx.bot.get_file(audio.file_id)
        audio_bytes = await file.download_as_bytearray()

        if GROQ_API_KEY:
            text = audio_to_text(bytes(audio_bytes))
            if text:
                await update.message.reply_text(f"Siz aytdingiz:\n<i>{text}</i>", parse_mode="HTML")
                fake_update = type("FakeUpdate", (), {
                    "message": type("Msg", (), {
                        "text": text,
                        "reply_text": update.message.reply_text
                    })()
                })()
                await matn_handler(fake_update, ctx)
            else:
                await update.message.reply_text("Ovozni tushuna olmadim. Qayta urinib koring.")
        else:
            await update.message.reply_text("Ovoz AI sozlanmagan. GROQ_API_KEY kiriting.")
    except Exception as e:
        logger.error(f"Voice xatosi: {e}")
        await update.message.reply_text(f"Xatolik: {e}")

async def photo_handler(update, ctx):
    try:
        await update.message.reply_text("Rasm qabul qilindi, tahlil qilinmoqda...")
        photo = update.message.photo[-1]
        file = await ctx.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()

        if OPENAI_API_KEY:
            text = await image_to_text(bytes(img_bytes))
            if text:
                await update.message.reply_text(f"Rasmdagi matn:\n<i>{text[:500]}</i>", parse_mode="HTML")
                fake_update = type("FakeUpdate", (), {
                    "message": type("Msg", (), {
                        "text": text,
                        "reply_text": update.message.reply_text
                    })()
                })()
                await matn_handler(fake_update, ctx)
            else:
                await update.message.reply_text("Rasmdan matn ajrata olmadim.")
        else:
            await update.message.reply_text("Rasm AI sozlanmagan. OPENAI_API_KEY kiriting.")
    except Exception as e:
        logger.error(f"Photo xatosi: {e}")
        await update.message.reply_text(f"Xatolik: {e}")

async def callback_handler(update, ctx):
    query = update.callback_query
    await query.answer()
    data = query.data
    session = Session()
    try:
        if data.startswith("kat_"):
            parts = data.split("|", 1)
            kat = parts[0].replace("kat_", "")
            matn = parts[1] if len(parts) > 1 else ""
            sana_str, vaqt_str, eslatma_str = sana_parse(matn)
            v = Vazifa(
                nomi=matn[:500],
                kategoriya=kat,
                masul="Xusniddin",
                sana=sana_str,
                vaqt=vaqt_str,
                eslatma_vaqt=eslatma_str,
                holat="kutilmoqda"
            )
            session.add(v)
            session.commit()
            session.refresh(v)
            txt = (
                f"Aniqladim:\n\n1. {v.nomi}\n"
                f"  Bolim: {KAT_NOMI.get(kat, kat)}\n"
                f"  Sana: {v.sana or '--'}\n"
                f"  Vaqt: {v.vaqt or '--'}\n"
                f"  Eslatma: {v.eslatma_vaqt or '--'}\n\nTogrimii?"
            )
            await query.edit_message_text(txt, reply_markup=tasdiqlash_keyboard(v.id), parse_mode="HTML")

        elif data.startswith("saqlash_"):
            vid = int(data.split("_")[1])
            v = session.get(Vazifa, vid)
            if v:
                v.holat = "kutilmoqda"
                session.commit()
                await query.edit_message_text(
                    f"Vazifa saqlandi!\n\n{vazifa_text(v)}",
                    reply_markup=holat_keyboard(v.id),
                    parse_mode="HTML"
                )

        elif data.startswith("holat_"):
            parts = data.split("_")
            vid = int(parts[1])
            yangi = parts[2]
            v = session.get(Vazifa, vid)
            if v:
                v.holat = yangi
                session.commit()
                await query.edit_message_text(
                    f"Holat yangilandi: {yangi}\n\n{vazifa_text(v)}",
                    reply_markup=holat_keyboard(v.id),
                    parse_mode="HTML"
                )

        elif data.startswith("ochir_"):
            vid = int(data.split("_")[1])
            v = session.get(Vazifa, vid)
            if v:
                session.delete(v)
                session.commit()
                await query.edit_message_text("Vazifa o'chirildi.", parse_mode="HTML")

        elif data.startswith("ozgartir_"):
            await query.edit_message_text("Yangi vazifani yozing va yuboring.", parse_mode="HTML")

    finally:
        session.close()

async def eslatma_tekshir(app):
    if not ADMIN_ID:
        return
    now = datetime.now(TIMEZONE)
    session = Session()
    try:
        vazifalar = session.query(Vazifa).filter(
            Vazifa.holat.in_(["kutilmoqda", "chala"]),
            Vazifa.eslatma_yuborildi == 0,
            Vazifa.sana != "",
            Vazifa.vaqt != ""
        ).all()
        for v in vazifalar:
            try:
                sana_parts = v.sana.replace("-", ".").replace("/", ".").split(".")
                if len(sana_parts) < 2:
                    continue
                kun = int(sana_parts[0])
                oy = int(sana_parts[1]) if sana_parts[1].isdigit() else now.month
                yil = int(sana_parts[2]) if len(sana_parts) > 2 else now.year
                vp = v.vaqt.split(":")
                soat = int(vp[0])
                daqiqa = int(vp[1]) if len(vp) > 1 else 0
                vazifa_dt = TIMEZONE.localize(datetime(yil, oy, kun, soat, daqiqa))
                delta = timedelta(minutes=30)
                if v.eslatma_vaqt:
                    em = re.search(r"(\d+)\s*(daqiqa|soat)", v.eslatma_vaqt)
                    if em:
                        n = int(em.group(1))
                        delta = timedelta(hours=n) if em.group(2) == "soat" else timedelta(minutes=n)
                eslatma_dt = vazifa_dt - delta
                if eslatma_dt <= now <= vazifa_dt:
                    txt = (
                        f"ESLATMA!\n==============\n"
                        f"{v.nomi}\nSana: {v.sana}\nVaqt: {v.vaqt}\n"
                        f"Bolim: {KAT_NOMI.get(v.kategoriya, v.kategoriya)}\n"
                        f"Masul: {v.masul}\n==============\nVaqt keldi!"
                    )
                    await app.bot.send_message(ADMIN_ID, txt, parse_mode="HTML")
                    v.eslatma_yuborildi = 1
                    session.commit()
            except Exception as e:
                logger.warning(f"Eslatma xatosi {v.id}: {e}")
    finally:
        session.close()

async def ertalabki_hisobot(app):
    if not ADMIN_ID:
        return
    session = Session()
    try:
        txt = hisobot_text(session, "ERTALABKI HISOBOT")
        await app.bot.send_message(ADMIN_ID, txt, parse_mode="HTML")
    finally:
        session.close()

async def kechki_hisobot(app):
    if not ADMIN_ID:
        return
    session = Session()
    try:
        txt = hisobot_text(session, "KECHKI YAKUNIY HISOBOT")
        await app.bot.send_message(ADMIN_ID, txt, parse_mode="HTML")
    finally:
        session.close()

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN muhit o'zgaruvchisi o'rnatilmagan!")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("yordam", cmd_yordam))
    app.add_handler(CommandHandler("hisobot", cmd_hisobot))
    app.add_handler(CommandHandler("jadval", cmd_jadval))
    app.add_handler(CommandHandler("gb", cmd_gb))
    app.add_handler(CommandHandler("davlat", cmd_davlat))
    app.add_handler(CommandHandler("shaxsiy", cmd_shaxsiy))
    app.add_handler(CommandHandler("tugallanmagan", cmd_tugallanmagan))
    app.add_handler(CommandHandler("barchasi", cmd_barchasi))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.add_handler(MessageHandler(filters.AUDIO, voice_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, matn_handler))

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(ertalabki_hisobot, "cron", hour=5, minute=0, args=[app])
    scheduler.add_job(kechki_hisobot, "cron", hour=21, minute=0, args=[app])
    scheduler.add_job(eslatma_tekshir, "interval", minutes=1, args=[app])
    scheduler.start()

    logger.info("XM Task Manager bot ishga tushdi! AI: Groq=%s, OpenAI=%s",
                bool(GROQ_API_KEY), bool(OPENAI_API_KEY))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
