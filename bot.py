import asyncio
import datetime
import html
import os
import re
import sqlite3
from aiogram import Bot, Dispatcher, F, types
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


# --- РАБОТА С БАЗОЙ ДАННЫХ (ОЧЕРЕДЬ) ---

def init_db():
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT,
            from_chat_id INTEGER,
            message_id INTEGER,
            author_id INTEGER,
            publish_timestamp INTEGER,
            snippet TEXT
        )
    """)
    try:
        cur.execute("ALTER TABLE scheduled_posts ADD COLUMN snippet TEXT DEFAULT ''")
    except Exception:
        pass
    conn.commit()
    conn.close()

def add_scheduled_post(channel_id: str, from_chat_id: int, message_id: int, author_id: int, publish_timestamp: int, snippet: str = "") -> int:
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO scheduled_posts (channel_id, from_chat_id, message_id, author_id, publish_timestamp, snippet)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (channel_id, from_chat_id, message_id, author_id, publish_timestamp, snippet))
    post_id = cur.lastrowid
    conn.commit()
    conn.close()
    return post_id

def get_scheduled_post(post_id: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("SELECT id, channel_id, from_chat_id, message_id, author_id, publish_timestamp, snippet FROM scheduled_posts WHERE id = ?", (post_id,))
    row = cur.fetchone()
    conn.close()
    return row

def update_scheduled_time(post_id: int, new_timestamp: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("UPDATE scheduled_posts SET publish_timestamp = ? WHERE id = ?", (new_timestamp, post_id))
    conn.commit()
    conn.close()

def get_due_posts(current_timestamp: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("SELECT id, channel_id, from_chat_id, message_id, author_id, snippet FROM scheduled_posts WHERE publish_timestamp <= ?", (current_timestamp,))
    rows = cur.fetchall()
    conn.close()
    return rows

def delete_scheduled_post(post_id: int):
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM scheduled_posts WHERE id = ?", (post_id,))
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

def get_initial_mod_keyboard(author_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить", callback_data=f"approve_menu:{author_id}")
    builder.button(text="❌ Отклонить", callback_data=f"no:{author_id}")
    builder.adjust(2)
    return builder.as_markup()

def get_schedule_keyboard(author_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="⚡ Сразу", callback_data=f"pub_now:{author_id}")
    builder.button(text="⏱ +30 мин", callback_data=f"pub_rel:30:{author_id}")
    builder.button(text="⏳ +1 час", callback_data=f"pub_rel:60:{author_id}")
    builder.button(text="⏳ +1.5 часа", callback_data=f"pub_rel:90:{author_id}")
    builder.button(text="✍️ Указать точное время", callback_data=f"pub_exact:{author_id}")
    builder.button(text="🔙 Назад", callback_data=f"pub_back:{author_id}")
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


# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ССЫЛКИ НА АВТОРА ---

def get_author_mention(user: types.User) -> str:
    if user.username:
        return f"@{user.username}"
    return f'<a href="tg://user?id={user.id}">{html.escape(user.first_name)}</a>'


# --- ЛОГИКА ПУБЛИКАЦИИ, УДАЛЕНИЯ И ПЛАНИРОВЩИКА ---

async def publish_ad_to_channel(channel_id: str, from_chat_id: int, message_id: int, author_id: int, snippet: str = ""):
    # Публикация в канал без инлайн-кнопки (контакты уже внутри текста, а плашка комментариев видна сразу)
    sent_msg = await bot.copy_message(
        chat_id=channel_id,
        from_chat_id=from_chat_id,
        message_id=message_id
    )

    del_kb = InlineKeyboardBuilder()
    del_kb.button(text="🗑 Удалить объявление из канала", callback_data=f"del_pub:{sent_msg.message_id}")

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
            "нажмите кнопку ниже, чтобы удалить его из канала:",
            parse_mode="HTML",
            reply_markup=del_kb.as_markup()
        )
    except Exception:
        pass


@dp.callback_query(F.data.startswith("del_pub:"))
async def delete_published_ad_callback(callback: types.CallbackQuery):
    channel_msg_id = int(callback.data.split(":")[1])
    try:
        await bot.delete_message(chat_id=CHANNEL_ID, message_id=channel_msg_id)
        await callback.message.edit_text("✅ Ваше объявление успешно удалено из канала.")
        await callback.answer("Объявление удалено!")
    except Exception:
        await callback.answer("⚠️ Не удалось удалить объявление (возможно, оно уже было удалено из канала).", show_alert=True)


async def scheduler_worker():
    while True:
        try:
            current_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            due_posts = get_due_posts(current_ts)
            for row in due_posts:
                post_id, ch_id, f_chat, msg_id, author_id, snippet = row
                try:
                    await publish_ad_to_channel(ch_id, f_chat, msg_id, author_id, snippet=snippet or "")
                    try:
                        await bot.edit_message_reply_markup(chat_id=f_chat, message_id=msg_id, reply_markup=None)
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
        "💡 Вы можете прислать текст или <b>фотографию с описанием</b>.",
        parse_mode="HTML"
    )
    await state.set_state(AdForm.content)

@dp.message(AdForm.content, F.photo | F.text)
async def content_received(message: types.Message, state: FSMContext):
    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id
        raw_text = message.caption or ""
    else:
        raw_text = message.text or ""

    user_description = html.escape(raw_text)
    await state.update_data(photo_id=photo_id, description=user_description)
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

    if photo_id:
        await message.answer_photo(
            photo=photo_id,
            caption=preview_text,
            parse_mode="HTML",
            reply_markup=get_confirmation_keyboard()
        )
    else:
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
    photo_id = data.get("photo_id")
    author_mention = get_author_mention(callback.from_user)

    formatted_ad = (
        f"{category}\n\n"
        f"📍 <b>Метро / Район:</b> {location}\n\n"
        f"{user_description}\n\n"
        f"👤 <b>Контакты:</b> {author_mention}"
    )

    mod_kb = get_initial_mod_keyboard(callback.from_user.id)

    if photo_id:
        await bot.send_photo(
            chat_id=MODERATION_CHAT_ID,
            photo=photo_id,
            caption=formatted_ad,
            parse_mode="HTML",
            reply_markup=mod_kb
        )
    else:
        await bot.send_message(
            chat_id=MODERATION_CHAT_ID,
            text=formatted_ad,
            parse_mode="HTML",
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
    author_id = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=get_schedule_keyboard(author_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("pub_back:"))
async def back_to_approval(callback: types.CallbackQuery):
    author_id = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=get_initial_mod_keyboard(author_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("pub_now:"))
async def publish_immediately(callback: types.CallbackQuery):
    author_id = int(callback.data.split(":")[1])
    snippet = callback.message.caption or callback.message.text or ""
    await callback.message.edit_reply_markup(reply_markup=None)
    
    await publish_ad_to_channel(
        channel_id=CHANNEL_ID,
        from_chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        author_id=author_id,
        snippet=snippet
    )

    await callback.message.reply(f"⚡ Опубликовано сразу модератором {callback.from_user.first_name}")
    await callback.answer("Опубликовано!")

@dp.callback_query(F.data.startswith("pub_rel:"))
async def schedule_relative(callback: types.CallbackQuery):
    _, minutes, author_id = callback.data.split(":")
    minutes = int(minutes)
    author_id = int(author_id)
    snippet = callback.message.caption or callback.message.text or ""

    target_dt_msk = datetime.datetime.now(MSK) + datetime.timedelta(minutes=minutes)
    target_ts = int(target_dt_msk.astimezone(datetime.timezone.utc).timestamp())

    post_id = add_scheduled_post(
        channel_id=CHANNEL_ID,
        from_chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        author_id=author_id,
        publish_timestamp=target_ts,
        snippet=snippet
    )

    time_str = target_dt_msk.strftime("%H:%M")
    await callback.message.edit_reply_markup(reply_markup=get_manage_scheduled_keyboard(post_id))
    await callback.message.reply(
        f"⏳ Одобрено! Запланировано на <b>{time_str} (МСК)</b> модератором {callback.from_user.first_name}",
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            author_id,
            f"🎉 Ваше объявление одобрено и будет опубликовано в канале сегодня в <b>{time_str} (МСК)</b>!",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await callback.answer("Запланировано!")

@dp.callback_query(F.data.startswith("pub_exact:"))
async def prompt_exact_time(callback: types.CallbackQuery, state: FSMContext):
    author_id = int(callback.data.split(":")[1])
    snippet = callback.message.caption or callback.message.text or ""
    await state.set_state(ModSchedule.waiting_for_exact_time)
    await state.update_data(
        author_id=author_id,
        target_message_id=callback.message.message_id,
        mod_chat_id=callback.message.chat.id,
        snippet=snippet
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

    post_id = add_scheduled_post(
        channel_id=CHANNEL_ID,
        from_chat_id=data["mod_chat_id"],
        message_id=data["target_message_id"],
        author_id=data["author_id"],
        publish_timestamp=target_ts,
        snippet=data.get("snippet", "")
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

    try:
        await bot.send_message(
            data["author_id"],
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

    author_id = post[4]
    delete_scheduled_post(post_id)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"🚫 Публикация отменена модератором {callback.from_user.first_name}")

    try:
        await bot.send_message(author_id, "❌ Публикация вашего объявления была отменена модератором.")
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

    _, ch_id, f_chat, msg_id, author_id, _, snippet = post
    delete_scheduled_post(post_id)
    await callback.message.edit_reply_markup(reply_markup=None)

    await publish_ad_to_channel(ch_id, f_chat, msg_id, author_id, snippet=snippet or "")
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

    author_id = post[4]
    new_dt_msk = datetime.datetime.now(MSK) + datetime.timedelta(minutes=minutes)
    new_ts = int(new_dt_msk.astimezone(datetime.timezone.utc).timestamp())

    update_scheduled_time(post_id, new_ts)
    time_str = new_dt_msk.strftime("%H:%M")

    await callback.message.edit_reply_markup(reply_markup=get_manage_scheduled_keyboard(post_id))
    await callback.message.reply(
        f"🔄 Публикация перенесена на <b>{time_str} (МСК)</b> модератором {callback.from_user.first_name}",
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            author_id,
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

    author_id = post[4]
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

    try:
        await bot.send_message(
            author_id,
            f"ℹ️ Время публикации вашего объявления перенесено на <b>{day_label} в {time_str} (МСК)</b>.",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await state.clear()

@dp.callback_query(F.data.startswith("no:"))
async def reject_handler(callback: types.CallbackQuery):
    author_id = int(callback.data.split(":")[1])
    try:
        await bot.send_message(author_id, "❌ Ваше объявление не прошло модерацию.")
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
    print("Бот успешно запущен: контакты в посте, комментарии активны...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
