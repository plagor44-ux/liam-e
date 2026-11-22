"""Fixed, content-free phrases; no model output or user data is interpolated."""
from itertools import cycle

FILLER_DELAY_SECONDS = 1.2
_PHRASES = {
    'de': {
        'mail': ('Ich schaue kurz in deinen Mails nach.', 'Ich prüfe kurz deine Mails.'),
        'calendar': ('Ich schaue kurz in deinen Kalender.', 'Ich prüfe kurz deine Termine.'),
        'task': ('Ich schaue kurz nach deinen Aufgaben.', 'Ich prüfe kurz deine Aufgaben.'),
        'general': ('Einen Moment, ich trage das zusammen.', 'Einen Moment, ich prüfe das.'),
    },
    'en': {
        'mail': ('I’ll check your emails.', 'Let me look through your emails.'),
        'calendar': ('I’ll check your calendar.', 'Let me check your appointments.'),
        'task': ('I’ll check your tasks.', 'Let me look at your tasks.'),
        'general': ('One moment, I’ll put that together.', 'One moment, let me check.'),
    },
}
_ROTATION = {(language, category): cycle(phrases) for language, groups in _PHRASES.items() for category, phrases in groups.items()}


def category_for_tool(name: str) -> str:
    if 'calendar' in name: return 'calendar'
    if 'task' in name: return 'task'
    if name in {'search_messages', 'get_message', 'get_insights', 'show_mails', 'find_newsletters', 'propose_action', 'create_draft'}: return 'mail'
    return 'general'


def phrase(locale: str, category: str) -> str:
    return next(_ROTATION[(locale if locale in _PHRASES else 'en', category if category in _PHRASES['en'] else 'general')])
