"""
HUMO O'quv markazi — SMS Bildirishnomalar va To'lov Eslatmalari Xizmati (sms_service.py)
Eskiz.uz va umumiy HTTP SMS shlyuzlari (Gateway) orqali ota-onalarga oylik to'lov eslatmalarini yuborish.
"""
import json
import logging
import re
from datetime import date, datetime
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    requests = None


# ─────────────────────────────────────────────────────────────
# 1. TELEFON RAQAMINI VA OY NOMLARINI STANDARTLASHTIRISH
# ─────────────────────────────────────────────────────────────

UZBEK_MONTHS = {
    1: 'Yanvar',
    2: 'Fevral',
    3: 'Mart',
    4: 'Aprel',
    5: 'May',
    6: 'Iyun',
    7: 'Iyul',
    8: 'Avgust',
    9: 'Sentabr',
    10: 'Oktabr',
    11: 'Noyabr',
    12: 'Dekabr',
}


def clean_phone_number(phone_raw: str | None) -> str:
    """
    Telefon raqamini faqat raqamlardan iborat 12 xonali (998XXXXXXXXX)
    Eskiz va SMS shlyuzlar talab qiladigan xalqaro formatga keltiradi.
    """
    if not phone_raw:
        return ''
    digits = re.sub(r'\D', '', str(phone_raw))
    if len(digits) == 9:
        return '998' + digits
    if len(digits) == 12 and digits.startswith('998'):
        return digits
    return digits


def get_month_name_uz(month_num: int) -> str:
    """Oy raqamiga (1..12) qarab o'zbekcha oy nomini qaytaradi."""
    return UZBEK_MONTHS.get(int(month_num), f"{month_num:02d}")


def format_money_str(amount: int | float | str | None) -> str:
    """Pul miqdorini probel bilan ajratilgan chiroyli formatga o'tkazadi (masalan: 350 000)."""
    try:
        val = int(float(str(amount or 0).replace(' ', '')))
        return f"{val:,}".replace(',', ' ')
    except (ValueError, TypeError):
        return str(amount or '0')


# ─────────────────────────────────────────────────────────────
# 2. ESKIZ.UZ TOKEN VA AVTORIZATSIYA
# ─────────────────────────────────────────────────────────────

ESKIZ_AUTH_URL = "https://notify.eskiz.uz/api/auth/login"
ESKIZ_USER_URL = "https://notify.eskiz.uz/api/auth/user"
ESKIZ_SEND_URL = "https://notify.eskiz.uz/api/message/sms/send"
ESKIZ_CACHE_KEY = "eskiz_sms_auth_token"


def get_token_expiry(token: str | None) -> datetime | None:
    """
    JWT token ichidagi `exp` maydonini o'qib, amal qilish muddatini qaytaradi.
    (Imzo tekshirilmaydi — faqat ma'lumot uchun.)
    """
    if not token or token.count('.') != 2:
        return None
    try:
        import base64
        payload_b64 = token.split('.')[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get('exp')
        if exp:
            return datetime.fromtimestamp(int(exp), tz=timezone.get_current_timezone())
    except Exception:
        return None
    return None


def _login_eskiz() -> str | None:
    """ESKIZ_EMAIL / ESKIZ_PASSWORD orqali yangi token oladi va keshlaydi."""
    email = getattr(settings, 'ESKIZ_EMAIL', '').strip()
    password = getattr(settings, 'ESKIZ_PASSWORD', '').strip()
    if not email or not password:
        return None

    if requests is None:
        logger.error("'requests' kutubxonasi o'rnatilmagan.")
        return None

    try:
        resp = requests.post(
            ESKIZ_AUTH_URL,
            data={'email': email, 'password': password},
            timeout=10,
        )
        if resp.status_code == 200:
            res_json = resp.json()
            token = res_json.get('data', {}).get('token')
            if token:
                # Eskiz tokeni ~30 kun amal qiladi, biz uni 23 soatga keshlaymiz
                cache.set(ESKIZ_CACHE_KEY, token, timeout=23 * 3600)
                return token
            logger.error("Eskiz auth javobida token topilmadi: %s", res_json)
        else:
            logger.error("Eskiz auth xatosi [%s]: %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.exception("Eskiz avtorizatsiyasida ulanish xatoligi: %s", exc)

    return None


def get_eskiz_auth_token(force_refresh: bool = False) -> str | None:
    """
    Eskiz.uz uchun Bearer tokenni qaytaradi. Ustuvorlik tartibi:
      1. Keshdagi (avval login orqali yangilangan) token
      2. settings.ESKIZ_API_TOKEN (.env dagi tayyor token)
      3. ESKIZ_EMAIL / ESKIZ_PASSWORD orqali login

    force_refresh=True bo'lsa (masalan, 401 javobidan keyin) kesh va statik token
    chetlab o'tilib, faqat login orqali yangi token olinadi.
    """
    if force_refresh:
        cache.delete(ESKIZ_CACHE_KEY)
        return _login_eskiz()

    cached_token = cache.get(ESKIZ_CACHE_KEY)
    if cached_token:
        return cached_token

    static_token = getattr(settings, 'ESKIZ_API_TOKEN', '').strip()
    if static_token:
        return static_token

    token = _login_eskiz()
    if not token:
        logger.warning("ESKIZ_API_TOKEN yoki ESKIZ_EMAIL/ESKIZ_PASSWORD sozlamalari ko'rsatilmagan.")
    return token


def check_eskiz_token() -> dict:
    """
    Tokenni SMS yubormasdan tekshiradi (GET /api/auth/user).
    Qaytaradi: {'ok': bool, 'status_code': int, 'expires_at': datetime|None, 'data': dict, 'error': str}
    """
    token = get_eskiz_auth_token()
    result = {'ok': False, 'status_code': 0, 'expires_at': get_token_expiry(token), 'data': {}, 'error': ''}
    if not token:
        result['error'] = "Token topilmadi (.env faylida ESKIZ_API_TOKEN ni kiriting)."
        return result
    if requests is None:
        result['error'] = "'requests' kutubxonasi o'rnatilmagan."
        return result
    try:
        resp = requests.get(ESKIZ_USER_URL, headers={'Authorization': f'Bearer {token}'}, timeout=10)
        result['status_code'] = resp.status_code
        try:
            result['data'] = resp.json()
        except ValueError:
            result['data'] = {'raw': resp.text}
        result['ok'] = resp.status_code == 200
        if not result['ok']:
            result['error'] = str(result['data'].get('message') or resp.text)
    except Exception as exc:
        result['error'] = str(exc)
    return result


# ─────────────────────────────────────────────────────────────
# 3. ASOSIY QAYTA ISHLATILUVCHI SEND_SMS FUNKSIYASI
# ─────────────────────────────────────────────────────────────

def send_sms(phone: str, message: str, provider: str | None = None) -> dict:
    """
    Tashqi SMS shlyuz (Eskiz.uz yoki Generic HTTP Gateway) orqali SMS yuboruvchi asosiy funksiya.
    
    Qaytaradi:
      {
          'success': bool,
          'status': 'sent' | 'failed' | 'mock',
          'message_id': str,
          'error': str,
          'response': dict | str
      }
    """
    cleaned_phone = clean_phone_number(phone)
    if not cleaned_phone:
        return {
            'success': False,
            'status': 'failed',
            'message_id': '',
            'error': "Telefon raqami bo'sh yoki yaroqsiz formatda.",
            'response': {},
        }

    sms_enabled = getattr(settings, 'SMS_ENABLED', False)
    provider_name = provider or getattr(settings, 'SMS_PROVIDER', 'eskiz')

    # Agar SMS yuborish o'chirilgan bo'lsa yoki console rejimi bo'lsa — simulyatsiya (mock) qilamiz
    if not sms_enabled or provider_name == 'console':
        logger.info(
            "[SMS SIMULATION / MOCK] Telefon: +%s | Xabar: %s",
            cleaned_phone, message
        )
        return {
            'success': True,
            'status': 'mock',
            'message_id': f'mock_{timezone.now().strftime("%Y%m%d%H%M%S")}',
            'error': '',
            'response': {'mock': True, 'recipient': cleaned_phone, 'chars': len(message)},
        }

    # Eskiz.uz orqali yuborish
    if provider_name == 'eskiz':
        if requests is None:
            return {
                'success': False,
                'status': 'failed',
                'message_id': '',
                'error': "'requests' kutubxonasi o'rnatilmagan (pip install requests).",
                'response': {},
            }

        token = get_eskiz_auth_token()
        if not token:
            return {
                'success': False,
                'status': 'failed',
                'message_id': '',
                'error': "Eskiz.uz tokeni topilmadi (.env da ESKIZ_API_TOKEN ni kiriting).",
                'response': {},
            }

        sender_from = getattr(settings, 'ESKIZ_FROM', '4546')
        headers = {
            'Authorization': f'Bearer {token}',
        }
        data = {
            'mobile_phone': cleaned_phone,
            'message': message,
            'from': sender_from,
        }

        def _parse(resp):
            try:
                return resp.json()
            except ValueError:
                return {'raw': resp.text}

        try:
            resp = requests.post(ESKIZ_SEND_URL, headers=headers, data=data, timeout=12)
            res_json = _parse(resp)

            # Token eskirgan bo'lsa (401) — login orqali yangilab, bir marta qayta urinamiz
            if resp.status_code == 401:
                new_token = get_eskiz_auth_token(force_refresh=True)
                if not new_token:
                    return {
                        'success': False,
                        'status': 'failed',
                        'message_id': '',
                        'error': (
                            "Eskiz tokeni yaroqsiz yoki muddati tugagan (401). "
                            "Yangi token oling yoki .env ga ESKIZ_EMAIL/ESKIZ_PASSWORD kiriting."
                        ),
                        'response': res_json,
                    }
                headers['Authorization'] = f'Bearer {new_token}'
                resp = requests.post(ESKIZ_SEND_URL, headers=headers, data=data, timeout=12)
                res_json = _parse(resp)

            if resp.status_code in (200, 201) and res_json.get('status') in ('waiting', 'success', 'success_send'):
                return {
                    'success': True,
                    'status': 'sent',
                    'message_id': str(res_json.get('id', '')),
                    'error': '',
                    'response': res_json,
                }
            else:
                err_msg = res_json.get('message') or f"HTTP {resp.status_code}: {resp.text}"
                return {
                    'success': False,
                    'status': 'failed',
                    'message_id': '',
                    'error': str(err_msg),
                    'response': res_json,
                }
        except Exception as exc:
            logger.exception("Eskiz.uz ga SMS yuborishda ulanish xatosi: %s", exc)
            return {
                'success': False,
                'status': 'failed',
                'message_id': '',
                'error': str(exc),
                'response': {},
            }

    # Boshqa umumiy HTTP Gateway (agar kerak bo'lsa)
    return {
        'success': False,
        'status': 'failed',
        'message_id': '',
        'error': f"Noma'lum SMS provayder: {provider_name}",
        'response': {},
    }


# ─────────────────────────────────────────────────────────────
# 4. TO'LOV ESLATMASI MATNINI SHAKLLANTIRISH
# ─────────────────────────────────────────────────────────────

def build_tuition_reminder_text(
    student,
    month_str: str | None = None,
    center_name: str | None = None,
    contact_phone: str | None = None,
) -> str:
    """
    Ota-onalar uchun professional, xushmuomala o'zbekcha (lotin) to'lov eslatmasi matnini tuzadi.
    """
    if not month_str:
        month_str = timezone.now().strftime('%Y-%m')

    try:
        y, m = map(int, month_str.split('-'))
        month_name = get_month_name_uz(m)
    except Exception:
        month_name = month_str

    student_name = f"{student.first_name} {student.last_name}".strip()

    group_name = ""
    if getattr(student, 'time_slot', None) and getattr(student.time_slot, 'group', None):
        grp = student.time_slot.group
        group_name = grp.subject_name or grp.name

    group_part = f"{group_name} kursi bo'yicha " if group_name else ""
    rate_val = getattr(student, 'payment_rate', 0)
    rate_formatted = format_money_str(rate_val)

    center = center_name or getattr(settings, 'CENTER_NAME', "Humo Ta'lim o'quv markazi")
    phone = contact_phone or getattr(settings, 'CENTER_PHONE', '+998 90 123 45 67')

    # Faqat GSM-7 (lotin) belgilar ishlatiladi — SMS bo'laklari soni kamroq bo'ladi
    msg = (
        f"Hurmatli ota-ona! Farzandingiz {student_name}ning {group_part}"
        f"{month_name} oyi uchun to'lov muddati keldi. "
        f"Oylik to'lov: {rate_formatted} so'm. "
        f"Iltimos, to'lovni o'z vaqtida amalga oshiring. "
        f"{center}. Tel: {phone}"
    )
    return msg


# ─────────────────────────────────────────────────────────────
# 5. BITTA O'QUVCHIGA ESLATMA YUBORISH VA LOGGA YOZISH
# ─────────────────────────────────────────────────────────────

def send_tuition_reminder(
    student,
    month_str: str | None = None,
    sent_by=None,
    force: bool = False,
    custom_message: str | None = None,
) -> dict:
    """
    Bitta o'quvchining ota-onasiga to'lov eslatmasi yuboradi va SMSLog ga qayd etadi.
    Agar shu oyda allaqachon eslatma yuborilgan bo'lsa (force=False bo'lsa) takroriy yuborilmaydi.
    """
    from core.models import SMSLog

    if not month_str:
        month_str = timezone.now().strftime('%Y-%m')

    # Allaqachon yuborilganligini tekshirish (spamdan himoya)
    if not force:
        already_sent = SMSLog.objects.filter(
            student=student,
            month=month_str,
            status__in=['sent', 'mock'],
        ).exists()
        if already_sent:
            return {
                'success': True,
                'status': 'skipped',
                'message': f"{student.first_name} {student.last_name} uchun {month_str} oyida allaqachon eslatma yuborilgan.",
                'student_id': student.id,
            }

    phone_to_use = getattr(student, 'phone_1', None) or getattr(student, 'phone_2', None)
    if not phone_to_use:
        return {
            'success': False,
            'status': 'no_phone',
            'message': f"{student.first_name} {student.last_name} da telefon raqami kiritilmagan.",
            'student_id': student.id,
        }

    message_text = custom_message or build_tuition_reminder_text(student, month_str=month_str)
    provider_name = getattr(settings, 'SMS_PROVIDER', 'eskiz')

    result = send_sms(phone_to_use, message_text, provider=provider_name)

    # SMSLog modeliga saqlash
    log_status = result['status']  # 'sent', 'failed', 'mock'
    log_payload = {
        'message_id': result.get('message_id', ''),
        'error': result.get('error', ''),
        'response': result.get('response', {}),
    }
    resp_text = json.dumps(log_payload, ensure_ascii=False, default=str)

    sms_log = SMSLog.objects.create(
        student=student,
        phone=clean_phone_number(phone_to_use),
        message=message_text,
        month=month_str,
        provider=provider_name,
        status=log_status,
        response_data=resp_text,
        sent_by=sent_by,
    )

    return {
        'success': result['success'],
        'status': log_status,
        'message_id': result.get('message_id', ''),
        'error': result.get('error', ''),
        'log_id': sms_log.id,
        'student_id': student.id,
        'phone': phone_to_use,
    }


# ─────────────────────────────────────────────────────────────
# 6. GURUH YOKI MARKAZ BO'YICHA QARZDORLARGA PACKET (BATCH) YUBORISH
# ─────────────────────────────────────────────────────────────

def send_tuition_reminders_batch(
    month_str: str | None = None,
    group_id: int | str | None = None,
    sent_by=None,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Tanlangan oy (`month_str`) bo'yicha to'lov qilmagan (qarzdor) barcha faol o'quvchilarga
    ota-onalariga eslatma SMS yuboradi.
    
    Qaytaradi:
      {
          'month': str,
          'total_unpaid': int,
          'sent': int,
          'failed': int,
          'skipped': int,
          'dry_run': bool,
          'details': list,
      }
    """
    from core.models import Student
    from core.payment_views import filter_students_by_payment_status, get_active_students_qs

    if not month_str:
        month_str = timezone.now().strftime('%Y-%m')

    # Faol o'quvchilar
    students_qs = get_active_students_qs()
    if group_id and str(group_id).isdigit():
        students_qs = students_qs.filter(time_slot__group_id=int(group_id))

    # Mavjud to'lov logikasi orqali aynan to'lamagan o'quvchilarni ajratib olamiz
    unpaid_students = filter_students_by_payment_status(students_qs, month_str, status_filter='unpaid')
    unpaid_students = unpaid_students.select_related('time_slot__group').order_by('time_slot__group__name', 'first_name', 'last_name')

    total_unpaid = unpaid_students.count()
    sent_count = 0
    failed_count = 0
    skipped_count = 0
    details = []

    for st in unpaid_students:
        if dry_run:
            phone = st.phone_1 or st.phone_2 or "Yo'q"
            preview_msg = build_tuition_reminder_text(st, month_str=month_str)
            details.append({
                'student_id': st.id,
                'name': f"{st.first_name} {st.last_name}",
                'group': st.time_slot.group.name if st.time_slot and st.time_slot.group else "—",
                'phone': phone,
                'status': 'dry_run',
                'message': preview_msg,
            })
            continue

        res = send_tuition_reminder(st, month_str=month_str, sent_by=sent_by, force=force)
        st_res = {
            'student_id': st.id,
            'name': f"{st.first_name} {st.last_name}",
            'group': st.time_slot.group.name if st.time_slot and st.time_slot.group else "—",
            'phone': res.get('phone', st.phone_1 or st.phone_2 or ''),
            'status': res.get('status'),
            'error': res.get('error', ''),
        }
        details.append(st_res)

        if res.get('status') in ('sent', 'mock'):
            sent_count += 1
        elif res.get('status') == 'skipped':
            skipped_count += 1
        else:
            failed_count += 1

    return {
        'month': month_str,
        'total_unpaid': total_unpaid,
        'sent': sent_count,
        'failed': failed_count,
        'skipped': skipped_count,
        'dry_run': dry_run,
        'details': details,
    }

