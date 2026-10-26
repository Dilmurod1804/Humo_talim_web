"""
HUMO O'quv markazi — Face ID va Biometrik Davomat Xizmatlari
- Yuzni aniqlash va taqqoslash (OpenCV)
- Geolokatsiya masofasini hisoblash (Haversine formula)
- Kelish/Ketish va kechikish jarimalarini avtomatik qayd etish
- Oylik maosh va jarimalar hisob-kitob kalkulyatori
"""
import io
import math
import os
import uuid
from datetime import date, datetime, time
from decimal import Decimal
try:
    import cv2
    import numpy as np
    HAS_OPENCV = True
except ImportError:
    cv2 = None
    np = None
    HAS_OPENCV = False

from PIL import Image, ImageOps

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from .models import CustomUser, Teacher, Manager, FaceIDSettings, FaceIDLog, StaffBonus, AuditLog, AdvanceRequest


# ─────────────────────────────────────────────────────────────
# 1. GEOLOKATSIYA VA MASOFA HISOBI (Haversine)
# ─────────────────────────────────────────────────────────────

def calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Yer yuzasidagi ikki koordinata orasidagi to'g'ri masofani metrlarda hisoblaydi.
    """
    if None in (lat1, lon1, lat2, lon2):
        return 0.0

    R = 6371000.0  # Yer radiusi (metr)
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    delta_phi = math.radians(float(lat2) - float(lat1))
    delta_lambda = math.radians(float(lon2) - float(lon1))

    a = math.sin(delta_phi / 2.0) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    distance = R * c
    return round(distance, 1)


# ─────────────────────────────────────────────────────────────
# 2. QAT'IY YUZNI QIDIRISH VA TAQQOSLASH (Strict Biometric Recognition)
# ─────────────────────────────────────────────────────────────

def extract_face_roi(cv_img):
    """
    OpenCV Haar Cascade yordamida yuzni aniqlaydi va normallashtirilgan (160x160) ROI qaytaradi.
    DIQQAT: Agar haqiqiy yuz aniqlanmasa, qat'iy None qaytaradi (soxta crop olinmaydi).
    """
    if cv_img is None or not HAS_OPENCV or cv2 is None:
        return None, None

    try:
        cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        face_cascade = cv2.CascadeClassifier(cascade_path)

        gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)

        faces = face_cascade.detectMultiScale(
            gray,
            scaleFactor=1.08,
            minNeighbors=5,
            minSize=(70, 70)
        )

        if len(faces) == 0:
            # Agar asosiy kaskad topolmasa, qo'shimcha sezgir kaskad bilan tekshirish
            alt_path = cv2.data.haarcascades + 'haarcascade_frontalface_alt2.xml'
            if os.path.exists(alt_path):
                alt_cascade = cv2.CascadeClassifier(alt_path)
                faces = alt_cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=4, minSize=(70, 70))

        if len(faces) == 0:
            # Yuz aniqlanmadi — soxta koordinata berilmaydi
            return None, None

        face_box = max(faces, key=lambda rect: rect[2] * rect[3])
        x, y, w, h = face_box

        # Kichik xavfsiz qirqim
        img_h, img_w = gray.shape
        pad_x = int(w * 0.05)
        pad_y = int(h * 0.05)
        x1 = max(0, x - pad_x)
        y1 = max(0, y - pad_y)
        x2 = min(img_w, x + w + pad_x)
        y2 = min(img_h, y + h + pad_y)

        face_crop = gray[y1:y2, x1:x2]
        face_resized = cv2.resize(face_crop, (160, 160), interpolation=cv2.INTER_AREA)
        face_norm = cv2.equalizeHist(face_resized)

        return face_norm, face_box
    except Exception as e:
        print(f"Face ROI extraction error: {e}")
        return None, None


def _load_pil_image(image_source):
    """Fayl yoki baytlardan PIL Image obyektini yuklash"""
    if not image_source:
        return None
    try:
        if isinstance(image_source, (str, os.PathLike)):
            if not os.path.exists(image_source):
                return None
            return Image.open(image_source).convert('L')
        if hasattr(image_source, 'read'):
            image_source.seek(0)
            img = Image.open(io.BytesIO(image_source.read())).convert('L')
            image_source.seek(0)
            return img
    except Exception:
        return None


def _load_cv_image_from_file_or_path(image_source):
    """Fayl, yo'l, baytlar, PIL tasvir yoki numpy massividan OpenCV BGR tasvir yuklaydi."""
    if image_source is None:
        return None
    try:
        if isinstance(image_source, np.ndarray):
            return image_source
        if isinstance(image_source, (str, os.PathLike)):
            p = str(image_source)
            if not os.path.exists(p):
                return None
            data = np.fromfile(p, dtype=np.uint8)
            return cv2.imdecode(data, cv2.IMREAD_COLOR)
        if hasattr(image_source, 'read'):
            image_source.seek(0)
            raw = image_source.read()
            image_source.seek(0)
            data = np.frombuffer(raw, dtype=np.uint8)
            return cv2.imdecode(data, cv2.IMREAD_COLOR)
        if isinstance(image_source, bytes):
            data = np.frombuffer(image_source, dtype=np.uint8)
            return cv2.imdecode(data, cv2.IMREAD_COLOR)
        if hasattr(image_source, 'convert'):
            rgb = image_source.convert('RGB')
            arr = np.array(rgb)
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    except Exception as e:
        print(f"Error loading cv image: {e}")
    return None


def compare_two_faces(ref_img_source, scan_img_source) -> tuple[float, bool, float]:
    """
    Xodimning saqlangan etalon rasmi bilan kamera kadrini qat'iy solishtiradi.
    Tolerance: <= 0.40 (Yuzlar begona bo'lsa yoki o'xshamasa, qat'iy rad etiladi).
    Natija: (similarity_score: 0..100, is_match: bool, face_distance: 0.0..1.0)
    """
    if HAS_OPENCV and cv2 is not None and np is not None:
        try:
            ref_img = _load_cv_image_from_file_or_path(ref_img_source)
            scan_img = _load_cv_image_from_file_or_path(scan_img_source)

            if ref_img is None or scan_img is None:
                return 0.0, False, 1.0

            ref_roi, ref_box = extract_face_roi(ref_img)
            scan_roi, scan_box = extract_face_roi(scan_img)

            # Agar ikkala tasvirda ham aniq yuz topilmasa, darhol rad etiladi!
            if ref_roi is None or scan_roi is None:
                return 0.0, False, 1.0

            # 1. To'liq yuz template matching
            res_full = cv2.matchTemplate(scan_roi, ref_roi, cv2.TM_CCOEFF_NORMED)
            _, max_val_full, _, _ = cv2.minMaxLoc(res_full)
            norm_val_full = max(0.0, float(max_val_full))

            # 2. Yuzning yuqori qismi (ko'z va qoshlar biometrikasi)
            upper_scan = scan_roi[0:90, :]
            upper_ref = ref_roi[0:90, :]
            res_upper = cv2.matchTemplate(upper_scan, upper_ref, cv2.TM_CCOEFF_NORMED)
            _, max_val_upper, _, _ = cv2.minMaxLoc(res_upper)
            norm_val_upper = max(0.0, float(max_val_upper))

            # 3. Yuz markazi (burun va yuz tuzilishi)
            mid_scan = scan_roi[40:130, 20:140]
            mid_ref = ref_roi[40:130, 20:140]
            res_mid = cv2.matchTemplate(mid_scan, mid_ref, cv2.TM_CCOEFF_NORMED)
            _, max_val_mid, _, _ = cv2.minMaxLoc(res_mid)
            norm_val_mid = max(0.0, float(max_val_mid))

            # 4. Gistogramma korrelyatsiyasi (yorug'likka chidamli)
            hist1 = cv2.calcHist([ref_roi], [0], None, [64], [0, 256])
            hist2 = cv2.calcHist([scan_roi], [0], None, [64], [0, 256])
            cv2.normalize(hist1, hist1, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)
            cv2.normalize(hist2, hist2, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)
            hist_score = max(0.0, float(cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL)))

            # 5. O'rtacha mutlaq piksel farqi
            diff = cv2.absdiff(ref_roi, scan_roi)
            mean_diff = float(np.mean(diff))
            diff_score = max(0.0, 1.0 - (mean_diff / 65.0))

            # 6. Umumiy o'xshashlik hisobi
            combined = (
                norm_val_full * 0.35 +
                norm_val_upper * 0.25 +
                norm_val_mid * 0.15 +
                hist_score * 0.10 +
                diff_score * 0.15
            ) * 100.0

            score = round(max(0.0, min(100.0, combined)), 1)
            face_distance = round(max(0.0, min(1.0, 1.0 - (score / 100.0))), 3)

            # QAT'IY XAVFSIZLIK SHARTLARI (Tolerance <= 0.40):
            # Begona shaxslar bu shartdan o'ta olmaydi!
            TOLERANCE_THRESHOLD = 0.40
            is_match = (
                face_distance <= TOLERANCE_THRESHOLD and
                score >= 60.0 and
                norm_val_full >= 0.48
            )

            return score, is_match, face_distance

        except Exception as e:
            print(f"OpenCV face comparison error: {e}")
            return 0.0, False, 1.0

    return 0.0, False, 1.0


def match_staff_by_face(scan_img_source):
    """
    Barcha faol va rasmi bor xodimlarni skanerlangan rasm bilan qat'iy solishtirib,
    faqat qat'iy tekshiruvdan o'tgan (tolerance <= 0.40, score >= 60.0) xodimni aniqlaydi.
    Begona shaxslar yoki kadrda yuz bo'lmaganda darhol None qaytaradi.
    """
    scan_img = _load_cv_image_from_file_or_path(scan_img_source)
    if scan_img is None:
        return None

    # Agar kadrda umuman yuz aniqlanmasa, ortiqcha solishtirishlarsiz darhol chiqish
    scan_roi, _ = extract_face_roi(scan_img)
    if scan_roi is None:
        return None

    staff_candidates = []

    # Menejerlar
    for m in Manager.objects.filter(is_active=True).exclude(photo=''):
        if m.photo and os.path.exists(m.photo.path):
            staff_candidates.append(('manager', m))

    # O'qituvchilar
    for t in Teacher.objects.filter(is_active=True).exclude(photo=''):
        if t.photo and os.path.exists(t.photo.path):
            staff_candidates.append(('teacher', t))

    if not staff_candidates:
        return None

    best_match = None
    best_score = 0.0
    best_distance = 1.0

    for role, staff in staff_candidates:
        score, is_match, dist = compare_two_faces(staff.photo.path, scan_img)
        if is_match and score > best_score and dist <= 0.40:
            best_score = score
            best_distance = dist
            best_match = (role, staff, score, dist)

    return best_match


def calculate_late_fine_percent(late_minutes: int) -> Decimal:
    """
    Kademeli Jarima va Oylikdan Kesish Mantig'i (Graduated Penalty Logic):
    - Har bir o'qituvchi va xodim uchun shaxsiy kelish vaqti bo'ladi (Masalan: 14:00).
    - 0 daqiqa kechikish: 0% jarima.
    - Dastlabki 5 daqiqa (1 dan 5 daqiqagacha): Har bir minut kechikish uchun oylikdan 0.1% dan kesiladi.
      (Masalan: 1 min -> 0.1%, 2 min -> 0.2%, 5 min -> 0.5%)
    - 5-daqiqadan keyin (6-daqiqadan boshlab): Dastlabki 5 daqiqa uchun 0.5% + 5-daqiqadan keyingi
      har bir minut kechikish uchun oylikdan 1.0% dan kesiladi.
      (Masalan: 6 min -> 1.5%, 7 min -> 2.5%, 10 min -> 5.5%, 15 min -> 10.5%)
    """
    if late_minutes <= 0:
        return Decimal('0.00')
    elif late_minutes <= 5:
        return round(Decimal(str(late_minutes)) * Decimal('0.10'), 2)
    else:
        extra_minutes = late_minutes - 5
        return round(Decimal('0.50') + (Decimal(str(extra_minutes)) * Decimal('1.00')), 2)


# ─────────────────────────────────────────────────────────────
# 3. FACE ID DAVOMAT VA KECHIKISH JARIMALARI
# ─────────────────────────────────────────────────────────────

def process_face_check_in(user: CustomUser, scan_file, lat=None, lon=None, bypass_geo=False):
    """
    Face ID orqali kelish yoki ketishni qayd qiladi:
    1. Geolokatsiya masofasini tekshiradi (bino radiusida ekanligini)
    2. Yuz o'xshashligini qat'iy tekshiradi (begona yuzlarni rad etadi)
    3. Shaxsiy kelish vaqti bo'yicha bosqichma-bosqich jarima hisoblaydi
    4. Jarima foizi va summasini hisoblab FaceIDLog va AuditLog ga yozadi
    """
    settings_obj = FaceIDSettings.get_settings()
    now = timezone.localtime()
    today = now.date()

    # Staff profilini aniqlash (Menejer, O'qituvchi, Admin yoki umumiy xodim)
    staff = getattr(user, 'manager_profile', None) or getattr(user, 'teacher_profile', None) or getattr(user, 'staff_profile', None)
    if not staff:
        if getattr(user, 'role', '') == 'manager':
            staff, _ = Manager.objects.get_or_create(
                user=user,
                defaults={
                    'first_name': user.first_name or user.username,
                    'last_name': user.last_name or '',
                    'generated_login': user.username,
                    'generated_password': '***',
                    'work_start_time': settings_obj.default_work_start_time,
                    'work_end_time': settings_obj.default_work_end_time,
                }
            )
        elif getattr(user, 'role', '') == 'admin' or user.is_superuser:
            class AdminStaff:
                first_name = user.first_name or "Admin"
                last_name = user.last_name or ""
                role_title = "Admin"
                photo = None
                monthly_salary = Decimal('0.00')
                work_start_time = settings_obj.default_work_start_time
                work_end_time = settings_obj.default_work_end_time
            staff = AdminStaff()
        else:
            return {
                'success': False,
                'message': "Foydalanuvchiga biriktirilgan xodim (Menejer/O'qituvchi) profili topilmadi."
            }

    # 1. Geolokatsiya (Geofencing) qat'iy tekshiruvi
    distance = None
    lat_val = None
    lon_val = None
    if lat not in (None, '', 'null', 'undefined') and lon not in (None, '', 'null', 'undefined'):
        try:
            lat_val = float(str(lat).replace(',', '.'))
            lon_val = float(str(lon).replace(',', '.'))
            distance = calculate_haversine_distance(
                lat_val, lon_val,
                float(settings_obj.latitude), float(settings_obj.longitude)
            )
        except Exception as e:
            print(f"Geolokatsiya hisoblash xatosi: {e}")
            distance = None

    if settings_obj.is_geofencing_enabled and not bypass_geo:
        # A) Agar GPS koordinatalari olinmagan bo'lsa:
        if distance is None or lat_val is None or lon_val is None:
            return {
                'success': False,
                'is_geo_error': True,
                'error_code': 'GPS_REQUIRED',
                'message': (
                    "Geolokatsiya koordinatalari olinmadi! "
                    "Markaz hududida ekanligingizni tasdiqlash uchun qurilmangizda GPS/Joylashuvni yoqishingiz va ruxsat berishingiz shart."
                )
            }

        # B) Agar masofa ruxsat etilgan radiusdan oshib ketsa:
        if distance > settings_obj.allowed_radius_meters:
            return {
                'success': False,
                'is_geo_error': True,
                'error_code': 'OUT_OF_GEOFENCE',
                'distance': distance,
                'allowed_radius': settings_obj.allowed_radius_meters,
                'message': (
                    f"Siz markaz hududida emassiz! "
                    f"Siz bino markazidan {int(distance)} metr masofadasiz "
                    f"(Ruxsat etilgan chegara: {settings_obj.allowed_radius_meters} metr). "
                    f"Iltimos, o'quv markazi hududiga kirib qaytadan urinib ko'ring."
                )
            }

    # 2. Qat'iy Yuz mosligini tekshirish (Begona yuzlarni rad etish)
    if not staff.photo or not os.path.exists(staff.photo.path):
        return {
            'success': False,
            'is_face_error': True,
            'message': "Xodimning tizimda saqlangan yuz rasmi topilmadi! Iltimos, admin orqali profilingizga rasm yuklang."
        }

    match_score, is_match, face_distance = compare_two_faces(staff.photo.path, scan_file)
    if not is_match or match_score < 60.0 or face_distance > 0.40:
        return {
            'success': False,
            'is_face_error': True,
            'match_score': match_score,
            'face_distance': face_distance,
            'message': f"Yuz aniqlanmadi yoki xodim topilmadi! (O'xshashlik: {match_score}% | Masofa: {face_distance}). Begona yoki tanilmagan shaxs aniqlandi."
        }

    # 3. Bugungi jurnal yozuvini tekshirish
    log = FaceIDLog.objects.filter(user=user, date=today).first()

    action_type = "kelish"
    expected_time = getattr(staff, 'work_start_time', None) or time(8, 0)

    if log is None:
        # ─── BIRINCHI SKANERLASH: KELISH ───────────────────────────
        action_type = "kelish"
        log = FaceIDLog(user=user, date=today)
        log.check_in_time = now

        # Shaxsiy kelish vaqti bo'yicha kechikish tekshiruvi
        expected_dt = datetime.combine(today, expected_time)
        expected_dt = timezone.make_aware(expected_dt) if timezone.is_aware(now) else expected_dt

        diff_seconds = (now - expected_dt).total_seconds()
        salary = Decimal(str(staff.monthly_salary or 0))

        if diff_seconds > 0:
            late_mins = int(diff_seconds // 60)
            if (diff_seconds % 60) > 0:
                late_mins += 1  # Qisman o'tgan minut ham to'liq minut hisoblanadi

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
        # ─── IKKINCHI SKANERLASH: KETISH ───────────────────────────
        action_type = "ketish"
        log.check_out_time = now

    elif log.check_in_time and log.check_out_time:
        # ─── UCHINCHI VA UNDAN KO'P SKANERLASH: KETISH YANGILASH ──
        # (Xodim tushlik vaqtida chiqib, qayta kirib, soatida chiqsa)
        action_type = "ketish"
        log.check_out_time = now  # Eng so'nggi ketish vaqtini yangilaydi

    else:
        # check_in_time yo'q, lekin log bor — check_in ni to'ldir
        action_type = "kelish"
        log.check_in_time = now

        expected_dt = datetime.combine(today, expected_time)
        expected_dt = timezone.make_aware(expected_dt) if timezone.is_aware(now) else expected_dt
        diff_seconds = (now - expected_dt).total_seconds()
        salary = Decimal(str(staff.monthly_salary or 0))

        if diff_seconds > 0:
            late_mins = int(diff_seconds // 60)
            if (diff_seconds % 60) > 0:
                late_mins += 1
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

    # Qo'shimcha parametrlar
    if lat_val is not None:
        log.latitude = lat_val
    if lon_val is not None:
        log.longitude = lon_val
    if distance is not None:
        log.distance_meters = float(distance)

    # Scan rasmini saqlash
    if scan_file and hasattr(scan_file, 'read'):
        try:
            scan_file.seek(0)
            filename = f"scan_{user.id}_{today}_{uuid.uuid4().hex[:6]}.jpg"
            log.verified_photo.save(filename, scan_file, save=False)
        except Exception as e:
            print(f"Scan rasm saqlashda xatolik: {e}")

    log.save()

    staff_full_name = f"{getattr(staff, 'first_name', '') or ''} {getattr(staff, 'last_name', '') or ''}".strip()
    if not staff_full_name:
        staff_full_name = user.get_full_name() or user.username
    staff_role_title = getattr(staff, 'role_title', None) or ('Menejer' if getattr(user, 'role', '') == 'manager' else ("O'qituvchi" if getattr(user, 'role', '') == 'teacher' else user.get_role_display()))

    # Tizim Tarixiga (AuditLog) yozish
    try:
        if action_type == "kelish":
            if log.is_late:
                AuditLog.objects.create(
                    action_type='fine',
                    actor=user,
                    actor_name=staff_full_name,
                    title=f"Kechikish qayd etildi: {log.late_minutes} daqiqa",
                    details=f"Kelgan vaqti: {now.strftime('%H:%M:%S')} (Belgilangan: {expected_time.strftime('%H:%M')}). Bosqichma-bosqich jarima foizi: {log.fine_percent_applied}%. Ushlab qolingan summa: {log.fine_amount_deducted:,.0f} so'm."
                )
            else:
                AuditLog.objects.create(
                    action_type='face_id',
                    actor=user,
                    actor_name=staff_full_name,
                    title=f"Ishga o'z vaqtida keldi ({now.strftime('%H:%M:%S')})",
                    details=f"Face ID orqali davomatdan o'tdi. Masofa: {int(distance or 0)} m. O'xshashlik: {match_score}%."
                )
        else:
            AuditLog.objects.create(
                action_type='face_id',
                actor=user,
                actor_name=staff_full_name,
                title=f"Ishdan ketish qayd etildi ({now.strftime('%H:%M:%S')})",
                details=f"Face ID orqali chiqish qayd etildi."
            )
    except Exception as e:
        print(f"Audit log yozishda xatolik: {e}")

    return {
        'success': True,
        'action_type': action_type,
        'staff_name': staff_full_name,
        'role': staff_role_title,
        'check_in_time': log.check_in_time.strftime('%H:%M:%S') if log.check_in_time else None,
        'check_out_time': log.check_out_time.strftime('%H:%M:%S') if log.check_out_time else None,
        'is_late': log.is_late,
        'late_minutes': log.late_minutes,
        'fine_percent': float(log.fine_percent_applied),
        'fine_amount': float(log.fine_amount_deducted),
        'distance': distance,
        'match_score': match_score,
        'message': (
            f"Muvaffaqiyatli qayd etildi! ({action_type.capitalize()})" +
            (f" Diqqat: {log.late_minutes} daqiqa kechikdingiz (Kademeli jarima: -{log.fine_percent_applied}% | {log.fine_amount_deducted:,.0f} so'm)." if log.is_late and action_type == 'kelish' else "")
        )
    }


# ─────────────────────────────────────────────────────────────
# 4. OYLIK MAOSH, KECHIKISH JARIMALARI VA AVANSLARNI HISOBLASH
# ─────────────────────────────────────────────────────────────

def calculate_staff_salary_summary(staff, year: int, month: int):
    """
    Muayyan oy uchun xodimning to'liq oylik hisob-kitobini chiqaradi:
    Formula: Yakuniy Oylik = Asosiy Maosh - (Kechikish Shtraflari) - (Tasdiqlangan Avanslar) + (Bonuslar)
    - Asosiy oylik stavkasi
    - Ish kunlari soni
    - Kechikishlar soni va jami kechikilgan daqiqalar
    - Kademeli jarima foizi va umumiy ushlab qolinadigan summa
    - Tasdiqlangan avans summasi (AdvanceRequest)
    - Direktor tomonidan berilgan qo'shimcha bonuslar (StaffBonus)
    - Qo'lga tegadigan toza oylik
    - Shu oydagi Face ID jurnallari ro'yxati
    """
    if hasattr(staff, 'user'):
        user = staff.user
    elif hasattr(staff, 'staff_profile') and staff.staff_profile:
        user = staff
        staff = user.staff_profile
    else:
        user = getattr(staff, 'user', staff)

    month_str = f"{year}-{month:02d}"

    logs = FaceIDLog.objects.filter(
        user=user,
        date__year=year,
        date__month=month
    ).order_by('-date')

    base_salary = Decimal(str(getattr(staff, 'monthly_salary', 0) or 0))

    total_days = logs.count()
    late_logs = [l for l in logs if l.is_late]
    total_late_count = len(late_logs)
    total_late_minutes = sum(l.late_minutes for l in late_logs)

    # Jami jarima foizi va jami ushlab qolingan shtraf summasi
    total_fine_percent = sum(l.fine_percent_applied for l in late_logs)
    total_fine_amount = sum(l.fine_amount_deducted for l in late_logs)

    # Tasdiqlangan avanslar (AdvanceRequest)
    advances_qs = AdvanceRequest.objects.filter(user=user, month=month_str, status='approved')
    total_advance_amount = sum((a.amount for a in advances_qs), Decimal('0.00'))

    # Qo'shimcha berilgan bonuslar (StaffBonus)
    bonuses_qs = StaffBonus.objects.filter(user=user, month=month_str)
    total_bonus_amount = sum((b.amount for b in bonuses_qs), Decimal('0.00'))

    # Yakuniy oylik maosh: Asosiy Maosh - Shtraflar - Avanslar + Bonuslar
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


