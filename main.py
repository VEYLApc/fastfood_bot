import os
import html
import json
import time
import hmac
import hashlib
import asyncio
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Update, Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
# Admin guruh ID si (manfiy son): ADMIN_GROUP_ID=-1001234567890
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "0") or 0)
# Karta orqali to'lov uchun
CARD_NUMBER = os.getenv("CARD_NUMBER", "")
CARD_OWNER = os.getenv("CARD_OWNER", "")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi. .env faylini main.py bilan bir papkaga qo'ying.")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ----------------------------------------------------------------------
# MENYU (shu yerni o'zingizga moslab o'zgartiring: (id, nomi, narxi))
# ----------------------------------------------------------------------
DELIVERY_FEE = 10000

CATEGORIES = {
    "🍔 Burgerlar": [
        (1, "Gamburger", 28000),
        (2, "Chizburger", 32000),
        (3, "Dablburger", 45000),
    ],
    "🌯 Lavash": [
        (4, "Mol go'shtli lavash", 35000),
        (5, "Tovuqli lavash", 32000),
        (6, "Pishloqli lavash", 38000),
    ],
    "🍟 Fri va gazaklar": [
        (7, "Kartoshka fri", 15000),
        (8, "Naggets (6 ta)", 22000),
    ],
    "🥤 Ichimliklar": [
        (9, "Coca-Cola 0.5L", 8000),
        (10, "Fanta 0.5L", 8000),
        (11, "Choy", 5000),
    ],
}
CAT_LIST = list(CATEGORIES.keys())
PRODUCTS = {
    pid: {"name": name, "price": price, "cat": cat}
    for cat, items in CATEGORIES.items()
    for pid, name, price in items
}

PAYMENT_NAMES = {
    "cash": "💵 Naqt pul",
    "card": "💳 Karta orqali",
}

STATUS_NAMES = {
    "new": "🕐 Yangi",
    "accepted": "✅ Qabul qilindi",
    "way": "🚚 Yo'lda",
    "done": "🎉 Yetkazildi",
    "cancelled": "❌ Bekor qilindi",
}

STATUS_CUSTOMER_MSG = {
    "accepted": "✅ Buyurtma №{id} qabul qilindi, tayyorlanmoqda 👨‍🍳",
    "way": "🚚 Buyurtma №{id} yo'lda! Kuryer tez orada yetib boradi.",
    "done": "🎉 Buyurtma №{id} yetkazildi. Yoqimli ishtaha! 😋",
    "cancelled": "❌ Buyurtma №{id} bekor qilindi. Savollar bo'lsa, biz bilan bog'laning.",
}

# ----------------------------------------------------------------------
# Xotira (oddiy variant). Serverni o'chirsangiz ma'lumot yo'qoladi.
# ----------------------------------------------------------------------
CARTS: dict[int, dict[int, int]] = {}      # user_id -> {product_id: soni}
ORDERS: dict[int, list[dict]] = {}         # user_id -> [buyurtmalar]
ORDERS_BY_ID: dict[int, dict] = {}         # order_id -> buyurtma
ORDER_COUNTER = 0


def money(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " so'm"


def cart_total(cart: dict[int, int]) -> int:
    return sum(PRODUCTS[pid]["price"] * q for pid, q in cart.items())


def cart_text(cart: dict[int, int]) -> str:
    lines = ["🛒 <b>Savatingiz:</b>\n"]
    for pid, q in cart.items():
        p = PRODUCTS[pid]
        lines.append(f"• {p['name']} × {q} = {money(p['price'] * q)}")
    total = cart_total(cart)
    lines.append(f"\n🚚 Yetkazib berish: {money(DELIVERY_FEE)}")
    lines.append(f"💰 <b>Jami: {money(total + DELIVERY_FEE)}</b>")
    return "\n".join(lines)


def card_info_text() -> str:
    text = "\n\n💳 <b>To'lov uchun karta:</b>\n"
    if CARD_NUMBER:
        text += f"<code>{html.escape(CARD_NUMBER)}</code>\n"
    if CARD_OWNER:
        text += f"👤 {html.escape(CARD_OWNER)}\n"
    return text


def address_html(address: str) -> str:
    if address.startswith("http"):
        return f'<a href="{html.escape(address)}">📍 Xaritada ochish</a>'
    return html.escape(address)


def group_order_text(order: dict) -> str:
    items = "\n".join(
        f"• {PRODUCTS[pid]['name']} × {q}" for pid, q in order["items"].items()
    )
    username = f" (@{order['username']})" if order["username"] else ""
    text = (
        f"🆕 <b>Buyurtma №{order['id']}</b>\n"
        f"Holati: <b>{STATUS_NAMES[order['status']]}</b>\n\n"
        f"👤 {html.escape(order['name'])}{username}\n"
        f"📞 {html.escape(order['phone'])}\n"
        f"📍 {address_html(order['address'])}\n"
        f"💰 To'lov: {PAYMENT_NAMES[order['payment']]}\n\n"
        f"{items}\n\n"
        f"💵 Jami: <b>{money(order['total'])}</b> (yetkazib berish bilan)"
    )
    if order.get("maps"):
        text += f'\n🗺 <a href="{html.escape(order["maps"])}">Xaritada ochish</a>'
    if order.get("note"):
        text += f"\n📝 Izoh: {html.escape(order['note'])}"
    if order["payment"] == "card":
        text += "\n🧾 Mijoz chek rasmini yuboradi."
    return text


# ----------------------------------------------------------------------
# Callback data va holatlar (FSM)
# ----------------------------------------------------------------------
class CatCb(CallbackData, prefix="cat"):
    idx: int


class ProdCb(CallbackData, prefix="prod"):
    action: str   # view | set | add
    pid: int
    qty: int = 1


class CartCb(CallbackData, prefix="cart"):
    action: str   # show | clear | remove | checkout
    pid: int = 0


class OrderCb(CallbackData, prefix="ord"):
    action: str   # accepted | way | done | cancelled
    oid: int


class OrderForm(StatesGroup):
    phone = State()
    location = State()
    payment = State()
    confirm = State()


# ----------------------------------------------------------------------
# Klaviaturalar
# ----------------------------------------------------------------------
def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🍔 Menyu"), KeyboardButton(text="🛒 Savat")],
            [KeyboardButton(text="📦 Buyurtmalarim")],
        ],
        resize_keyboard=True,
    )


def categories_kb():
    kb = InlineKeyboardBuilder()
    for i, name in enumerate(CAT_LIST):
        kb.button(text=name, callback_data=CatCb(idx=i))
    kb.button(text="🛒 Savat", callback_data=CartCb(action="show"))
    kb.adjust(2)
    return kb.as_markup()


def products_kb(cat_idx: int):
    kb = InlineKeyboardBuilder()
    for pid, name, price in CATEGORIES[CAT_LIST[cat_idx]]:
        kb.button(text=f"{name} — {money(price)}",
                  callback_data=ProdCb(action="view", pid=pid, qty=1))
    kb.button(text="⬅️ Orqaga", callback_data=CatCb(idx=-1))
    kb.button(text="🛒 Savat", callback_data=CartCb(action="show"))
    kb.adjust(1, 1, 1, 2)
    return kb.as_markup()


def product_kb(pid: int, qty: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="➖", callback_data=ProdCb(action="set", pid=pid, qty=max(1, qty - 1)))
    kb.button(text=str(qty), callback_data=ProdCb(action="set", pid=pid, qty=qty))
    kb.button(text="➕", callback_data=ProdCb(action="set", pid=pid, qty=min(20, qty + 1)))
    kb.button(text="✅ Savatga qo'shish", callback_data=ProdCb(action="add", pid=pid, qty=qty))
    cat_idx = CAT_LIST.index(PRODUCTS[pid]["cat"])
    kb.button(text="⬅️ Orqaga", callback_data=CatCb(idx=cat_idx))
    kb.adjust(3, 1, 1)
    return kb.as_markup()


def cart_kb(cart: dict[int, int]):
    kb = InlineKeyboardBuilder()
    for pid in cart:
        kb.button(text=f"❌ {PRODUCTS[pid]['name']}",
                  callback_data=CartCb(action="remove", pid=pid))
    kb.button(text="✅ Buyurtma berish", callback_data=CartCb(action="checkout"))
    kb.button(text="➕ Yana qo'shish", callback_data=CatCb(idx=-1))
    kb.button(text="🗑 Tozalash", callback_data=CartCb(action="clear"))
    kb.adjust(1)
    return kb.as_markup()


def payment_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text=PAYMENT_NAMES["cash"], callback_data="pay_cash")
    kb.button(text=PAYMENT_NAMES["card"], callback_data="pay_card")
    kb.adjust(1)
    return kb.as_markup()


def confirm_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Tasdiqlash", callback_data="order_yes")
    kb.button(text="❌ Bekor qilish", callback_data="order_no")
    kb.adjust(2)
    return kb.as_markup()


def admin_order_kb(order: dict):
    """Guruhdagi buyurtma tugmalari (holatga qarab o'zgaradi)."""
    oid = order["id"]
    status = order["status"]
    kb = InlineKeyboardBuilder()
    if status == "new":
        kb.button(text="✅ Qabul qilish", callback_data=OrderCb(action="accepted", oid=oid))
        kb.button(text="❌ Bekor qilish", callback_data=OrderCb(action="cancelled", oid=oid))
        kb.adjust(2)
    elif status == "accepted":
        kb.button(text="🚚 Yo'lga chiqdi", callback_data=OrderCb(action="way", oid=oid))
        kb.button(text="❌ Bekor qilish", callback_data=OrderCb(action="cancelled", oid=oid))
        kb.adjust(2)
    elif status == "way":
        kb.button(text="🎉 Yetkazildi", callback_data=OrderCb(action="done", oid=oid))
        kb.adjust(1)
    else:
        return None  # done / cancelled — tugma kerak emas
    return kb.as_markup()


async def safe_edit(call: CallbackQuery, text: str, markup=None):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass  # "message is not modified" xatosini e'tiborsiz qoldiramiz


# ----------------------------------------------------------------------
# Yordamchi komanda: guruh ID sini bilish uchun
# ----------------------------------------------------------------------
@dp.message(Command("id"))
async def chat_id_handler(message: Message):
    await message.answer(f"Bu chat ID si: <code>{message.chat.id}</code>")


# ----------------------------------------------------------------------
# /start va asosiy menyu
# ----------------------------------------------------------------------
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Assalomu alaykum! 👋\n"
        "<b>Fast Food</b> botiga xush kelibsiz!\n"
        "Mazali taomlarni tez va oson buyurtma qiling 🍔",
        reply_markup=main_kb(),
    )


@dp.message(F.text == "🍔 Menyu")
async def menu_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Kategoriyani tanlang:", reply_markup=categories_kb())


@dp.message(F.text == "🛒 Savat")
async def cart_handler(message: Message, state: FSMContext):
    await state.clear()
    cart = CARTS.get(message.from_user.id)
    if not cart:
        await message.answer("🛒 Savatingiz bo'sh. Menyudan taom tanlang.",
                             reply_markup=categories_kb())
        return
    await message.answer(cart_text(cart), reply_markup=cart_kb(cart))


@dp.message(F.text == "📦 Buyurtmalarim")
async def my_orders_handler(message: Message, state: FSMContext):
    await state.clear()
    orders = ORDERS.get(message.from_user.id)
    if not orders:
        await message.answer("Sizda hali buyurtmalar yo'q.")
        return
    lines = ["📦 <b>Oxirgi buyurtmalaringiz:</b>\n"]
    for o in orders[-5:]:
        lines.append(
            f"№{o['id']} — {money(o['total'])} — "
            f"{PAYMENT_NAMES[o['payment']]} — {STATUS_NAMES[o['status']]}"
        )
    await message.answer("\n".join(lines))


# ----------------------------------------------------------------------
# Menyu bo'ylab yurish (inline tugmalar)
# ----------------------------------------------------------------------
@dp.callback_query(CatCb.filter())
async def category_cb(call: CallbackQuery, callback_data: CatCb):
    if callback_data.idx == -1:
        await safe_edit(call, "Kategoriyani tanlang:", categories_kb())
    else:
        await safe_edit(call, f"<b>{CAT_LIST[callback_data.idx]}</b>\nTaomni tanlang:",
                        products_kb(callback_data.idx))
    await call.answer()


@dp.callback_query(ProdCb.filter())
async def product_cb(call: CallbackQuery, callback_data: ProdCb):
    pid, qty = callback_data.pid, callback_data.qty
    p = PRODUCTS[pid]

    if callback_data.action == "add":
        cart = CARTS.setdefault(call.from_user.id, {})
        cart[pid] = cart.get(pid, 0) + qty
        await call.answer(f"✅ {p['name']} × {qty} savatga qo'shildi")
        await safe_edit(call, "Kategoriyani tanlang:", categories_kb())
        return

    text = (f"<b>{p['name']}</b>\n"
            f"Narxi: {money(p['price'])}\n"
            f"Miqdor: {qty}\n"
            f"Summa: <b>{money(p['price'] * qty)}</b>")
    await safe_edit(call, text, product_kb(pid, qty))
    await call.answer()


# ----------------------------------------------------------------------
# Savat
# ----------------------------------------------------------------------
@dp.callback_query(CartCb.filter())
async def cart_cb(call: CallbackQuery, callback_data: CartCb, state: FSMContext):
    uid = call.from_user.id
    cart = CARTS.get(uid, {})
    action = callback_data.action

    if action == "clear":
        CARTS.pop(uid, None)
        await safe_edit(call, "🗑 Savat tozalandi.", categories_kb())
        await call.answer()
        return

    if action == "remove":
        cart.pop(callback_data.pid, None)

    if not cart:
        await safe_edit(call, "🛒 Savatingiz bo'sh.", categories_kb())
        await call.answer()
        return

    if action == "checkout":
        await state.set_state(OrderForm.phone)
        await call.message.answer(
            "📞 Telefon raqamingizni yuboring (tugmani bosing yoki +998901234567 shaklida yozing):",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)],
                    [KeyboardButton(text="❌ Bekor qilish")],
                ],
                resize_keyboard=True,
            ),
        )
        await call.answer()
        return

    await safe_edit(call, cart_text(cart), cart_kb(cart))
    await call.answer()


# ----------------------------------------------------------------------
# Buyurtma berish (FSM): telefon -> manzil -> to'lov -> tasdiqlash
# ----------------------------------------------------------------------
@dp.message(F.text == "❌ Bekor qilish")
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_kb())


@dp.message(OrderForm.phone)
async def phone_handler(message: Message, state: FSMContext):
    if message.contact:
        phone = message.contact.phone_number
    elif message.text and len(message.text.strip()) >= 9:
        phone = message.text.strip()
    else:
        await message.answer("Iltimos, to'g'ri telefon raqam yuboring.")
        return

    await state.update_data(phone=phone)
    await state.set_state(OrderForm.location)
    await message.answer(
        "📍 Yetkazib berish manzilini yuboring (lokatsiya tugmasi yoki manzilni yozing):",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📍 Lokatsiyani yuborish", request_location=True)],
                [KeyboardButton(text="❌ Bekor qilish")],
            ],
            resize_keyboard=True,
        ),
    )


@dp.message(OrderForm.location)
async def location_handler(message: Message, state: FSMContext):
    if message.location:
        lat, lon = message.location.latitude, message.location.longitude
        address = f"https://maps.google.com/?q={lat},{lon}"
        shown = "📍 Lokatsiya yuborildi"
    elif message.text:
        address = message.text.strip()
        shown = html.escape(address)
    else:
        await message.answer("Iltimos, lokatsiya yuboring yoki manzilni yozing.")
        return

    await state.update_data(address=address, shown=shown)
    await state.set_state(OrderForm.payment)
    await message.answer("Ma'lumotlar qabul qilindi ✅", reply_markup=ReplyKeyboardRemove())
    await message.answer("💰 To'lov turini tanlang:", reply_markup=payment_kb())


@dp.callback_query(OrderForm.payment, F.data.in_({"pay_cash", "pay_card"}))
async def payment_handler(call: CallbackQuery, state: FSMContext):
    payment = "cash" if call.data == "pay_cash" else "card"
    await state.update_data(payment=payment)
    await state.set_state(OrderForm.confirm)

    data = await state.get_data()
    cart = CARTS.get(call.from_user.id, {})
    if not cart:
        await call.answer("Savat bo'sh", show_alert=True)
        return

    text = (cart_text(cart)
            + f"\n\n📞 Telefon: {html.escape(data['phone'])}"
            + f"\n📍 Manzil: {data['shown']}"
            + f"\n💰 To'lov: {PAYMENT_NAMES[payment]}")
    if payment == "card":
        text += card_info_text()
        text += "\nTasdiqlagandan keyin to'lov chekini (rasm) shu yerga yuboring."
    text += "\n\nBuyurtmani tasdiqlaysizmi?"

    await safe_edit(call, text, confirm_kb())
    await call.answer()


@dp.callback_query(OrderForm.confirm, F.data == "order_yes")
async def order_confirm(call: CallbackQuery, state: FSMContext):
    global ORDER_COUNTER
    uid = call.from_user.id
    cart = CARTS.get(uid)
    if not cart:
        await call.answer("Savat bo'sh", show_alert=True)
        return

    data = await state.get_data()
    ORDER_COUNTER += 1
    total = cart_total(cart) + DELIVERY_FEE
    order = {
        "id": ORDER_COUNTER,
        "user_id": uid,
        "name": call.from_user.full_name,
        "username": call.from_user.username,
        "items": dict(cart),
        "total": total,
        "phone": data["phone"],
        "address": data["address"],
        "payment": data["payment"],
        "status": "new",
    }
    ORDERS.setdefault(uid, []).append(order)
    ORDERS_BY_ID[order["id"]] = order

    # Admin guruhga yuborish
    if ADMIN_GROUP_ID:
        try:
            await bot.send_message(
                ADMIN_GROUP_ID,
                group_order_text(order),
                reply_markup=admin_order_kb(order),
                disable_web_page_preview=True,
            )
        except Exception as e:
            print("Guruhga yuborib bo'lmadi:", e)
    else:
        print("DIQQAT: ADMIN_GROUP_ID berilmagan, buyurtma guruhga yuborilmadi!")

    CARTS.pop(uid, None)
    await state.clear()

    done_text = (f"✅ <b>Buyurtma №{order['id']} qabul qilindi!</b>\n"
                 f"Jami: {money(total)}\n"
                 f"To'lov: {PAYMENT_NAMES[order['payment']]}\n")
    if order["payment"] == "card":
        done_text += (card_info_text()
                      + f"\nIltimos, {money(total)} o'tkazib, chek rasmini shu yerga yuboring 📸")
    else:
        done_text += "\nTo'lovni kuryerga naqd pulda berasiz. Tez orada yetkazib beramiz 🚀"

    await safe_edit(call, done_text)
    await call.message.answer("Yoqimli ishtaha! 😋", reply_markup=main_kb())
    await call.answer()


@dp.callback_query(OrderForm.confirm, F.data == "order_no")
async def order_decline(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, "❌ Buyurtma bekor qilindi. Savatingiz saqlanib qoldi.")
    await call.message.answer("Asosiy menyu:", reply_markup=main_kb())
    await call.answer()


# ----------------------------------------------------------------------
# Admin guruhdagi tugmalar: buyurtma holatini o'zgartirish
# ----------------------------------------------------------------------
@dp.callback_query(OrderCb.filter())
async def order_status_cb(call: CallbackQuery, callback_data: OrderCb):
    # Faqat admin guruhidagi tugmalar ishlaydi
    if call.message.chat.id != ADMIN_GROUP_ID:
        await call.answer("Ruxsat yo'q", show_alert=True)
        return

    order = ORDERS_BY_ID.get(callback_data.oid)
    if not order:
        await call.answer("Buyurtma topilmadi (server qayta ishga tushgan bo'lishi mumkin)",
                          show_alert=True)
        return

    if order["status"] in ("done", "cancelled"):
        await call.answer("Bu buyurtma yakunlangan", show_alert=True)
        return

    order["status"] = callback_data.action
    admin_name = html.escape(call.from_user.full_name)

    text = group_order_text(order) + f"\n\n👨‍💼 {admin_name}"
    await safe_edit(call, text, admin_order_kb(order))
    await call.answer(STATUS_NAMES[order["status"]])

    # Mijozga xabar
    try:
        await bot.send_message(
            order["user_id"],
            STATUS_CUSTOMER_MSG[order["status"]].format(id=order["id"]),
        )
    except Exception as e:
        print("Mijozga xabar yuborib bo'lmadi:", e)


# ----------------------------------------------------------------------
# Chek rasmini admin guruhga yuborish (karta orqali to'lov)
# ----------------------------------------------------------------------
@dp.message(F.photo, F.chat.type == "private")
async def receipt_handler(message: Message):
    orders = ORDERS.get(message.from_user.id)
    if not orders:
        await message.answer("Avval buyurtma bering 🍔", reply_markup=main_kb())
        return

    last = orders[-1]
    if ADMIN_GROUP_ID:
        try:
            await bot.send_photo(
                ADMIN_GROUP_ID,
                message.photo[-1].file_id,
                caption=(f"🧾 <b>Chek — buyurtma №{last['id']}</b>\n"
                         f"👤 {html.escape(message.from_user.full_name)}\n"
                         f"💵 Summa: {money(last['total'])}"),
            )
        except Exception as e:
            print("Chekni guruhga yuborib bo'lmadi:", e)
    await message.answer("✅ Chek qabul qilindi. Rahmat! Admin tekshirib chiqadi.")


# ----------------------------------------------------------------------
# MINI APP (Netlify) uchun API: menyu olish va buyurtma qabul qilish
# ----------------------------------------------------------------------
def verify_init_data(init_data: str):
    """Telegram initData imzosini tekshiradi. To'g'ri bo'lsa user dict qaytaradi."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", None)
        if not received:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


class ApiItem(BaseModel):
    id: int
    qty: int


class ApiLocation(BaseModel):
    lat: float
    lon: float


class ApiOrder(BaseModel):
    initData: str
    items: list[ApiItem]
    phone: str
    address: str = ""
    location: ApiLocation | None = None
    payment: str = "cash"
    note: str = ""


# ----------------------------------------------------------------------
# FastAPI + webhook / polling
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    polling_task = None

    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        polling_task = asyncio.create_task(dp.start_polling(bot))

    yield

    if polling_task:
        polling_task.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)

# Netlify sahifasi boshqa manzildan so'rov yuboradi, shuning uchun CORS kerak.
# Xohlasangiz Render'da ALLOWED_ORIGIN=https://chipper-klepon-15e11e.netlify.app deb cheklang.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("ALLOWED_ORIGIN", "*")],
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/api/menu")
async def api_menu():
    return {
        "delivery_fee": DELIVERY_FEE,
        "card": {"number": CARD_NUMBER, "owner": CARD_OWNER},
        "categories": [
            {"name": cat,
             "items": [{"id": pid, "name": n, "price": pr} for pid, n, pr in items]}
            for cat, items in CATEGORIES.items()
        ],
    }


@app.post("/api/order")
async def api_order(data: ApiOrder):
    global ORDER_COUNTER
    user = verify_init_data(data.initData)
    if not user:
        raise HTTPException(401, "Mini appni Telegram ichida oching")

    cart: dict[int, int] = {}
    for it in data.items:
        if it.id in PRODUCTS and 1 <= it.qty <= 20:
            cart[it.id] = cart.get(it.id, 0) + it.qty
    if not cart:
        raise HTTPException(400, "Savat bo'sh")

    phone = data.phone.strip()
    if len(phone) < 7:
        raise HTTPException(400, "Telefon raqami noto'g'ri")

    address = data.address.strip()
    maps = None
    if data.location:
        maps = f"https://maps.google.com/?q={data.location.lat},{data.location.lon}"
    if len(address) < 3 and not maps:
        raise HTTPException(400, "Manzil yoki lokatsiya kerak")

    payment = data.payment if data.payment in PAYMENT_NAMES else "cash"
    uid = int(user["id"])
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "Mijoz"

    ORDER_COUNTER += 1
    total = cart_total(cart) + DELIVERY_FEE
    order = {
        "id": ORDER_COUNTER,
        "user_id": uid,
        "name": name,
        "username": user.get("username"),
        "items": cart,
        "total": total,
        "phone": phone,
        "address": address or "Lokatsiya yuborilgan",
        "maps": maps,
        "note": data.note.strip()[:200],
        "payment": payment,
        "status": "new",
    }
    ORDERS.setdefault(uid, []).append(order)
    ORDERS_BY_ID[order["id"]] = order

    # Admin guruhga
    if ADMIN_GROUP_ID:
        try:
            await bot.send_message(
                ADMIN_GROUP_ID, group_order_text(order),
                reply_markup=admin_order_kb(order), disable_web_page_preview=True,
            )
            if data.location:
                await bot.send_location(ADMIN_GROUP_ID, data.location.lat, data.location.lon)
        except Exception as e:
            print("Guruhga yuborib bo'lmadi:", e)
    else:
        print("DIQQAT: ADMIN_GROUP_ID berilmagan, buyurtma guruhga yuborilmadi!")

    # Mijozga
    done_text = (f"✅ <b>Buyurtma №{order['id']} qabul qilindi!</b>\n"
                 f"Jami: {money(total)}\n"
                 f"To'lov: {PAYMENT_NAMES[payment]}\n")
    if payment == "card":
        done_text += (card_info_text()
                      + f"\nIltimos, {money(total)} o'tkazib, chek rasmini shu yerga yuboring 📸")
    else:
        done_text += "\nTo'lovni kuryerga naqd pulda berasiz. Tez orada yetkazib beramiz 🚀"
    try:
        await bot.send_message(uid, done_text)
        await bot.send_message(uid, "Yoqimli ishtaha! 😋", reply_markup=main_kb())
    except Exception as e:
        print("Mijozga xabar yuborib bo'lmadi:", e)

    return {"ok": True, "order_id": order["id"]}

@app.get("/webapp")
async def webapp():
    return FileResponse("static/index.html")


@app.get("/")
async def home():
    return {"message": "Fast Food Bot Server ishlayapti!"}


@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}