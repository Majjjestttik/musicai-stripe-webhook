import os
import stripe
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

BOT_USERNAME = "mu_sic_aibot"  # твой бот

stripe.api_key = STRIPE_SECRET_KEY
app = FastAPI()

PACK_TO_SONGS = {"pack_1": 1, "pack_5": 5, "pack_30": 30}


def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                lang TEXT NOT NULL DEFAULT 'en',
                balance INT NOT NULL DEFAULT 0,
                demo_used INT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # защита от повторного начисления по одному и тому же Checkout Session
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_purchases (
                session_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                pack TEXT NOT NULL,
                songs INT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        conn.commit()


def add_balance_once(session_id: str, user_id: int, pack: str, songs: int) -> bool:
    """
    Начисляет баланс ровно один раз на session_id.
    Возвращает True если начислило, False если session_id уже был обработан.
    """
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO users(user_id) VALUES(%s) ON CONFLICT DO NOTHING",
            (user_id,),
        )

        try:
            conn.execute(
                """
                INSERT INTO stripe_purchases(session_id, user_id, pack, songs)
                VALUES (%s, %s, %s, %s)
                """,
                (session_id, user_id, pack, songs),
            )
        except Exception:
            # уже есть такой session_id -> значит начисляли ранее
            conn.rollback()
            return False

        conn.execute(
            "UPDATE users SET balance = balance + %s WHERE user_id=%s",
            (songs, user_id),
        )
        conn.commit()
        return True


@app.on_event("startup")
def _startup():
    init_db()


# ---------- CREATE CHECKOUT SESSION ----------

class CreateCheckoutBody(BaseModel):
    user_id: int           # telegram user id
    pack: str              # pack_1 / pack_5 / pack_30
    price_id: str          # Stripe Price ID: price_...


@app.post("/stripe/create-checkout")
async def create_checkout(body: CreateCheckoutBody):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY not set")

    if body.pack not in PACK_TO_SONGS:
        raise HTTPException(status_code=400, detail="Unknown pack")

    # ВАЖНО: тут f-string -> плейсхолдер Stripe должен остаться {CHECKOUT_SESSION_ID}
    success_url = f"https://t.me/{BOT_USERNAME}?start=paid_{{CHECKOUT_SESSION_ID}}"
    cancel_url = f"https://t.me/{BOT_USERNAME}?start=cancel"

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": body.price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "user_id": str(body.user_id),
                "pack": body.pack,
            },
            client_reference_id=str(body.user_id),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {e}")

    return {"checkout_url": session.url, "session_id": session.id}


# ---------- WEBHOOK ----------

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET not set")

    payload = await request.body()
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature header")

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid signature: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        # Страховка: начисляем только если реально paid
        if session.get("payment_status") != "paid":
            return JSONResponse({"ok": True, "ignored": "not_paid"})

        session_id = session.get("id")
        meta = session.get("metadata") or {}
        user_id = meta.get("user_id")
        pack = meta.get("pack")

        if session_id and user_id and pack in PACK_TO_SONGS:
            songs = PACK_TO_SONGS[pack]
            added = add_balance_once(session_id=session_id, user_id=int(user_id), pack=pack, songs=songs)
            return {"ok": True, "credited": added}

    return {"ok": True}
