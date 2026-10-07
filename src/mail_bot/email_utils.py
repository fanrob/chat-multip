import re
from typing import Optional


def extract_last_message(email_text: str) -> str:
    """
    Извлекает последнее сообщение из тела письма,
    отбрасывая всю историю переписки (цитаты)
    """
    if not email_text:
        return ""

    lines = email_text.split('\n')

    last_message_lines = []
    found_message = False

    for line in reversed(lines):
        stripped = line.strip()

        if stripped.startswith('>'):
            continue

        if re.search(r'On\s+\w+,\s+\w+\s+\d+,\s+\d{4}\s+at\s+\d+:\d+\s+[AP]M\s+\w+\s+<\S+>\s+wrote:', stripped):
            continue

        if not stripped:
            continue

        if re.match(r'^(Subject|From|To|Date|CC|BCC):', stripped, re.IGNORECASE):
            break

        last_message_lines.append(line)
        found_message = True

    last_message_lines.reverse()

    if not found_message:
        lines = email_text.split('\n')
        result = []
        for line in lines:
            if line.strip().startswith('>') or re.search(r'On\s+\w+,\s+\w+\s+\d+,\s+\d{4}', line):
                break
            result.append(line)
        return '\n'.join(result).strip() or email_text[:500]

    return '\n'.join(last_message_lines).strip()


def clean_email_text(email_text: str, max_length: int = 500) -> str:
    """Очищает и форматирует текст письма для отправки в Telegram"""
    last_message = extract_last_message(email_text)

    if len(last_message) > max_length:
        last_message = last_message[:max_length] + "..."

    return last_message


def get_message_id(msg) -> Optional[str]:
    """Достаёт Message-ID из заголовков письма (регистр имени заголовка не важен)."""
    for key, values in msg.headers.items():
        if key.lower() == 'message-id' and values:
            return values[0].strip() or None
    return None


def normalize_email_subject(subject: str) -> str:
    """Приводит тему к виду без стандартных префиксов ответа и пересылки."""
    normalized = (subject or '').strip()
    while re.match(r'^(re|fw|fwd)\s*:\s*', normalized, re.IGNORECASE):
        normalized = re.sub(
            r'^(re|fw|fwd)\s*:\s*', '', normalized, count=1, flags=re.IGNORECASE
        )
    return re.sub(r'\s+', ' ', normalized).casefold()
