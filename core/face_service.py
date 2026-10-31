"""
HUMO O'quv markazi — Yengil (Lightweight) Face ID va Davomat Xizmatlari
- Og'ir kutubxonalarsiz (face_recognition / dlib / DeepFace / TensorFlow olib tashlangan)
- Yengil OpenCV / Pillow descriptor va frontend descriptor/snapshot asosida tezkor solishtirish
- Geolokatsiya masofasini hisoblash (Haversine formula)
- Kelish va ketishni avtomatik qayd etish hamda oylik maosh kalkulyatori
"""
import base64
import io
import json
import math
import os
import re
import uuid
from datetime import datetime, time
from decimal import Decimal

import numpy as np
from PIL import Image, ImageOps

try:
    import cv2
    HAS_OPENCV = True
except ImportError:
    cv2 = None
    HAS_OPENCV = False

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from .models import (
    AdvanceRequest, AuditLog, CustomUser, FaceIDLog,
    FaceIDSettings, Manager, StaffBonus, Teacher,
)


# ─────────────────────────────────────────────────────────────
# 1. YENGIL OPENCV DETEKTOR (Agar ONNX yoki Haar mavjud bo'lsa)
# ─────────────────────────────────────────────────────────────

_YUNET_DETECTOR = None
_SFACE_RECOGNIZER = None

YUNET_MODEL_PATH = os.path.join(settings.BASE_DIR, 'weights', 'face_detection_yunet_2023mar.onnx')
SFACE_MODEL_PATH = os.path.join(settings.BASE_DIR, 'weights', 'face_recognition_sface_2021dec.onnx')


def _get_lightweight_opencv_models():
    """
    OpenCV ning o'z ichiga o'rnatilgan yengil C++ ONNX o'quvchisini (bor-yo'g'i ~5 MB) yuklaydi.
    Hech qanday dlib, face_recognition yoki DeepFace ishlatilmaydi.
    """
    global _YUNET_DETECTOR, _SFACE_RECOGNIZER
    if not HAS_OPENCV or cv2 is None:
        return None, None
    try:
        if _YUNET_DETECTOR is None and os.path.exists(YUNET_MODEL_PATH):
            _YUNET_DETECTOR = cv2.FaceDetectorYN.create(
                YUNET_MODEL_PATH, "", (320, 320),
                score_threshold=0.45, nms_threshold=0.3, top_k=500,
            )
        if _SFACE_RECOGNIZER is None and os.path.exists(SFACE_MODEL_PATH):
            _SFACE_RECOGNIZER = cv2.FaceRecognizerSF.create(SFACE_MODEL_PATH, "")
    except Exception:
        pass
    return _YUNET_DETECTOR, _SFACE_RECOGNIZER


# ─────────────────────────────────────────────────────────────
# 2. GEOLOKATSIYA VA MASOFA HISOBI (Haversine)
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


# ─────────────────────────────────────────────────────────────
# 3. BASE64 VA RASM O'QISH YORDAMCHILARI
# ─────────────────────────────────────────────────────────────

def decode_base64_image(base64_str: str):
    """
    Frontenddan Base64 formatida kelgan snapshot rasmni ('data:image/jpeg;base64,...')
    tozalab, ContentFile va RGB numpy massivga aylantiradi.
    """
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
        if not raw_bytes:
            return None, None, "Tasvir baytlari bo'sh"

        pil_img = Image.open(io.BytesIO(raw_bytes))
        try:
            pil_img = ImageOps.exif_transpose(pil_img)
        except Exception:
            pass
        pil_rgb = pil_img.convert('RGB')
        rgb_array = np.ascontiguousarray(np.array(pil_rgb, dtype=np.uint8))

        out_buf = io.BytesIO()
        pil_rgb.save(out_buf, format='JPEG', quality=88)
        out_buf.seek(0)
        content_file = ContentFile(out_buf.read(), name=f"scan_{uuid.uuid4().hex[:8]}.jpg")
        return content_file, rgb_array, None
    except Exception as e:
        return None, None, str(e)


def _load_bgr_image(image_source):
    """Fayl, path, Base64 yoki ContentFile manbasidan OpenCV BGR massiv oladi."""
    if image_source is None:
        return None
    try:
        if isinstance(image_source, np.ndarray):
            return image_source

        if hasattr(image_source, 'path'):
            try:
                if os.path.exists(image_source.path):
                    image_source = image_source.path
            except Exception:
                pass

        if isinstance(image_source, str) and (
            image_source.startswith('data:image') or len(image_source) > 512
        ):
            _, rgb_arr, _ = decode_base64_image(image_source)
            if rgb_arr is not None:
                return rgb_arr[:, :, ::-1].copy()

        raw_bytes = None
        if isinstance(image_source, (str, os.PathLike)):
            p = str(image_source)
            if not os.path.exists(p):
                return None
            with open(p, 'rb') as f:
                raw_bytes = f.read()
        elif hasattr(image_source, 'read'):
            image_source.seek(0)
            raw_bytes = image_source.read()
            image_source.seek(0)
        elif isinstance(image_source, bytes):
            raw_bytes = image_source

        if raw_bytes:
            pil_img = Image.open(io.BytesIO(raw_bytes))
            try:
                pil_img = ImageOps.exif_transpose(pil_img)
            except Exception:
                pass
            rgb = np.ascontiguousarray(np.array(pil_img.convert('RGB'), dtype=np.uint8))
            return rgb[:, :, ::-1].copy()
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────────────────────
# 4. YENGIL YUZ DESKRIPTORI (EMBEDDING) CHIQARISH VA SOLISHTIRISH
# ─────────────────────────────────────────────────────────────

def extract_face_embedding(image_source, allow_portrait_fallback: bool = False):
    """
    Yengil OpenCV yordamida kadrdan yuzni topib, 128-o'lchamli normallashtirilgan
    vektor (descriptor) qaytaradi: (feature_vector, face_box, confidence).
    Agar yuz topilmasa: (None, None, 0.0)
    """
    img = _load_bgr_image(image_source)
    if img is None:
        return None, None, 0.0

    h, w = img.shape[:2]
    if h < 20 or w < 20:
        return None, None, 0.0

    detector, recognizer = _get_lightweight_opencv_models()

    # 1. Agar OpenCV YuNet + SFace ONNX mavjud bo'lsa (juda tez, < 10ms)
    if detector is not None and recognizer is not None:
        try:
            scale = min(1.0, 640.0 / float(max(h, w)))
            proc = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1.0 else img
            ph, pw = proc.shape[:2]
            detector.setInputSize((pw, ph))
            _, faces = detector.detect(proc)

            if faces is not None and len(faces) > 0:
                best = max(faces, key=lambda f: f[2] * f[3] * f[-1])
                aligned = recognizer.alignCrop(proc, best)
                feat = recognizer.feature(aligned).astype(np.float32).reshape(1, 128)
                norm = np.linalg.norm(feat)
                if norm > 0:
                    feat = feat / norm
                box = (
                    int(best[0] / scale),
                    int(best[1] / scale),
                    int(best[2] / scale),
                    int(best[3] / scale),
                )
                return feat, box, float(best[-1])
        except Exception:
            pass

    # 2. Yengil Haar Cascade + Gistogramma/Piksel deskriptori (zaxira)
    face_crop = None
    box = None
    if HAS_OPENCV and cv2 is not None:
        try:
            cv2_data = getattr(cv2, 'data', None)
            haar_dir = getattr(cv2_data, 'haarcascades', '') if cv2_data else ''
            cascade_path = os.path.join(haar_dir, 'haarcascade_frontalface_default.xml') if haar_dir else ''
            if cascade_path and os.path.exists(cascade_path):
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                cascade = cv2.CascadeClassifier(cascade_path)
                detected = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=3, minSize=(40, 40))
                if len(detected) > 0:
                    x, y, fw, fh = max(detected, key=lambda f: f[2] * f[3])
                    face_crop = img[max(0, y):min(h, y + fh), max(0, x):min(w, x + fw)]
                    box = (int(x), int(y), int(fw), int(fh))
        except Exception:
            pass

    if face_crop is None:
        if allow_portrait_fallback:
            cy1, cy2 = int(h * 0.1), int(h * 0.9)
            cx1, cx2 = int(w * 0.1), int(w * 0.9)
            face_crop = img[cy1:cy2, cx1:cx2]
            box = (cx1, cy1, cx2 - cx1, cy2 - cy1)
        else:
            return None, None, 0.0

    if recognizer is not None and HAS_OPENCV:
        try:
            aligned = cv2.resize(face_crop, (112, 112))
            feat = recognizer.feature(aligned).astype(np.float32).reshape(1, 128)
            norm = np.linalg.norm(feat)
            if norm > 0:
                feat = feat / norm
            return feat, box, 0.75
        except Exception:
            pass

    # Oddiy 128-o'lchamli yengil deskriptor (PIL / NumPy)
    try:
        pil_crop = Image.fromarray(face_crop[:, :, ::-1]).convert('L').resize((16, 8))
        feat = np.asarray(pil_crop, dtype=np.float32).reshape(1, 128)
        feat = feat - np.mean(feat)
        norm = np.linalg.norm(feat)
        if norm > 0:
            feat = feat / norm
        return feat, box, 0.70
    except Exception:
        return None, None, 0.0


def parse_descriptor(descriptor_data):
    """Frontend yuborgan tayyor yuz deskriptorini (JSON list yoki array) 128-D numpy vektorga o'giradi."""
    if not descriptor_data:
        return None
    try:
        if isinstance(descriptor_data, str):
            descriptor_data = json.loads(descriptor_data)
        if isinstance(descriptor_data, dict):
            descriptor_data = list(descriptor_data.values())
        arr = np.asarray(descriptor_data, dtype=np.float32).reshape(1, -1)
        if arr.size < 16 or not np.all(np.isfinite(arr)):
            return None
        norm = np.linalg.norm(arr)
        if norm > 0:
            arr = arr / norm
        return arr
    except Exception:
        return None


def compare_embeddings(feat1, feat2) -> tuple[float, bool, float]:
    """
    Ikki yuz deskriptorini (vektorini) kosinus o'xshashligi bo'yicha solishtiradi.
    Natija: (similarity_score: 0..100, is_match: bool, distance: 0..1)
    """
    if feat1 is None or feat2 is None:
        return 0.0, False, 1.0
    try:
        f1 = np.asarray(feat1, dtype=np.float32).reshape(1, -1)
        f2 = np.asarray(feat2, dtype=np.float32).reshape(1, -1)
        if f1.shape[1] != f2.shape[1]:
            return 0.0, False, 1.0

        n1 = np.linalg.norm(f1)
        n2 = np.linalg.norm(f2)
        if n1 > 0:
            f1 = f1 / n1
        if n2 > 0:
            f2 = f2 / n2

        cos_sim = float(np.dot(f1, f2.T)[0, 0])
        threshold = 0.28

        if cos_sim >= threshold:
            pct = 65.0 + ((cos_sim - threshold) / (0.80 - threshold)) * 33.5
            score = round(min(99.9, max(65.0, pct)), 1)
            is_match = True
        else:
            pct = max(0.0, (max(0.0, cos_sim) / threshold) * 58.0)
            score = round(min(58.0, pct), 1)
            is_match = False

        distance = round(max(0.0, 1.0 - cos_sim), 3)
        return score, is_match, distance
    except Exception:
        return 0.0, False, 1.0


def compare_two_faces(ref_img_source, scan_img_source) -> tuple[float, bool, float]:
    """Ikki rasmni yengil deskriptor orqali solishtirish."""
    feat1, _, _ = extract_face_embedding(ref_img_source, allow_portrait_fallback=True)
    feat2, _, _ = extract_face_embedding(scan_img_source, allow_portrait_fallback=False)
    if feat1 is None or feat2 is None:
        return 0.0, False, 1.0
    return compare_embeddings(feat1, feat2)


# ─────────────────────────────────────────────────────────────
# 5. XODIM PROFILLARI VA DESKRIPTORLAR BILAN ISHLASH
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
    """Xodimning profili, rasmi va face_embedding satrini qaytaradi."""
    staff_profile = get_user_staff_profile(user_or_staff)
    if not staff_profile:
        return None, None, ''
    return (
        staff_profile,
        getattr(staff_profile, 'photo', None),
        getattr(staff_profile, 'face_embedding', '') or '',
    )


def sync_staff_face_embedding(user_or_staff) -> bool:
    """Xodimning rasmidan yengil deskriptor yaratib bazaga saqlaydi."""
    staff_obj, photo_field, _ = get_user_face_data(user_or_staff)
    if not staff_obj or not photo_field:
        return False
    try:
        photo_src = photo_field.path if (hasattr(photo_field, 'path') and os.path.exists(photo_field.path)) else photo_field
        feat, _, _ = extract_face_embedding(photo_src, allow_portrait_fallback=True)
        if feat is not None:
            staff_obj.face_embedding = json.dumps(feat.flatten().tolist())
            if getattr(staff_obj, 'pk', None):
                staff_obj.save(update_fields=['face_embedding'])
            return True
    except Exception:
        pass
    return False


def get_or_create_face_embedding(user_or_staff, force_refresh: bool = False):
    """Xodimning bazadagi deskriptorini o'qiydi yoki rasmidan avtomatik hisoblaydi."""
    staff_obj, photo_field, raw_emb = get_user_face_data(user_or_staff)
    if not staff_obj:
        return None

    if not force_refresh and raw_emb and len(raw_emb) > 20:
        vec = parse_descriptor(raw_emb)
        if vec is not None:
            return vec

    if photo_field and sync_staff_face_embedding(staff_obj):
        return parse_descriptor(getattr(staff_obj, 'face_embedding', ''))
    return None


def sync_all_face_embeddings(force: bool = False) -> dict:
    """Barcha faol xodimlarning yuz deskriptorlarini yengil usulda yangilaydi."""
    synced_teachers = synced_managers = created_profiles = failed = 0
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

    for t in Teacher.objects.filter(is_active=True).exclude(photo=''):
        if not t.photo:
            continue
        if force or not t.face_embedding or len(t.face_embedding) < 20:
            if sync_staff_face_embedding(t):
                synced_teachers += 1
            else:
                failed += 1

    for m in Manager.objects.filter(is_active=True).exclude(photo=''):
        if not m.photo:
            continue
        if force or not m.face_embedding or len(m.face_embedding) < 20:
            if sync_staff_face_embedding(m):
                synced_managers += 1
            else:
                failed += 1

    return {
        'teachers': synced_teachers,
        'managers': synced_managers,
        'created_profiles': created_profiles,
        'failed': failed,
    }


def match_staff_by_face(scan_source=None, scan_descriptor=None):
    """
    Kelgan rasm (snapshot) yoki tayyor descriptor orqali bazadagi xodimlarni qidiradi.
    Natija: (role, staff_obj, score, distance) yoki None
    """
    scan_feat = parse_descriptor(scan_descriptor) if scan_descriptor is not None else None
    if scan_feat is None and scan_source is not None:
        scan_feat, _, _ = extract_face_embedding(scan_source, allow_portrait_fallback=False)
    if scan_feat is None:
        return None

    candidates = []
    for t in Teacher.objects.filter(is_active=True).select_related('user'):
        if t.photo or t.has_face_embedding:
            candidates.append(('teacher', t))
    for m in Manager.objects.filter(is_active=True).select_related('user'):
        if m.photo or m.has_face_embedding:
            candidates.append(('manager', m))

    best_match = None
    best_score = 0.0

    for role, staff in candidates:
        staff_feat = get_or_create_face_embedding(staff)
        if staff_feat is None:
            continue
        score, is_match, dist = compare_embeddings(staff_feat, scan_feat)
        if not is_match and getattr(staff, 'photo', None):
            refreshed = get_or_create_face_embedding(staff, force_refresh=True)
            if refreshed is not None:
                score, is_match, dist = compare_embeddings(refreshed, scan_feat)

        if is_match and score >= 60.0 and score > best_score:
            best_score = score
            best_match = (role, staff, score, dist)

    return best_match


# ─────────────────────────────────────────────────────────────
# 6. KADEMELI JARIMA VA DAVOMATNI YOZISH
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
    pre_verified: bool = False,
    match_score: float = 95.0,
):
    """
    Xodimni yuz ma'lumoti yoki frontend tasdig'i asosida davomatga yozadi.
    Javob formati:
      {"success": True, "message": "Xodim keldi deb belgilandi", "employee_name": "...", ...}
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

    # 1. Geolokatsiya tekshiruvi (agar yoqilgan bo'lsa)
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

    # 2. Agar hali tekshirilmagan bo'lsa va rasm yuborilgan bo'lsa — yengil solishtirish
    if not pre_verified and scan_file is not None:
        scan_feat, _, _ = extract_face_embedding(scan_file, allow_portrait_fallback=False)
        if scan_feat is None:
            return {
                'success': False,
                'is_face_error': True,
                'detected': False,
                'error_type': 'no_face',
                'message': "Yuz topilmadi yoki xodim ro'yxatda yo'q",
            }
        staff_feat = get_or_create_face_embedding(staff)
        if staff_feat is not None:
            score, is_match, _ = compare_embeddings(staff_feat, scan_feat)
            if not is_match and getattr(staff, 'photo', None):
                refreshed = get_or_create_face_embedding(staff, force_refresh=True)
                if refreshed is not None:
                    score, is_match, _ = compare_embeddings(refreshed, scan_feat)
            if not is_match:
                return {
                    'success': False,
                    'is_face_error': True,
                    'detected': True,
                    'error_type': 'unknown_face',
                    'message': "Yuz topilmadi yoki xodim ro'yxatda yo'q",
                }
            match_score = score

    # 3. Davomat logini yozish
    employee_name = f"{getattr(staff, 'first_name', '') or ''} {getattr(staff, 'last_name', '') or ''}".strip()
    if not employee_name:
        employee_name = user.get_full_name() or user.username

    role_title = getattr(staff, 'role_title', None) or (
        'Menejer' if getattr(user, 'role', '') == 'manager' else "O'qituvchi"
    )

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
                'message': "Xodim keldi deb belgilandi",
                'employee_name': employee_name,
                'staff_name': employee_name,
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
        'message': status_msg,
        'employee_name': employee_name,
        'staff_name': employee_name,
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
# 7. OYLIK MAOSH HISOB-KITOBI
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
