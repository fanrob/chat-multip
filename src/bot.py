import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, ContextTypes

from config import settings_storage, CLOSE_PROMPT_AFTER_DAYS, AUTO_CLOSE_AFTER_DAYS, INACTIVITY_CHECK_INTERVAL
from models import Ticket, generate_ticket_id
from storage import (
    ticket_storage, operator_message_storage, master_storage,
    is_operator, get_operator_ids, is_admin, get_admin_id
)
from mail import send_email_reply, notify_operators

logger = logging.getLogger(__name__)

ALLOW_FOREIGN_TICKET_REPLIES = "разрешить ответы на чужие заявки"

SETTING_CLOSE_PROMPT_DAYS = "close_prompt_after_days"
SETTING_AUTO_CLOSE_DAYS = "auto_close_after_days"


def get_close_prompt_days() -> int:
    return int(settings_storage.get(SETTING_CLOSE_PROMPT_DAYS, str(CLOSE_PROMPT_AFTER_DAYS)))


def get_auto_close_days() -> int:
    return int(settings_storage.get(SETTING_AUTO_CLOSE_DAYS, str(AUTO_CLOSE_AFTER_DAYS)))

# Глобальное хранилище заявок на роль мастера: {user_id: user_name}
operator_requests = {}
admin_transfer_requests = {}


def build_close_button(ticket_id: str, role: str = "client") -> InlineKeyboardMarkup:
    """Создаёт кнопку закрытия заявки для клиента или мастера."""
    callback_prefix = "close_client" if role == "client" else "close_op"
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Закрыть заявку", callback_data=f"{callback_prefix}_{ticket_id}")]])


def build_operator_keyboard() -> ReplyKeyboardMarkup:
    """Создаёт постоянную клавиатуру для мастера."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📋 Мои заявки"), KeyboardButton("❌ Закрыть заявку")],
         [KeyboardButton("📤 Отправить диалог")]],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def build_admin_keyboard() -> ReplyKeyboardMarkup:
    """Создаёт постоянную клавиатуру администратора-мастера."""
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("📋 Мои заявки"),
                KeyboardButton("❌ Закрыть заявку"),
                KeyboardButton("👑 Панель администратора"),
            ],
            [KeyboardButton("📤 Отправить диалог")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

ALL_WORKSHOPS_BUTTON = "🌍 Отправить заявку для всех цехов"


def build_client_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура клиента: кнопки выбора цеха + отправка всем."""
    workshops = master_storage.get_workshops()
    rows = [[KeyboardButton(w["name"])] for w in workshops]
    rows.append([KeyboardButton(ALL_WORKSHOPS_BUTTON)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)

# ==================== ТЕЛЕГРАМ-БОТ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение для новых пользователей."""
    if is_admin(update.effective_user.id):
        await update.message.reply_text(
                    "👋 Привет, администратор! Вы можете принимать заявки от клиентов, управлять мастерами и настройками",
                    reply_markup=(
                        build_admin_keyboard()
                    ),
                )
        return
    elif is_operator(update.effective_user.id):
        await update.message.reply_text(
            "👋 Привет, мастер! Вы можете принимать заявки от клиентов.",
            reply_markup=(
                build_operator_keyboard()
            ),
        )
        return
    await update.message.reply_text(
        "👋 Привет! Я бот для приёма заявок.\n"
        "Выберите цех кнопкой ниже (или отправьте заявку для всех цехов) "
        "и напишите текст заявки.",
        reply_markup=build_client_keyboard(),
    )


async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает сообщения от пользователей (не мастеров)."""
    user = update.effective_user

    # Ожидаем ввод нового значения настроек (количество дней) от администратора
    awaiting_setting = context.user_data.get('awaiting_days_setting')
    if awaiting_setting and is_admin(user.id):
        await process_days_setting_input(update, context, awaiting_setting)
        return

    # Ожидаем ввод названия нового цеха от администратора
    if context.user_data.get('awaiting_workshop_name') and is_admin(user.id):
        await process_workshop_name_input(update, context)
        return

    # Ожидаем ввод нового значения параметра чтения почты от администратора
    awaiting_email_setting = context.user_data.get('awaiting_email_setting')
    if awaiting_email_setting and is_admin(user.id):
        await process_email_setting_input(update, context, awaiting_email_setting)
        return

    if is_operator(user.id) or is_admin(user.id):
        # Проверяем нажатие на кнопку "Мои заявки"
        if update.message.text == "📋 Мои заявки":
            await get_my_tickets(update, context)
            return
        if update.message.text == "❌ Закрыть заявку":
            await show_close_ticket_menu(update, context)
            return
        if update.message.text == "📤 Отправить диалог":
            await show_send_dialog_ticket_menu(update, context)
            return
        if is_admin(user.id) and update.message.text == "👥 Список мастеров":
            await show_operator_list(update, context)
            return
        if is_admin(user.id) and update.message.text == "🗑 Удалить мастера":
            await show_delete_operator_menu(update, context)
            return
        if is_admin(user.id) and update.message.text == "👑 Панель администратора":
            await admin_panel(update, context)
            return
        await handle_operator_reply(update, context)
        return

    replied_ticket_id = None
    if update.message.reply_to_message:
        replied_ticket_id = operator_message_storage.get_ticket_id(update.message.reply_to_message.message_id)

    if replied_ticket_id:
        replied_ticket = ticket_storage.get(replied_ticket_id)
        if replied_ticket:
            if replied_ticket.taken_by:
                operator_id = replied_ticket.taken_by
                reply_text = f"📨 **Сообщение от клиента** (ID заявки: {replied_ticket.id}):\n\n{update.message.text}"
                try:
                    sent_message = await context.bot.send_message(chat_id=operator_id, text=reply_text, parse_mode='Markdown')
                    operator_message_storage.add(sent_message.message_id, operator_id, replied_ticket.id, sender='client', sender_id=str(user.id), text=update.message.text)
                    await update.message.reply_text("✅ Ваше сообщение отправлено мастеру.")
                    return
                except Exception as e:
                    logger.error(f"Error sending message to operator {operator_id}: {e}")
                    await update.message.reply_text(f"❌ Ошибка при отправке сообщения мастеру: {e}")
                    return

    open_ticket = ticket_storage.get_open_ticket(user.id)

    if update.message.text == ALL_WORKSHOPS_BUTTON:
        context.user_data.pop("ticket_workshop_id", None)
        await update.message.reply_text(
            "🌍 Заявка будет отправлена мастерам всех цехов. Напишите текст заявки.",
            reply_markup=build_client_keyboard(),
        )
        return

    workshops = {w["name"]: w["id"] for w in master_storage.get_workshops()}
    if update.message.text in workshops:
        context.user_data["ticket_workshop_id"] = workshops[update.message.text]
        workshop_name = update.message.text
        await update.message.reply_text(
            f"🏭 Вы выбрали цех «{workshop_name}». Мастера этого цеха примут вашу заявку.\n"
            "Напишите текст заявки.",
            reply_markup=build_client_keyboard(),
        )
        return

    if open_ticket:
        if open_ticket.taken_by:
            operator_id = open_ticket.taken_by
            reply_text = f"📨 **Сообщение от клиента** (ID заявки: {open_ticket.id}):\n\n{update.message.text}"
            try:
                sent_message = await context.bot.send_message(chat_id=operator_id, text=reply_text, parse_mode='Markdown')
                operator_message_storage.add(sent_message.message_id, operator_id, open_ticket.id, sender='client', sender_id=str(user.id), text=update.message.text)
                await update.message.reply_text("✅ Ваше сообщение отправлено мастеру.")
            except Exception as e:
                logger.error(f"Error sending message to operator {operator_id}: {e}")
                await update.message.reply_text(f"❌ Ошибка при отправке сообщения мастеру: {e}")
        else:
            await update.message.reply_text("❗ У вас уже есть открытая заявка. Пожалуйста, дождитесь ответа мастера.")
        return

    workshop_id = context.user_data.get("ticket_workshop_id")
    if workshop_id is not None:
        workshop_name = master_storage.get_workshop_name(workshop_id) or "выбранный цех"
        if not get_operator_ids(workshop_id):
            await update.message.reply_text(
                f"⚠️ В цехе «{workshop_name}» пока нет мастеров. "
                "Заявка не принята. Выберите другой цех или отправьте заявку для всех цехов."
            )
            return

    ticket = Ticket(
        id=generate_ticket_id(ticket_storage.get_all_ids()),
        source='telegram',
        client_id=str(user.id),
        client_name=user.full_name or user.username or str(user.id),
        text=update.message.text,
        workshop_id=workshop_id,
    )
    ticket_storage.add(ticket)

    close_markup = build_close_button(ticket.id, role="client")
    if workshop_id is not None:
        workshop_name = master_storage.get_workshop_name(workshop_id) or "выбранный цех"
        await update.message.reply_text(
            f"✅ Ваша заявка принята! Мастера цеха «{workshop_name}» скоро свяжутся с вами.",
            reply_markup=close_markup,
        )
    else:
        await update.message.reply_text("✅ Ваша заявка принята! Мастера скоро свяжутся с вами.", reply_markup=close_markup)
    await notify_operators(context.bot, ticket)


async def process_days_setting_input(update: Update, context: ContextTypes.DEFAULT_TYPE, setting_key: str):
    """Принимает введённое администратором число дней и сохраняет настройку."""
    text = (update.message.text or "").strip()
    try:
        value = int(text)
    except ValueError:
        await update.message.reply_text("❌ Введите число (количество дней).")
        return
    if value < 1 or value > 365:
        await update.message.reply_text("❌ Число дней должно быть от 1 до 365.")
        return

    settings_storage.set(setting_key, str(value))
    context.user_data.pop('awaiting_days_setting', None)
    context.user_data.pop('awaiting_message_id', None)

    label = (
        "предложение закрыть заявку" if setting_key == SETTING_CLOSE_PROMPT_DAYS
        else "автозакрытие заявки"
    )
    await update.message.reply_text(
        f"✅ Настройка «{label}» обновлена: теперь **{value} дн.**",
        parse_mode='Markdown',
        reply_markup=build_admin_panel_keyboard(),
    )


async def process_workshop_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает название цеха от администратора и создаёт его."""
    text = (update.message.text or "").strip()
    if text.lower() in {"отмена", "отменить", "cancel"}:
        context.user_data.pop('awaiting_workshop_name', None)
        await update.message.reply_text("❌ Добавление цеха отменено.")
        return
    if not text:
        await update.message.reply_text("❌ Название не может быть пустым.")
        return
    workshop_id = master_storage.add_workshop(text)
    context.user_data.pop('awaiting_workshop_name', None)
    await update.message.reply_text(
        f"🏭 Цех «{text}» добавлен (ID: {workshop_id}).",
        reply_markup=build_admin_panel_keyboard(),
    )


EMAIL_SETTING_LABELS = {
    "email": "адрес почты",
    "email_password": "пароль почты",
    "imap_server": "IMAP-сервер",
    "imap_port": "IMAP-порт",
    "check_interval": "интервал проверки почты (секунды)",
}


async def process_email_setting_input(update: Update, context: ContextTypes.DEFAULT_TYPE, setting_key: str):
    """Принимает введённое администратором значение параметра чтения почты."""
    text = (update.message.text or "").strip()

    # отмена изменения
    if text.lower() in {"отмена", "отменить", "cancel"}:
        context.user_data.pop('awaiting_email_setting', None)
        await update.message.reply_text(
            "❌ Изменение отменено.",
            reply_markup=build_extra_settings_keyboard(),
        )
        return

    if not text:
        await update.message.reply_text("❌ Значение не может быть пустым.")
        return

    if setting_key in {"imap_port", "check_interval"}:
        try:
            numeric_value = int(text)
        except ValueError:
            await update.message.reply_text(
                f"❌ «{EMAIL_SETTING_LABELS.get(setting_key, setting_key)}» должно быть числом."
            )
            return
        if numeric_value < 1:
            await update.message.reply_text("❌ Значение должно быть положительным числом.")
            return
        settings_storage.set(setting_key, str(numeric_value))
    else:
        settings_storage.set(setting_key, text)

    context.user_data.pop('awaiting_email_setting', None)
    await update.message.reply_text(
        f"✅ Настройка «{EMAIL_SETTING_LABELS.get(setting_key, setting_key)}» обновлена.",
        reply_markup=build_extra_settings_keyboard(),
    )


async def handle_operator_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ответы мастеров с поддержкой нескольких одновременных заявок."""
    operator_id = update.effective_user.id
    ticket = None
    ticket_id = None

    if update.message.reply_to_message:
        replied_message_id = update.message.reply_to_message.message_id
        ticket_id = operator_message_storage.get_ticket_id(replied_message_id)
        if ticket_id:
            ticket = ticket_storage.get(ticket_id)

    if not ticket:
        ticket_id = operator_message_storage.get_last_open_ticket_id(operator_id)
        if ticket_id:
            ticket = ticket_storage.get(ticket_id)

    if not ticket:
        operator_tickets = ticket_storage.get_operator_tickets(operator_id)
        if not operator_tickets:
            await update.message.reply_text("❗ У вас нет открытых заявок. Сначала примите заявку или ответьте на сообщение из заявки.")
            return

        if len(operator_tickets) == 1:
            ticket = operator_tickets[0]
        else:
            lines = ["📋 Выберите заявку для ответа:\n"]
            for i, t in enumerate(operator_tickets, 1):
                lines.append(f"{i}. **{t.id}** — {t.client_name}")
            lines.append("\n_Примечание: ответьте на сообщение о заявке или напишите /close <номер> для закрытия_")
            await update.message.reply_text("\n".join(lines), parse_mode='Markdown')
            return

    if not ticket:
        await update.message.reply_text("❗ Заявка не найдена или закрыта.")
        return

    if ticket.status == 'closed' and not update.message.reply_to_message:
        await update.message.reply_text("❗ Эта заявка уже закрыта. Чтобы продолжить её, нажмите «Ответить» на старом сообщении из этой заявки.")
        return

    replies_allowed = settings_storage.get(
        ALLOW_FOREIGN_TICKET_REPLIES, "false"
    ).lower() == "true"
    if ticket.taken_by != operator_id and not replies_allowed:
        await update.message.reply_text(
            "❗ Эта заявка вам не принадлежит. Ответ отправить невозможно."
        )
        return

    reply_text = f"📨 **Ответ от мастера** (ID заявки: {ticket.id}):\n\n{update.message.text}"
    client_id = ticket.client_id

    try:
        if ticket.source == 'telegram':
            sent_message = await context.bot.send_message(chat_id=int(client_id), text=reply_text, parse_mode='Markdown')
            operator_message_storage.add(
                sent_message.message_id, int(client_id), ticket.id,
                sender='op', sender_id=str(operator_id), text=update.message.text,
            )
        elif ticket.source == 'email':
            await send_email_reply(
                ticket.client_id, ticket.id, update.message.text,
                ticket.message_id, ticket.subject,
            )

        sent_message = await update.message.reply_text(f"✅ Ваш ответ отправлен клиенту по заявке {ticket.id}.")
        if ticket.source == 'email':
            operator_message_storage.add(
                sent_message.message_id, operator_id, ticket.id,
                sender='op', sender_id=str(operator_id), text=update.message.text,
            )
        else:
            operator_message_storage.add(sent_message.message_id, operator_id, ticket.id)
    except Exception as e:
        logger.error(f"Error sending reply: {e}")
        await update.message.reply_text(f"❌ Ошибка при отправке ответа: {e}")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на кнопки (например, 'Принять заявку')."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data or ""

    if data.startswith("close_prompt_yes_") or data.startswith("close_prompt_no_"):
        answer = "yes" if data.startswith("close_prompt_yes_") else "no"
        ticket_id = data.replace("close_prompt_yes_", "").replace("close_prompt_no_", "")
        ticket = ticket_storage.get(ticket_id)
        if not is_operator(user_id):
            await query.edit_message_text("⛔ У вас нет прав для выполнения этого действия.")
            return
        if not ticket:
            await query.edit_message_text("❌ Заявка не найдена.")
            return

        if answer == "yes":
            await close_ticket_and_notify(context.bot, ticket, by=user_id)
            await query.edit_message_text(f"✅ Заявка {ticket.id} закрыта.")
        else:
            # Начинаем отсчёт заново
            ticket.close_prompted_at = None
            ticket.close_prompt_message_id = None
            ticket.close_no_at = datetime.now().isoformat()
            ticket_storage.update(ticket)
            await query.edit_message_text(
                f"🕒 Заявка {ticket.id} остаётся открытой. "
                f"Вопрос о закрытии появится снова через {get_close_prompt_days()} дн. неактивности."
            )
        return

    if data.startswith("close_client_"):
        ticket_id = data.replace("close_client_", "")
        ticket = ticket_storage.get(ticket_id)
        if not ticket:
            await query.edit_message_text("❌ Заявка не найдена.")
            return
        if ticket.client_id != str(user_id):
            await query.answer("⛔ Вы можете закрыть только свою заявку.", show_alert=True)
            return
        ticket.status = 'closed'
        ticket_storage.update(ticket)
        await query.edit_message_text(f"✅ Заявка {ticket.id} закрыта. Для продолжения напишите сообщение с ответом на прошлое сообщение.")
        return

    if data.startswith("close_op_"):
        if not is_operator(user_id):
            await query.answer("⛔ У вас нет прав для выполнения этого действия.", show_alert=True)
            return
        ticket_id = data.replace("close_op_", "")
        ticket = ticket_storage.get(ticket_id)
        if not ticket:
            await query.edit_message_text("❌ Заявка не найдена.")
            return
        if ticket.taken_by != user_id:
            await query.answer("⛔ Вы можете закрыть только свою активную заявку.", show_alert=True)
            return
        ticket.status = 'closed'
        ticket_storage.update(ticket)
        await query.edit_message_text(f"✅ Заявка {ticket.id} закрыта.")
        close_notification = f"✅ Заявка **{ticket.id}** закрыта. Спасибо за обращение!"
        try:
            if ticket.source == 'telegram':
                await context.bot.send_message(chat_id=int(ticket.client_id), text=close_notification, parse_mode='Markdown')
            elif ticket.source == 'email':
                await send_email_reply(
                    ticket.client_id, ticket.id,
                    "Заявка закрыта. Спасибо за обращение!",
                    ticket.message_id, ticket.subject,
                )
        except Exception as e:
            logger.error(f"Could not notify client about ticket closure: {e}")
        return

    if not is_operator(user_id):
        await query.edit_message_text("⛔ У вас нет прав для выполнения этого действия.")
        return

    if data.startswith("send_dialog_ticket_"):
        ticket_id = data.replace("send_dialog_ticket_", "")
        ticket = ticket_storage.get(ticket_id)
        if not ticket:
            await query.edit_message_text("❌ Заявка не найдена.")
            return

        masters = [m for m in master_storage.all() if m['user_id'] != user_id]
        if not masters:
            await query.edit_message_text("❗ Нет других мастеров для отправки диалога.")
            return

        keyboard = [
            [InlineKeyboardButton(m['full_name'] or str(m['user_id']), callback_data=f"send_dialog_to_{ticket_id}_{m['user_id']}")]
            for m in masters
        ]
        await query.edit_message_text(
            f"🤝 Заявка {ticket.id} ({ticket.client_name}).\nКому отправить диалог?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("send_dialog_to_"):
        payload = data.replace("send_dialog_to_", "")
        ticket_id, _, target_str = payload.rpartition("_")
        target_user_id = int(target_str)
        ticket = ticket_storage.get(ticket_id)
        if not ticket:
            await query.edit_message_text("❌ Заявка не найдена.")
            return
        try:
            chunks = await send_ticket_dialog(context, ticket, target_user_id)
            await query.edit_message_text(
                f"✅ Диалог заявки {ticket.id} отправлен мастеру "
                f"({chunks} сообщ.) и открыт в чате с ним."
            )
        except Exception as e:
            logger.error(f"Error sending dialog: {e}")
            await query.edit_message_text(f"❌ Ошибка при отправке диалога: {e}")
        return

    if data.startswith("take_"):
        ticket_id = data.replace("take_", "")
        ticket = ticket_storage.get(ticket_id)

        if not ticket:
            await query.message.reply_text("❌ Заявка не найдена.")
            return

        if ticket.status == 'taken':
            await query.message.reply_text(f"⚠️ Заявка уже принята мастером {ticket.taken_by}.")
            return

        ticket.status = 'taken'
        ticket.taken_by = user_id
        ticket_storage.update(ticket)

        confirmation_msg = await query.message.reply_text(
            text=f"✅ Вы приняли заявку {ticket.id}.\n"
                 f"👤 Клиент: {ticket.client_name}\n"
                 f"📝 Текст: {ticket.text}\n\n"
                 f"Теперь вы можете отвечать на неё, просто отправляя сообщения в этот чат или отвечая на это сообщение.",
            reply_markup=build_close_button(ticket.id, role="operator")
        )
        operator_message_storage.add(confirmation_msg.message_id, user_id, ticket_id)

        notification_text = f"👤 мастер {query.from_user.full_name} принял заявку **{ticket.id}**."
        for op_id in get_operator_ids():
            if op_id != user_id:
                try:
                    await context.bot.send_message(chat_id=op_id, text=notification_text, parse_mode='Markdown')
                except Exception as e:
                    logger.error(f"Could not notify operator {op_id}: {e}")


async def close_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для закрытия заявки. Использование: /close [ticket_id]"""
    user_id = update.effective_user.id
    if not is_operator(user_id):
        await update.message.reply_text("⛔ У вас нет прав.")
        return

    ticket_id = None

    if context.args:
        ticket_id = context.args[0]
    else:
        ticket_id = operator_message_storage.get_last_open_ticket_id(user_id)

    active_tickets = ticket_storage.get_operator_tickets(user_id)
    if not ticket_id and len(active_tickets) > 1:
        keyboard = [[InlineKeyboardButton(f"{t.id} — {t.client_name}", callback_data=f"close_op_{t.id}")] for t in active_tickets]
        await update.message.reply_text("📋 Выберите заявку, которую нужно закрыть:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if not ticket_id:
        await update.message.reply_text("❗ У вас нет открытых заявок. Сначала примите заявку или выберите активную заявку для закрытия.")
        return

    ticket = ticket_storage.get(ticket_id)
    if not ticket:
        await update.message.reply_text("❌ Заявка не найдена.")
        return

    if ticket.taken_by != user_id:
        await update.message.reply_text("⛔ Вы не можете закрыть заявку, которая не принята вами.")
        return

    ticket.status = 'closed'
    ticket_storage.update(ticket)
    await update.message.reply_text(f"✅ Заявка {ticket.id} закрыта.")

    close_notification = f"✅ Заявка **{ticket.id}** закрыта. Спасибо за обращение!"
    try:
        if ticket.source == 'telegram':
            await context.bot.send_message(chat_id=int(ticket.client_id), text=close_notification, parse_mode='Markdown')
        elif ticket.source == 'email':
            await send_email_reply(
                ticket.client_id, ticket.id,
                "Заявка закрыта. Спасибо за обращение!",
                ticket.message_id, ticket.subject,
            )
    except Exception as e:
        logger.error(f"Could not notify client about ticket closure: {e}")


async def show_close_ticket_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает мастеру список его открытых заявок для закрытия."""
    user_id = update.effective_user.id
    if not is_operator(user_id):
        await update.message.reply_text("⛔ У вас нет прав.")
        return

    active_tickets = ticket_storage.get_operator_tickets(user_id)
    if not active_tickets:
        await update.message.reply_text("❗ У вас нет открытых заявок.")
        return

    keyboard = [
        [InlineKeyboardButton(f"{ticket.id} — {ticket.client_name}", callback_data=f"close_op_{ticket.id}")]
        for ticket in active_tickets
    ]
    await update.message.reply_text(
        "📋 Выберите заявку, которую нужно закрыть:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def get_my_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /mytickets — выводит список заявок, принятых мастером."""
    user_id = update.effective_user.id
    if not is_operator(user_id):
        await update.message.reply_text("⛔ У вас нет прав.")
        return

    tickets = ticket_storage.get_operator_tickets(user_id)
    if not tickets:
        await update.message.reply_text("📭 У вас нет открытых заявок.")
        return

    lines = ["📋 Ваши открытые заявки:"]
    for t in tickets:
        lines.append(f"• **{t.id}** — {t.client_name} (Статус: {t.status})")
    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


# ==================== ОТПРАВКА ДИАЛОГА ====================

MAX_MESSAGE_CHARS = 4000


def split_long_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Разбивает длинный текст на части не длиннее limit (по возможности на границе слов)."""
    text = text.strip()
    if len(text) <= limit:
        return [text]

    parts = []
    while len(text) > limit:
        cut = text.rfind(' ', 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def format_ticket_dialog(ticket, dialog) -> list[str]:
    """Форматирует переписку в один/несколько красивых сообщений (до 4000 символов каждое)."""
    header = (
        f"📋 **Диалог по заявке {ticket.id}**\n"
        f"👤 Клиент: {ticket.client_name}\n"
        f"📬 Источник: {ticket.source}\n"
        f"📅 Заявка создана: {ticket.created_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"{'─' * 30}\n"
    )

    blocks = []
    for i, entry in enumerate(dialog):
        role = "🧑 Клиент" if entry['role'] == 'client' else "🛠 Мастер"
        author = entry['author']
        time = entry['time']
        if isinstance(time, str):
            try:
                time = datetime.fromisoformat(time)
            except ValueError:
                time = None
        time_str = time.strftime('%d.%m.%Y %H:%M') if time else "—"
        text = entry['text'] or ""
        blocks.append(f"**{i + 1}. {role}: {author}**\n🕐 {time_str}\n{text}")

    header_budget = MAX_MESSAGE_CHARS - len(header)
    chunks = []
    current = ""
    for block in blocks:
        if len(current) + len(block) + 1 > header_budget and current:
            chunks.append(current)
            current = ""
        if len(block) > header_budget:
            for part in split_long_text(block, header_budget):
                if len(current) + len(part) + 1 > header_budget and current:
                    chunks.append(current)
                    current = ""
                current = f"{current}\n{part}" if current else part
            continue
        current = f"{current}\n{block}" if current else block

    if current:
        chunks.append(current)

    if not chunks:
        chunks = ["(нет сообщений)"]

    result = [f"{header}{chunks[0]}"]
    for chunk in chunks[1:]:
        result.append(f"_(продолжение диалога по заявке {ticket.id})_\n\n{chunk}")
    return result


async def show_send_dialog_ticket_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Первый шаг: оператор выбирает заявку, диалог которой хочет отправить."""
    user_id = update.effective_user.id
    if not is_operator(user_id):
        await update.message.reply_text("⛔ У вас нет прав.")
        return

    tickets = ticket_storage.get_operator_all_tickets(user_id)
    if not tickets:
        await update.message.reply_text("❗ У вас нет заявок для отправки.")
        return

    keyboard = [
        [InlineKeyboardButton(f"{ticket.id} — {ticket.client_name} ({'открыта' if ticket.status in ('new', 'taken') else 'закрыта'})", callback_data=f"send_dialog_ticket_{ticket.id}")]
        for ticket in tickets
    ]
    await update.message.reply_text(
        "📤 Выберите заявку, диалог которой хотите отправить:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def send_ticket_dialog(context: ContextTypes.DEFAULT_TYPE, ticket, target_user_id: int):
    """Формирует и отправляет весь диалог заявки мастеру target_user_id."""
    dialog = operator_message_storage.get_ticket_dialog(ticket)
    chunks = format_ticket_dialog(ticket, dialog)
    for chunk in chunks:
        try:
            await context.bot.send_message(chat_id=target_user_id, text=chunk, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error sending dialog chunk to {target_user_id}: {e}")
            await context.bot.send_message(chat_id=target_user_id, text=chunk)
    return len(chunks)


# ==================== КОНТРОЛЬ НЕАКТИВНОСТИ ====================


def build_close_prompt_keyboard(ticket_id: str) -> InlineKeyboardMarkup:
    keyboard = [[
        InlineKeyboardButton("✅ Да", callback_data=f"close_prompt_yes_{ticket_id}"),
        InlineKeyboardButton("❌ Нет", callback_data=f"close_prompt_no_{ticket_id}"),
    ]]
    return InlineKeyboardMarkup(keyboard)


async def prompt_operator_to_close(app, ticket: Ticket):
    """Отправляет мастеру заявки запрос о закрытии и сохраняет время запроса."""
    if not ticket.taken_by:
        return
    days = get_close_prompt_days()
    text = (
        f"🤔 В диалоге по заявке **{ticket.id}** ({ticket.client_name}) "
        f"не было сообщений уже {days} дн.\n"
        f"Закрыть заявку?"
    )
    try:
        sent_message = await app.bot.send_message(
            chat_id=ticket.taken_by,
            text=text,
            parse_mode='Markdown',
            reply_markup=build_close_prompt_keyboard(ticket.id),
        )
        ticket.close_prompted_at = datetime.now().isoformat()
        ticket.close_prompt_message_id = sent_message.message_id
        ticket_storage.update(ticket)
        logger.info(f"Запрошено закрытие заявки {ticket.id} у мастера {ticket.taken_by}")
    except Exception as e:
        logger.error(f"Не удалось отправить запрос о закрытии заявки {ticket.id}: {e}")


async def close_ticket_and_notify(bot, ticket: Ticket, by: Optional[int] = None):
    """Закрывает заявку и уведомляет клиента."""
    ticket.status = 'closed'
    ticket_storage.update(ticket)
    logger.info(f"Заявка {ticket.id} закрыта автоматически после неактивности")
    close_notification = f"✅ Заявка **{ticket.id}** закрыта. Спасибо за обращение!"
    try:
        if ticket.source == 'telegram':
            await bot.send_message(chat_id=int(ticket.client_id), text=close_notification, parse_mode='Markdown')
        elif ticket.source == 'email':
            await send_email_reply(
                ticket.client_id, ticket.id,
                "Заявка закрыта. Спасибо за обращение!",
                ticket.message_id, ticket.subject,
            )
    except Exception as e:
        logger.error(f"Could not notify client about ticket closure: {e}")


async def check_inactive_tickets(app: Application):
    """Фоновая задача: контроль неактивных заявок.

    - Если в диалоге нет сообщений больше CLOSE_PROMPT_AFTER_DAYS дней —
      мастеру приходит запрос «Закрыть заявку?».
    - Если на запрос не ответили за AUTO_CLOSE_AFTER_DAYS дней —
      заявка закрывается автоматически.
    """
    try:
        now = datetime.now()
        prompt_days = get_close_prompt_days()
        auto_close_days = get_auto_close_days()
        open_tickets = ticket_storage.get_active_tickets()
        for ticket in open_tickets:
            if not ticket.taken_by:
                continue
            last_activity = ticket_storage.get_last_activity_time(ticket.id)
            if last_activity is None:
                last_activity = ticket.created_at

            prompted_at = None
            if ticket.close_prompted_at:
                try:
                    prompted_at = datetime.fromisoformat(ticket.close_prompted_at)
                except ValueError:
                    prompted_at = None

            if prompted_at is None:
                # Ещё не спрашивали: если нет активности N дней — спрашиваем
                if (now - last_activity) >= timedelta(days=prompt_days):
                    await prompt_operator_to_close(app, ticket)
            else:
                # Уже спрашивали: если клиент писал после запроса — считаем заново
                if last_activity > prompted_at:
                    ticket.close_prompted_at = None
                    ticket_storage.update(ticket)
                    continue
                # Если мастер не нажал кнопку за N дней — закрываем автоматически
                if (now - prompted_at) >= timedelta(days=auto_close_days):
                    await close_ticket_and_notify(app.bot, ticket)
    except Exception as e:
        logger.error(f"Error checking inactive tickets: {e}")


async def periodic_inactivity_check(app: Application):
    """Запускает бесконечный цикл проверки неактивных заявок."""
    while True:
        await check_inactive_tickets(app)
        await asyncio.sleep(INACTIVITY_CHECK_INTERVAL)


# ==================== АДМИН-ПАНЕЛЬ ====================


def build_admin_panel_keyboard() -> InlineKeyboardMarkup:
    replies_allowed = settings_storage.get(ALLOW_FOREIGN_TICKET_REPLIES, "false").lower() == "true"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Список мастеров", callback_data="adm_ops_list")],
        [InlineKeyboardButton("🗑 Удалить мастера", callback_data="adm_ops_delete")],
        [InlineKeyboardButton("🏭 Добавить цех", callback_data="adm_wshop_add")],
        [InlineKeyboardButton("🏭 Удалить цех", callback_data="adm_wshop_delete")],
        [InlineKeyboardButton(
            f"⏳ Предупреждение: {get_close_prompt_days()} дн.",
            callback_data="adm_ops_days_req_close",
        )],
        [InlineKeyboardButton(
            f"🚫 Автозакрытие: {get_auto_close_days()} дн.",
            callback_data="adm_ops_days_autoclose",
        )],
        [InlineKeyboardButton(
            "✅ Отвечать на чужие заявки: вкл" if replies_allowed else "❌ Отвечать на чужие заявки: выкл",
            callback_data="adm_foreign_replies_status",
        )],
        [InlineKeyboardButton("Включить ответы на чужие заявки", callback_data="adm_foreign_replies_on")],
        [InlineKeyboardButton("Отключить ответы на чужие заявки", callback_data="adm_foreign_replies_off")],
        [InlineKeyboardButton("⚙️ Дополнительные настройки", callback_data="adm_extra_settings")],
    ])


def build_extra_settings_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура подменю «Дополнительные настройки» (чтение почты)."""
    email = settings_storage.get("email")
    has_password = bool(settings_storage.get("email_password"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📧 Адрес почты{f': {email}' if email else ''}",
            callback_data="adm_extra_email",
        )],
        [InlineKeyboardButton(
            f"🔑 Пароль почты{f': ••••••••' if has_password else ''}",
            callback_data="adm_extra_email_password",
        )],
        [InlineKeyboardButton(
            f"📥 IMAP-сервер{f': {settings_storage.get('imap_server')}' if settings_storage.get('imap_server') else ''}",
            callback_data="adm_extra_imap_server",
        )],
        [InlineKeyboardButton(
            f"🔌 IMAP-порт{f': {settings_storage.get('imap_port')}' if settings_storage.get('imap_port') else ''}",
            callback_data="adm_extra_imap_port",
        )],
        [InlineKeyboardButton(
            f"⏱ Интервал проверки{f': {settings_storage.get('check_interval')} сек' if settings_storage.get('check_interval') else ''}",
            callback_data="adm_extra_check_interval",
        )],
        [InlineKeyboardButton("⬅️ Назад", callback_data="adm_back")],
    ])


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /admin — панель управления мастерами."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ только для администратора.")
        return
    await update.message.reply_text("👑 Панель администратора:", reply_markup=build_admin_panel_keyboard())


async def show_operator_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает администратору список активных мастеров."""
    if not is_admin(update.effective_user.id):
        return

    operators = master_storage.all(include_deleted=False)
    if not operators:
        text = "📭 Список мастеров пуст."
    else:
        lines = ["👥 Активные мастеры:"]
        lines += [
            f"• {row['full_name'] or 'без имени'} ({row['user_id']}) — 🏭 {row['workshop_name'] or 'без цеха'}"
            for row in operators
        ]
        text = "\n".join(lines)
    await update.message.reply_text(text)


async def show_delete_operator_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает администратору список мастеров для удаления."""
    if not is_admin(update.effective_user.id):
        return

    operators = master_storage.all(include_deleted=False)
    if not operators:
        await update.message.reply_text("📭 Нет мастеров для удаления.")
        return

    keyboard = [
        [InlineKeyboardButton(
            f"🗑 {row['full_name'] or row['user_id']} ({row['user_id']})",
            callback_data=f"delete_op_{row['user_id']}"
        )]
        for row in operators
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="adm_back")])
    await update.message.reply_text(
        "Выберите мастера для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия кнопок админ-панели."""
    query = update.callback_query
    await query.answer()

    data = query.data
    
    # Обработка заявок на роль мастера (доступно админу)
    if data.startswith("approve_op_"):
        if not is_admin(query.from_user.id):
            await query.edit_message_text("⛔ Доступ только для администратора.")
            return
        
        applicant_id = int(data.replace("approve_op_", ""))
        applicant_name = operator_requests.get(applicant_id, "Неизвестный пользователь")
        
        # Показываем админу выбор цеха для нового мастера
        workshops = master_storage.get_workshops()
        keyboard = [
            [InlineKeyboardButton(f"🏭 {w['name']}", callback_data=f"op_confirm_{applicant_id}:{w['id']}")]
            for w in workshops
        ]
        keyboard.append([InlineKeyboardButton("🚫 Без цеха", callback_data=f"op_confirm_{applicant_id}:0")])
        keyboard.append([InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_op_{applicant_id}")])

        await query.edit_message_text(
            f"Выберите цех для мастера **{applicant_name}** ({applicant_id}):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("op_confirm_"):
        if not is_admin(query.from_user.id):
            await query.edit_message_text("⛔ Доступ только для администратора.")
            return
        
        payload = data.replace("op_confirm_", "")
        applicant_id_str, workshop_id_str = payload.split(":", 1)
        applicant_id = int(applicant_id_str)
        workshop_id = int(workshop_id_str) if workshop_id_str and workshop_id_str != "0" else None
        applicant_name = operator_requests.get(applicant_id, "Неизвестный пользователь")
        
        master_storage.add(applicant_id, applicant_name, workshop_id)
        
        operator_requests.pop(applicant_id, None)
        
        workshop_name = master_storage.get_workshop_name(workshop_id)
        workshop_text = f"🏭 Цех: {workshop_name}" if workshop_name else "🏭 Без цеха"
        
        await query.edit_message_text(
            f"✅ Пользователь **{applicant_name}** ({applicant_id}) добавлен как мастер.\n{workshop_text}"
        )
        
        # Уведомляем нового мастера
        try:
            await context.bot.send_message(
                chat_id=applicant_id,
                text="✅ Поздравляем! Вы добавлены как мастер.\n\n"
                     "Теперь вы можете принимать заявки от клиентов.",
                reply_markup=build_operator_keyboard()
            )
        except Exception as e:
            logger.error(f"Could not notify new operator {applicant_id}: {e}")
        return

    if data.startswith("reject_op_"):
        if not is_admin(query.from_user.id):
            await query.edit_message_text("⛔ Доступ только для администратора.")
            return
        
        applicant_id = int(data.replace("reject_op_", ""))
        applicant_name = operator_requests.get(applicant_id, "Неизвестный пользователь")
        
        await query.edit_message_text(f"❌ Заявка пользователя **{applicant_name}** ({applicant_id}) отклонена.")
        
        # Удаляем заявку из хранилища
        operator_requests.pop(applicant_id, None)
        
        # Уведомляем об отказе
        try:
            await context.bot.send_message(
                chat_id=applicant_id,
                text="❌ К сожалению, ваша заявка на роль мастера была отклонена."
            )
        except Exception as e:
            logger.error(f"Could not notify applicant {applicant_id}: {e}")
        return

    if data.startswith("transfer_admin_"):
        if not is_admin(query.from_user.id):
            return

        decision, request_id = data.split(":", 1)
        request = admin_transfer_requests.pop(request_id, None)
        if not request:
            await query.edit_message_text("❗ Запрос уже обработан или устарел.")
            return

        if decision == "transfer_admin_yes":
            previous_admin_id = query.from_user.id
            settings_storage.set("admin_id", request["target_id"])
            await query.edit_message_text(
                f"✅ Права администратора переданы пользователю "
                f"{request['target_name']} ({request['target_id']})."
            )
            try:
                await context.bot.send_message(
                    chat_id=previous_admin_id,
                    text="ℹ️ Вы больше не являетесь администратором.",
                    reply_markup=build_operator_keyboard(),
                )
                await context.bot.send_message(
                    chat_id=request["target_id"],
                    text="✅ Вам переданы права администратора.",
                    reply_markup=build_admin_keyboard(),
                )
            except Exception as e:
                logger.error(f"Could not notify new administrator {request['target_id']}: {e}")
        else:
            await query.edit_message_text(
                f"❌ Запрос пользователя {request['target_name']} отклонён."
            )
        return

    if data in {"adm_foreign_replies_on", "adm_foreign_replies_off"}:
        if not is_admin(query.from_user.id):
            return
        value = "true" if data.endswith("_on") else "false"
        settings_storage.set(ALLOW_FOREIGN_TICKET_REPLIES, value)
        status = "включены" if value == "true" else "отключены"
        await query.edit_message_text(
            f"✅ Ответы на чужие заявки {status}.",
            reply_markup=build_admin_panel_keyboard(),
        )
        return

    if data == "adm_foreign_replies_status":
        if not is_admin(query.from_user.id):
            return
        await query.edit_message_text(
            "Настройка отображается на кнопке ниже.",
            reply_markup=build_admin_panel_keyboard(),
        )
        return

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Доступ только для администратора.")
        return

    if data == "adm_wshop_add":
        await query.edit_message_text(
            "🏭 Введите название нового цеха.\n"
            "Для отмены напишите «отмена»."
        )
        context.user_data['awaiting_workshop_name'] = True
        return

    elif data == "adm_wshop_delete":
        workshops = master_storage.get_workshops()
        if not workshops:
            await query.edit_message_text("📭 Нет цехов для удаления.", reply_markup=build_admin_panel_keyboard())
            return
        keyboard = [
            [InlineKeyboardButton(f"🗑 {w['name']}", callback_data=f"del_wshop_{w['id']}")]
            for w in workshops
        ]
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="adm_back")])
        await query.edit_message_text("Выберите цех для удаления:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data.startswith("del_wshop_"):
        workshop_id = int(data.replace("del_wshop_", ""))
        workshop_name = master_storage.get_workshop_name(workshop_id) or str(workshop_id)
        reset_count = master_storage.delete_workshop(workshop_id)
        if reset_count == -1:
            await query.edit_message_text("❌ Цех не найден.", reply_markup=build_admin_panel_keyboard())
        else:
            await query.edit_message_text(
                f"✅ Цех «{workshop_name}» удалён. Мастеров без цеха: {reset_count}.",
                reply_markup=build_admin_panel_keyboard(),
            )
        return

    if data == "adm_extra_settings":
        await query.edit_message_text(
            "⚙️ Дополнительные настройки (чтение почты):\n"
            "Выберите параметр для изменения.",
            reply_markup=build_extra_settings_keyboard(),
        )
        return

    _extra_setting_keys = {
        "adm_extra_email": "email",
        "adm_extra_email_password": "email_password",
        "adm_extra_imap_server": "imap_server",
        "adm_extra_imap_port": "imap_port",
        "adm_extra_check_interval": "check_interval",
    }
    if data in _extra_setting_keys:
        setting_key = _extra_setting_keys[data]
        context.user_data['awaiting_email_setting'] = setting_key
        await query.edit_message_text(
            f"Текущее значение «{EMAIL_SETTING_LABELS.get(setting_key, setting_key)}»: "
            f"**{'••••••••' if setting_key == 'email_password' else (settings_storage.get(setting_key) or '—')}**\n\n"
            f"Введите новое значение. Для отмены напишите «отмена».",
            parse_mode='Markdown',
        )
        return

    if data == "adm_ops_list":
        operators = master_storage.all(include_deleted=False)
        if not operators:
            text = "📭 Список мастеров пуст."
        else:
            lines = ["👥 Активные мастеры:"]
            lines += [
                f"• {row['full_name'] or 'без имени'} ({row['user_id']}) — 🏭 {row['workshop_name'] or 'без цеха'}"
                for row in operators
            ]
            text = "\n".join(lines)
        await query.edit_message_text(text, reply_markup=build_admin_panel_keyboard())
        return

    elif data == "adm_ops_delete":
        operators = master_storage.all(include_deleted=False)
        if not operators:
            await query.edit_message_text("📭 Нет мастеров для удаления.", reply_markup=build_admin_panel_keyboard())
            return
        keyboard = [
            [InlineKeyboardButton(
                f"🗑 {row['full_name'] or row['user_id']} ({row['user_id']})",
                callback_data=f"delete_op_{row['user_id']}"
            )]
            for row in operators
        ]
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="adm_back")])
        await query.edit_message_text("Выберите мастера для удаления:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data.startswith("delete_op_"):
        target_id = int(data.replace("delete_op_", ""))
        operator = master_storage.all(include_deleted=True)
        operator_name = None
        for op in operator:
            if op['user_id'] == target_id:
                operator_name = op['full_name'] or str(target_id)
                break
        
        if master_storage.delete(target_id):
            await query.edit_message_text(
                f"✅ мастер **{operator_name}** ({target_id}) удалён.",
                reply_markup=build_admin_panel_keyboard(),
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text("❌ Ошибка при удалении мастера.", reply_markup=build_admin_panel_keyboard())
        return

    elif data == "adm_ops_days_req_close":
        if not is_admin(query.from_user.id):
            return
        await query.edit_message_text(
            f"Текущее значение: **{get_close_prompt_days()} дн.**\n\n"
            f"Введите новое количество дней до предложения закрыть заявку:",
            parse_mode='Markdown',
        )
        context.user_data['awaiting_days_setting'] = SETTING_CLOSE_PROMPT_DAYS
        context.user_data['awaiting_message_id'] = query.message.message_id
        return

    elif data == "adm_ops_days_autoclose":
        if not is_admin(query.from_user.id):
            return
        await query.edit_message_text(
            f"Текущее значение: **{get_auto_close_days()} дн.**\n\n"
            f"Введите новое количество дней до автозакрытия заявки:",
            parse_mode='Markdown',
        )
        context.user_data['awaiting_days_setting'] = SETTING_AUTO_CLOSE_DAYS
        context.user_data['awaiting_message_id'] = query.message.message_id
        return

    elif data == "adm_back":
        await query.edit_message_text("👑 Панель администратора:", reply_markup=build_admin_panel_keyboard())


async def apply_operator_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /apply_operator — отправляет запрос администратору на добавление в мастеры."""
    user_id = update.effective_user.id
    user_name = update.effective_user.full_name or update.effective_user.username or str(user_id)
    
    # Проверяем, уже ли пользователь мастер
    if is_operator(user_id):
        await update.message.reply_text("ℹ️ Вы уже являетесь мастером.")
        return
    
    # Создаем inline клавиатуру для админа
    approve_btn = InlineKeyboardButton("✅ Принять", callback_data=f"approve_op_{user_id}")
    reject_btn = InlineKeyboardButton("❌ Отказать", callback_data=f"reject_op_{user_id}")
    keyboard = InlineKeyboardMarkup([[approve_btn, reject_btn]])
    
    # Сохраняем информацию о заявителе в глобальное хранилище
    operator_requests[user_id] = user_name
    
    # Отправляем уведомление админу
    notification_text = (
        f"🔔 Новая заявка на роль мастера\n\n"
        f"👤 Пользователь: {user_name}\n"
        f"🆔 ID: {user_id}\n\n"
        f"Принять или отклонить заявку?"
    )
    
    try:
        await context.bot.send_message(
            chat_id=get_admin_id(),
            text=notification_text,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        await update.message.reply_text("✅ Ваша заявка отправлена администратору. Пожалуйста, ждите решения.")
    except Exception as e:
        logger.error(f"Error sending operator request to admin: {e}")
        await update.message.reply_text(f"❌ Ошибка при отправке заявки: {e}")


async def transfer_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создаёт запрос на передачу прав администратора текущему администратору."""
    user = update.effective_user
    if not is_operator(user.id):
        return

    if is_admin(user.id):
        return

    target_id = user.id
    target_name = user.full_name or user.username or str(target_id)

    request_id = str(target_id)
    admin_transfer_requests[request_id] = {
        "target_id": target_id,
        "target_name": target_name,
        "requester_id": target_id,
    }
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Передать", callback_data=f"transfer_admin_yes:{request_id}"),
        InlineKeyboardButton("❌ Отказать", callback_data=f"transfer_admin_no:{request_id}"),
    ]])
    await context.bot.send_message(
        chat_id=get_admin_id(),
        text=(
            f"Пользователь {target_name} (ID: {target_id}) предлагает передать ему права администратора.\n"
            "Передать права?"
        ),
        reply_markup=keyboard,
    )
