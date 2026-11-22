"""Build bounded provider-only speech input for the selected pace."""


def paced_text(text: str, pace: str, max_characters: int) -> str:
    tags = {"slow": "[pace=slow]", "normal": "", "fast": "[pace=fast]"}
    if pace not in tags:
        raise ValueError("invalid_speech_pace")
    result = tags[pace] + text
    if len(result) > max_characters:
        raise ValueError("audio_text_too_long")
    return result
