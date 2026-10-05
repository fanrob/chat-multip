from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Iterable


@dataclass
class Ticket:
    """Класс для хранения информации о заявке."""
    id: str
    source: str
    client_id: str
    client_name: str
    text: str
    status: str = 'new'
    # Legacy: мастеры больше не работают в Telegram, это поле не заполняется.
    taken_by: Optional[int] = None
    created_at: datetime = None
    message_id: Optional[str] = None
    subject: str = ''
    workshop_id: Optional[int] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()


def generate_ticket_id(used_ids: Optional[Iterable[str]] = None) -> str:
    """Генерирует короткий 5-значный идентификатор заявки без повторений."""
    used = set(used_ids or ())
    for value in range(10000, 100000):
        ticket_id = f"{value:05d}"
        if ticket_id not in used:
            return ticket_id
    raise ValueError("Не осталось свободных 5-значных идентификаторов заявок.")
