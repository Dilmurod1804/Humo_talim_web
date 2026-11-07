import re
from django import template

register = template.Library()

@register.filter(name='format_tel')
def format_tel(value):
    """
    Telefon raqamini tel: havolasi uchun tozalaydi va xalqaro formatga keltiradi.
    Masalan:
      '90 123 45 67' -> '+998901234567'
      '+998 (90) 123-45-67' -> '+998901234567'
    """
    if not value:
        return ''
    cleaned = ''.join(c for c in str(value).strip() if c.isdigit() or c == '+')
    if not cleaned:
        return ''
    if cleaned.startswith('+'):
        return cleaned
    if len(cleaned) == 9:
        return '+998' + cleaned
    if len(cleaned) == 12 and cleaned.startswith('998'):
        return '+' + cleaned
    return cleaned

@register.filter(name='phone_display')
def phone_display(value):
    """
    Telefon raqamini chiroyli ko'rinishga keltiradi: +998 90 123 45 67
    """
    if not value:
        return ''
    tel = format_tel(value)
    if tel.startswith('+998') and len(tel) == 13:
        return f"{tel[:4]} {tel[4:6]} {tel[6:9]} {tel[9:11]} {tel[11:]}"
    return str(value)

