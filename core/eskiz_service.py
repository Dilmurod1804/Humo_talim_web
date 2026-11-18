"""
HUMO O'quv markazi — Eskiz SMS Xizmati (eskiz_service.py)
Eskiz.uz API orqali to'lovli o'quvchilarga to'lov eslatmalari yuborish xizmati.

Talablar:
- Faqat to'lovli o'quvchilar (student_type='paid', is_active=True) ga SMS yuboriladi.
- Mahalla / Imtiyozli (student_type='mahalla', is_local=True) o'quvchilar mutlaqo chiqarib tashlanadi va ularga SMS yuborilmaydi.
"""
import json
import logging
import re
from datetime import datetime
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    requests = None

# Eskiz.uz API doimiylari
ESKIZ_AUTH_URL = "https://notify.eskiz.uz/api/auth/login"
ESKIZ_USER_URL = "https://notify.eskiz.uz/api/auth/user"
ESKIZ_SEND_URL = "https://notify.eskiz.uz/api/message/sms/send"
ESKIZ_CACHE_KEY = "eskiz_sms_bearer_token"


def clean_phone_number(phone_raw: str | None) -> str:
    """
    Telefon raqamini faqat raqamlardan iborat 12 xonali (998XXXXXXXXX)
    Eskiz talab qiladigan xalqaro formatga keltiradi.
    """
    if not phone_raw:
        return ''
    digits = re.sub(r'\D', '', str(phone_raw))
    if len(digits) == 9:
        return '998' + digits
    if len(digits) == 12 and digits.startswith('998'):
        return digits
    return digits


# ─────────────────────────────────────────────────────────────
# 1. ESKIZ.UZ AVTORIZATSIYA (BEARER TOKEN OLISH)
# ─────────────────────────────────────────────────────────────

def authenticate_eskiz(email: str | None = None, password: str | None = None) -> str | None:
    """
    Eskiz.uz API bilan avtorizatsiyadan o'tib (`https://notify.eskiz.uz/api/auth/login`),
    yangi Bearer tokenni oladi va keshga saqlaydi.
    """
    req_email = (email or getattr(settings, 'ESKIZ_EMAIL', '')).strip()
    req_pass = (password or getattr(settings, 'ESKIZ_PASSWORD', '')).strip()

    if not req_email or not req_pass:
        logger.warning("Eskiz email yoki parol ko'rsatilmagan (ESKIZ_EMAIL, ESKIZ_PASSWORD).")
        return None

    if requests is None:
        logger.error("'requests' kutubxonasi o'rnatilmagan (pip install requests).")
        return None

    try:
        resp = requests.post(
            ESKIZ_AUTH_URL,
            data={'email': req_email, 'password': req_pass},
            timeout=10,
        )
        if resp.status_code == 200:
            res_json = resp.json()
            token = res_json.get('data', {}).get('token')
            if token:
                # Eskiz tokeni ~30 kun amal qiladi, biz uni 23 soatga keshlaymiz
                cache.set(ESKIZ_CACHE_KEY, token, timeout=23 * 3600)
                logger.info("Eskiz Bearer token muvaffaqiyatli olindi va keshlandi.")
                return token
            logger.error("Eskiz auth javobida token topilmadi: %s", res_json)
        else:
            logger.error("Eskiz auth xatosi [%s]: %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.exception("Eskiz avtorizatsiyasida ulanish xatosi: %s", exc)

    return None


def get_bearer_token(force_refresh: bool = False, email: str | None = None, password: str | None = None) -> str | None:
    """
    Eskiz uchun Bearer tokenni qaytaradi:
      1. force_refresh=True bo'lsa yangi token oladi
      2. Keshdagi token
      3. settings.ESKIZ_API_TOKEN (.env dagi tayyor token)
      4. authenticate_eskiz() orqali yangi olish
    """
    if force_refresh:
        cache.delete(ESKIZ_CACHE_KEY)
        return authenticate_eskiz(email=email, password=password)

    cached_token = cache.get(ESKIZ_CACHE_KEY)
    if cached_token:
        return cached_token

    static_token = getattr(settings, 'ESKIZ_API_TOKEN', '').strip()
    if static_token:
        return static_token

    return authenticate_eskiz(email=email, password=password)


# Aliaslar
get_eskiz_token = get_bearer_token
login_eskiz = authenticate_eskiz


# ─────────────────────────────────────────────────────────────
# 2. ESKIZ.UZ ORQALI SMS YUBORISH
# ─────────────────────────────────────────────────────────────

def send_eskiz_sms(phone: str, message: str, token: str | None = None, sender: str | None = None) -> dict:
    """
    Eskiz.uz API (`https://notify.eskiz.uz/api/message/sms/send`) orqali SMS yuboradi.
    
    Qaytaradi:
      {
          'success': bool,
          'status': 'sent' | 'failed' | 'mock',
          'message_id': str,
          'error': str,
          'response': dict
      }
    """
    cleaned_phone = clean_phone_number(phone)
    if not cleaned_phone:
        return {
            'success': False,
            'status': 'failed',
            'message_id': '',
            'error': "Telefon raqami bo'sh yoki noto'g'ri formatda.",
            'response': {},
        }

    sms_enabled = getattr(settings, 'SMS_ENABLED', False)
    provider_name = getattr(settings, 'SMS_PROVIDER', 'eskiz')

    # Agar SMS o'chirilgan bo'lsa yoki test/mock rejimida bo'lsa
    if not sms_enabled or provider_name in ('console', 'mock'):
        logger.info("[ESKIZ MOCK SMS] Telefon: +%s | Xabar: %s", cleaned_phone, message)
        return {
            'success': True,
            'status': 'mock',
            'message_id': f'mock_{timezone.now().strftime("%Y%m%d%H%M%S")}',
            'error': '',
            'response': {'mock': True, 'recipient': cleaned_phone, 'chars': len(message)},
        }

    if requests is None:
        return {
            'success': False,
            'status': 'failed',
            'message_id': '',
            'error': "'requests' kutubxonasi o'rnatilmagan.",
            'response': {},
        }

    active_token = token or get_bearer_token()
    if not active_token:
        return {
            'success': False,
            'status': 'failed',
            'message_id': '',
            'error': "Eskiz.uz Bearer tokeni topilmadi (.env da ESKIZ_API_TOKEN yoki login/parol kiritilmagan).",
            'response': {},
        }

    from_header = sender or getattr(settings, 'ESKIZ_FROM', '4546')
    payload = {
        'mobile_phone': cleaned_phone,
        'message': message,
        'from': from_header,
    }
    headers = {'Authorization': f'Bearer {active_token}'}

    try:
        resp = requests.post(ESKIZ_SEND_URL, data=payload, headers=headers, timeout=12)

        # Token eskirgan bo'lsa (401 Unauthorized), yangilab qayta urinish
        if resp.status_code == 401:
            logger.warning("Eskiz 401 Unauthorized qaytardi. Token yangilanmoqda...")
            refreshed_token = get_bearer_token(force_refresh=True)
            if refreshed_token:
                headers['Authorization'] = f'Bearer {refreshed_token}'
                resp = requests.post(ESKIZ_SEND_URL, data=payload, headers=headers, timeout=12)

        try:
            res_json = resp.json()
        except ValueError:
            res_json = {'raw': resp.text}

        if resp.status_code in (200, 201):
            msg_id = str(res_json.get('id') or res_json.get('data', {}).get('id') or '')
            return {
                'success': True,
                'status': 'sent',
                'message_id': msg_id,
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
        logger.exception("Eskiz.uz ga SMS yuborishda xatolik: %s", exc)
        return {
            'success': False,
            'status': 'failed',
            'message_id': '',
            'error': str(exc),
            'response': {},
        }


# Qulay alias
send_sms = send_eskiz_sms


# ─────────────────────────────────────────────────────────────
# 3. FILTRLANGAN TO'LOV ESLATMALARI: FAQAT TO'LOVLI O'QUVCHILARGA
# ─────────────────────────────────────────────────────────────

def send_payment_reminders_to_paid_students(
    month_str: str | None = None,
    group_id: int | str | None = None,
    dry_run: bool = False,
    force: bool = False,
    sent_by=None,
    custom_message: str | None = None,
    debtors_only: bool = True,
) -> dict:
    """
    QAT'IY FILTRLANGAN TO'LOV ESLATMALARI:
    1. Faqat to'lovli o'quvchilar: Student.objects.filter(student_type='paid', is_active=True, is_deleted=False)
    2. Mahalla / Imtiyozli (student_type='mahalla', is_local=True, is_mahalla=True) o'quvchilar
       MUTLAQO O'TKAZIB YUBORILADI VA HECH QACHON SMS OLMAYDI.
    3. To'lovli o'quvchilarga Eskiz orqali eslatma SMS yuboriladi va SMSLog ga qayd etiladi.
    
    Qaytaradi:
      {
          'month': str,
          'total_paid_students': int,
          'sent': int,
          'failed': int,
          'skipped': int,
          'skipped_mahalla': int,
          'dry_run': bool,
          'details': list,
      }
    """
    from core.models import Student, SMSLog
    from core.sms_service import build_tuition_reminder_text

    if not month_str:
        month_str = timezone.now().strftime('%Y-%m')

    # Faqat to'lovli va faol o'quvchilarni bazadan so'raymiz
    paid_students_qs = Student.objects.filter(
        student_type='paid',
        is_active=True,
        is_deleted=False,
        left_date__isnull=True,
    )

    if group_id and str(group_id).isdigit():
        paid_students_qs = paid_students_qs.filter(time_slot__group_id=int(group_id))

    # Qat'iy qarzdorlar (faqat shu oyda to'lov qilmaganlar) bo'yicha saralash
    if debtors_only:
        try:
            from core.payment_views import get_active_students_qs, _build_payments_data
            base_qs = get_active_students_qs(
                group_id=int(group_id) if group_id and str(group_id).isdigit() else None
            )
            rows, _ = _build_payments_data(
                base_qs,
                month_str,
                status_filter='unpaid',
                is_group_specific=bool(group_id and str(group_id).isdigit()),
            )
            unpaid_student_ids = {r['student_id'] for r in rows}
            paid_students_qs = paid_students_qs.filter(id__in=unpaid_student_ids)
        except Exception as e:
            logger.warning("debtors_only filtrlashda ogohlantirish: %s", e)

    paid_students = list(
        paid_students_qs.select_related('time_slot__group').order_by('first_name', 'last_name')
    )

    total_paid = len(paid_students)
    sent_count = 0
    failed_count = 0
    skipped_count = 0
    skipped_mahalla_count = 0
    details = []

    for student in paid_students:
        # QAT'IY XAVFSIZLIK TEKSHIRUVI: Agar o'quvchi mahalla yoki imtiyozli bo'lsa, mutlaqo o'tkazib yuboriladi
        if (
            student.student_type != 'paid'
            or getattr(student, 'is_local', False)
            or getattr(student, 'is_mahalla', False)
        ):
            skipped_mahalla_count += 1
            details.append({
                'student_id': student.id,
                'name': f"{student.first_name} {student.last_name}",
                'group': student.time_slot.group.name if student.time_slot and student.time_slot.group else "—",
                'phone': student.phone_1 or student.phone_2 or '',
                'status': 'skipped_mahalla',
                'error': "Mahalla / Imtiyozli o'quvchi — SMS yuborilishi taqiqlangan",
            })
            continue

        phone_to_use = student.phone_1 or student.phone_2
        if not phone_to_use:
            failed_count += 1
            details.append({
                'student_id': student.id,
                'name': f"{student.first_name} {student.last_name}",
                'group': student.time_slot.group.name if student.time_slot and student.time_slot.group else "—",
                'phone': "—",
                'status': 'no_phone',
                'error': "Telefon raqami kiritilmagan",
            })
            continue

        # Avval yuborilganlik tekshiruvi (force=False bo'lsa qayta yubormaydi)
        if not force:
            already_sent = SMSLog.objects.filter(
                student=student,
                month=month_str,
                status__in=['sent', 'mock'],
            ).exists()
            if already_sent:
                skipped_count += 1
                details.append({
                    'student_id': student.id,
                    'name': f"{student.first_name} {student.last_name}",
                    'group': student.time_slot.group.name if student.time_slot and student.time_slot.group else "—",
                    'phone': phone_to_use,
                    'status': 'skipped',
                    'error': f"{month_str} oyida allaqachon eslatma yuborilgan",
                })
                continue

        # Xabar matni: agar custom_message bo'lsa har bir o'quvchi uchun placeholderlar to'ldiriladi
        msg_text = build_tuition_reminder_text(student, month_str=month_str, custom_template=custom_message)

        if dry_run:
            details.append({
                'student_id': student.id,
                'name': f"{student.first_name} {student.last_name}",
                'group': student.time_slot.group.name if student.time_slot and student.time_slot.group else "—",
                'phone': phone_to_use,
                'status': 'dry_run',
                'message': msg_text,
            })
            continue

        # Haqiqiy yoki mock SMS yuborish
        res = send_eskiz_sms(phone_to_use, msg_text)
        log_status = res['status']

        # SMSLog ga qayd etish
        log_payload = {
            'message_id': res.get('message_id', ''),
            'error': res.get('error', ''),
            'response': res.get('response', {}),
        }
        resp_text = json.dumps(log_payload, ensure_ascii=False, default=str)

        SMSLog.objects.create(
            student=student,
            phone=clean_phone_number(phone_to_use),
            message=msg_text,
            month=month_str,
            provider='eskiz',
            status=log_status,
            response_data=resp_text,
            sent_by=sent_by,
        )

        details.append({
            'student_id': student.id,
            'name': f"{student.first_name} {student.last_name}",
            'group': student.time_slot.group.name if student.time_slot and student.time_slot.group else "—",
            'phone': phone_to_use,
            'status': log_status,
            'error': res.get('error', ''),
            'message_id': res.get('message_id', ''),
        })

        if log_status in ('sent', 'mock'):
            sent_count += 1
        elif log_status == 'skipped':
            skipped_count += 1
        else:
            failed_count += 1

    return {
        'month': month_str,
        'total_paid_students': total_paid,
        'sent': sent_count,
        'failed': failed_count,
        'skipped': skipped_count,
        'skipped_mahalla': skipped_mahalla_count,
        'dry_run': dry_run,
        'details': details,
    }


# ─────────────────────────────────────────────────────────────
# 4. SINOV (TEST MODE) SMS YUBORISH
# ─────────────────────────────────────────────────────────────

def send_test_payment_reminder(
    student_id: int | str | None = None,
    month_str: str | None = None,
    sent_by=None,
    test_phone: str | None = None,
    custom_message: str | None = None,
    group_id: int | str | None = None,
) -> dict:
    """
    SINOV (TEST) SMS YUBORISH:
    Xabar faqat va faqat .env / settings dagi TEST_SMS_PHONE raqamiga yuboriladi.
    Haqiqiy o'quvchi yoki ota-onalarga mutlaqo yuborilmaydi!
    Xabar ichida o'quvchining shaxsiy to'lov havolasi (https://hummokids.uz/pay/{student.uuid}/) mavjud bo'ladi.
    """
    from core.models import Student, SMSLog
    from core.sms_service import build_tuition_reminder_text

    target_phone = test_phone or getattr(settings, 'TEST_SMS_PHONE', '+998901234567')
    cleaned_phone = clean_phone_number(target_phone)
    if not cleaned_phone:
        return {
            'success': False,
            'status': 'failed',
            'error': f"Noto'g'ri test telefon raqami: {target_phone}",
            'test_phone': target_phone,
        }

    try:
        student = None
        if student_id and str(student_id).isdigit():
            student = Student.objects.filter(id=int(student_id)).first()

        # Agar group_id berilgan bo'lsa, avval o'sha guruhdagi to'lovli o'quvchini olamiz
        if not student and group_id and str(group_id).isdigit():
            student = Student.objects.filter(
                time_slot__group_id=int(group_id),
                student_type='paid',
                is_active=True,
                is_deleted=False,
                left_date__isnull=True,
            ).first()
            if not student:
                student = Student.objects.filter(
                    time_slot__group_id=int(group_id),
                    is_active=True,
                    is_deleted=False,
                ).first()

        if not student:
            student = Student.objects.filter(
                student_type='paid',
                is_active=True,
                is_deleted=False,
                left_date__isnull=True,
            ).first()

        if not student:
            student = Student.objects.filter(is_active=True, is_deleted=False).first()

        if not student:
            return {
                'success': False,
                'status': 'failed',
                'error': "Bazada sinov uchun birorta ham o'quvchi topilmadi.",
                'test_phone': cleaned_phone,
            }

        if not month_str:
            month_str = timezone.now().strftime('%Y-%m')

        msg_text = build_tuition_reminder_text(student, month_str=month_str, custom_template=custom_message)

        res = send_eskiz_sms(cleaned_phone, msg_text)
        log_status = res['status']

        log_payload = {
            'is_test': True,
            'test_phone': cleaned_phone,
            'student_original_phone': student.phone_1 or student.phone_2 or '',
            'message_id': res.get('message_id', ''),
            'error': res.get('error', ''),
            'response': res.get('response', {}),
        }
        resp_text = json.dumps(log_payload, ensure_ascii=False, default=str)

        SMSLog.objects.create(
            student=student,
            phone=cleaned_phone,
            message=f"[TEST SMS] {msg_text}",
            month=month_str,
            provider='eskiz',
            status=log_status,
            response_data=resp_text,
            sent_by=sent_by,
        )

        return {
            'success': res['success'],
            'status': log_status,
            'test_phone': cleaned_phone,
            'student_id': student.id,
            'student_name': f"{student.first_name} {student.last_name}",
            'payment_url': student.payment_url,
            'message': msg_text,
            'message_id': res.get('message_id', ''),
            'error': res.get('error', ''),
        }
    except Exception as exc:
        logger.exception("send_test_payment_reminder xatoligi: %s", exc)
        return {
            'success': False,
            'status': 'failed',
            'error': f"Sinov SMS xatoligi: {str(exc)}",
            'test_phone': cleaned_phone,
        }

