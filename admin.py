import logging
from aiogram import Router, types, F
from aiogram.filters import Command
from sqlalchemy import select, func, delete
from aiogram.types import InlineKeyboardButton, BufferedInputFile, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters.state import StateFilter
import pandas as pd
import os
from aiogram.exceptions import TelegramBadRequest
from datetime import datetime

from database import (
    AsyncSessionLocal,
    ConferenceCreationRequest,
    ConferenceEditRequest,
    Conference,
    Application,
    User,
    Role,
    get_or_create_user,
    DeletedConference,
    get_bot_status,
    set_bot_paused,
    SupportRequest
)
from sqlalchemy.orm import joinedload
from database import ConferenceRating  # ← добавь
from keyboards import get_main_menu_keyboard, get_cancel_keyboard
from config import CHIEF_ADMIN_IDS, TECH_SPECIALIST_ID

router = Router()

# States для админских действий
class AdminStates(StatesGroup):
    waiting_pause_reason = State()
    delete_conf_reason = State()
    waiting_support_reply = State()  # ← Новое состояние для ответа на обращение

# Пагинация для обращений
edit_pagination = {}
support_pagination = {}
create_pagination = {}

# Проверки ролей
async def is_admin_or_chief(user_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return False
        return user.role in [Role.ADMIN.value, Role.CHIEF_ADMIN.value] if user else False

async def is_chief_admin(user_id: int) -> bool:
    return user_id in CHIEF_ADMIN_IDS

from config import TECH_SPECIALIST_ID

async def is_chief_tech(user_id: int) -> bool:
    return user_id == TECH_SPECIALIST_ID

async def can_delete_conference(user_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return False
        return user.role in [Role.ADMIN.value, Role.CHIEF_ADMIN.value, Role.CHIEF_TECH.value] if user else False

async def can_pause_bot(user_id: int) -> bool:
    return user_id in CHIEF_ADMIN_IDS or await is_chief_tech(user_id)

async def can_view_conferences(user_id: int) -> bool:
    return await is_admin_or_chief(user_id) or await is_chief_tech(user_id)

# Универсальная функция обновления списка всех заявок (создание + редактирование + апелляции)
async def update_requests_message(event: types.Message | types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        create_requests = (await session.execute(
            select(ConferenceCreationRequest).where(ConferenceCreationRequest.status == "pending")
        )).scalars().all()

        edit_requests = (await session.execute(
            select(ConferenceEditRequest).where(ConferenceEditRequest.status == "pending")
        )).scalars().all()

        appeal_requests = (await session.execute(
            select(ConferenceCreationRequest).where(
                ConferenceCreationRequest.status == "rejected",
                ConferenceCreationRequest.appeal == True
            )
        )).scalars().all()

        if not create_requests and not edit_requests and not appeal_requests:
            text = "Нет активных заявок."
            if isinstance(event, types.Message):
                await event.answer(text)
            else:
                await event.message.edit_text(text)
            return

        if create_requests:
            await event.bot.send_message(event.from_user.id, "<b>Заявки на создание конференций:</b>")
            for req in create_requests:
                user = await session.get(User, req.user_id)
                data = req.data

                text = f"ID: <code>{req.id}</code>\n"
                text += f"От: {user.full_name or user.telegram_id}\n\n"
                text += f"<b>Название:</b> {data.get('name', '—')}\n"
                if data.get('description'):
                    text += f"<b>Описание:</b>\n{data.get('description')}\n\n"
                text += f"<b>Город:</b> {data.get('city', 'Онлайн')}\n"
                text += f"<b>Дата проведения:</b> {data.get('date', '—')}\n"
                text += f"<b>Орг взнос:</b> {int(data.get('fee', 0))} сом\n"

                builder = InlineKeyboardBuilder()
                builder.row(
                    InlineKeyboardButton(text="Одобрить", callback_data=f"conf_create_approve_{req.id}"),
                    InlineKeyboardButton(text="Отклонить", callback_data=f"conf_create_reject_{req.id}")
                )

                if data.get('poster_path') and os.path.exists(data['poster_path']):
                    photo = FSInputFile(data['poster_path'])
                    await event.bot.send_photo(event.from_user.id, photo, caption=text, reply_markup=builder.as_markup())
                else:
                    await event.bot.send_message(event.from_user.id, text, reply_markup=builder.as_markup())

        if edit_requests:
            await event.bot.send_message(event.from_user.id, "<b>Заявки на редактирование:</b>")
            for req in edit_requests:
                conf = await session.get(Conference, req.conference_id)
                organizer = await session.get(User, req.organizer_id)
                data = req.data

                text = f"ID: <code>{req.id}</code>\n"
                text += f"Конференция: <b>{conf.name}</b>\n"
                text += f"От: {organizer.full_name or organizer.telegram_id}\n\n"
                text += f"<b>Текущие данные:</b>\n"
                text += f"Название: {conf.name}\n"
                if conf.description:
                    text += f"Описание: {conf.description}\n"
                text += f"Город: {conf.city or 'Онлайн'}\n"
                text += f"Дата проведения: {conf.date}\n"
                text += f"Орг взнос: {conf.fee} руб.\n\n"
                text += f"<b>Новые данные:</b>\n"
                text += f"Название: {data.get('name', conf.name)}\n"
                if data.get('description') is not None:
                    text += f"Описание: {data.get('description') or '(удалено)'}\n"
                text += f"Город: {data.get('city', conf.city)}\n"
                text += f"Дата проведения: {data.get('date', conf.date)}\n"
                text += f"Орг взнос: {data.get('fee', conf.fee)} руб.\n"

                builder = InlineKeyboardBuilder()
                builder.row(
                    InlineKeyboardButton(text="Одобрить", callback_data=f"conf_edit_approve_{req.id}"),
                    InlineKeyboardButton(text="Отклонить", callback_data=f"conf_edit_reject_{req.id}")
                )

                if data.get('poster_path') and os.path.exists(data['poster_path']):
                    photo = FSInputFile(data['poster_path'])
                    await event.bot.send_photo(event.from_user.id, photo, caption=text, reply_markup=builder.as_markup())
                else:
                    if conf.poster_path and os.path.exists(conf.poster_path):
                        photo = FSInputFile(conf.poster_path)
                        await event.bot.send_photo(event.from_user.id, photo, caption=text, reply_markup=builder.as_markup())
                    else:
                        await event.bot.send_message(event.from_user.id, text, reply_markup=builder.as_markup())

        if appeal_requests:
            await event.bot.send_message(event.from_user.id, "<b>Апелляции к Глав Админу:</b>")
            for req in appeal_requests:
                user = await session.get(User, req.user_id)
                data = req.data

                text = f"ID: <code>{req.id}</code> (апелляция)\n"
                text += f"От: {user.full_name or user.telegram_id}\n\n"
                text += f"Название: {data.get('name')}\n"
                if data.get('description'):
                    text += f"Описание: {data.get('description')}\n"
                text += f"Город: {data.get('city')}\n"
                text += f"Дата проведения: {data.get('date')}\n"
                text += f"Орг взнос: {int(data.get('fee', 0))} сом\n"

                builder = InlineKeyboardBuilder()
                builder.row(
                    InlineKeyboardButton(text="Одобрить", callback_data=f"conf_appeal_approve_{req.id}"),
                    InlineKeyboardButton(text="Отклонить", callback_data=f"conf_appeal_reject_{req.id}")
                )

                if data.get('poster_path') and os.path.exists(data['poster_path']):
                    photo = FSInputFile(data['poster_path'])
                    await event.bot.send_photo(event.from_user.id, photo, caption=text, reply_markup=builder.as_markup())
                else:
                    await event.bot.send_message(event.from_user.id, text, reply_markup=builder.as_markup())

# Функция для заявок на редактирование
async def update_edit_requests_message(event: types.Message | types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        edit_requests = (await session.execute(
            select(ConferenceEditRequest).where(ConferenceEditRequest.status == "pending")
        )).scalars().all()

        if not edit_requests:
            text = "Нет заявок на редактирование конференций."
            if isinstance(event, types.Message):
                await event.answer(text)
            else:
                await event.message.edit_text(text)
            return

        await event.bot.send_message(event.from_user.id, "<b>Заявки на редактирование конференций:</b>")
        for req in edit_requests:
            conf = await session.get(Conference, req.conference_id)
            organizer = await session.get(User, req.organizer_id)
            data = req.data

            text = f"ID: <code>{req.id}</code>\n"
            text += f"Конференция: <b>{conf.name}</b>\n"
            text += f"От: {organizer.full_name or organizer.telegram_id}\n\n"
            text += f"<b>Текущие данные:</b>\n"
            text += f"Название: {conf.name}\n"
            if conf.description:
                text += f"Описание: {conf.description}\n"
            text += f"Город: {conf.city or 'Онлайн'}\n"
            text += f"Дата проведения: {conf.date}\n"
            text += f"Орг взнос: {conf.fee} руб.\n\n"
            text += f"<b>Новые данные:</b>\n"
            text += f"Название: {data.get('name', conf.name)}\n"
            if data.get('description') is not None:
                text += f"Описание: {data.get('description') or '(удалено)'}\n"
            text += f"Город: {data.get('city', conf.city)}\n"
            text += f"Дата проведения: {data.get('date', conf.date)}\n"
            text += f"Орг взнос: {data.get('fee', conf.fee)} руб.\n"

            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="Одобрить", callback_data=f"conf_edit_approve_{req.id}"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"conf_edit_reject_{req.id}")
            )

            if data.get('poster_path') and os.path.exists(data['poster_path']):
                photo = FSInputFile(data['poster_path'])
                await event.bot.send_photo(event.from_user.id, photo, caption=text, reply_markup=builder.as_markup())
            else:
                if conf.poster_path and os.path.exists(conf.poster_path):
                    photo = FSInputFile(conf.poster_path)
                    await event.bot.send_photo(event.from_user.id, photo, caption=text, reply_markup=builder.as_markup())
                else:
                    await event.bot.send_message(event.from_user.id, text, reply_markup=builder.as_markup())

# Команда просмотра всех заявок
@router.message(F.text == "📩 Просмотр заявок на конференции")
async def admin_conference_requests(message: types.Message):
    if not await is_admin_or_chief(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with AsyncSessionLocal() as session:
        create_requests = (await session.execute(
            select(ConferenceCreationRequest).where(ConferenceCreationRequest.status == "pending")
        )).scalars().all()

        if not create_requests:
            await message.answer("Нет заявок на создание.")
            return

        create_pagination[message.from_user.id] = {"requests": create_requests, "index": 0}
        await show_create_request(message, create_requests, 0)

# Кнопка заявок на редактирование

# Кнопка "Посмотреть апелляции"
@router.message(F.text == "📥 Посмотреть апелляции")
async def view_appeals(message: types.Message):
    if not await is_chief_admin(message.from_user.id):
        await message.answer("Доступ только Глав Админу.")
        return

    async with AsyncSessionLocal() as session:
        appeal_requests = (await session.execute(
            select(ConferenceCreationRequest).where(
                ConferenceCreationRequest.status == "rejected",
                ConferenceCreationRequest.appeal == True
            )
        )).scalars().all()

        if not appeal_requests:
            await message.answer("Нет активных апелляций.")
            return

        await message.answer("<b>Активные апелляции:</b>")
        for req in appeal_requests:
            user = await session.get(User, req.user_id)
            data = req.data

            text = f"ID: <code>{req.id}</code> (апелляция)\n"
            text += f"От: {user.full_name or user.telegram_id}\n\n"
            text += f"Название: {data.get('name')}\n"
            if data.get('description'):
                text += f"Описание: {data.get('description')}\n"
            text += f"Город: {data.get('city')}\n"
            text += f"Дата проведения: {data.get('date')}\n"
            text += f"Орг взнос: {int(data.get('fee', 0))} сом\n"

            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="Одобрить", callback_data=f"conf_appeal_approve_{req.id}"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"conf_appeal_reject_{req.id}")
            )

            if data.get('poster_path') and os.path.exists(data['poster_path']):
                photo = FSInputFile(data['poster_path'])
                await message.answer_photo(photo, caption=text, reply_markup=builder.as_markup())
            else:
                await message.answer(text, reply_markup=builder.as_markup())

# Просмотр всех конференций
@router.message(F.text == "🗂 Все конференции")
async def view_all_conferences(message: types.Message):
    if not await can_view_conferences(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with AsyncSessionLocal() as session:
        conferences = (await session.execute(select(Conference).where(Conference.is_active == True))).scalars().all()

        if not conferences:
            await message.answer("Нет активных конференций.")
            return

        for conf in conferences:
            organizer = await session.get(User, conf.organizer_id)
            organizer_name = organizer.full_name or organizer.telegram_id if organizer else "—"

            text = f"<b>{conf.name}</b> (ID: {conf.id})\n"
            text += f"Организатор: {organizer_name}\n"
            text += f"Город: {conf.city or 'Онлайн'}\n"
            text += f"Дата проведения: {conf.date}\n"
            text += f"Орг взнос: {int(conf.fee)} сом\n"
            if conf.description:
                text += f"\n<i>{conf.description}</i>\n"

            builder = InlineKeyboardBuilder()
            if await can_delete_conference(message.from_user.id):
                builder.row(InlineKeyboardButton(text="Удалить конференцию", callback_data=f"admin_delete_conf_{conf.id}"))

            if conf.poster_path and os.path.exists(conf.poster_path):
                photo = FSInputFile(conf.poster_path)
                await message.answer_photo(photo, caption=text, reply_markup=builder.as_markup())
            else:
                await message.answer(text, reply_markup=builder.as_markup())

# Статистика
@router.message(F.text == "📊 Статистика")
async def stats(message: types.Message):
    if not (await is_admin_or_chief(message.from_user.id) or await is_chief_tech(message.from_user.id)):
        await message.answer("Доступ запрещён.")
        return

    async with AsyncSessionLocal() as session:
        users_count = await session.scalar(select(func.count(User.id)))
        conf_count = await session.scalar(select(func.count(Conference.id)).where(Conference.is_active == True))
        apps_count = await session.scalar(select(func.count(Application.id)))

        text = "<b>Статистика бота:</b>\n\n"
        text += f"Пользователей: {users_count}\n"
        text += f"Активных конференций: {conf_count}\n"
        text += f"Всего заявок на участие: {apps_count}\n"

        await message.answer(text)

# Приостановка/запуск бота
@router.message(F.text.in_({"🛑 Приостановить бота", "▶ Возобновить работу бота"}))
async def pause_bot_handler(message: types.Message, state: FSMContext):
    if not await can_pause_bot(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    status = await get_bot_status()

    if message.text == "🛑 Приостановить бота":
        if status.is_paused:
            await message.answer("Бот уже приостановлен.")
            return
        await state.set_state(AdminStates.waiting_pause_reason)
        await message.answer("Введите причину приостановки бота:", reply_markup=get_cancel_keyboard())
    else:
        if not status.is_paused:
            await message.answer("Бот уже работает.")
            return
        await set_bot_paused(False, None, message.from_user.id)
        await message.answer("▶ Бот успешно запущен!")

        for admin_id in CHIEF_ADMIN_IDS + [TECH_SPECIALIST_ID]:
            if admin_id != message.from_user.id:
                try:
                    await message.bot.send_message(admin_id, f"Бот запущен пользователем {message.from_user.full_name or message.from_user.id}")
                except:
                    pass

# Обработка причины приостановки
@router.message(AdminStates.waiting_pause_reason)
async def pause_reason_handler(message: types.Message, state: FSMContext):
    if message.text and message.text.strip().lower() in ["отмена", "cancel"]:
        await message.answer("Приостановка отменена.")
        await state.clear()
        return

    reason = message.text.strip()
    await set_bot_paused(True, reason, message.from_user.id)
    await message.answer(f"🛑 Бот приостановлен.\nПричина: {reason}")

    for admin_id in CHIEF_ADMIN_IDS + [TECH_SPECIALIST_ID]:
        if admin_id != message.from_user.id:
            try:
                await message.bot.send_message(admin_id, f"Бот приостановлен пользователем {message.from_user.full_name or message.from_user.id}\nПричина: {reason}")
            except:
                pass

    await state.clear()

# Удаление через кнопку
@router.callback_query(F.data.startswith("admin_delete_conf_"))
async def admin_delete_start(callback: types.CallbackQuery, state: FSMContext):
    if not await can_delete_conference(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    conf_id = int(callback.data.split("_")[-1])
    await state.update_data(conf_id=conf_id)
    await state.set_state(AdminStates.delete_conf_reason)

    # 🔥 Самое надёжное решение: удаляем текущее сообщение (фото или текст)
    # и отправляем новое чисто текстовое
    try:
        await callback.message.delete()
    except Exception:
        pass  # если сообщение уже удалено или ошибка — не критично

    await callback.message.answer(
        f"Введите причину удаления конференции (ID {conf_id}):\n\n"
        "Напишите текст и отправьте.",
        reply_markup=get_cancel_keyboard()
    )
    await callback.answer("Готов к вводу причины")

@router.message(Command("delete_conf"))
async def delete_conference_command(message: types.Message):
    if not await can_delete_conference(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    try:
        _, conf_id_str, *reason_parts = message.text.split(maxsplit=2)
        conf_id = int(conf_id_str)
        reason = " ".join(reason_parts).strip()
        if not reason:
            await message.answer("Укажите причину: /delete_conf ID_конференции причина")
            return
    except:
        await message.answer("Формат: /delete_conf ID_конференции причина")
        return

    await perform_conference_deletion(message, conf_id, reason)

@router.message(StateFilter(AdminStates.delete_conf_reason))
async def delete_reason_handler(message: types.Message, state: FSMContext):
    reason = message.text.strip()
    data = await state.get_data()
    conf_id = data["conf_id"]

    await perform_conference_deletion(message, conf_id, reason)
    await state.clear()

async def perform_conference_deletion(target, conf_id: int, reason: str):
    async with AsyncSessionLocal() as session:
        conf = await session.get(Conference, conf_id)
        if not conf:
            await target.answer("Конференция не найдена.")
            return

        organizer = await session.get(User, conf.organizer_id)

        deleted_log = DeletedConference(
            conference_name=conf.name,
            organizer_telegram_id=organizer.telegram_id,
            deleted_by_telegram_id=target.from_user.id,
            reason=reason,
            deleted_at=datetime.now().strftime("%Y-%m-%d %H:%M")
        )
        session.add(deleted_log)

        await session.execute(delete(Application).where(Application.conference_id == conf_id))
        await session.execute(delete(ConferenceEditRequest).where(ConferenceEditRequest.conference_id == conf_id))

        await session.delete(conf)
        await session.commit()

    await target.answer(f"Конференция <b>{conf.name}</b> удалена по причине: {reason}")

    try:
        await target.bot.send_message(
            organizer.telegram_id,
            f"❌ Ваша конференция <b>{conf.name}</b> удалена администратором.\nПричина: {reason}"
        )
    except:
        pass

# Обработка создания
@router.callback_query(F.data.startswith("conf_create_approve_") | F.data.startswith("conf_create_reject_"))
async def process_create_request(callback: types.CallbackQuery):
    action = "approve" if "approve" in callback.data else "reject"
    req_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        req = await session.get(ConferenceCreationRequest, req_id)
        if not req:
            await callback.answer("Заявка не найдена.")
            return

        user = await session.get(User, req.user_id)
        req_data = req.data

        if action == "approve":
            req.status = "approved"
            user.role = Role.ORGANIZER.value

            conference = Conference(
                name=req_data["name"],
                description=req_data.get("description"),
                city=req_data.get("city"),
                date=req_data.get("date"),
                fee=float(req_data.get("fee", 0)),
                qr_code_path=req_data.get("qr_code_path"),
                poster_path=req_data.get("poster_path"),
                organizer_id=user.id,
                is_active=True
            )
            session.add(conference)
            await session.commit()

            await callback.bot.send_message(
                user.telegram_id,
                f"🎉 Ваша заявка на создание конференции <b>{req_data['name']}</b> одобрена!\n\n"
                "Теперь вы — Организатор.\n"
                "Перезапустите бота командой /main_menu."
            )
        else:
            req.status = "rejected"
            await session.commit()

            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="Подать апелляцию", callback_data=f"appeal_submit_{req.id}"),
                InlineKeyboardButton(text="Главное меню", callback_data="back_to_main")
            )

            await callback.bot.send_message(
                user.telegram_id,
                f"❌ Ваша заявка на создание конференции <b>{req_data['name']}</b> отклонена.",
                reply_markup=builder.as_markup()
            )

        await callback.answer(f"Заявка {'одобрена' if action == 'approve' else 'отклонена'}")

    try:
        await callback.message.delete()
    except:
        pass

    await update_requests_message(callback)

# Обработка редактирования
@router.callback_query(F.data.startswith("conf_edit_approve_") | F.data.startswith("conf_edit_reject_"))
async def process_edit_request(callback: types.CallbackQuery):
    action = "approve" if "approve" in callback.data else "reject"
    req_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        req = await session.get(ConferenceEditRequest, req_id)
        if not req:
            await callback.answer("Заявка не найдена.")
            return

        conf = await session.get(Conference, req.conference_id)
        organizer = await session.get(User, req.organizer_id)
        edit_data = req.data

        if action == "approve":
            conf.name = edit_data.get("name", conf.name)
            conf.description = edit_data.get("description", conf.description)
            conf.city = edit_data.get("city", conf.city)
            conf.date = edit_data.get("date", conf.date)
            conf.fee = edit_data.get("fee", conf.fee)
            if edit_data.get("qr_code_path"):
                conf.qr_code_path = edit_data["qr_code_path"]
            if edit_data.get("poster_path"):
                conf.poster_path = edit_data["poster_path"]

            req.status = "approved"
            await session.commit()

            await callback.bot.send_message(
                organizer.telegram_id,
                f"✅ Ваши изменения в конференции <b>{conf.name}</b> одобрены!"
            )
        else:
            req.status = "rejected"
            await session.commit()

            await callback.bot.send_message(
                organizer.telegram_id,
                f"❌ Ваши изменения в конференции <b>{conf.name}</b> отклонены."
            )

        await callback.answer(f"Редактирование {'одобрено' if action == 'approve' else 'отклонено'}")

    try:
        await callback.message.delete()
    except:
        pass

    await update_edit_requests_message(callback)

# Подача апелляции
@router.callback_query(F.data.startswith("appeal_submit_"))
async def appeal_submit(callback: types.CallbackQuery):
    req_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        req = await session.get(ConferenceCreationRequest, req_id)
        if not req:
            await callback.answer("Заявка не найдена.")
            return

        req.appeal = True
        await session.commit()

    await callback.message.edit_text("Ваша апелляция отправлена Глав Админу.\nОжидайте решения.")

    for admin_id in CHIEF_ADMIN_IDS:
        try:
            await callback.bot.send_message(admin_id, f"🆕 Новая апелляция! ID: <code>{req_id}</code>")
        except:
            pass

    await callback.answer()

# Возврат в главное меню
@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery):
    db_user = await get_or_create_user(callback.from_user.id)
    await callback.message.edit_text("Главное меню", reply_markup=get_main_menu_keyboard(db_user.role))
    await callback.answer()

# Обработка апелляции
@router.callback_query(F.data.startswith("conf_appeal_approve_") | F.data.startswith("conf_appeal_reject_"))
async def process_appeal(callback: types.CallbackQuery):
    if not await is_chief_admin(callback.from_user.id):
        await callback.answer("Доступ только Глав Админу.")
        return

    action = "approve" if "approve" in callback.data else "reject"
    req_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        req = await session.get(ConferenceCreationRequest, req_id)
        if not req:
            await callback.answer("Заявка не найдена.")
            return

        user = await session.get(User, req.user_id)
        req_data = req.data

        if action == "approve":
            req.status = "approved"
            user.role = Role.ORGANIZER.value

            conference = Conference(
                name=req_data["name"],
                description=req_data.get("description"),
                city=req_data.get("city"),
                date=req_data.get("date"),
                fee=float(req_data.get("fee", 0)),
                qr_code_path=req_data.get("qr_code_path"),
                poster_path=req_data.get("poster_path"),
                organizer_id=user.id,
                is_active=True
            )
            session.add(conference)
            await session.commit()

            await callback.bot.send_message(user.telegram_id, "✅ Ваша апелляция одобрена! Вы стали Организатором.")
        else:
            req.appeal = False
            await session.commit()

            await callback.bot.send_message(user.telegram_id, "❌ Ваша апелляция отклонена.")

        await callback.answer("Апелляция обработана")

    try:
        await callback.message.delete()
    except:
        pass

    await update_requests_message(callback)

# Экспорт данных бота
@router.message(F.text == "📤 Экспорт данных бота")
async def export_bot_data(message: types.Message):
    user_id = message.from_user.id

    if user_id == TECH_SPECIALIST_ID:
        async with AsyncSessionLocal() as session:
            users = (await session.execute(select(User))).scalars().all()
            users_data = []
            for user in users:
                users_data.append({
                    "Telegram ID": user.telegram_id,
                    "Username": user.username or "—",
                    "ФИО": user.full_name or "—",
                    "Роль": user.role,
                    "Забанен": "Да" if user.is_banned else "Нет",
                    "Причина бана": user.ban_reason or "—"
                })

            # Экспорт рейтингов
            ratings = (await session.execute(
                select(ConferenceRating)
                .options(joinedload(ConferenceRating.conference), joinedload(ConferenceRating.user))
            )).scalars().all()

            rating_data = []
            for r in ratings:
                rating_data.append({
                    "Конференция": r.conference.name,
                    "Участник": r.user.full_name or r.user.telegram_id,
                    "Оценка": r.rating,
                    "Отзыв": r.review or "—",
                    "Дата оценки": r.created_at.strftime("%Y-%m-%d")
                })

            df_ratings = pd.DataFrame(rating_data)
            ratings_filename = "tech_conference_ratings.xlsx"
            df_ratings.to_excel(ratings_filename, index=False)

            with open(ratings_filename, "rb") as f4:
                await message.answer_document(
                    BufferedInputFile(f4.read(), filename=ratings_filename),
                    caption="4/4 Экспорт: Рейтинги конференций"
                )
            os.remove(ratings_filename)

            df_users = pd.DataFrame(users_data)
            users_filename = "tech_export_users_with_bans.xlsx"
            df_users.to_excel(users_filename, index=False)

            conferences = (await session.execute(select(Conference).where(Conference.is_active == True))).scalars().all()
            conf_data = []
            for conf in conferences:
                organizer = await session.get(User, conf.organizer_id)
                organizer_name = organizer.full_name or organizer.telegram_id if organizer else "—"
                conf_data.append({
                    "ID": conf.id,
                    "Название": conf.name,
                    "Организатор": organizer_name,
                    "Город": conf.city or "Онлайн",
                    "Дата проведения": conf.date,
                    "Оргвзнос": conf.fee
                })

            df_confs = pd.DataFrame(conf_data)
            confs_filename = "tech_active_conferences.xlsx"
            df_confs.to_excel(confs_filename, index=False)

            deleted = (await session.execute(select(DeletedConference))).scalars().all()
            deleted_data = []
            for d in deleted:
                deleted_data.append({
                    "Название конференции": d.conference_name,
                    "Организатор ID": d.organizer_telegram_id,
                    "Удалил (ID)": d.deleted_by_telegram_id,
                    "Причина удаления": d.reason,
                    "Дата удаления": d.deleted_at
                })

            df_deleted = pd.DataFrame(deleted_data)
            deleted_filename = "tech_deleted_conferences.xlsx"
            df_deleted.to_excel(deleted_filename, index=False)

        with open(users_filename, "rb") as f1:
            await message.answer_document(BufferedInputFile(f1.read(), filename=users_filename), caption="1/3 Экспорт: Пользователи (с банами)")
        with open(confs_filename, "rb") as f2:
            await message.answer_document(BufferedInputFile(f2.read(), filename=confs_filename), caption="2/3 Экспорт: Активные конференции")
        with open(deleted_filename, "rb") as f3:
            await message.answer_document(BufferedInputFile(f3.read(), filename=deleted_filename), caption="3/3 Экспорт: Удалённые конференции")

        os.remove(users_filename)
        os.remove(confs_filename)
        os.remove(deleted_filename)
        return

    if user_id in CHIEF_ADMIN_IDS:
        async with AsyncSessionLocal() as session:
            users = (await session.execute(select(User))).scalars().all()
            users_data = []
            for user in users:
                users_data.append({
                    "Telegram ID": user.telegram_id,
                    "Username": user.username or "—",
                    "ФИО": user.full_name or "—",
                    "Роль": user.role,
                    "Забанен": "Да" if user.is_banned else "Нет",
                    "Причина бана": user.ban_reason or "—"
                })

            df_users = pd.DataFrame(users_data)
            users_filename = "admin_users_with_bans.xlsx"
            df_users.to_excel(users_filename, index=False)

            conferences = (await session.execute(select(Conference).where(Conference.is_active == True))).scalars().all()
            conf_data = []
            for conf in conferences:
                organizer = await session.get(User, conf.organizer_id)
                organizer_name = organizer.full_name or organizer.telegram_id if organizer else "—"
                conf_data.append({
                    "Статус": "Активна",
                    "ID": conf.id,
                    "Название": conf.name,
                    "Организатор": organizer_name,
                    "Город": conf.city or "Онлайн",
                    "Дата проведения": conf.date,
                    "Оргвзнос": conf.fee
                })

            deleted = (await session.execute(select(DeletedConference))).scalars().all()
            for d in deleted:
                conf_data.append({
                    "Статус": "Удалена",
                    "ID": "—",
                    "Название": d.conference_name,
                    "Организатор": d.organizer_telegram_id,
                    "Город": "—",
                    "Дата проведения": "—",
                    "Оргвзнос": "—",
                    "Удалил": d.deleted_by_telegram_id,
                    "Причина": d.reason,
                    "Дата удаления": d.deleted_at
                })

            df_confs = pd.DataFrame(conf_data)
            confs_filename = "admin_conferences_full.xlsx"
            df_confs.to_excel(confs_filename, index=False)

        with open(users_filename, "rb") as f1:
            await message.answer_document(
                BufferedInputFile(f1.read(), filename=users_filename),
                caption="1/2 Экспорт: Пользователи (с ролями и банами)"
            )

        with open(confs_filename, "rb") as f2:
            await message.answer_document(
                BufferedInputFile(f2.read(), filename=confs_filename),
                caption="2/2 Экспорт: Все конференции (активные + удалённые)"
            )

        os.remove(users_filename)
        os.remove(confs_filename)
        return

    await message.answer("Доступ запрещён.")

# Назначение роли — только Глав Тех
@router.message(Command("set_role"))
async def set_role(message: types.Message):
    if not await is_chief_tech(message.from_user.id):
        await message.answer("Доступ запрещён. Только для Главного Тех Специалиста.")
        return

    await message.answer(
        "Формат: /set_role @username роль\n"
        "Роли: Участник, Организатор, Админ"
    )

    try:
        _, target, role_str = message.text.split(maxsplit=2)
        target = target.lstrip("@")

        async with AsyncSessionLocal() as session:
            if target.isdigit():
                result = await session.execute(select(User).where(User.telegram_id == int(target)))
            else:
                result = await session.execute(select(User).where(User.full_name.ilike(f"%{target}%")))
            target_user = result.scalar_one_or_none()

            if not target_user:
                await message.answer("Пользователь не найден.")
                return

            if role_str not in [r.value for r in Role]:
                await message.answer("Неверная роль.")
                return

            target_user.role = role_str
            await session.commit()

            await message.answer(f"Роль пользователя {target_user.full_name or target_user.telegram_id} изменена на {role_str}")
            try:
                await message.bot.send_message(target_user.telegram_id, f"Ваша роль изменена на: {role_str}")
            except:
                pass
    except:
        await message.answer("Неверный формат команды.")

# === НОВЫЕ ФУНКЦИИ ДЛЯ ТЕХПОДДЕРЖКИ ===

# Просмотр обращений
# Просмотр обращений — исправленная версия
@router.message(F.text == "📩 Обращения пользователей")
async def view_support_requests(message: types.Message):
    if not await is_chief_tech(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(SupportRequest).order_by(SupportRequest.id.desc())
        )
        requests = result.scalars().all()

        if not requests:
            await message.answer("Нет обращений в техподдержку.")
            return

        # Загружаем пользователей заранее
        enriched_requests = []
        for req in requests:
            user_result = await session.execute(select(User).where(User.id == req.user_id))
            user = user_result.scalar_one_or_none()
            enriched_requests.append({
                "request": req,
                "user": user
            })

        support_pagination[message.from_user.id] = {
            "index": 0,
            "total": len(enriched_requests),
            "requests": enriched_requests
        }
        await show_support_request(message, enriched_requests, 0)

async def show_support_request(target, enriched_requests: list, index: int):
    item = enriched_requests[index]
    req = item["request"]
    user = item["user"]

    if not user:
        user_name = f"ID {req.user_id} (пользователь удалён)"
    else:
        user_name = user.full_name or f"ID {user.telegram_id}"

    text = f"<b>Обращение {index + 1} из {len(enriched_requests)}</b>\n\n"
    text += f"<b>ID:</b> <code>{req.id}</code>\n"
    text += f"<b>От:</b> {user_name}\n"
    text += f"<b>Текст:</b>\n{req.message}\n\n"
    text += f"<b>Статус:</b> {req.status}"
    if req.response:
        text += f"\n<b>Ответ:</b> {req.response}"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📩 Ответить", callback_data=f"reply_support_{req.id}"))

    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"nav_support_{index-1}"))
    if index < len(enriched_requests) - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"nav_support_{index+1}"))
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu"))

    keyboard = builder.as_markup()

    has_screenshot = req.screenshot_path and os.path.exists(req.screenshot_path)

    if isinstance(target, types.Message):
        # Первое сообщение — отправляем как есть
        if has_screenshot:
            photo = FSInputFile(req.screenshot_path)
            await target.answer_photo(photo, caption=text, reply_markup=keyboard)
        else:
            await target.answer(text, reply_markup=keyboard)
        return

    # Это callback — редактируем
    message = target.message

    try:
        if has_screenshot:
            photo = FSInputFile(req.screenshot_path)
            await message.edit_media(
                media=types.InputMediaPhoto(media=photo, caption=text),
                reply_markup=keyboard
            )
        else:
            await message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "there is no text in the message to edit" in str(e) or "there is no media in the message" in str(e):
            # Тип сообщения не совпадает — удаляем старое и отправляем новое
            await message.delete()
            if has_screenshot:
                photo = FSInputFile(req.screenshot_path)
                await target.bot.send_photo(message.chat.id, photo, caption=text, reply_markup=keyboard)
            else:
                await target.bot.send_message(message.chat.id, text, reply_markup=keyboard)
        else:
            raise e

# Навигация по обращениям
@router.callback_query(F.data.startswith("nav_support_"))
async def navigate_support(callback: types.CallbackQuery):
    index = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    data = support_pagination.get(user_id)
    if not data:
        await callback.answer("🔄 Сессия истекла. Нажмите кнопку заново.", show_alert=True)
        return

    total = len(data["requests"])
    if index < 0 or index >= total:
        await callback.answer("Конец списка.", show_alert=True)
        return

    data["index"] = index
    await show_support_request(callback, data["requests"], index)
    await callback.answer(f"{index + 1}/{total}")

# Начало ответа
@router.callback_query(F.data.startswith("reply_support_"))
async def start_reply_support(callback: types.CallbackQuery, state: FSMContext):
    if not await is_chief_tech(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    req_id = int(callback.data.split("_")[-1])
    await state.update_data(support_id=req_id)
    await state.set_state(AdminStates.waiting_support_reply)
    await callback.message.answer(
        f"Введите ответ на обращение ID <code>{req_id}</code>:",
        reply_markup=get_cancel_keyboard()
    )
    await callback.answer()

# Обработка ответа
# Обработка ответа на обращение (через кнопку "Ответить")
@router.message(StateFilter(AdminStates.waiting_support_reply))
async def process_support_reply(message: types.Message, state: FSMContext):
    if not await is_chief_tech(message.from_user.id):
        await message.answer("Доступ запрещён.")
        await state.clear()
        return

    data = await state.get_data()
    support_id = data.get("support_id")
    if not support_id:
        await message.answer("Ошибка: ID обращения не найден.")
        await state.clear()
        return

    response_text = message.text.strip()
    if not response_text:
        await message.answer("Ответ не может быть пустым.")
        return

    async with AsyncSessionLocal() as session:
        req_result = await session.execute(select(SupportRequest).where(SupportRequest.id == support_id))
        req = req_result.scalar_one_or_none()
        if not req:
            await message.answer("Обращение не найдено.")
            await state.clear()
            return

        # Обновляем обращение
        req.response = response_text
        req.status = "answered"
        await session.commit()

        # Загружаем пользователя для отправки ответа
        user_result = await session.execute(select(User).where(User.id == req.user_id))
        user = user_result.scalar_one_or_none()

        if user and user.telegram_id:
            try:
                await message.bot.send_message(
                    user.telegram_id,
                    f"📩 <b>Ответ от техподдержки:</b>\n\n{response_text}"
                )
            except Exception as e:
                await message.answer(f"Ответ сохранён, но не удалось отправить пользователю: {e}")
        else:
            await message.answer("Ответ сохранён, но пользователь не найден или заблокировал бота.")

    await message.answer(
        "✅ Ответ успешно отправлен и сохранён.",
        reply_markup=get_main_menu_keyboard("Глав Тех Специалист")
    )
    await state.clear()

# Команда /reply_support
# Команда /reply_support ID текст
@router.message(Command("reply_support"))
async def cmd_reply_support(message: types.Message):
    if not await is_chief_tech(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            raise ValueError
        _, support_id_str, response_text = parts
        support_id = int(support_id_str)
    except:
        await message.answer("Формат: /reply_support ID_обращения текст_ответа")
        return

    if not response_text.strip():
        await message.answer("Текст ответа не может быть пустым.")
        return

    async with AsyncSessionLocal() as session:
        req_result = await session.execute(select(SupportRequest).where(SupportRequest.id == support_id))
        req = req_result.scalar_one_or_none()
        if not req:
            await message.answer("Обращение не найдено.")
            return

        req.response = response_text
        req.status = "answered"
        await session.commit()

        user_result = await session.execute(select(User).where(User.id == req.user_id))
        user = user_result.scalar_one_or_none()

        if user and user.telegram_id:
            try:
                await message.bot.send_message(
                    user.telegram_id,
                    f"📩 <b>Ответ от техподдержки:</b>\n\n{response_text}"
                )
            except Exception as e:
                await message.answer(f"Ответ сохранён, но не удалось отправить: {e}")
        else:
            await message.answer("Ответ сохранён, но пользователь не найден.")

    await message.answer("Ответ отправлен пользователю.")

# Экспорт обращений
@router.message(F.text == "📤 Экспорт обращений")
async def export_support_requests(message: types.Message):
    if not await is_chief_tech(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with AsyncSessionLocal() as session:
        requests = (await session.execute(select(SupportRequest))).scalars().all()

        if not requests:
            await message.answer("Нет обращений для экспорта.")
            return

        data = []
        for req in requests:
            user = await session.get(User, req.user_id)
            data.append({
                "ID": req.id,
                "ФИО": user.full_name or "—",
                "Telegram ID": user.telegram_id,
                "Текст обращения": req.message,
                "Скриншот (путь)": req.screenshot_path or "—",
                "Статус": req.status,
                "Ответ": req.response or "—"
            })

        df = pd.DataFrame(data)
        filename = "support_requests_export.xlsx"
        df.to_excel(filename, index=False)

        with open(filename, "rb") as f:
            await message.answer_document(
                BufferedInputFile(f.read(), filename=filename),
                caption="📤 Экспорт всех обращений в техподдержку"
            )

        os.remove(filename)

@router.message(Command("backup_db"))
async def backup_db(message: types.Message):
    if message.from_user.id != TECH_SPECIALIST_ID:
        await message.answer("🚫 Доступ запрещён.")
        return

    try:
        with open("mun_bot.db", "rb") as db_file:
            await message.answer_document(
                BufferedInputFile(db_file.read(), filename=f"mun_bot_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db"),
                caption="✅ Бэкап базы данных mun_bot.db"
            )
        await message.answer("✅ Бэкап успешно отправлен!")
    except FileNotFoundError:
        await message.answer("❌ Ошибка: файл базы не найден (mun_bot.db).")
    except Exception as e:
        await message.answer(f"❌ Ошибка при отправке бэкапа: {e}")

@router.message(F.text == "✏️ Заявки на редактирование")
async def admin_edit_requests(message: types.Message):
    if not await is_admin_or_chief(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConferenceEditRequest)
            .options(
                joinedload(ConferenceEditRequest.conference),
                joinedload(ConferenceEditRequest.organizer)
            )
            .where(ConferenceEditRequest.status == "pending")
            .order_by(ConferenceEditRequest.id.desc())
        )
        requests = result.unique().scalars().all()

        if not requests:
            await message.answer("Нет активных заявок на редактирование.")
            return

        user_id = message.from_user.id
        edit_pagination[user_id] = {"requests": requests, "index": 0}
        await show_edit_request(message, requests, 0)

@router.callback_query(F.data.startswith("edit_approve_"))
async def approve_edit(callback: types.CallbackQuery):
    req_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConferenceEditRequest)
            .options(joinedload(ConferenceEditRequest.conference),
                     joinedload(ConferenceEditRequest.organizer))
            .where(ConferenceEditRequest.id == req_id)
        )
        req = result.unique().scalar_one_or_none()
        if not req:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return

        conf = req.conference
        organizer = req.organizer
        changes = req.data.get("changes", {})

        # Применяем изменения
        for field, value in changes.items():
            if field in ["qr", "poster"]:
                field_name = "qr_code_path" if field == "qr" else "poster_path"
                setattr(conf, field_name, value)
            else:
                setattr(conf, field, value)

        req.status = "approved"
        await session.commit()

        try:
            await callback.bot.send_message(
                organizer.telegram_id,
                f"✅ Изменения в конференции <b>{conf.name}</b> одобрены!"
            )
        except:
            pass

    # === ОБНОВЛЕНИЕ ПАГИНАЦИИ И ПОКАЗ СЛЕДУЮЩЕЙ ЗАЯВКИ ===
    if user_id in edit_pagination:
        data = edit_pagination[user_id]
        data["requests"] = [r for r in data["requests"] if r.id != req_id]

        if not data["requests"]:
            await callback.message.delete()
            await callback.message.answer(
                "✅ Все заявки на редактирование обработаны!",
                reply_markup=get_main_menu_keyboard("Админ")
            )
            del edit_pagination[user_id]
            await callback.answer("✅ Одобрено!")
            return

        # Корректируем индекс
        if data["index"] >= len(data["requests"]):
            data["index"] = max(0, len(data["requests"]) - 1)

        await show_edit_request(callback, data["requests"], data["index"])
    else:
        await callback.message.edit_text("✅ Заявка одобрена.")

    await callback.answer("✅ Одобрено!")


@router.callback_query(F.data.startswith("edit_reject_"))
async def reject_edit(callback: types.CallbackQuery):
    req_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConferenceEditRequest)
            .options(joinedload(ConferenceEditRequest.organizer))
            .where(ConferenceEditRequest.id == req_id)
        )
        req = result.unique().scalar_one_or_none()
        if not req:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return

        organizer = req.organizer
        req.status = "rejected"
        await session.commit()

        try:
            await callback.bot.send_message(
                organizer.telegram_id,
                f"❌ Заявка на редактирование конференции <b>{req.conference.name}</b> отклонена."
            )
        except:
            pass

    # === ТО ЖЕ САМОЕ — ОБНОВЛЕНИЕ ПАГИНАЦИИ ===
    if user_id in edit_pagination:
        data = edit_pagination[user_id]
        data["requests"] = [r for r in data["requests"] if r.id != req_id]

        if not data["requests"]:
            await callback.message.delete()
            await callback.message.answer(
                "✅ Все заявки обработаны!",
                reply_markup=get_main_menu_keyboard("Админ")
            )
            del edit_pagination[user_id]
            await callback.answer("✅ Отклонено!")
            return

        if data["index"] >= len(data["requests"]):
            data["index"] = max(0, len(data["requests"]) - 1)

        await show_edit_request(callback, data["requests"], data["index"])
    else:
        await callback.message.edit_text("✅ Заявка отклонена.")

    await callback.answer("✅ Отклонено!")

async def show_edit_request(target, requests: list, index: int):
    req = requests[index]
    conf = req.conference
    org = req.organizer
    changes = req.data.get("changes", {})

    text = f"<b>Заявка на редактирование {index + 1} из {len(requests)}</b>\n\n"
    text += f"ID: <code>{req.id}</code>\nКонференция: <b>{conf.name}</b>\nОрганизатор: {org.full_name or org.telegram_id}\n\n"
    text += "<b>Изменения:</b>\n"
    for field, value in changes.items():
        original = req.data.get("original", {}).get(field, "—")
        text += f"• {field.capitalize()}: {original} → <b>{value or 'удалить'}</b>\n"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"edit_approve_{req.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"edit_reject_{req.id}")
    )
    nav = []
    if index > 0: nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"nav_edit_{index-1}"))
    if index < len(requests)-1: nav.append(InlineKeyboardButton(text="▶ Вперёд", callback_data=f"nav_edit_{index+1}"))
    if nav: builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu"))

    # ПОКАЗ ФОТО
    photo_path = None
    if "poster" in changes and changes["poster"] and os.path.exists(changes["poster"]):
        photo_path = changes["poster"]
    elif conf.poster_path and os.path.exists(conf.poster_path):
        photo_path = conf.poster_path

    if photo_path:
        photo = FSInputFile(photo_path)
        if isinstance(target, types.Message):
            await target.answer_photo(photo, caption=text, reply_markup=builder.as_markup())
        else:
            await target.message.edit_media(
                media=types.InputMediaPhoto(media=photo, caption=text),  # ← ДОБАВИЛИ media=
                reply_markup=builder.as_markup()
            )
    else:
        if isinstance(target, types.Message):
            await target.answer(text, reply_markup=builder.as_markup())
        else:
            await target.message.edit_text(text, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("nav_edit_"))
async def navigate_edit(callback: types.CallbackQuery):
    index = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    if user_id not in edit_pagination:
        await callback.answer("Сессия истекла. Нажмите кнопку заново.", show_alert=True)
        return

    data = edit_pagination[user_id]
    if index < 0 or index >= len(data["requests"]):
        await callback.answer("Конец списка.", show_alert=True)
        return

    data["index"] = index
    await show_edit_request(callback, data["requests"], index)
    await callback.answer()

async def show_create_request(target, requests: list, index: int):
    req = requests[index]

    # ← ИСПРАВЛЕНИЕ ЗДЕСЬ
    async with AsyncSessionLocal() as session:
        user = await session.get(User, req.user_id)

    data = req.data

    text = f"<b>Заявка на создание {index + 1} из {len(requests)}</b>\n\n"
    text += f"ID: <code>{req.id}</code>\nОт: {user.full_name or user.telegram_id}\n\n"
    text += f"<b>Название:</b> {data.get('name')}\n"

    # ← ЭТУ СТРОКУ ДОБАВИЛИ
    text += f"<b>Описание:</b>\n{data.get('description', '—')}\n\n"

    text += f"<b>Город:</b> {data.get('city', 'Онлайн')}\n"
    text += f"<b>Дата:</b> {data.get('date')}\n"
    text += f"<b>Орг взнос:</b> {int(data.get('fee', 0))} сом"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"conf_create_approve_{req.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"conf_create_reject_{req.id}")
    )
    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"nav_create_{index-1}"))
    if index < len(requests) - 1:
        nav.append(InlineKeyboardButton(text="▶ Вперёд", callback_data=f"nav_create_{index+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu"))

    if data.get('poster_path') and os.path.exists(data['poster_path']):
        photo = FSInputFile(data['poster_path'])
        if isinstance(target, types.Message):
            await target.answer_photo(photo, caption=text, reply_markup=builder.as_markup())
        else:
            await target.message.edit_media(
                media=types.InputMediaPhoto(media=photo, caption=text),  # ← ДОБАВИЛИ media=
                reply_markup=builder.as_markup()
            )
    else:
        if isinstance(target, types.Message):
            await target.answer(text, reply_markup=builder.as_markup())
        else:
            await target.message.edit_text(text, reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("nav_create_"))
async def navigate_create(callback: types.CallbackQuery):
    index = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id
    if user_id not in create_pagination:
        await callback.answer("Сессия истекла.")
        return
    data = create_pagination[user_id]
    data["index"] = index
    await show_create_request(callback, data["requests"], index)
    await callback.answer()