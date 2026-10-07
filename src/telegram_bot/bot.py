import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes

from config import settings_storage
from bridge import record_client_message, record_ticket_closed, record_ticket_created
from models import Ticket, generate_ticket_id
from storage import (
    ticket_storage, operator_message_storage, workshop_storage,
    is_admin, get_admin_id, list_api_masters, workshop_has_masters, set_master_active
)

logger = logging.getLogger(__name__)

# Глобальное хранилище запросов на передачу прав администратора.
admin_transfer_requests = {}


def build_close_button(ticket_id: str) -> InlineKeyboardMarkup:
    """Кнопка закрытия заявки клиентом."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Закрыть заявку", callback_data=f"close_client_{ticket_id}")]]
    )


def build_admin_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура администратора."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("👑 Панель администратора")]],
        resize_keyboard=True,
        one_time_keyboard=False
    )

ALL_WORKSHOPS_BUTTON = "🌍 Отправить заявку для всех цехов"


def build_client_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура клиента: кнопки выбора цеха + отправка всем."""
    workshops = workshop_storage.get_workshops()
    rows = [[KeyboardButton(w["name"])] for w in workshops]
    rows.append([KeyboardButton(ALL_WORKSHOPS_BUTTON)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)

# ==================== ТЕЛЕГРАМ-БОТ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение для новых пользователей."""
    if is_admin(update.effective_user.id):
        await update.message.reply_text(
            "👋 Привет, администратор! Заявки клиентов вы получаете в приложении, "
            "здесь доступны управление мастерами и настройки.",
            reply_markup=build_admin_keyboard(),
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

    # Ожидаем ввод названия нового цеха от администратора
    if context.user_data.get('awaiting_workshop_name') and is_admin(user.id):
        await process_workshop_name_input(update, context)
        return

    # Ожидаем ввод нового значения параметра чтения почты от администратора
    awaiting_email_setting = context.user_data.get('awaiting_email_setting')
    if awaiting_email_setting and is_admin(user.id):
        await process_email_setting_input(update, context, awaiting_email_setting)
        return

    if is_admin(user.id):
        # Админские кнопки; заявки клиентов админ обрабатывает в приложении.
        if update.message.text == "👥 Список мастеров":
            await show_operator_list(update, context)
            return
        if update.message.text == "🗑 Удалить мастера":
            await show_manage_masters_menu(update, context)
            return
        if update.message.text == "👑 Панель администратора":
            await admin_panel(update, context)
            return

    if is_admin(user.id):
        # Мастера работают с заявками в приложении, а не в боте.
        await update.message.reply_text(
            "ℹ️ Заявки и ответы клиентам доступны в приложении мастера. "
            "Здесь бот только принимает заявки от клиентов и ведёт админ-настройки."
        )
        return

    replied_ticket_id = None
    if update.message.reply_to_message:
        replied_ticket_id = operator_message_storage.get_ticket_id(update.message.reply_to_message.message_id)

    if replied_ticket_id:
        replied_ticket = ticket_storage.get(replied_ticket_id)
        if replied_ticket:
            # Мастер работает в приложении, в Telegram ему ничего не шлём:
            # сообщение уходит в заявку через мост и приходит мастеру событием.
            record_client_message(
                replied_ticket.id,
                replied_ticket.client_name or str(user.id),
                update.message.text,
                chat_id=user.id,
                message_id=update.message.message_id,
            )
            await update.message.reply_text("✅ Ваше сообщение отправлено мастеру.")
            return

    open_ticket = ticket_storage.get_open_ticket(user.id)

    if update.message.text == ALL_WORKSHOPS_BUTTON:
        context.user_data.pop("ticket_workshop_id", None)
        await update.message.reply_text(
            "🌍 Заявка будет отправлена мастерам всех цехов. Напишите текст заявки.",
            reply_markup=build_client_keyboard(),
        )
        return

    workshops = {w["name"]: w["id"] for w in workshop_storage.get_workshops()}
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
        # Уточнение по открытой заявке: мастер принимает её в приложении.
        record_client_message(
            open_ticket.id,
            open_ticket.client_name or str(user.id),
            update.message.text,
            chat_id=user.id,
            message_id=update.message.message_id,
        )
        await update.message.reply_text("✅ Ваше сообщение отправлено мастеру.")
        return

    workshop_id = context.user_data.get("ticket_workshop_id")
    if workshop_id is not None:
        workshop_name = workshop_storage.get_workshop_name(workshop_id) or "выбранный цех"
        if not workshop_has_masters(workshop_id):
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
    record_ticket_created(ticket.id)

    close_markup = build_close_button(ticket.id)
    if workshop_id is not None:
        workshop_name = workshop_storage.get_workshop_name(workshop_id) or "выбранный цех"
        await update.message.reply_text(
            f"✅ Ваша заявка принята! Мастера цеха «{workshop_name}» скоро свяжутся с вами.",
            reply_markup=close_markup,
        )
    else:
        await update.message.reply_text("✅ Ваша заявка принята! Мастера скоро свяжутся с вами.", reply_markup=close_markup)


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
    workshop_id = workshop_storage.add_workshop(text)
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


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия клиента: закрытие своей заявки.

    Мастерских кнопок больше нет — мастер работает в приложении, а не в боте.
    """
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data or ""

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
        record_ticket_closed(ticket.id)
        await query.edit_message_text(
            f"✅ Заявка {ticket.id} закрыта. Чтобы начать новую — просто напишите текст заявки."
        )
        return

    await query.edit_message_text("❓ Неизвестная кнопка.")


# ==================== АДМИН-ПАНЕЛЬ ====================


def build_admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Список мастеров", callback_data="adm_ops_list")],
        [InlineKeyboardButton("🗑 Удалить мастера", callback_data="adm_ops_delete")],
        [InlineKeyboardButton("🏭 Добавить цех", callback_data="adm_wshop_add")],
        [InlineKeyboardButton("🏭 Удалить цех", callback_data="adm_wshop_delete")],
        [InlineKeyboardButton("⚙️ Настройки почты", callback_data="adm_extra_settings")],
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
            f"🔑 Пароль почты{': ••••••••' if has_password else ''}",
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
    """Показывает администратору список мастеров (из приложения)."""
    if not is_admin(update.effective_user.id):
        return

    operators = list_api_masters(active_only=False)
    if not operators:
        text = "📭 Список мастеров пуст."
    else:
        lines = ["👥 Активные мастеры:"]
        for row in operators:
            status = "" if row["is_active"] else " — ⛔ отключён"
            lines.append(
                f"• {row['full_name'] or 'без имени'} ({row['master_uid']}) — "
                f"🏭 {row['workshop_name'] or 'без цеха'}{status}"
            )
        text = "\n".join(lines)
    await update.message.reply_text(text)


async def show_manage_masters_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления мастерами (сообщением): отключить или включить мастера."""
    if not is_admin(update.effective_user.id):
        return

    text, keyboard = build_manage_masters_menu()
    if keyboard is None:
        await update.message.reply_text("📭 Нет мастеров.", reply_markup=build_admin_keyboard())
        return
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


def build_manage_masters_menu():
    """(текст, клавиатура) для меню управления мастерами. (None, None), если мастеров нет."""
    operators = list_api_masters(active_only=False)
    if not operators:
        return None, None

    keyboard = []
    for row in operators:
        if row["is_active"]:
            label = f"🚫 Отключить {row['full_name'] or row['master_uid']}"
            callback = f"disable_op_{row['master_uid']}"
        else:
            label = f"✅ Включить {row['full_name'] or row['master_uid']}"
            callback = f"enable_op_{row['master_uid']}"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="adm_back")])
    return "Управление мастерами (отключение/включение):", keyboard


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия кнопок админ-панели."""
    query = update.callback_query
    await query.answer()

    data = query.data

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
                    text="ℹ️ Вы больше не являетесь администратором.\n"
                         "Заявки клиентов доступны в приложении мастера.",
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
        workshops = workshop_storage.get_workshops()
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
        workshop_name = workshop_storage.get_workshop_name(workshop_id) or str(workshop_id)
        reset_count = workshop_storage.delete_workshop(workshop_id)
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
        operators = list_api_masters(active_only=False)
        if not operators:
            text = "📭 Список мастеров пуст."
        else:
            lines = ["👥 Мастеры:"]
            for row in operators:
                status = "" if row["is_active"] else " — ⛔ отключён"
                lines.append(
                    f"• {row['full_name'] or 'без имени'} ({row['master_uid']}) — "
                    f"🏭 {row['workshop_name'] or 'без цеха'}{status}"
                )
            text = "\n".join(lines)
        await query.edit_message_text(text, reply_markup=build_admin_panel_keyboard())
        return

    elif data == "adm_ops_delete":
        text, keyboard = build_manage_masters_menu()
        if keyboard is None:
            await query.edit_message_text("📭 Нет мастеров.", reply_markup=build_admin_panel_keyboard())
            return
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data.startswith("disable_op_"):
        master_uid = data.replace("disable_op_", "")
        if set_master_active(master_uid, False):
            await query.edit_message_text(
                f"✅ Мастер {master_uid} отключён.", reply_markup=build_admin_panel_keyboard()
            )
        else:
            await query.edit_message_text(
                "❌ Мастер не найден.", reply_markup=build_admin_panel_keyboard()
            )
        return

    elif data.startswith("enable_op_"):
        master_uid = data.replace("enable_op_", "")
        if set_master_active(master_uid, True):
            await query.edit_message_text(
                f"✅ Мастер {master_uid} включён.", reply_markup=build_admin_panel_keyboard()
            )
        else:
            await query.edit_message_text(
                "❌ Мастер не найден.", reply_markup=build_admin_panel_keyboard()
            )
        return

    elif data == "adm_back":
        await query.edit_message_text("👑 Панель администратора:", reply_markup=build_admin_panel_keyboard())


async def transfer_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создаёт запрос на передачу прав администратора текущему администратору."""
    user = update.effective_user

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
