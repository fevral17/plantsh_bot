import asyncio
import datetime
import html
import json
import os
import re
import sqlite3
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
MODERATION_CHAT_ID = int(os.getenv("MODERATION_CHAT_ID", "0"))

MSK = datetime.timezone(datetime.timedelta(hours=3))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# --- СБОРЩИК МЕДИА-ГРУПП (АЛЬБОМОВ) ---

class AlbumMiddleware(BaseMiddleware):
    def __init__(self, latency: float = 0.6):
        self.latency = latency
        self.albums: Dict[str, list[types.Message]] = {}

    async def __call__(
        self,
        handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]],
        event: types.Message,
        data: Dict[str, Any]
    ) -> Any:
        if event.chat.type != "private" or not event.media_group_id:
            data["album"] = None
            return await handler(event, data)

        mg_id = event.media_group_id
        if mg_id not in self.albums:
            self.albums[mg_id] = [event]
            await asyncio.sleep(self.latency)
            album_messages = self.albums.pop(mg_id, [])
            data["album"] = album_messages
            return await handler(event, data)
        else:
            self.albums[mg_id].append(event)
            return

dp.message.outer_middleware(AlbumMiddleware())


# --- РАБОТА С БАЗОЙ ДАННЫХ ---

def init_db():
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ads_storage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author_id INTEGER,
            category TEXT,
            location TEXT,
            description TEXT,
            photos_json TEXT,
            formatted_ad TEXT,
            snippet TEXT
        )
    """)

    cur.execute("PRAGMA table_info(scheduled_posts)")
    cols = [r[1] for r in cur.fetchall()]
    if cols and "ad_id" not in cols:
        cur.execute("DROP TABLE IF EXISTS scheduled_posts")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id INTEGER,
            publish_timestamp INTEGER,
            mod_chat_id INTEGER,
            mod_msg_id INTEGER
        )
    """)

    # Хранилище опубликованных постов с текстом для подтверждения удаления
    cur.execute("""
        CREATE TABLE IF NOT EXISTS published_ads (
            lead_channel_msg_id INTEGER PRIMARY KEY,
            all_channel_msg_ids TEXT,
            snippet TEXT
        )
    """)
    try:
        cur.execute("ALTER TABLE published_ads ADD COLUMN snippet TEXT DEFAULT ''")
    except Exception:
        pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS post_comments_map (
            channel_msg_id INTEGER PRIMARY KEY,
            discussion_chat_id INTEGER,
            discussion_msg_id INTEGER
        )
    """)

    conn.commit()
    conn.close()

def save_ad(author_id: int, category: str, location: str, description: str, photos: list[str], formatted_ad: str, snippet: str) -> int:
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ads_storage (author_id, category, location, description, photos_json, formatted_ad, snippet)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (author_id, category, location, description, json.dumps(photos), formatted_ad, snippet))
    ad_id = cur.lastrowid
    conn.commit()
    conn.close()
    return ad_id

def get_ad(ad_id: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("""
        SELECT id, author_id, category, location, description, photos_json, formatted_ad, snippet
        FROM ads_storage WHERE id = ?
    """, (ad_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "author_id": row[1],
        "category": row[2],
        "location": row[3],
        "description": row[4],
        "photos_json": row[5],
        "formatted_ad": row[6],
        "snippet": row[7]
    }

def add_scheduled_post(ad_id: int, publish_timestamp: int, mod_chat_id: int, mod_msg_id: int) -> int:
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO scheduled_posts (ad_id, publish_timestamp, mod_chat_id, mod_msg_id)
        VALUES (?, ?, ?, ?)
    """, (ad_id, publish_timestamp, mod_chat_id, mod_msg_id))
    post_id = cur.lastrowid
    conn.commit()
    conn.close()
    return post_id

def get_scheduled_post(post_id: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("SELECT id, ad_id, publish_timestamp, mod_chat_id, mod_msg_id FROM scheduled_posts WHERE id = ?", (post_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "ad_id": row[1],
        "publish_timestamp": row[2],
        "mod_chat_id": row[3],
        "mod_msg_id": row[4]
    }

def update_scheduled_time(post_id: int, new_timestamp: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("UPDATE scheduled_posts SET publish_timestamp = ? WHERE id = ?", (new_timestamp, post_id))
    conn.commit()
    conn.close()

def get_due_posts(current_timestamp: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("SELECT id, ad_id, mod_chat_id, mod_msg_id FROM scheduled_posts WHERE publish_timestamp <= ?", (current_timestamp,))
    rows = cur.fetchall()
    conn.close()
    return rows

def delete_scheduled_post(post_id: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM scheduled_posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()

def save_published_ad(lead_channel_msg_id: int, all_channel_msg_ids: str, snippet: str = ""):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO published_ads (lead_channel_msg_id, all_channel_msg_ids, snippet)
        VALUES (?, ?, ?)
    """, (lead_channel_msg_id, all_channel_msg_ids, snippet))
    conn.commit()
    conn.close()

def get_published_ad(lead_channel_msg_id: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("SELECT all_channel_msg_ids, snippet FROM published_ads WHERE lead_channel_msg_id = ?", (lead_channel_msg_id,))
    row = cur.fetchone()
    conn.close()
    return row

def delete_published_ad(lead_channel_msg_id: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM published_ads WHERE lead_channel_msg_id = ?", (lead_channel_msg_id,))
    conn.commit()
    conn.close()

def save_discussion_mapping(channel_msg_id: int, discussion_chat_id: int, discussion_msg_id: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO post_comments_map (channel_msg_id, discussion_chat_id, discussion_msg_id)
        VALUES (?, ?, ?)
    """, (channel_msg_id, discussion_chat_id, discussion_msg_id))
    conn.commit()
    conn.close()

def get_discussion_mapping(channel_msg_id: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("SELECT discussion_chat_id, discussion_msg_id FROM post_comments_map WHERE channel_msg_id = ?", (channel_msg_id,))
    row = cur.fetchone()
    conn.close()
    return row

def delete_discussion_mapping(channel_msg_id: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM post_comments_map WHERE channel_msg_id = ?", (channel_msg_id,))
    conn.commit()
    conn.close()


# --- FSM (СОСТОЯНИЯ) ---

class AdForm(StatesGroup):
    category = State()
    location = State()
    content = State()
    confirmation = State()

class ModSchedule(StatesGroup):
    waiting_for_exact_time = State()
    waiting_for_reschedule_time = State()


# --- КЛАВИАТУРЫ ---

async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return False

def get_subscribe_keyboard():
    channel_clean = CHANNEL_ID.lstrip("@")
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Подписаться на канал", url=f"https://t.me/{channel_clean}")
    builder.button(text="🔄 Я подписался", callback_data="check_sub")
    builder.adjust(1)
    return builder.as_markup()

def get_category_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎁 Отдам даром", callback_data="cat:#отдам_даром")
    builder.button(text="🙏 Приму в дар", callback_data="cat:#приму_в_дар")
    builder.adjust(2)
    return builder.as_markup()

def get_new_ad_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Подать ещё одно объявление", callback_data="new_ad")
    return builder.as_markup()

def get_confirmation_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Отправить на модерацию", callback_data="confirm_send")
    builder.button(text="🔄 Заполнить заново", callback_data="restart_ad")
    builder.adjust(1)
    return builder.as_markup()

def get_initial_mod_keyboard(ad_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить", callback_data=f"approve_menu:{ad_id}")
    builder.button(text="❌ Отклонить", callback_data=f"no:{ad_id}")
    builder.adjust(2)
    return builder.as_markup()

def get_schedule_keyboard(ad_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="⚡ Сразу", callback_data=f"pub_now:{ad_id}")
    builder.button(text="⏱ +30 мин", callback_data=f"pub_rel:30:{ad_id}")
    builder.button(text="⏳ +1 час", callback_data=f"pub_rel:60:{ad_id}")
    builder.button(text="⏳ +1.5 часа", callback_data=f"pub_rel:90:{ad_id}")
    builder.button(text="✍️ Указать точное время", callback_data=f"pub_exact:{ad_id}")
    builder.button(text="🔙 Назад", callback_data=f"pub_back:{ad_id}")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()

def get_manage_scheduled_keyboard(post_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="⏱ Перенести время", callback_data=f"resched_menu:{post_id}")
    builder.button(text="🚫 Отменить публикацию", callback_data=f"cancel_sched:{post_id}")
    builder.adjust(2)
    return builder.as_markup()

def get_reschedule_keyboard(post_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="⚡ Опубликовать сейчас", callback_data=f"resched_now:{post_id}")
    builder.button(text="⏱ +30 мин", callback_data=f"resched_rel:30:{post_id}")
    builder.button(text="⏳ +1 час", callback_data=f"resched_rel:60:{post_id}")
    builder.button(text="⏳ +1.5 часа", callback_data=f"resched_rel:90:{post_id}")
    builder.button(text="✍️ Точное время", callback_data=f"resched_exact:{post_id}")
    builder.button(text="🔙 Назад", callback_data=f"resched_back:{post_id}")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()

def get_author_mention(user: types.User) -> str:
    if user.username:
        return f"@{user.username}"
    return f'<a href="tg://user?id={user.id}">{html.escape(user.first_name)}</a>'


# --- ЛОГИКА ПУБЛИКАЦИИ И ПЛАНИРОВЩИКА ---

async def publish_ad_to_channel(ad_id: int):
    ad = get_ad(ad_id)
    if not ad:
        return

    author_id = ad["author_id"]
    formatted_ad = ad["formatted_ad"]
    photos = json.loads(ad["photos_json"])
    snippet = ad["snippet"]

    channel_msg_ids = []

    if not photos:
        msg = await bot.send_message(
            chat_id=CHANNEL_ID,
            text=formatted_ad,
            parse_mode="HTML"
        )
        channel_msg_ids.append(msg.message_id)
    elif len(photos) == 1:
        msg = await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=photos[0],
            caption=formatted_ad,
            parse_mode="HTML"
        )
        channel_msg_ids.append(msg.message_id)
    else:
        media = [types.InputMediaPhoto(media=photos[0], caption=formatted_ad, parse_mode="HTML")]
        for pid in photos[1:10]:
            media.append(types.InputMediaPhoto(media=pid))
        sent_msgs = await bot.send_media_group(
            chat_id=CHANNEL_ID,
            media=media
        )
        channel_msg_ids = [m.message_id for m in sent_msgs]

    lead_id = channel_msg_ids[0]
    all_ids_str = ",".join(str(i) for i in channel_msg_ids)
    save_published_ad(lead_channel_msg_id=lead_id, all_channel_msg_ids=all_ids_str, snippet=snippet or "")

    del_kb = InlineKeyboardBuilder()
    del_kb.button(text="🗑 Удалить объявление из канала", callback_data=f"del_pub:{lead_id}")

    preview_block = ""
    if snippet:
        raw_snippet = snippet.strip()
        short_snippet = raw_snippet[:180] + "..." if len(raw_snippet) > 180 else raw_snippet
        preview_block = f"🌿 <b>Объявление:</b>\n<blockquote>{html.escape(short_snippet)}</blockquote>\n\n"

    try:
        await bot.send_message(
            author_id,
            "🎉 <b>Ваше объявление опубликовано в канале!</b>\n\n"
            f"{preview_block}"
            "Когда растение заберут или объявление потеряет актуальность, "
            "нажмите кнопку ниже, чтобы удалить его из канала и комментариев:",
            parse_mode="HTML",
            reply_markup=del_kb.as_markup()
        )
    except Exception:
        pass


@dp.message(F.is_automatic_forward)
async def capture_discussion_forward(message: types.Message):
    orig_msg_id = None
    if getattr(message, "forward_origin", None) and hasattr(message.forward_origin, "message_id"):
        orig_msg_id = message.forward_origin.message_id
    elif getattr(message, "forward_from_message_id", None):
        orig_msg_id = message.forward_from_message_id

    if orig_msg_id:
        save_discussion_mapping(
            channel_msg_id=orig_msg_id,
            discussion_chat_id=message.chat.id,
            discussion_msg_id=message.message_id
        )


@dp.callback_query(F.data.startswith("del_pub:"))
async def delete_published_ad_callback(callback: types.CallbackQuery):
    lead_id = int(callback.data.split(":")[1])
    pub_data = get_published_ad(lead_id)

    if pub_data:
        all_ids_str, snippet = pub_data
        channel_msg_ids = [int(x) for x in all_ids_str.split(",") if x.strip()]
    else:
        channel_msg_ids = [lead_id]
        snippet = ""

    deleted_any = False

    for c_id in channel_msg_ids:
        try:
            await bot.delete_message(chat_id=CHANNEL_ID, message_id=c_id)
            deleted_any = True
        except Exception:
            pass

        mapping = get_discussion_mapping(c_id)
        if mapping:
            disc_chat_id, disc_msg_id = mapping
            try:
                await bot.delete_message(chat_id=disc_chat_id, message_id=disc_msg_id)
            except Exception:
                pass
            delete_discussion_mapping(c_id)

    delete_published_ad(lead_id)

    if deleted_any:
        quote_block = ""
        if snippet:
            raw_snippet = snippet.strip()
            short_snippet = raw_snippet[:180] + "..." if len(raw_snippet) > 180 else raw_snippet
            quote_block = f"\n\n🌿 <b>Удалённое объявление:</b>\n<blockquote>{html.escape(short_snippet)}</blockquote>"

        await callback.message.edit_text(
            f"✅ Ваше объявление успешно удалено из канала и комментариев.{quote_block}",
            parse_mode="HTML"
        )
        await callback.answer("Объявление удалено!")
    else:
        await callback.answer("⚠️ Не удалось удалить (возможно, оно уже было удалено).", show_alert=True)


async def scheduler_worker():
    while True:
        try:
            current_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            due_posts = get_due_posts(current_ts)
            for row in due_posts:
                post_id, ad_id, mod_chat_id, mod_msg_id = row
                try:
                    await publish_ad_to_channel(ad_id)
                    try:
                        if mod_chat_id and mod_msg_id:
                            await bot.edit_message_reply_markup(chat_id=mod_chat_id, message_id=mod_msg_id, reply_markup=None)
                    except Exception:
                        pass
                except Exception as err:
                    print(f"Ошибка публикации отложенного поста {post_id}: {err}")
                delete_scheduled_post(post_id)
        except Exception as err:
            print(f"Ошибка в работе планировщика: {err}")
        await asyncio.sleep(15)


# --- ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ (АНКЕТА) ---

async def start_ad_creation(target: types.Message | types.CallbackQuery, state: FSMContext):
    text = (
        "👋 Давайте создадим объявление!\n\n"
        "Шаг 1 из 3: Выберите категорию:"
    )
    if isinstance(target, types.CallbackQuery):
        await target.message.answer(text, reply_markup=get_category_keyboard())
    else:
        await target.answer(text, reply_markup=get_category_keyboard())
    await state.set_state(AdForm.category)

@dp.message(CommandStart(), F.chat.type == "private")
async def start_handler(message: types.Message, state: FSMContext):
    await state.clear()
    is_subscribed = await check_subscription(message.from_user.id)
    if not is_subscribed:
        await message.answer(
            "⚠️ Подача объявлений доступна <b>только подписчикам</b> нашего канала.\n\n"
            "Пожалуйста, подпишитесь и нажмите кнопку <b>«Я подписался»</b>:",
            parse_mode="HTML",
            reply_markup=get_subscribe_keyboard()
        )
        return
    await start_ad_creation(message, state)

@dp.callback_query(F.data.in_({"check_sub", "new_ad", "restart_ad"}))
async def sub_or_new_ad_callback(callback: types.CallbackQuery, state: FSMContext):
    is_subscribed = await check_subscription(callback.from_user.id)
    if is_subscribed:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await state.clear()
        await start_ad_creation(callback, state)
    else:
        await callback.answer("❌ Вы ещё не подписались на канал!", show_alert=True)

@dp.message(Command("cancel"), F.chat.type == "private")
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Заполнение отменено.", reply_markup=get_new_ad_keyboard())

@dp.callback_query(AdForm.category, F.data.startswith("cat:"))
async def category_chosen(callback: types.CallbackQuery, state: FSMContext):
    category_tag = callback.data.split(":")[1]
    await state.update_data(category=category_tag)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"Выбрано: <b>{category_tag}</b>\n\nШаг 2 из 3: Укажите вашу <b>станцию метро или район</b>:",
        parse_mode="HTML"
    )
    await state.set_state(AdForm.location)
    await callback.answer()

@dp.message(AdForm.location, F.text)
async def location_chosen(message: types.Message, state: FSMContext):
    await state.update_data(location=message.text.strip())
    await message.answer(
        "Шаг 3 из 3: Отправьте <b>описание объявления</b>.\n\n"
        "💡 Вы можете прислать просто текст или <b>от 1 до 10 фотографий</b> (с описанием в подписи к фото).",
        parse_mode="HTML"
    )
    await state.set_state(AdForm.content)

@dp.message(AdForm.content, F.photo | F.text)
async def content_received(message: types.Message, state: FSMContext, album: list[types.Message] | None = None):
    photo_ids = []
    raw_text = ""

    if album:
        photo_ids = [m.photo[-1].file_id for m in album if m.photo][:10]
        for m in album:
            if m.caption:
                raw_text = m.caption
                break
    elif message.photo:
        photo_ids = [message.photo[-1].file_id]
        raw_text = message.caption or ""
    else:
        raw_text = message.text or ""

    user_description = html.escape(raw_text) if raw_text else "<i>(без описания)</i>"
    await state.update_data(photo_ids=photo_ids, description=user_description)
    data = await state.get_data()

    author_mention = get_author_mention(message.from_user)

    preview_text = (
        "👀 <b>Проверьте ваше объявление перед отправкой:</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{data['category']}\n\n"
        f"📍 <b>Метро / Район:</b> {html.escape(data['location'])}\n\n"
        f"{user_description}\n\n"
        f"👤 <b>Контакты:</b> {author_mention}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Если всё верно, нажмите кнопку внизу:"
    )

    if not photo_ids:
        await message.answer(
            text=preview_text,
            parse_mode="HTML",
            reply_markup=get_confirmation_keyboard()
        )
    elif len(photo_ids) == 1:
        await message.answer_photo(
            photo=photo_ids[0],
            caption=preview_text,
            parse_mode="HTML",
            reply_markup=get_confirmation_keyboard()
        )
    else:
        media = [types.InputMediaPhoto(media=pid) for pid in photo_ids]
        await message.answer_media_group(media=media)
        await message.answer(
            text=preview_text,
            parse_mode="HTML",
            reply_markup=get_confirmation_keyboard()
        )

    await state.set_state(AdForm.confirmation)

@dp.callback_query(AdForm.confirmation, F.data == "confirm_send")
async def confirm_send_callback(callback: types.CallbackQuery, state: FSMContext):
    if not await check_subscription(callback.from_user.id):
        await state.clear()
        await callback.message.answer(
            "⚠️ Вы не подписаны на канал. Подпишитесь, чтобы отправить объявление:",
            reply_markup=get_subscribe_keyboard()
        )
        await callback.answer()
        return

    data = await state.get_data()
    category = data["category"]
    location = html.escape(data["location"])
    user_description = data["description"]
    photo_ids = data.get("photo_ids", [])
    author_mention = get_author_mention(callback.from_user)

    formatted_ad = (
        f"{category}\n\n"
        f"📍 <b>Метро / Район:</b> {location}\n\n"
        f"{user_description}\n\n"
        f"👤 <b>Контакты:</b> {author_mention}"
    )

    snippet = f"{category}\n📍 {location}\n{user_description}"
    ad_id = save_ad(
        author_id=callback.from_user.id,
        category=category,
        location=location,
        description=user_description,
        photos=photo_ids,
        formatted_ad=formatted_ad,
        snippet=snippet
    )

    mod_kb = get_initial_mod_keyboard(ad_id)

    if not photo_ids:
        await bot.send_message(
            chat_id=MODERATION_CHAT_ID,
            text=formatted_ad,
            parse_mode="HTML",
            reply_markup=mod_kb
        )
    elif len(photo_ids) == 1:
        await bot.send_photo(
            chat_id=MODERATION_CHAT_ID,
            photo=photo_ids[0],
            caption=formatted_ad,
            parse_mode="HTML",
            reply_markup=mod_kb
        )
    else:
        media = [types.InputMediaPhoto(media=photo_ids[0], caption=formatted_ad, parse_mode="HTML")]
        for pid in photo_ids[1:]:
            media.append(types.InputMediaPhoto(media=pid))
        album_msgs = await bot.send_media_group(chat_id=MODERATION_CHAT_ID, media=media)
        await bot.send_message(
            chat_id=MODERATION_CHAT_ID,
            text=f"👆 <b>Объявление с альбомом ({len(photo_ids)} фото) выше.</b>",
            parse_mode="HTML",
            reply_to_message_id=album_msgs[0].message_id,
            reply_markup=mod_kb
        )

    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()

    await callback.message.answer(
        "🎉 Спасибо! Ваше объявление передано модераторам.\n"
        "Когда его проверят, вы получите уведомление.",
        reply_markup=get_new_ad_keyboard()
    )
    await callback.answer()

@dp.message(F.chat.type == "private")
async def fallback_handler(message: types.Message):
    await message.answer(
        "Чтобы подать объявление, нажмите кнопку ниже или отправьте команду /start:",
        reply_markup=get_new_ad_keyboard()
    )


# --- ПАНЕЛЬ МОДЕРАЦИИ: ПЕРВИЧНОЕ ОДОБРЕНИЕ ---

@dp.callback_query(F.data.startswith("approve_menu:"))
async def open_time_menu(callback: types.CallbackQuery):
    ad_id = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=get_schedule_keyboard(ad_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("pub_back:"))
async def back_to_approval(callback: types.CallbackQuery):
    ad_id = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=get_initial_mod_keyboard(ad_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("pub_now:"))
async def publish_immediately(callback: types.CallbackQuery):
    ad_id = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=None)
    await publish_ad_to_channel(ad_id)
    await callback.message.reply(f"⚡ Опубликовано сразу модератором {callback.from_user.first_name}")
    await callback.answer("Опубликовано!")

@dp.callback_query(F.data.startswith("pub_rel:"))
async def schedule_relative(callback: types.CallbackQuery):
    _, minutes, ad_id = callback.data.split(":")
    minutes = int(minutes)
    ad_id = int(ad_id)

    ad = get_ad(ad_id)
    if not ad:
        await callback.answer("⚠️ Объявление не найдено!", show_alert=True)
        return

    target_dt_msk = datetime.datetime.now(MSK) + datetime.timedelta(minutes=minutes)
    target_ts = int(target_dt_msk.astimezone(datetime.timezone.utc).timestamp())

    post_id = add_scheduled_post(
        ad_id=ad_id,
        publish_timestamp=target_ts,
        mod_chat_id=callback.message.chat.id,
        mod_msg_id=callback.message.message_id
    )

    time_str = target_dt_msk.strftime("%H:%M")
    await callback.message.edit_reply_markup(reply_markup=get_manage_scheduled_keyboard(post_id))
    await callback.message.reply(
        f"⏳ Одобрено! Запланировано на <b>{time_str} (МСК)</b> модератором {callback.from_user.first_name}",
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            ad["author_id"],
            f"🎉 Ваше объявление одобрено и будет опубликовано в канале сегодня в <b>{time_str} (МСК)</b>!",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await callback.answer("Запланировано!")

@dp.callback_query(F.data.startswith("pub_exact:"))
async def prompt_exact_time(callback: types.CallbackQuery, state: FSMContext):
    ad_id = int(callback.data.split(":")[1])
    await state.set_state(ModSchedule.waiting_for_exact_time)
    await state.update_data(
        ad_id=ad_id,
        target_message_id=callback.message.message_id,
        mod_chat_id=callback.message.chat.id
    )

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(
        "✍️ Отправьте время публикации в формате <b>ЧЧ:ММ</b> по МСК (например, <code>18:30</code>):",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(ModSchedule.waiting_for_exact_time, F.text)
async def process_exact_time(message: types.Message, state: FSMContext):
    text = message.text.strip()
    match = re.match(r"^([0-1]?[0-9]|2[0-3]):([0-5][0-9])$", text)
    
    if not match:
        await message.reply("⚠️ Неверный формат! Введите время в виде <b>ЧЧ:ММ</b> (например, <code>14:30</code>):", parse_mode="HTML")
        return

    hours, minutes = int(match.group(1)), int(match.group(2))
    now_msk = datetime.datetime.now(MSK)
    scheduled_dt_msk = now_msk.replace(hour=hours, minute=minutes, second=0, microsecond=0)

    day_label = "сегодня"
    if scheduled_dt_msk <= now_msk:
        scheduled_dt_msk += datetime.timedelta(days=1)
        day_label = "завтра"

    target_ts = int(scheduled_dt_msk.astimezone(datetime.timezone.utc).timestamp())
    data = await state.get_data()
    ad_id = data["ad_id"]
    ad = get_ad(ad_id)

    post_id = add_scheduled_post(
        ad_id=ad_id,
        publish_timestamp=target_ts,
        mod_chat_id=data["mod_chat_id"],
        mod_msg_id=data["target_message_id"]
    )

    time_str = scheduled_dt_msk.strftime("%H:%M")
    
    try:
        await bot.edit_message_reply_markup(
            chat_id=data["mod_chat_id"],
            message_id=data["target_message_id"],
            reply_markup=get_manage_scheduled_keyboard(post_id)
        )
    except Exception:
        pass

    await message.reply(
        f"⏳ Запланировано на <b>{day_label} в {time_str} (МСК)</b>!",
        parse_mode="HTML"
    )

    if ad:
        try:
            await bot.send_message(
                ad["author_id"],
                f"🎉 Ваше объявление одобрено! Оно будет опубликовано <b>{day_label} в {time_str} (МСК)</b>.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await state.clear()


# --- ПАНЕЛЬ МОДЕРАЦИИ: ПЕРЕНОС И ОТМЕНА ОЧЕРЕДИ ---

@dp.callback_query(F.data.startswith("cancel_sched:"))
async def cancel_scheduled_handler(callback: types.CallbackQuery):
    post_id = int(callback.data.split(":")[1])
    post = get_scheduled_post(post_id)

    if not post:
        await callback.answer("⚠️ Этот пост уже опубликован или отменён!", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    ad_id = post["ad_id"]
    ad = get_ad(ad_id)
    delete_scheduled_post(post_id)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"🚫 Публикация отменена модератором {callback.from_user.first_name}")

    if ad:
        try:
            await bot.send_message(ad["author_id"], "❌ Публикация вашего объявления была отменена модератором.")
        except Exception:
            pass

    await callback.answer("Публикация отменена")

@dp.callback_query(F.data.startswith("resched_menu:"))
async def reschedule_menu_handler(callback: types.CallbackQuery):
    post_id = int(callback.data.split(":")[1])
    post = get_scheduled_post(post_id)

    if not post:
        await callback.answer("⚠️ Этот пост уже опубликован или отменён!", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    await callback.message.edit_reply_markup(reply_markup=get_reschedule_keyboard(post_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("resched_back:"))
async def reschedule_back_handler(callback: types.CallbackQuery):
    post_id = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=get_manage_scheduled_keyboard(post_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("resched_now:"))
async def reschedule_now_handler(callback: types.CallbackQuery):
    post_id = int(callback.data.split(":")[1])
    post = get_scheduled_post(post_id)

    if not post:
        await callback.answer("⚠️ Этот пост уже опубликован или отменён!", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    ad_id = post["ad_id"]
    delete_scheduled_post(post_id)
    await callback.message.edit_reply_markup(reply_markup=None)

    await publish_ad_to_channel(ad_id)
    await callback.message.reply(f"⚡ Опубликовано прямо сейчас модератором {callback.from_user.first_name}")
    await callback.answer("Опубликовано!")

@dp.callback_query(F.data.startswith("resched_rel:"))
async def reschedule_relative_handler(callback: types.CallbackQuery):
    _, minutes, post_id = callback.data.split(":")
    minutes = int(minutes)
    post_id = int(post_id)

    post = get_scheduled_post(post_id)
    if not post:
        await callback.answer("⚠️ Этот пост уже опубликован или отменён!", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    ad = get_ad(post["ad_id"])
    new_dt_msk = datetime.datetime.now(MSK) + datetime.timedelta(minutes=minutes)
    new_ts = int(new_dt_msk.astimezone(datetime.timezone.utc).timestamp())

    update_scheduled_time(post_id, new_ts)
    time_str = new_dt_msk.strftime("%H:%M")

    await callback.message.edit_reply_markup(reply_markup=get_manage_scheduled_keyboard(post_id))
    await callback.message.reply(
        f"🔄 Публикация перенесена на <b>{time_str} (МСК)</b> модератором {callback.from_user.first_name}",
        parse_mode="HTML"
    )

    if ad:
        try:
            await bot.send_message(
                ad["author_id"],
                f"ℹ️ Время публикации вашего объявления перенесено на <b>{time_str} (МСК)</b>.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await callback.answer("Время изменено!")

@dp.callback_query(F.data.startswith("resched_exact:"))
async def reschedule_prompt_exact_handler(callback: types.CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split(":")[1])
    post = get_scheduled_post(post_id)

    if not post:
        await callback.answer("⚠️ Этот пост уже опубликован или отменён!", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    await state.set_state(ModSchedule.waiting_for_reschedule_time)
    await state.update_data(post_id=post_id, target_message_id=callback.message.message_id)

    await callback.message.reply(
        "✍️ Отправьте новое время публикации в формате <b>ЧЧ:ММ</b> по МСК (например, <code>20:15</code>):",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(ModSchedule.waiting_for_reschedule_time, F.text)
async def process_reschedule_exact_time(message: types.Message, state: FSMContext):
    text = message.text.strip()
    match = re.match(r"^([0-1]?[0-9]|2[0-3]):([0-5][0-9])$", text)
    
    if not match:
        await message.reply("⚠️ Неверный формат! Введите время в виде <b>ЧЧ:ММ</b> (например, <code>19:00</code>):", parse_mode="HTML")
        return

    hours, minutes = int(match.group(1)), int(match.group(2))
    now_msk = datetime.datetime.now(MSK)
    scheduled_dt_msk = now_msk.replace(hour=hours, minute=minutes, second=0, microsecond=0)

    day_label = "сегодня"
    if scheduled_dt_msk <= now_msk:
        scheduled_dt_msk += datetime.timedelta(days=1)
        day_label = "завтра"

    target_ts = int(scheduled_dt_msk.astimezone(datetime.timezone.utc).timestamp())
    data = await state.get_data()
    post_id = data["post_id"]

    post = get_scheduled_post(post_id)
    if not post:
        await message.reply("⚠️ Этот пост уже опубликован или удалён.")
        await state.clear()
        return

    ad = get_ad(post["ad_id"])
    update_scheduled_time(post_id, target_ts)
    time_str = scheduled_dt_msk.strftime("%H:%M")

    try:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=data["target_message_id"],
            reply_markup=get_manage_scheduled_keyboard(post_id)
        )
    except Exception:
        pass

    await message.reply(
        f"🔄 Публикация перенесена на <b>{day_label} в {time_str} (МСК)</b>!",
        parse_mode="HTML"
    )

    if ad:
        try:
            await bot.send_message(
                ad["author_id"],
                f"ℹ️ Время публикации вашего объявления перенесено на <b>{day_label} в {time_str} (МСК)</b>.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await state.clear()

@dp.callback_query(F.data.startswith("no:"))
async def reject_handler(callback: types.CallbackQuery):
    ad_id = int(callback.data.split(":")[1])
    ad = get_ad(ad_id)
    if ad:
        try:
            await bot.send_message(ad["author_id"], "❌ Ваше объявление не прошло модерацию.")
        except Exception:
            pass

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"❌ Отклонено ({callback.from_user.first_name})")
    await callback.answer("Отклонено")


# --- ЗАПУСК БОТА ---

async def main():
    init_db()
    asyncio.create_task(scheduler_worker())

    await bot.set_my_commands([
        types.BotCommand(command="start", description="Подать объявление"),
        types.BotCommand(command="cancel", description="Отменить заполнение")
    ])
    print("Бот успешно запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
