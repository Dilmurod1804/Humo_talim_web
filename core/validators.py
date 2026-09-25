import re
from django.core.exceptions import ValidationError

CYRILLIC_REGEX = re.compile(r'[\u0400-\u04FF]')

def validate_latin_only(value):
    """
    Faqat Lotin (o'zbekcha/inglizcha) alifbosidagi harflarga ruxsat beradi.
    Kirill harflari kiritilsa ValidationError beradi.
    """
    if value and CYRILLIC_REGEX.search(str(value)):
        raise ValidationError("Faqat lotin alifbosidagi harflardan foydalaning! Kirill (krill) harflari qabul qilinmaydi.")

def contains_cyrillic(value: str) -> bool:
    """Tekshirish funksiyasi: Kirill harflari bormi?"""
    if not value:
        return False
    return bool(CYRILLIC_REGEX.search(str(value)))
