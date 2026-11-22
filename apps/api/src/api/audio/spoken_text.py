"""Remove addresses from synthesized speech without changing displayed chat text."""
import re

_EMAIL = re.compile(r'\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*', re.IGNORECASE)
_URL = re.compile(r'(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.|mailto:)[^\s<>()]+|\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>()]*)?')


def spoken_text(text: str) -> str:
    text = re.sub(r'\s*<\s*' + _EMAIL.pattern + r'\s*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'(?i)\bmailto:[^\s<>()]+', '', text)
    text = _EMAIL.sub('', text)
    return _URL.sub(lambda match: '.' if match.group().endswith('.') else '', text)
