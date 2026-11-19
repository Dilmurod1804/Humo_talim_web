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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    requests = None
    HTTPAdapter = None
    Retry = None

# Standart ulanish va o'qish timeouti: (connect_timeout, read_timeout)
# Gunicorn worker abort bo'lishining oldini olish uchun connect timeout 5s, read 10s qilib belgilangan
DEFAULT_TIMEOUT = getattr(settings, 'ESKIZ_TIMEOUT', (5, 10))

# Eskiz.uz API doimiylari
ESKIZ_AUTH_URL = "https://notify.eskiz.uz/api/auth/login"
ESKIZ_USER_URL = "https://notify.eskiz.uz/api/auth/user"
ESKIZ_SEND_URL = "https://notify.eskiz.uz/api/message/sms/send"
ESKIZ_CACHE_KEY = "eskiz_sms_bearer_token"


def create_eskiz_session(pool_size: int = 15) -> "requests.Session | None":
    """
    Eskiz API uchun ulanishlar puli (Keep-Alive) va qayta urinish (Retry) bilan
    requests.Session obyektini yaratadi.
    """
    if requests is None:
        return None
    session = requests.Session()
    if HTTPAdapter and Retry:
        try:
            adapter = HTTPAdapter(
                pool_connections=pool_size,
                pool_maxsize=pool_size,
                max_retries=Retry(total=1, backoff_factor=0.2, status_forcelist=[502, 503, 504]),
            )
            session.mount('https://', adapter)
            session.mount('http://', adapter)
        except Exception as exc:
            logger.warning("Eskiz session HTTPAdapter sozlashda ogohlantirish: %s", exc)
    return session


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

def authenticate_eskiz(
    email: str | None = None,
    password: str | None = None,
    session: "requests.Session | None" = None,
    timeout: tuple[int, int] | int | None = None,
) -> str | None:
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

    client = session if session is not None else requests
    use_timeout = timeout or DEFAULT_TIMEOUT

    try:
        resp = client.post(
            ESKIZ_AUTH_URL,
            data={'email': req_email, 'password': req_pass},
            timeout=use_timeout,
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
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.Timeout) as exc:
        logger.warning("Eskiz avtorizatsiyasida ulanish vaqti tugadi (timeout): %s", exc)
    except requests.exceptions.RequestException as exc:
        logger.warning("Eskiz avtorizatsiyasida tarmoq xatosi: %s", exc)
    except Exception as exc:
        logger.exception("Eskiz avtorizatsiyasida kutilmagan ulanish xatosi: %s", exc)

    return None


def get_bearer_token(
    force_refresh: bool = False,
    email: str | None = None,
    password: str | None = None,
    session: "requests.Session | None" = None,
) -> str | None:
    """
    Eskiz uchun Bearer tokenni qaytaradi:
      1. force_refresh=True bo'lsa yangi token oladi
      2. Keshdagi token
      3. settings.ESKIZ_API_TOKEN (.env dagi tayyor token)
      4. authenticate_eskiz() orqali yangi olish
    """
    if force_refresh:
        cache.delete(ESKIZ_CACHE_KEY)
        return authenticate_eskiz(email=email, password=password, session=session)

    cached_token = cache.get(ESKIZ_CACHE_KEY)
    if cached_token:
        return cached_token

    static_token = getattr(settings, 'ESKIZ_API_TOKEN', '').strip()
    if static_token:
        return static_token

    return authenticate_eskiz(email=email, password=password, session=session)


# Aliaslar
get_eskiz_token = get_bearer_token
login_eskiz = authenticate_eskiz


# ─────────────────────────────────────────────────────────────
# 2. ESKIZ.UZ ORQALI SMS YUBORISH
# ─────────────────────────────────────────────────────────────

def send_eskiz_sms(
    phone: str,
    message: str,
    token: str | None = None,
    sender: str | None = None,
    session: "requests.Session | None" = None,
    timeout: tuple[int, int] | int | None = None,
) -> dict:
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

    active_token = token or get_bearer_token(session=session)
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
    use_timeout = timeout or DEFAULT_TIMEOUT
    client = session if session is not None else requests

    try:
        resp = client.post(ESKIZ_SEND_URL, data=payload, headers=headers, timeout=use_timeout)

        # Token eskirgan bo'lsa (401 Unauthorized), yangilab qayta urinish
        if resp.status_code == 401:
            logger.warning("Eskiz 401 Unauthorized qaytardi. Token yangilanmoqda...")
            refreshed_token = get_bearer_token(force_refresh=True, session=session)
            if refreshed_token:
                headers['Authorization'] = f'Bearer {refreshed_token}'
                resp = client.post(ESKIZ_SEND_URL, data=payload, headers=headers, timeout=use_timeout)

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
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.Timeout) as exc:
        logger.warning("Eskiz.uz ga SMS yuborishda ulanish vaqti tugadi (Timeout +%s): %s", cleaned_phone, exc)
        return {
            'success': False,
            'status': 'failed',
            'message_id': '',
            'error': f"Eskiz.uz ulanish vaqti tugadi (Timeout: {exc})",
            'response': {},
        }
    except requests.exceptions.RequestException as exc:
        logger.warning("Eskiz.uz ga SMS yuborishda tarmoq xatosi (+%s): %s", cleaned_phone, exc)
        return {
            'success': False,
            'status': 'failed',
            'message_id': '',
            'error': f"Eskiz.uz tarmoq xatosi ({type(exc).__name__}): {exc}",
            'response': {},
        }
    except Exception as exc:
        logger.exception("Eskiz.uz ga SMS yuborishda kutilmagan xatolik: %s", exc)
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
    4. requests.Session() ulanish havzasi (connection pooling) va ThreadPoolExecutor orqali
       Gunicorn 30-soniyalik timeoutidan oshib ketmasligi kafolatlanadi.
    
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

    # 1. Bitta so'rov bilan avval yuborilgan o'quvchilarni olish (N+1 query muammosini oldini olish)
    already_sent_student_ids = set()
    if not force and paid_students:
        already_sent_student_ids = set(
            SMSLog.objects.filter(
                student_id__in=[s.id for s in paid_students],
                month=month_str,
                status__in=['sent', 'mock'],
            ).values_list('student_id', flat=True)
        )

    # 2. Saralash va filtrlash
    items_to_send = []
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

        if not force and student.id in already_sent_student_ids:
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

        items_to_send.append((student, phone_to_use, msg_text))

    if dry_run or not items_to_send:
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

    # 3. Parallel va Session orqali jo'natish (Gunicorn timeout va ulanishni optimallashtirish)
    session = create_eskiz_session(pool_size=15)
    logs_to_create = []
    max_time_seconds = getattr(settings, 'SMS_MAX_EXECUTION_SECONDS', 22.0)
    start_time = time.time()

    try:
        active_token = get_bearer_token(session=session)
        num_workers = min(8, len(items_to_send))

        def _send_worker(item):
            std, ph, msg = item
            res = send_eskiz_sms(
                ph,
                msg,
                token=active_token,
                session=session,
                timeout=DEFAULT_TIMEOUT,
            )
            return std, ph, msg, res

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_item = {executor.submit(_send_worker, it): it for it in items_to_send}

            for future in as_completed(future_to_item):
                time_elapsed = time.time() - start_time
                item = future_to_item[future]
                std, ph, msg = item

                # Gunicorn timeout himoyasi: vaqt tugasa qolgan vazifalarni bekor qilib tezda javob qaytarish
                if time_elapsed > max_time_seconds and not future.done():
                    future.cancel()
                    failed_count += 1
                    details.append({
                        'student_id': std.id,
                        'name': f"{std.first_name} {std.last_name}",
                        'group': std.time_slot.group.name if std.time_slot and std.time_slot.group else "—",
                        'phone': ph,
                        'status': 'timeout_deferred',
                        'error': f"Gunicorn timeout himoyasi ({max_time_seconds}s chegara): keyinroq qayta yuboring",
                    })
                    continue

                try:
                    std, ph, msg, res = future.result()
                    log_status = res.get('status', 'failed')
                    err_msg = res.get('error', '')
                    msg_id = res.get('message_id', '')

                    log_payload = {
                        'message_id': msg_id,
                        'error': err_msg,
                        'response': res.get('response', {}),
                    }
                    resp_text = json.dumps(log_payload, ensure_ascii=False, default=str)

                    logs_to_create.append(
                        SMSLog(
                            student=std,
                            phone=clean_phone_number(ph),
                            message=msg,
                            month=month_str,
                            provider='eskiz',
                            status=log_status,
                            response_data=resp_text,
                            sent_by=sent_by,
                        )
                    )

                    details.append({
                        'student_id': std.id,
                        'name': f"{std.first_name} {std.last_name}",
                        'group': std.time_slot.group.name if std.time_slot and std.time_slot.group else "—",
                        'phone': ph,
                        'status': log_status,
                        'error': err_msg,
                        'message_id': msg_id,
                    })

                    if log_status in ('sent', 'mock'):
                        sent_count += 1
                    elif log_status == 'skipped':
                        skipped_count += 1
                    else:
                        failed_count += 1

                except Exception as exc:
                    logger.exception("SMS yuborishda worker xatoligi (student=%s): %s", getattr(std, 'id', None), exc)
                    failed_count += 1
                    details.append({
                        'student_id': std.id,
                        'name': f"{std.first_name} {std.last_name}",
                        'group': std.time_slot.group.name if std.time_slot and std.time_slot.group else "—",
                        'phone': ph,
                        'status': 'failed',
                        'error': str(exc),
                    })

        # Asosiy oqimda (main thread) barcha loglarni yozish (DB lock xatolarini oldini olish)
        if logs_to_create:
            SMSLog.objects.bulk_create(logs_to_create)

    except Exception as exc:
        logger.exception("send_payment_reminders_to_paid_students xatoligi: %s", exc)
    finally:
        try:
            session.close()
        except Exception:
            pass

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

        res = send_eskiz_sms(cleaned_phone, msg_text, timeout=DEFAULT_TIMEOUT)
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

