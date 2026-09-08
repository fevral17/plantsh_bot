import asyncio
import html
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- СЧИТЫВАНИЕ ДАННЫХ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
MODERATION_CHAT_ID = int(os.getenv("MODERATION_CHAT_ID", "0"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- FSM (СОСТОЯНИЯ АНКЕТЫ) ---
class AdForm(StatesGroup):
    category = State()
    location = State()
    content = State()


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И КЛАВИАТУРЫ ---

# Проверка подписки пользователя на канал
async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        # Статусы, означающие, что человек состоит в канале
        return member.status not in ("left", "kicked")
    except Exception:
        # Если бот не может проверить (например, не добавлен в админы)
        return False


# Клавиатура с предложением подписаться
def get_subscribe_keyboard():
    channel_clean = CHANNEL_ID.lstrip("@")
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Подписаться на канал", url=f"https://t.me/{channel_clean}")
    builder.button(text="🔄 Я подписался", callback_data="check_sub")
    builder.adjust(1)
    return builder.as_markup()


# Клавиатура выбора категории
def get_category_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎁 Отдам даром", callback_data="cat:#отдам_даром")
    builder.button(text="🙏 Приму в дар", callback_data="cat:#приму_в_дар")
    builder.adjust(2)
    return builder.as_markup()


# Запуск создания объявления
async def start_ad_creation(target: types.Message | types.CallbackQuery, state: FSMContext):
    text = (
        "👋 Здравствуйте! Давайте создадим объявление.\n\n"
        "Шаг 1 из 3: Выберите подходящую категорию:"
    )
    if isinstance(target, types.CallbackQuery):
        await target.message.answer(text, reply_markup=get_category_keyboard())
    else:
        await target.answer(text, reply_markup=get_category_keyboard())
    await state.set_state(AdForm.category)


# --- СТАРТ И ПРОВЕРКА ПОДПИСКИ ---

@dp.message(CommandStart(), F.chat.type == "private")
async def start_handler(message: types.Message, state: FSMContext):
    await state.clear()
    is_subscribed = await check_subscription(message.from_user.id)

    if not is_subscribed:
        await message.answer(
            "⚠️ Подача объявлений доступна <b>только подписчикам</b> нашего канала.\n\n"
            "Пожалуйста, подпишитесь на канал и нажмите кнопку <b>«Я подписался»</b> ниже:",
            parse_mode="HTML",
            reply_markup=get_subscribe_keyboard()
        )
        return

    await start_ad_creation(message, state)


# Обработка клика по кнопке «Я подписался»
@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(callback: types.CallbackQuery, state: FSMContext):
    is_subscribed = await check_subscription(callback.from_user.id)

    if is_subscribed:
        await callback.message.delete()
        await start_ad_creation(callback, state)
    else:
        await callback.answer("❌ Вы ещё не подписались на канал!", show_alert=True)


@dp.message(Command("cancel"), F.chat.type == "private")
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Заполнение объявления отменено. Чтобы начать заново, отправьте /start.")


# --- ШАГИ АНКЕТЫ ---

@dp.callback_query(AdForm.category, F.data.startswith("cat:"))
async def category_chosen(callback: types.CallbackQuery, state: FSMContext):
    category_tag = callback.data.split(":")[1]
    await state.update_data(category=category_tag)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"Выбрано: <b>{category_tag}</b>\n\n"
        "Шаг 2 из 3: Укажите вашу <b>станцию метро или район</b>:",
        parse_mode="HTML"
    )
    await state.set_state(AdForm.location)
    await callback.answer()


@dp.message(AdForm.location, F.text)
async def location_chosen(message: types.Message, state: FSMContext):
    await state.update_data(location=message.text.strip())
    await message.answer(
        "Шаг 3 из 3: Отправьте <b>описание объявления</b>.\n\n"
        "💡 Вы можете прислать просто текст или <b>фотографию с описанием</b>.",
        parse_mode="HTML"
    )
    await state.set_state(AdForm.content)


@dp.message(AdForm.content, F.photo | F.text)
async def content_received(message: types.Message, state: FSMContext):
    # Дополнительная страховочная проверка подписки перед отправкой на модерацию
    if not await check_subscription(message.from_user.id):
        await state.clear()
        await message.answer(
            "⚠️ Вы отписались от канала в процессе заполнения. "
            "Пожалуйста, подпишитесь снова, чтобы отправить объявление:",
            reply_markup=get_subscribe_keyboard()
        )
        return

    data = await state.get_data()
    category = data["category"]
    location = html.escape(data["location"])

    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id
        raw_text = message.caption or ""
    else:
        raw_text = message.text or ""

    user_description = html.escape(raw_text)

    # Собираем пост
    formatted_ad = (
        f"{category}\n\n"
        f"📍 <b>Метро / Район:</b> {location}\n\n"
        f"{user_description}"
    )

    # Кнопки для админов
    mod_kb = InlineKeyboardBuilder()
    mod_kb.button(text="✅ Одобрить", callback_data=f"ok:{message.from_user.id}")
    mod_kb.button(text="❌ Отклонить", callback_data=f"no:{message.from_user.id}")
    mod_kb.adjust(2)

    # Пересылаем модераторам
    if photo_id:
        await bot.send_photo(
            chat_id=MODERATION_CHAT_ID,
            photo=photo_id,
            caption=formatted_ad,
            parse_mode="HTML",
            reply_markup=mod_kb.as_markup()
        )
    else:
        await bot.send_message(
            chat_id=MODERATION_CHAT_ID,
            text=formatted_ad,
            parse_mode="HTML",
            reply_markup=mod_kb.as_markup()
        )

    await state.clear()
    await message.answer(
        "🎉 Спасибо! Ваше объявление оформлено и передано модераторам.\n"
        "Когда его проверят, вы получите уведомление."
    )


# --- ПАНЕЛЬ МОДЕРАЦИИ ---

@dp.callback_query(F.data.startswith("ok:"))
async def approve_handler(callback: types.CallbackQuery):
    _, author_id = callback.data.split(":")
    author_id = int(author_id)

    try:
        author_chat = await bot.get_chat(author_id)
        author_username = author_chat.username
    except Exception:
        author_username = None

    post_kb = InlineKeyboardBuilder()
    if author_username:
        post_kb.button(text="💬 Написать автору", url=f"https://t.me/{author_username}")
    else:
        post_kb.button(text="💬 Написать автору", url=f"tg://openmessage?user_id={author_id}")

    # Публикуем в публичный канал
    await bot.copy_message(
        chat_id=CHANNEL_ID,
        from_chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        reply_markup=post_kb.as_markup()
    )

    try:
        await bot.send_message(author_id, "🎉 Ваше объявление опубликовано в канале!")
    except Exception:
        pass

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"✅ Опубликовано модератором {callback.from_user.first_name}")
    await callback.answer()


@dp.callback_query(F.data.startswith("no:"))
async def reject_handler(callback: types.CallbackQuery):
    _, author_id = callback.data.split(":")
    author_id = int(author_id)

    try:
        await bot.send_message(author_id, "❌ Ваше объявление не прошло модерацию.")
    except Exception:
        pass

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"❌ Отклонено модератором {callback.from_user.first_name}")
    await callback.answer()


async def main():
    print("Бот успешно запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())