import re
from datetime import date

WEEKDAY_PATTERNS = {
    0: [r'du', r'dushanba', r'mon', r'monday'],
    1: [r'se', r'seshanba', r'tue', r'tuesday'],
    2: [r'chor', r'chorshanba', r'cho', r'wed', r'wednesday'],
    3: [r'pay', r'payshanba', r'thu', r'thursday'],
    4: [r'ju', r'juma', r'jum', r'fri', r'friday'],
    5: [r'sha', r'shanba', r'shan', r'sat', r'saturday'],
    6: [r'yak', r'yakshanba', r'ya', r'sun', r'sunday'],
}

def parse_schedule_weekdays(days_text: str) -> set:
    """
    Berilgan dars kunlari matnidan (masalan, 'Dushanba, Chorshanba, Juma' yoki 'Du, Chor, Ju')
    mos keladigan hafta kuni indekslarini (0=Du, 1=Se, 2=Chor, 3=Pay, 4=Ju, 5=Sha, 6=Yak) to'plam sifatida qaytaradi.
    """
    if not days_text:
        return {0, 1, 2, 3, 4, 5, 6}
    
    text = str(days_text).lower().strip()
    
    if any(k in text for k in ['har kun', 'barcha', 'every', 'daily', 'har kuni']):
        return {0, 1, 2, 3, 4, 5, 6}
    if 'toq' in text:
        return {0, 2, 4} # Dushanba, Chorshanba, Juma
    if 'juft' in text:
        return {1, 3, 5} # Seshanba, Payshanba, Shanba

    found_weekdays = set()
    # Tokenlarga bo'lish
    tokens = re.split(r'[,;\s/|+-]+', text)
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        for w_idx, patterns in WEEKDAY_PATTERNS.items():
            for pat in patterns:
                if token == pat or token.startswith(pat):
                    found_weekdays.add(w_idx)
                    break

    # Agar tokenlar bo'yicha topilmagan bo'lsa, to'liq matndan qidirish
    if not found_weekdays:
        for w_idx, patterns in WEEKDAY_PATTERNS.items():
            for pat in patterns:
                if re.search(r'\b' + pat, text):
                    found_weekdays.add(w_idx)
                    break
                    
    return found_weekdays if found_weekdays else {0, 1, 2, 3, 4, 5, 6}


def is_timeslot_lesson_day(timeslot_days: str, target_date: date) -> bool:
    """
    Berilgan sana (target_date) timeslot dars kunlariga to'g'ri keladimi?
    """
    weekdays = parse_schedule_weekdays(timeslot_days)
    return target_date.weekday() in weekdays
