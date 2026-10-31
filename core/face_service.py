"""
HUMO O'quv markazi — Yengil (Pure-Python) Face ID va Davomat Xizmatlari
- Serverda hech qanday og'ir kutubxona (dlib, face_recognition, DeepFace, OpenCV) ISHLATILMAYDI.
- Yuzni aniqlash va 128-o'lchamli descriptor ajratib olish Frontend'da (face-api.js) bajariladi.
- Backend faqat kelgan 128-D descriptor massivini bazadagi xodimlarning descriptor'lari bilan
  oddiy matematik Evklid masofasi (Euclidean distance) orqali solishtiradi va davomatni yozadi.
"""
import base64
import io
import json
import math
import re
import uuid
from datetime import datetime, time
from decimal import Decimal

from PIL import Image, ImageOps
from django.core.files.base import ContentFile
from django.utils import timezone

from .models import (
    AdvanceRequest, AuditLog, CustomUser, FaceIDLog,
    FaceIDSettings, Manager, StaffBonus, Teacher,
)

# face-api.js 128-D descriptor uchun standart Evklid masofasi chegarasi (threshold)
FACE_API_EUCLIDEAN_THRESHOLD = 0.58


# ─────────────────────────────────────────────────────────────
# 1. MATEMATIK MASOFA (EUCLIDEAN DISTANCE) VA DESKRIPTOR SOLISHTIRISH
# ─────────────────────────────────────────────────────────────

def parse_descriptor(descriptor_data) -> list[float] | None:
    """
    Frontend (face-api.js) yuborgan yoki bazada JSON sifatida saqlangan
    128-o'lchamli descriptor ma'lumotini toza float ro'yxatiga (list[float]) o'giradi.
    """
    if not descriptor_data:
        return None
    try:
        if isinstance(descriptor_data, str):
            descriptor_data = json.loads(descriptor_data)
        if isinstance(descriptor_data, dict):
            descriptor_data = list(descriptor_data.values())
        vec = [float(x) for x in descriptor_data]
        if len(vec) != 128:
            return None
        if any(math.isnan(x) or math.isinf(x) for x in vec):
            return None
        return vec
    except Exception:
        return None


def calculate_euclidean_distance(desc1: list[float], desc2: list[float]) -> float:
    """
    Ikki 128-o'lchamli yuz deskriptori orasidagi Evklid masofasini (Euclidean distance)
    sof Python (math.sqrt) yordamida hisoblaydi:
        d = sqrt( sum((a - b)^2) )
    """
    if not desc1 or not desc2 or len(desc1) != len(desc2):
        return 999.0
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(desc1, desc2)))


def compare_embeddings(feat1, feat2, threshold: float = FACE_API_EUCLIDEAN_THRESHOLD) -> tuple[float, bool, float]:
    """
    Ikki 128-o'lchamli deskriptorni Evklid masofasi bo'yicha solishtiradi.
    face-api.js da distance < 0.58 bo'lsa bir xil shaxs hisoblanadi.
    Qaytaradi: (similarity_score: 0..100, is_match: bool, euclidean_distance: float)
    """
    d1 = parse_descriptor(feat1) if not isinstance(feat1, list) else feat1
    d2 = parse_descriptor(feat2) if not isinstance(feat2, list) else feat2
    if not d1 or not d2 or len(d1) != len(d2):
        return 0.0, False, 1.0

    dist = calculate_euclidean_distance(d1, d2)
    is_match = dist <= threshold

    if is_match:
        # dist = 0.0 -> 99.9%, dist = threshold (0.58) -> 65.0%
        ratio = max(0.0, min(1.0, 1.0 - (dist / max(threshold, 0.01))))
        score = round(min(99.9, 65.0 + ratio * 34.9), 1)
    else:
        score = round(max(0.0, min(58.0, (1.0 - min(dist, 1.0)) * 58.0)), 1)

    return score, is_match, round(dist, 4)


def compare_two_faces(ref_descriptor, scan_descriptor) -> tuple[float, bool, float]:
    """Orqaga moslik uchun: ikki deskriptorni solishtiradi."""
    return compare_embeddings(ref_descriptor, scan_descriptor)


def extract_face_embedding(image_or_descriptor, allow_portrait_fallback: bool = False):
    """
    Orqaga moslik uchun yordamchi funksiya:
    Agar 128-D descriptor uzatilgan bo'lsa, uni qaytaradi.
    (Serverda og'ir rasm skanerlash o'chirilgan — barcha skanerlash face-api.js da bajariladi).
    """
    vec = parse_descriptor(image_or_descriptor)
    if vec is not None:
        return vec, (0, 0, 0, 0), 1.0
    return None, None, 0.0


# ─────────────────────────────────────────────────────────────
# 2. GEOLOKATSIYA VA BASE64 SURAT YORDAMCHILARI
# ─────────────────────────────────────────────────────────────

def calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Yer yuzasidagi ikki koordinata orasidagi masofani metrlarda hisoblaydi."""
    if None in (lat1, lon1, lat2, lon2):
        return 0.0
    R = 6371000.0
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    delta_phi = math.radians(float(lat2) - float(lat1))
    delta_lambda = math.radians(float(lon2) - float(lon1))

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(R * c, 1)


def decode_base64_image(base64_str: str):
    """Base64 formatidagi rasmni ('data:image/jpeg;base64,...') ContentFile ga aylantiradi."""
    if not base64_str or not isinstance(base64_str, str):
        return None, None, "Base64 tasvir ma'lumoti bo'sh"
    try:
        cleaned = base64_str.strip()
        if ';base64,' in cleaned:
            cleaned = cleaned.split(';base64,', 1)[1]
        elif ',' in cleaned and cleaned.startswith('data:'):
            cleaned = cleaned.split(',', 1)[1]

        cleaned = re.sub(r'[\r\n\t]', '', cleaned).strip().replace(' ', '+')
        rem = len(cleaned) % 4
        if rem:
            cleaned += '=' * (4 - rem)

        raw_bytes = base64.b64decode(cleaned)
        pil_img = Image.open(io.BytesIO(raw_bytes))
        try:
            pil_img = ImageOps.exif_transpose(pil_img)
        except Exception:
            pass
        pil_rgb = pil_img.convert('RGB')

        out_buf = io.BytesIO()
        pil_rgb.save(out_buf, format='JPEG', quality=88)
        out_buf.seek(0)
        content_file = ContentFile(out_buf.read(), name=f"scan_{uuid.uuid4().hex[:8]}.jpg")
        return content_file, None, None
    except Exception as e:
        return None, None, str(e)


# ─────────────────────────────────────────────────────────────
# 3. XODIM PROFILLARI VA DESKRIPTOR BAZASI BILAN ISHLASH
# ─────────────────────────────────────────────────────────────

def get_user_staff_profile(user_or_staff):
    """CustomUser obyektidan Teacher yoki Manager profilini oladi."""
    if user_or_staff is None:
        return None
    if isinstance(user_or_staff, (Teacher, Manager)):
        return user_or_staff
    if isinstance(user_or_staff, CustomUser):
        try:
            if hasattr(user_or_staff, 'teacher_profile') and user_or_staff.teacher_profile:
                return user_or_staff.teacher_profile
        except Exception:
            pass
        try:
            if hasattr(user_or_staff, 'manager_profile') and user_or_staff.manager_profile:
                return user_or_staff.manager_profile
        except Exception:
            pass
        return (
            Teacher.objects.filter(user=user_or_staff).first()
            or Manager.objects.filter(user=user_or_staff).first()
        )
    return None


def get_user_face_data(user_or_staff):
    """Xodimning profili, rasmi va face_embedding (JSON descriptor) satrini qaytaradi."""
    staff_profile = get_user_staff_profile(user_or_staff)
    if not staff_profile:
        return None, None, ''
    return (
        staff_profile,
        getattr(staff_profile, 'photo', None),
        getattr(staff_profile, 'face_embedding', '') or '',
    )


def save_staff_descriptor(user_or_staff, descriptor_data) -> bool:
    """
    Frontend (face-api.js) yuborgan 128-o'lchamli descriptor massivini
    xodimning `face_embedding` maydoniga JSON formatda saqlaydi.
    """
    staff_obj = get_user_staff_profile(user_or_staff)
    vec = parse_descriptor(descriptor_data)
    if not staff_obj or vec is None:
        return False
    try:
        staff_obj.face_embedding = json.dumps([round(x, 6) for x in vec])
        if getattr(staff_obj, 'pk', None):
            staff_obj.save(update_fields=['face_embedding'])
        return True
    except Exception:
        return False


def sync_staff_face_embedding(user_or_staff, descriptor_data=None) -> bool:
    """Agar descriptor uzatilgan bo'lsa saqlaydi, aks holda mavjud descriptor borligini tekshiradi."""
    if descriptor_data is not None:
        return save_staff_descriptor(user_or_staff, descriptor_data)
    staff_obj = get_user_staff_profile(user_or_staff)
    if not staff_obj:
        return False
    return parse_descriptor(getattr(staff_obj, 'face_embedding', '')) is not None


def get_or_create_face_embedding(user_or_staff, force_refresh: bool = False):
    """Xodimning bazadagi 128-o'lchamli descriptor ro'yxatini qaytaradi."""
    _, _, raw_emb = get_user_face_data(user_or_staff)
    return parse_descriptor(raw_emb)


def sync_all_face_embeddings(force: bool = False) -> dict:
    """Yetishmayotgan Teacher / Manager profillarini get_or_create bilan yaratadi va tayyor descriptorlarni sanaydi."""
    created_profiles = 0
    settings_obj = FaceIDSettings.get_settings()

    for u in CustomUser.objects.filter(is_active=True, role__in=('teacher', 'manager')):
        try:
            model_cls = Teacher if u.role == 'teacher' else Manager
            _, created = model_cls.objects.get_or_create(
                user=u,
                defaults={
                    'first_name': u.first_name or u.username,
                    'last_name': u.last_name or '',
                    'phone': '',
                    'generated_login': u.username,
                    'generated_password': '***',
                    'work_start_time': settings_obj.default_work_start_time,
                    'work_end_time': settings_obj.default_work_end_time,
                    'late_fine_rate': settings_obj.default_late_fine_rate,
                },
            )
            if created:
                created_profiles += 1
        except Exception:
            continue

    teachers_ready = sum(
        1 for t in Teacher.objects.filter(is_active=True)
        if parse_descriptor(t.face_embedding) is not None
    )
    managers_ready = sum(
        1 for m in Manager.objects.filter(is_active=True)
        if parse_descriptor(m.face_embedding) is not None
    )

    return {
        'teachers': teachers_ready,
        'managers': managers_ready,
        'created_profiles': created_profiles,
        'errors': 0,
    }


def match_staff_by_face(scan_source=None, scan_descriptor=None, threshold: float = FACE_API_EUCLIDEAN_THRESHOLD):
    """
    Frontend (face-api.js) dan kelgan 128-o'lchamli `scan_descriptor` massivini
    bazadagi barcha faol xodimlarning (`Teacher` va `Manager`) saqlangan `face_embedding`
    descriptorlari bilan Evklid masofasi (Euclidean distance) bo'yicha solishtiradi.

    Eng yaqin (masofasi eng kichik va threshold dan past) xodimni qaytaradi:
        (role, staff_obj, similarity_score, euclidean_distance) yoki None
    """
    target_vec = parse_descriptor(scan_descriptor if scan_descriptor is not None else scan_source)
    if target_vec is None:
        return None

    candidates = []
    for t in Teacher.objects.filter(is_active=True).select_related('user'):
        if t.face_embedding:
            candidates.append(('teacher', t))
    for m in Manager.objects.filter(is_active=True).select_related('user'):
        if m.face_embedding:
            candidates.append(('manager', m))

    best_match = None
    min_distance = threshold

    for role, staff in candidates:
        staff_vec = parse_descriptor(staff.face_embedding)
        if staff_vec is None:
            continue

        score, is_match, dist = compare_embeddings(staff_vec, target_vec, threshold=threshold)
        if is_match and dist <= min_distance:
            min_distance = dist
            best_match = (role, staff, score, dist)

    return best_match


# ─────────────────────────────────────────────────────────────
# 4. DAVOMATNI YOZISH VA JARIMA HISOBLASH
# ─────────────────────────────────────────────────────────────

def calculate_late_fine_percent(late_minutes: int) -> Decimal:
    """Kechikish jarima foizini hisoblaydi."""
    if late_minutes <= 0:
        return Decimal('0.00')
    if late_minutes <= 5:
        return round(Decimal(str(late_minutes)) * Decimal('0.10'), 2)
    extra_minutes = late_minutes - 5
    return round(Decimal('0.50') + (Decimal(str(extra_minutes)) * Decimal('1.00')), 2)


def process_face_check_in(
    user: CustomUser,
    scan_file=None,
    lat=None,
    lon=None,
    bypass_geo=False,
    pre_verified: bool = True,
    match_score: float = 95.0,
):
    """
    Topilgan xodimning davomatini (FaceIDLog modeliga) avtomatik yozadi va JSON javob qaytaradi:
        {"success": True, "name": "Xodim ismi", "employee_name": "Xodim ismi", ...}
    """
    settings_obj = FaceIDSettings.get_settings()
    now = timezone.localtime()
    today = now.date()

    if isinstance(user, (Teacher, Manager)):
        staff = user
        user = staff.user
    else:
        staff = get_user_staff_profile(user)

    if not staff:
        return {
            'success': False,
            'message': "Yuz topilmadi yoki xodim ro'yxatda yo'q",
        }

    # 1. Geolokatsiya tekshiruvi
    distance = None
    lat_val = lon_val = None
    if lat not in (None, '', 'null', 'undefined') and lon not in (None, '', 'null', 'undefined'):
        try:
            lat_val = float(str(lat).replace(',', '.'))
            lon_val = float(str(lon).replace(',', '.'))
            distance = calculate_haversine_distance(
                lat_val, lon_val,
                float(settings_obj.latitude), float(settings_obj.longitude),
            )
        except Exception:
            distance = None

    if settings_obj.is_geofencing_enabled and not bypass_geo:
        if distance is not None and distance > settings_obj.allowed_radius_meters:
            return {
                'success': False,
                'is_geo_error': True,
                'distance': distance,
                'allowed_radius': settings_obj.allowed_radius_meters,
                'message': f"Siz markaz hududida emassiz! Masofa: {int(distance)}m (Ruxsat: {settings_obj.allowed_radius_meters}m).",
            }

    # 2. Xodim ismi va lavozimi
    employee_name = f"{getattr(staff, 'first_name', '') or ''} {getattr(staff, 'last_name', '') or ''}".strip()
    if not employee_name:
        employee_name = user.get_full_name() or user.username

    role_title = getattr(staff, 'role_title', None) or (
        'Menejer' if getattr(user, 'role', '') == 'manager' else "O'qituvchi"
    )

    # 3. Davomat logini (FaceIDLog) yozish
    log = FaceIDLog.objects.filter(user=user, date=today).first()
    expected_time = getattr(staff, 'work_start_time', None) or time(8, 0)
    work_end_time = getattr(staff, 'work_end_time', None) or time(18, 0)
    action_type = 'kelish'

    if log is None:
        action_type = 'kelish'
        log = FaceIDLog(user=user, date=today, check_in_time=now)

        expected_dt = datetime.combine(today, expected_time)
        expected_dt = timezone.make_aware(expected_dt) if timezone.is_aware(now) else expected_dt
        end_dt = datetime.combine(today, work_end_time)
        end_dt = timezone.make_aware(end_dt) if timezone.is_aware(now) else end_dt

        diff_seconds = (now - expected_dt).total_seconds()
        salary = Decimal(str(getattr(staff, 'monthly_salary', 0) or 0))

        if diff_seconds > 0 and now <= end_dt:
            late_mins = int(diff_seconds // 60) + (1 if (diff_seconds % 60) > 0 else 0)
            log.is_late = True
            log.late_minutes = late_mins
            fine_percent = calculate_late_fine_percent(late_mins)
            log.fine_percent_applied = fine_percent
            log.fine_amount_deducted = round((salary * fine_percent) / Decimal('100.0'), 2)
            log.status = 'late'
        else:
            log.is_late = False
            log.late_minutes = 0
            log.fine_percent_applied = Decimal('0.00')
            log.fine_amount_deducted = Decimal('0.00')
            log.status = 'on_time'

    elif log.check_in_time and not log.check_out_time:
        diff_since_in = (now - log.check_in_time).total_seconds()
        if diff_since_in < 5:
            return {
                'success': True,
                'name': employee_name,
                'employee_name': employee_name,
                'staff_name': employee_name,
                'message': "Xodim keldi deb belgilandi",
                'role': role_title,
                'action_type': 'already_checked_in',
                'is_duplicate': True,
                'check_in_time': log.check_in_time.strftime('%H:%M:%S'),
                'is_late': log.is_late,
                'late_minutes': log.late_minutes,
                'fine_percent': float(log.fine_percent_applied),
                'fine_amount': float(log.fine_amount_deducted),
                'match_score': match_score,
            }
        action_type = 'ketish'
        log.check_out_time = now
    else:
        action_type = 'ketish'
        log.check_out_time = now

    if lat_val is not None:
        log.latitude = lat_val
    if lon_val is not None:
        log.longitude = lon_val
    if distance is not None:
        log.distance_meters = float(distance)

    if scan_file and hasattr(scan_file, 'read'):
        try:
            scan_file.seek(0)
            filename = f"scan_{user.id}_{today}_{uuid.uuid4().hex[:6]}.jpg"
            log.verified_photo.save(filename, scan_file, save=False)
        except Exception:
            pass

    log.save()

    try:
        AuditLog.objects.create(
            action_type='fine' if (action_type == 'kelish' and log.is_late) else 'face_id',
            actor=user,
            actor_name=employee_name,
            title=(
                f"Kechikish qayd etildi: {log.late_minutes} daqiqa"
                if (action_type == 'kelish' and log.is_late)
                else f"Face ID ({'Ketish' if action_type == 'ketish' else 'Kelish'}): {now.strftime('%H:%M:%S')}"
            ),
            details=f"Xodim: {employee_name}. Amal: {action_type}. Aniqlik: {match_score}%.",
        )
    except Exception:
        pass

    status_msg = "Xodim keldi deb belgilandi" if action_type == 'kelish' else "Xodim ketdi deb belgilandi"

    return {
        'success': True,
        'name': employee_name,
        'employee_name': employee_name,
        'staff_name': employee_name,
        'message': status_msg,
        'role': role_title,
        'action_type': action_type,
        'check_in_time': log.check_in_time.strftime('%H:%M:%S') if log.check_in_time else None,
        'check_out_time': log.check_out_time.strftime('%H:%M:%S') if log.check_out_time else None,
        'is_late': log.is_late,
        'late_minutes': log.late_minutes,
        'fine_percent': float(log.fine_percent_applied),
        'fine_amount': float(log.fine_amount_deducted),
        'distance': distance,
        'match_score': match_score,
    }


# ─────────────────────────────────────────────────────────────
# 5. OYLIK MAOSH HISOB-KITOBI
# ─────────────────────────────────────────────────────────────

def calculate_staff_salary_summary(staff, year: int, month: int):
    """Muayyan oy uchun xodimning oylik maosh hisobotini chiqaradi."""
    user = getattr(staff, 'user', staff)
    if hasattr(staff, 'staff_profile') and staff.staff_profile:
        staff = staff.staff_profile

    month_str = f"{year}-{month:02d}"
    logs = FaceIDLog.objects.filter(user=user, date__year=year, date__month=month).order_by('-date')
    base_salary = Decimal(str(getattr(staff, 'monthly_salary', 0) or 0))

    total_days = logs.count()
    late_logs = [l for l in logs if l.is_late]
    total_late_count = len(late_logs)
    total_late_minutes = sum(l.late_minutes for l in late_logs)
    total_fine_percent = sum(l.fine_percent_applied for l in late_logs)
    total_fine_amount = sum(l.fine_amount_deducted for l in late_logs)

    advances_qs = AdvanceRequest.objects.filter(user=user, month=month_str, status='approved')
    total_advance_amount = sum((a.amount for a in advances_qs), Decimal('0.00'))

    bonuses_qs = StaffBonus.objects.filter(user=user, month=month_str)
    total_bonus_amount = sum((b.amount for b in bonuses_qs), Decimal('0.00'))

    net_salary = max(Decimal('0.00'), base_salary - total_fine_amount - total_advance_amount + total_bonus_amount)

    return {
        'staff': staff,
        'user': user,
        'year': year,
        'month': month,
        'month_str': month_str,
        'base_salary': base_salary,
        'total_days': total_days,
        'total_late_count': total_late_count,
        'total_late_minutes': total_late_minutes,
        'total_fine_percent': total_fine_percent,
        'total_fine_amount': total_fine_amount,
        'total_advance_amount': total_advance_amount,
        'advances': list(advances_qs),
        'total_bonus_amount': total_bonus_amount,
        'bonuses': list(bonuses_qs),
        'net_salary': net_salary,
        'logs': logs,
    }
